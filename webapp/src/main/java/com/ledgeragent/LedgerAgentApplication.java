package com.ledgeragent;

import com.ledgeragent.runtime.PythonRuntimeExtractor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;

import java.awt.Desktop;
import java.net.URI;
import java.nio.file.Path;

/**
 * Spring Boot 3 entry point for ledger-agent Form D (ARCH-09).
 *
 * <p>Start sequence:
 * <ol>
 *   <li>Extract bundled Python runtime to a temp directory (ARCH-10)
 *       if running from the fat jar (i.e. the runtime tarball is on the
 *       classpath).  No-op if running from source with Python on PATH.</li>
 *   <li>Inject {@code LEDGER_PYTHON_HOME} env var so {@link
 *       com.ledgeragent.bridge.PythonBridge} finds the extracted executable.</li>
 *   <li>Start Spring Boot — which in turn starts the Python bridge subprocess
 *       (the {@code PythonBridge} bean implements {@code InitializingBean}).</li>
 *   <li>Open the default browser to {@code http://localhost:8080} if
 *       running in a desktop environment.</li>
 * </ol>
 *
 * <p>Usage:
 * <pre>
 *   # From source
 *   cd webapp && ./mvnw spring-boot:run
 *
 *   # From fat jar (ARCH-10)
 *   java -jar ledger-agent-webapp-2.1.0.jar
 * </pre>
 */
@SpringBootApplication
public class LedgerAgentApplication {

    private static final Logger log = LoggerFactory.getLogger(LedgerAgentApplication.class);

    public static void main(String[] args) {
        // ── 1. Extract embedded Python runtime (ARCH-10) ──────────────────
        try {
            Path pythonHome = PythonRuntimeExtractor.extractToTempDir();
            if (pythonHome != null) {
                // Make available to PythonBridge via system property
                System.setProperty("ledger.python.home", pythonHome.toString());
                log.info("Extracted Python runtime to {}", pythonHome);
            }
        } catch (Exception e) {
            // Not fatal — bridge will try PATH
            log.warn("Python runtime extraction skipped: {}", e.getMessage());
        }

        // ── 2. Start Spring Boot ──────────────────────────────────────────
        ConfigurableApplicationContext ctx = SpringApplication.run(LedgerAgentApplication.class, args);

        // ── 3. Open browser ───────────────────────────────────────────────
        String port = ctx.getEnvironment().getProperty("server.port", "8080");
        String url = "http://localhost:" + port;
        openBrowser(url);
        log.info("ledger-agent webapp running at {}", url);
    }

    private static void openBrowser(String url) {
        try {
            if (Desktop.isDesktopSupported() && Desktop.getDesktop().isSupported(Desktop.Action.BROWSE)) {
                Desktop.getDesktop().browse(new URI(url));
            }
        } catch (Exception e) {
            // Non-fatal — headless environments (CI, Docker) don't have a browser
            log.debug("Could not open browser: {}", e.getMessage());
        }
    }
}
