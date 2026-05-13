package com.ledgeragent.bridge;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.beans.factory.InitializingBean;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Manages the lifecycle of the Python subprocess that runs
 * {@code ledger_agent.bridge.jsonrpc_stdio} and exposes a typed API for the
 * six core ledger operations.
 *
 * <p>The subprocess is started once at Spring Boot startup
 * ({@link InitializingBean}) and stopped on shutdown ({@link DisposableBean}).
 * The process is kept alive across all requests to avoid the ~400 ms cold-start
 * cost on every HTTP call.
 *
 * <p>If the Python home is embedded in the fat jar, call
 * {@link #setPythonHome(Path)} before the bean is initialised; otherwise the
 * bridge resolves {@code python3} / {@code python} from {@code PATH}.
 *
 * <p>ARCH-08
 */
@Component
public class PythonBridge implements AutoCloseable, InitializingBean, DisposableBean {

    private static final Logger log = LoggerFactory.getLogger(PythonBridge.class);

    private final ObjectMapper mapper = new ObjectMapper();
    private Path pythonHome;
    private Process process;
    private JsonRpcClient client;

    // ── Configuration ────────────────────────────────────────────────────────

    /**
     * Override the Python executable directory.  Call before {@link #afterPropertiesSet()}.
     * If not set, the bridge searches {@code PATH} for {@code python3} or {@code python}.
     *
     * @param pythonHome directory containing the {@code python} executable
     *                   (e.g. {@code /tmp/ledger-agent/py-abc123/bin/})
     */
    public void setPythonHome(Path pythonHome) {
        this.pythonHome = pythonHome;
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    @Override
    public void afterPropertiesSet() throws Exception {
        start();
    }

    @Override
    public void destroy() {
        close();
    }

    /**
     * Start the Python bridge subprocess.
     *
     * @throws IOException      if the process cannot be started
     * @throws BridgeException  if the initial ping fails
     */
    public synchronized void start() throws IOException, BridgeException {
        if (process != null && process.isAlive()) {
            log.debug("Python bridge already running (pid={})", process.pid());
            return;
        }

        String python = resolvePython();
        List<String> cmd = new ArrayList<>();
        cmd.add(python);
        cmd.add("-m");
        cmd.add("ledger_agent.bridge.jsonrpc_stdio");

        log.info("Starting Python bridge: {}", String.join(" ", cmd));

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectErrorStream(false);  // keep stderr separate for logging
        // Inherit the webapp's environment so FI_DB_PATH etc. are visible
        pb.inheritIO().redirectInput(ProcessBuilder.Redirect.PIPE)
                      .redirectOutput(ProcessBuilder.Redirect.PIPE)
                      .redirectError(ProcessBuilder.Redirect.INHERIT);

        process = pb.start();
        client = new JsonRpcClient(process.getInputStream(), process.getOutputStream());

        // Verify the process is responsive
        if (!client.ping()) {
            throw new BridgeException(
                    "Python bridge started but did not respond to ping. "
                    + "Check that ledger_agent is installed and the Python path is correct.");
        }
        log.info("Python bridge ready (pid={})", process.pid());
    }

    /**
     * Send a raw JSON-RPC call to the Python bridge.
     *
     * @param method JSON-RPC method name
     * @param params Method parameters (may be null for no-arg methods)
     * @return The {@code result} node from the response
     * @throws BridgeException on any error
     */
    public JsonNode call(String method, JsonNode params) throws BridgeException {
        ensureAlive();
        return client.call(method, params);
    }

    // ── Typed API (one method per core.api function) ─────────────────────────

    /**
     * Call {@code generate_balance_sheet} for the given fiscal year.
     *
     * @param fiscalYear Four-digit year (e.g. 2024)
     * @return JSON result node
     * @throws BridgeException on any error
     */
    public JsonNode generateBalanceSheet(int fiscalYear) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("generate_balance_sheet", params);
    }

    /**
     * Call {@code generate_form_1065} for the given fiscal year.
     */
    public JsonNode generateForm1065(int fiscalYear) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("generate_form_1065", params);
    }

    /**
     * Call {@code generate_k1} for the given fiscal year and partner.
     *
     * @param fiscalYear Four-digit year
     * @param partnerId  "yash" or "parin"
     */
    public JsonNode generateK1(int fiscalYear, String partnerId) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        params.put("partner_id", partnerId);
        return call("generate_k1", params);
    }

    /**
     * Call {@code pte_estimate} for the given fiscal year.
     */
    public JsonNode pteEstimate(int fiscalYear) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("pte_estimate", params);
    }

    /**
     * Call {@code reconcile_year} for the given fiscal year.
     */
    public JsonNode reconcileYear(int fiscalYear) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("reconcile_year", params);
    }

    /**
     * Call {@code import_statements} for the given folder path.
     *
     * @param folder        Absolute path to the statements folder
     * @param allowPartial  Skip R-45 completeness gate
     */
    public JsonNode importStatements(String folder, boolean allowPartial) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("folder", folder);
        params.put("allow_partial", allowPartial);
        return call("import_statements", params);
    }

    // ── Internal helpers ─────────────────────────────────────────────────────

    private void ensureAlive() throws BridgeException {
        if (process == null || !process.isAlive()) {
            log.warn("Python bridge is not running — attempting restart");
            try {
                start();
            } catch (IOException e) {
                throw new BridgeException("Failed to restart Python bridge", e);
            }
        }
    }

    private String resolvePython() {
        if (pythonHome != null) {
            // Unix
            Path unix = pythonHome.resolve("bin/python3");
            if (unix.toFile().exists()) return unix.toString();
            Path unix2 = pythonHome.resolve("bin/python");
            if (unix2.toFile().exists()) return unix2.toString();
            // Windows
            Path win = pythonHome.resolve("python.exe");
            if (win.toFile().exists()) return win.toString();
        }
        // Fall back to PATH
        for (String candidate : new String[]{"python3", "python"}) {
            try {
                Process check = new ProcessBuilder(candidate, "--version").start();
                check.waitFor();
                if (check.exitValue() == 0) return candidate;
            } catch (Exception ignored) {}
        }
        throw new IllegalStateException(
                "Python 3.10+ not found on PATH and no pythonHome configured. "
                + "Install Python or set LEDGER_PYTHON_HOME.");
    }

    @Override
    public void close() {
        if (client != null) {
            try { client.close(); } catch (Exception ignored) {}
        }
        if (process != null && process.isAlive()) {
            log.info("Stopping Python bridge (pid={})", process.pid());
            process.destroy();
            try { process.waitFor(); } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
