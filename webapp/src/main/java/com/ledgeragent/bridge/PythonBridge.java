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
 * Manages the lifecycle of the Python subprocess running
 * {@code ledger_agent.bridge.jsonrpc_stdio} and exposes a typed API for the
 * six core ledger operations.
 *
 * The subprocess is started once at Spring Boot startup ({@link InitializingBean})
 * and stopped on shutdown ({@link DisposableBean}). It is kept alive across all
 * requests to avoid the ~400 ms cold-start cost on every HTTP call.
 *
 * All typed methods default to {@code allowPii=false} (R-46). Callers that
 * need raw PII must explicitly pass {@code allowPii=true}; the flag is forwarded
 * through the JSON-RPC {@code _meta} envelope to the Python firewall.
 */
@Component
public class PythonBridge implements AutoCloseable, InitializingBean, DisposableBean {

    private static final Logger log = LoggerFactory.getLogger(PythonBridge.class);

    private final ObjectMapper mapper = new ObjectMapper();
    private Path pythonHome;
    private Process process;
    private JsonRpcClient client;

    public void setPythonHome(Path pythonHome) {
        this.pythonHome = pythonHome;
    }

    @Override
    public void afterPropertiesSet() throws Exception {
        start();
    }

    @Override
    public void destroy() {
        close();
    }

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
        pb.redirectErrorStream(false);
        pb.inheritIO()
          .redirectInput(ProcessBuilder.Redirect.PIPE)
          .redirectOutput(ProcessBuilder.Redirect.PIPE)
          .redirectError(ProcessBuilder.Redirect.INHERIT);
        pb.environment().put("PYTHONUNBUFFERED", "1");
        String auditDisabled = System.getenv("FI_AUDIT_DISABLED");
        if (auditDisabled != null) {
            pb.environment().put("FI_AUDIT_DISABLED", auditDisabled);
        }
        java.io.File projectRoot = pythonHome != null
                ? pythonHome.getParent().toFile()
                : java.nio.file.Paths.get(System.getProperty("user.dir"))
                        .getParent().toFile();
        if (projectRoot.isDirectory()) {
            pb.directory(projectRoot);
        }

        process = pb.start();
        client = new JsonRpcClient(process.getInputStream(), process.getOutputStream());

        if (!client.ping()) {
            throw new BridgeException(
                    "Python bridge started but did not respond to ping. "
                    + "Check that ledger_agent is installed and the Python path is correct.");
        }
        log.info("Python bridge ready (pid={})", process.pid());
    }

    // ── Raw call ─────────────────────────────────────────────────────────────

    public JsonNode call(String method, JsonNode params) throws BridgeException {
        return call(method, params, false);
    }

    public JsonNode call(String method, JsonNode params, boolean allowPii) throws BridgeException {
        ensureAlive();
        return client.call(method, params, allowPii);
    }

    // ── Typed API — allow_pii=false (safe defaults) ──────────────────────────

    public JsonNode generateBalanceSheet(int fiscalYear) throws BridgeException {
        return generateBalanceSheet(fiscalYear, false);
    }

    public JsonNode generateBalanceSheet(int fiscalYear, boolean allowPii) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("generate_balance_sheet", params, allowPii);
    }

    public JsonNode generateForm1065(int fiscalYear) throws BridgeException {
        return generateForm1065(fiscalYear, false);
    }

    public JsonNode generateForm1065(int fiscalYear, boolean allowPii) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("generate_form_1065", params, allowPii);
    }

    public JsonNode generateK1(int fiscalYear, String partnerId) throws BridgeException {
        return generateK1(fiscalYear, partnerId, false);
    }

    public JsonNode generateK1(int fiscalYear, String partnerId, boolean allowPii)
            throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        params.put("partner_id", partnerId);
        return call("generate_k1", params, allowPii);
    }

    public JsonNode pteEstimate(int fiscalYear) throws BridgeException {
        return pteEstimate(fiscalYear, false);
    }

    public JsonNode pteEstimate(int fiscalYear, boolean allowPii) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("pte_estimate", params, allowPii);
    }

    public JsonNode reconcileYear(int fiscalYear) throws BridgeException {
        return reconcileYear(fiscalYear, false);
    }

    public JsonNode reconcileYear(int fiscalYear, boolean allowPii) throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("fiscal_year", fiscalYear);
        return call("reconcile_year", params, allowPii);
    }

    public JsonNode importStatements(String folder, boolean allowPartial) throws BridgeException {
        return importStatements(folder, allowPartial, false);
    }

    public JsonNode importStatements(String folder, boolean allowPartial, boolean allowPii)
            throws BridgeException {
        ObjectNode params = mapper.createObjectNode();
        params.put("folder", folder);
        params.put("allow_partial", allowPartial);
        return call("import_statements", params, allowPii);
    }

    // ── Health check ─────────────────────────────────────────────────────────

    public boolean ping() {
        try {
            ensureAlive();
            return client.ping();
        } catch (BridgeException e) {
            return false;
        }
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
            Path unix = pythonHome.resolve("bin/python3");
            if (unix.toFile().exists()) return unix.toString();
            Path unix2 = pythonHome.resolve("bin/python");
            if (unix2.toFile().exists()) return unix2.toString();
            Path win = pythonHome.resolve("python.exe");
            if (win.toFile().exists()) return win.toString();
        }
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
