package com.ledgeragent.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import com.ledgeragent.service.ReportType;
import com.ledgeragent.service.RunService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/**
 * REST API transport — Form D mode 2.
 *
 * <p>Exposes the same ledger engine as the HTML form transport ({@code RunController})
 * via JSON at {@code /api/v1/*}.  Both transports call {@link RunService#dispatch}
 * so behavior cannot drift between them.
 *
 * <pre>
 * POST /api/v1/run         — run a report; returns bridge JsonNode verbatim
 * GET  /api/v1/healthz     — bridge health check
 * GET  /api/v1/reports     — list of valid report wire names
 * </pre>
 *
 * <p>HTTP status mapping per Section 6.3:
 * <ul>
 *   <li>400 — invalid {@code report} enum or {@code fiscalYear} out of {@code [2020, 2099]}</li>
 *   <li>422 — Python bridge raised {@link BridgeException} (engine-side failure)</li>
 *   <li>503 — bridge subprocess dead at request time</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1")
public class ApiController {

    private static final Logger log = LoggerFactory.getLogger(ApiController.class);

    private final RunService runService;
    private final PythonBridge bridge;

    public ApiController(RunService runService, PythonBridge bridge) {
        this.runService = runService;
        this.bridge = bridge;
    }

    @PostMapping("/run")
    public ResponseEntity<JsonNode> run(@RequestBody RunRequest req) throws BridgeException {
        if (!bridge.ping()) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .build();
        }

        ReportType reportType = ReportType.fromWire(req.report());
        JsonNode result = runService.dispatch(
                reportType,
                req.fiscalYear(),
                req.folder(),
                req.effectiveAllowPii());

        return ResponseEntity.ok(result);
    }

    @GetMapping("/healthz")
    public Map<String, Object> healthz() {
        boolean ok = bridge.ping();
        return Map.of("status", ok ? "ok" : "degraded", "bridge", ok);
    }

    @GetMapping("/reports")
    public List<String> reports() {
        return runService.availableReports();
    }

    // ── Exception handlers ────────────────────────────────────────────────────

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, String>> badRequest(IllegalArgumentException e) {
        log.debug("Bad request: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(Map.of("error", e.getMessage()));
    }

    @ExceptionHandler(BridgeException.class)
    public ResponseEntity<Map<String, String>> bridgeFailure(BridgeException e) {
        log.warn("Bridge error: {}", e.getMessage(), e);
        return ResponseEntity.status(HttpStatus.UNPROCESSABLE_ENTITY)
                .body(Map.of("error", e.getMessage()));
    }
}
