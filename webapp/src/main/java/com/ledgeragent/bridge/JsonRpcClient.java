package com.ledgeragent.bridge;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicLong;

/**
 * JSON-RPC 2.0 client that communicates with a Python subprocess over stdio.
 *
 * <p>Wire format: one newline-delimited JSON object per message (same framing
 * as the MCP stdio transport).  The client sends a request and blocks until
 * the matching response arrives.
 *
 * <p>Thread safety: each call acquires a unique id; concurrent calls on the
 * same client are serialised by {@code synchronized} on the writer.  For
 * high-concurrency use, create one client per thread or use a connection pool.
 *
 * <p>ARCH-08
 */
public class JsonRpcClient implements Closeable {

    private static final Logger log = LoggerFactory.getLogger(JsonRpcClient.class);
    private static final int DEFAULT_TIMEOUT_MS = 30_000;

    private final ObjectMapper mapper;
    private final PrintWriter writer;
    private final BufferedReader reader;
    private final AtomicLong idSeq = new AtomicLong(1);
    private volatile boolean closed = false;

    /**
     * Create a JSON-RPC client from an already-started process's streams.
     *
     * @param processIn  stdout of the Python subprocess (we read from it)
     * @param processOut stdin of the Python subprocess (we write to it)
     */
    public JsonRpcClient(InputStream processIn, OutputStream processOut) {
        this.mapper = new ObjectMapper();
        this.writer = new PrintWriter(
                new OutputStreamWriter(processOut, StandardCharsets.UTF_8), true);
        this.reader = new BufferedReader(
                new InputStreamReader(processIn, StandardCharsets.UTF_8));
    }

    /**
     * Send a JSON-RPC 2.0 request and block until the response arrives.
     *
     * @param method JSON-RPC method name (e.g. {@code "generate_balance_sheet"})
     * @param params Method parameters as a {@link JsonNode}
     * @return The {@code result} field from the response
     * @throws BridgeException if the response contains an {@code error} field,
     *                         the process is not running, or the call times out
     */
    public synchronized JsonNode call(String method, JsonNode params) throws BridgeException {
        if (closed) {
            throw new BridgeException("JsonRpcClient is closed");
        }

        long id = idSeq.getAndIncrement();
        ObjectNode request = mapper.createObjectNode();
        request.put("jsonrpc", "2.0");
        request.put("id", id);
        request.put("method", method);
        request.set("params", params != null ? params : mapper.createObjectNode());

        try {
            String line = mapper.writeValueAsString(request);
            log.debug("→ {}", line);
            writer.println(line);
            if (writer.checkError()) {
                throw new BridgeException("Write error — Python process may have exited");
            }

            // Read lines until we find the response with matching id
            long deadline = System.currentTimeMillis() + DEFAULT_TIMEOUT_MS;
            while (System.currentTimeMillis() < deadline) {
                String responseLine = reader.readLine();
                if (responseLine == null) {
                    throw new BridgeException("Python process closed stdout unexpectedly");
                }
                responseLine = responseLine.trim();
                if (responseLine.isEmpty()) {
                    continue;
                }
                log.debug("← {}", responseLine);
                JsonNode response = mapper.readTree(responseLine);
                JsonNode responseId = response.get("id");
                if (responseId == null || responseId.asLong() != id) {
                    // Not our response (could be a notification) — skip
                    log.debug("Skipping response id={}", responseId);
                    continue;
                }
                if (response.has("error")) {
                    JsonNode err = response.get("error");
                    throw new BridgeException(
                            "Python bridge error [" + err.path("code").asInt() + "]: "
                            + err.path("message").asText());
                }
                return response.get("result");
            }
            throw new BridgeException(
                    "Timeout waiting for response to method=" + method + " id=" + id);

        } catch (IOException e) {
            throw new BridgeException("I/O error communicating with Python bridge", e);
        }
    }

    /**
     * Send a {@code ping} and verify the Python process responds.
     *
     * @return {@code true} if the process is alive
     */
    public boolean ping() {
        try {
            JsonNode result = call("ping", null);
            return result != null && result.path("pong").asBoolean(false);
        } catch (BridgeException e) {
            log.warn("Ping failed: {}", e.getMessage());
            return false;
        }
    }

    @Override
    public void close() {
        closed = true;
        try { writer.close(); } catch (Exception ignored) {}
        try { reader.close(); } catch (Exception ignored) {}
    }
}
