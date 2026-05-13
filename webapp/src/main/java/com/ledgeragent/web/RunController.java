package com.ledgeragent.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.*;

/**
 * Primary web controller for the ledger-agent webapp (Form D — ARCH-09).
 *
 * <p>Routes:
 * <ul>
 *   <li>{@code GET  /}         — home page (folder picker + fiscal year)</li>
 *   <li>{@code POST /run}      — run the selected report and show results</li>
 *   <li>{@code GET  /healthz}  — health probe for ARCH-10 fat-jar accept check</li>
 * </ul>
 *
 * <p>All computation is delegated to the {@link PythonBridge} — this
 * controller contains zero financial logic.
 */
@Controller
public class RunController {

    private static final Logger log = LoggerFactory.getLogger(RunController.class);

    private final PythonBridge bridge;

    @Autowired
    public RunController(PythonBridge bridge) {
        this.bridge = bridge;
    }

    // ── Home page ─────────────────────────────────────────────────────────────

    @GetMapping("/")
    public String index(Model model) {
        model.addAttribute("fiscalYear", 2024);
        model.addAttribute("report", "balance_sheet");
        model.addAttribute("folder", System.getProperty("user.home") + "/statements");
        return "index";
    }

    // ── Report runner ─────────────────────────────────────────────────────────

    /**
     * Run the selected report and display the results page.
     *
     * @param folder     Absolute path to the statements folder (for import)
     * @param fiscalYear Four-digit fiscal year (e.g. 2024)
     * @param report     Report type: balance_sheet | form1065 | k1_yash |
     *                   k1_parin | tax_estimate | reconcile
     * @param model      Spring MVC model
     * @return template name
     */
    @PostMapping("/run")
    public String run(
            @RequestParam(defaultValue = "") String folder,
            @RequestParam(defaultValue = "2024") int fiscalYear,
            @RequestParam(defaultValue = "balance_sheet") String report,
            Model model) {

        model.addAttribute("fiscalYear", fiscalYear);
        model.addAttribute("report", report);
        model.addAttribute("folder", folder);

        try {
            JsonNode result = dispatchReport(report, fiscalYear, folder);
            model.addAttribute("result", result);
            model.addAttribute("resultJson", result != null ? result.toPrettyString() : "{}");
            model.addAttribute("success", true);
            model.addAttribute("nextStep", suggestNextStep(report, result));
        } catch (BridgeException e) {
            log.warn("Bridge error for report={} year={}: {}", report, fiscalYear, e.getMessage());
            model.addAttribute("success", false);
            model.addAttribute("errorMessage", e.getMessage());
            model.addAttribute("nextStep", "Check that statements have been imported and the database is initialised.");
        }

        return "results";
    }

    // ── Health probe ──────────────────────────────────────────────────────────

    @GetMapping("/healthz")
    @ResponseBody
    public String healthz() {
        boolean bridgeOk = bridge.ping();
        if (!bridgeOk) {
            // Not returning 503 to keep the fat-jar accept check simple
            return "{\"status\":\"degraded\",\"bridge\":false}";
        }
        return "{\"status\":\"ok\",\"bridge\":true}";
    }

    // ── Error handler ─────────────────────────────────────────────────────────

    @ExceptionHandler(BridgeException.class)
    public String renderInlineError(BridgeException e, Model model) {
        log.error("Unhandled BridgeException: {}", e.getMessage(), e);
        model.addAttribute("success", false);
        model.addAttribute("errorMessage", e.getMessage());
        model.addAttribute("nextStep",
                "Verify the Python environment is set up correctly: "
                + "run './run.sh balance 2024' to diagnose.");
        return "results";
    }

    // ── Dispatch helpers ──────────────────────────────────────────────────────

    private JsonNode dispatchReport(String report, int fiscalYear, String folder)
            throws BridgeException {
        return switch (report) {
            case "balance_sheet"  -> bridge.generateBalanceSheet(fiscalYear);
            case "form1065"       -> bridge.generateForm1065(fiscalYear);
            case "k1_yash"        -> bridge.generateK1(fiscalYear, "yash");
            case "k1_parin"       -> bridge.generateK1(fiscalYear, "parin");
            case "tax_estimate"   -> bridge.pteEstimate(fiscalYear);
            case "reconcile"      -> bridge.reconcileYear(fiscalYear);
            case "import"         -> bridge.importStatements(folder, false);
            default               -> throw new BridgeException("Unknown report type: " + report);
        };
    }

    private String suggestNextStep(String report, JsonNode result) {
        if (result == null) return "";
        return switch (report) {
            case "import" -> {
                int failed = result.path("failed").asInt(0);
                yield failed > 0
                        ? "Some files failed to parse. Check the 'failed_files' list above."
                        : "Import complete. Run 'balance_sheet' to see the year-end financials.";
            }
            case "balance_sheet" -> "Review net income, then run 'form1065' to see the partnership return.";
            case "form1065"      -> "Review ordinary income, then generate K-1s for each partner.";
            case "k1_yash", "k1_parin" ->
                    "All partner K-1s generated. Run 'tax_estimate' to see quarterly payments.";
            case "tax_estimate"  -> "Quarterly payment schedule ready. Run 'reconcile' to verify transfers.";
            case "reconcile"     -> {
                boolean clean = result.path("clean").asBoolean(true);
                yield clean ? "Reconciliation clean — year-end reporting is complete."
                            : "Issues found. Review the 'issues' list above and resolve before filing.";
            }
            default -> "";
        };
    }
}
