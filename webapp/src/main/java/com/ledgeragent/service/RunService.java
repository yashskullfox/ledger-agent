package com.ledgeragent.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * Shared dispatch layer used by both the HTML form transport ({@code RunController})
 * and the REST API transport ({@code ApiController}).  Having one service prevents
 * logic drift between the two entry points — same engine, two transports, one binary
 * (per Section 6.3 requirement).
 *
 * <p>Validation rules:
 * <ul>
 *   <li>{@code fiscalYear} must be in {@code [2020, 2099]}; throws
 *       {@link IllegalArgumentException} otherwise (mapped to 400 by REST layer,
 *       rendered as inline error by HTML layer).</li>
 *   <li>{@code allowPii} defaults to {@code false} for the HTML form; REST callers
 *       may pass {@code true} explicitly (threaded through to the R-46 firewall).</li>
 * </ul>
 */
@Service
public class RunService {

    private static final int YEAR_MIN = 2020;
    private static final int YEAR_MAX = 2099;

    private final PythonBridge bridge;

    public RunService(PythonBridge bridge) {
        this.bridge = bridge;
    }

    /**
     * Dispatch a report to the Python bridge.
     *
     * @param report     which report to run
     * @param fiscalYear must be in {@code [2020, 2099]}
     * @param folder     absolute path to statements folder (used only for IMPORT)
     * @param allowPii   when true, PII passes through R-46 firewall (default: false)
     * @return bridge result as {@link JsonNode}
     * @throws IllegalArgumentException if {@code fiscalYear} is out of range
     * @throws BridgeException          if the Python bridge fails
     */
    public JsonNode dispatch(ReportType report, int fiscalYear, String folder, boolean allowPii)
            throws BridgeException {

        if (fiscalYear < YEAR_MIN || fiscalYear > YEAR_MAX) {
            throw new IllegalArgumentException(
                    "fiscalYear must be between " + YEAR_MIN + " and " + YEAR_MAX
                    + ", got: " + fiscalYear);
        }

        return switch (report) {
            case BALANCE_SHEET -> bridge.generateBalanceSheet(fiscalYear, allowPii);
            case FORM1065      -> bridge.generateForm1065(fiscalYear, allowPii);
            case K1_PARTNER_1  -> bridge.generateK1(fiscalYear, "partner_1", allowPii);
            case K1_PARTNER_2  -> bridge.generateK1(fiscalYear, "partner_2", allowPii);
            case TAX_ESTIMATE  -> bridge.pteEstimate(fiscalYear, allowPii);
            case RECONCILE     -> bridge.reconcileYear(fiscalYear, allowPii);
            case IMPORT        -> bridge.importStatements(
                                      folder != null ? folder : "", false, allowPii);
        };
    }

    /** All report wire names in canonical order (for {@code GET /api/v1/reports}). */
    public List<String> availableReports() {
        return ReportType.allWireNames();
    }
}
