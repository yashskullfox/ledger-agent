package com.ledgeragent.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import com.ledgeragent.service.ReportType;
import com.ledgeragent.service.RunService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.*;

@Controller
public class RunController {

    private static final Logger log = LoggerFactory.getLogger(RunController.class);

    private final PythonBridge bridge;
    private final RunService runService;

    @Value("${ledger.app-title:Financial Intelligence}")
    private String appTitle;

    @Value("${ledger.app-badge:Form D}")
    private String appBadge;

    @Value("${ledger.partner-a-label:Partner A (99%)}")
    private String partnerALabel;

    @Value("${ledger.partner-b-label:Partner B (1%)}")
    private String partnerBLabel;

    @Autowired
    public RunController(PythonBridge bridge, RunService runService) {
        this.bridge = bridge;
        this.runService = runService;
    }

    @GetMapping("/")
    public String index(Model model) {
        addCommonAttrs(model);
        model.addAttribute("fiscalYear", 2024);
        model.addAttribute("report", "balance_sheet");
        model.addAttribute("folder", System.getProperty("user.home") + "/statements");
        return "index";
    }

    @PostMapping("/run")
    public String run(
            @RequestParam(defaultValue = "") String folder,
            @RequestParam(defaultValue = "2024") int fiscalYear,
            @RequestParam(defaultValue = "balance_sheet") String report,
            Model model) {

        addCommonAttrs(model);
        model.addAttribute("fiscalYear", fiscalYear);
        model.addAttribute("report", report);
        model.addAttribute("folder", folder);

        try {
            ReportType reportType = ReportType.fromWire(report);
            // HTML form never exposes allowPii — default deny (R-46)
            JsonNode result = runService.dispatch(reportType, fiscalYear, folder, false);
            model.addAttribute("result", result);
            model.addAttribute("resultJson", result != null ? result.toPrettyString() : "{}");
            model.addAttribute("success", true);
            model.addAttribute("nextStep", suggestNextStep(report, result));
        } catch (IllegalArgumentException e) {
            model.addAttribute("success", false);
            model.addAttribute("errorMessage", e.getMessage());
            model.addAttribute("nextStep", "Correct the report type or fiscal year and try again.");
        } catch (BridgeException e) {
            log.warn("Bridge error for report={} year={}: {}", report, fiscalYear, e.getMessage());
            model.addAttribute("success", false);
            model.addAttribute("errorMessage", e.getMessage());
            model.addAttribute("nextStep",
                    "Check that statements have been imported and the database is initialised.");
        }

        return "results";
    }

    @GetMapping("/healthz")
    @ResponseBody
    public String healthz() {
        boolean bridgeOk = bridge.ping();
        return bridgeOk
                ? "{\"status\":\"ok\",\"bridge\":true}"
                : "{\"status\":\"degraded\",\"bridge\":false}";
    }

    @ExceptionHandler(BridgeException.class)
    public String renderInlineError(BridgeException e, Model model) {
        log.error("Unhandled BridgeException: {}", e.getMessage(), e);
        addCommonAttrs(model);
        model.addAttribute("success", false);
        model.addAttribute("errorMessage", e.getMessage());
        model.addAttribute("nextStep",
                "Verify the Python environment is set up: run './run.sh balance 2024' to diagnose.");
        return "results";
    }

    private void addCommonAttrs(Model model) {
        model.addAttribute("appTitle", appTitle);
        model.addAttribute("appBadge", appBadge);
        model.addAttribute("partnerALabel", partnerALabel);
        model.addAttribute("partnerBLabel", partnerBLabel);
    }

    private String suggestNextStep(String report, JsonNode result) {
        if (result == null) return "";
        return switch (report) {
            case "import" -> {
                int failed = result.has("failed") ? result.path("failed").asInt(0) : 0;
                yield failed > 0
                        ? "Some files failed to parse. Check the 'failed_files' list above."
                        : "Import complete. Run 'balance_sheet' to see the year-end financials.";
            }
            case "balance_sheet" -> "Review net income, then run 'form1065' to see the partnership return.";
            case "form1065"      -> "Review ordinary income, then generate K-1s for each partner.";
            case "k1_yash", "k1_parin" ->
                    "Partner K-1 generated. Run 'tax_estimate' to see quarterly payments.";
            case "tax_estimate"  -> "Quarterly payment schedule ready. Run 'reconcile' to verify transfers.";
            case "reconcile" -> {
                if (!result.has("clean")) yield "Report shape unexpected — see raw JSON above.";
                boolean clean = result.path("clean").asBoolean(true);
                yield clean ? "Reconciliation clean — year-end reporting is complete."
                            : "Issues found. Review the 'issues' list above before filing.";
            }
            default -> "";
        };
    }
}
