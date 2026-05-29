package com.ledgeragent.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import com.ledgeragent.service.ReportType;
import com.ledgeragent.service.RunService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.util.List;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/**
 * Integration slice tests for {@link RunController} — verifies that the results
 * page renders correctly for all report types, including the W29 summary cards
 * for {@code customer_summary} and the raw-JSON fallback for other report types.
 *
 * <p>Run via:
 * <pre>
 *   cd webapp && ./mvnw -q test -Dtest=ResultsIT
 * </pre>
 */
@WebMvcTest(RunController.class)
@DisplayName("RunController — results page rendering (W29)")
class ResultsIT {

    @Autowired
    private MockMvc mvc;

    @MockitoBean
    private RunService runService;

    @MockitoBean
    private PythonBridge bridge;

    private final ObjectMapper mapper = new ObjectMapper();

    // ── Synthetic customer_summary JSON matching the W27 contract ─────────────

    private static final String CUSTOMER_SUMMARY_JSON = "{"
            + "\"fiscal_year\": 2024,"
            + "\"period_covered\": \"2024-01 to 2024-12\","
            + "\"profit_or_loss\": {"
            + "  \"status\": \"profit\","
            + "  \"signal\": \"positive\","
            + "  \"ordinary_business_income\": 7400.00"
            + "},"
            + "\"growth_signal\": {"
            + "  \"status\": \"stable\","
            + "  \"basis\": \"prior_year\","
            + "  \"note\": \"Revenue on track with prior period.\""
            + "},"
            + "\"balance_sheet_health\": {"
            + "  \"is_balanced\": true,"
            + "  \"status\": \"healthy\","
            + "  \"total_assets\": 50000.00,"
            + "  \"total_liabilities\": 0.00,"
            + "  \"total_equity\": 50000.00,"
            + "  \"period\": \"2024-12\","
            + "  \"skipped_accounts\": 0"
            + "},"
            + "\"pte_due_signal\": {"
            + "  \"status\": \"not_due\","
            + "  \"annual_estimate\": 740.00,"
            + "  \"next_due\": \"Q1 2025\""
            + "},"
            + "\"tax_due_signal\": {"
            + "  \"status\": \"monitor\","
            + "  \"basis\": \"pte_estimate\","
            + "  \"note\": \"Review quarterly.\""
            + "},"
            + "\"confidence_flags\": [\"CLOSE_READY\"],"
            + "\"next_actions\": [\"File Form 1065 for fiscal year 2024.\"]"
            + "}";

    @BeforeEach
    void setUp() throws Exception {
        when(bridge.ping()).thenReturn(true);
        when(runService.availableReports()).thenReturn(ReportType.allWireNames());
    }

    // ── GET / — index page ────────────────────────────────────────────────────

    @Test
    @DisplayName("GET / renders index page")
    void getIndexPage() throws Exception {
        mvc.perform(get("/"))
                .andExpect(status().isOk())
                .andExpect(view().name("index"));
    }

    // ── POST /run — balance_sheet (raw-JSON path) ─────────────────────────────

    @Test
    @DisplayName("POST /run balance_sheet renders results with raw JSON, no summary cards")
    void runBalanceSheet_showsRawJson() throws Exception {
        JsonNode balanceJson = mapper.readTree(
                "{\"total_assets\": 50000.0, \"total_liabilities\": 0.0, \"total_equity\": 50000.0}");
        when(runService.dispatch(eq(ReportType.BALANCE_SHEET), eq(2024), anyString(), anyBoolean()))
                .thenReturn(balanceJson);

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "balance_sheet")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andExpect(view().name("results"))
                .andExpect(model().attribute("success", true))
                .andExpect(model().attributeDoesNotExist("summary"))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("Raw Result (JSON)")));
    }

    // ── POST /run — customer_summary (W29 summary cards path) ────────────────

    @Test
    @DisplayName("POST /run customer_summary populates summary model attribute")
    void runCustomerSummary_populatesSummaryAttribute() throws Exception {
        JsonNode summaryJson = mapper.readTree(CUSTOMER_SUMMARY_JSON);
        when(runService.dispatch(eq(ReportType.CUSTOMER_SUMMARY), eq(2024), anyString(), anyBoolean()))
                .thenReturn(summaryJson);

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "customer_summary")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andExpect(view().name("results"))
                .andExpect(model().attribute("success", true))
                .andExpect(model().attributeExists("summary"));
    }

    @Test
    @DisplayName("POST /run customer_summary renders summary cards in HTML")
    void runCustomerSummary_rendersSummaryCards() throws Exception {
        JsonNode summaryJson = mapper.readTree(CUSTOMER_SUMMARY_JSON);
        when(runService.dispatch(eq(ReportType.CUSTOMER_SUMMARY), eq(2024), anyString(), anyBoolean()))
                .thenReturn(summaryJson);

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "customer_summary")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("summary-card")))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("Profit / Loss")))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("Balance Sheet Health")))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("PTE Due")));
    }

    @Test
    @DisplayName("POST /run customer_summary renders confidence flags")
    void runCustomerSummary_rendersConfidenceFlags() throws Exception {
        JsonNode summaryJson = mapper.readTree(CUSTOMER_SUMMARY_JSON);
        when(runService.dispatch(eq(ReportType.CUSTOMER_SUMMARY), eq(2024), anyString(), anyBoolean()))
                .thenReturn(summaryJson);

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "customer_summary")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("Confidence flags")))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("CLOSE_READY")));
    }

    @Test
    @DisplayName("POST /run customer_summary raw JSON section is collapsed when cards shown")
    void runCustomerSummary_rawJsonIsCollapsed() throws Exception {
        JsonNode summaryJson = mapper.readTree(CUSTOMER_SUMMARY_JSON);
        when(runService.dispatch(eq(ReportType.CUSTOMER_SUMMARY), eq(2024), anyString(), anyBoolean()))
                .thenReturn(summaryJson);

        String html = mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "customer_summary")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();

        // Template: th:open="${summary == null}" — when summary is set, <details> has no open attr
        org.junit.jupiter.api.Assertions.assertFalse(
                html.contains("<details open"),
                "Raw JSON <details> must be closed when summary cards are shown"
        );
    }

    @Test
    @DisplayName("POST /run customer_summary renders next_actions list")
    void runCustomerSummary_rendersNextActions() throws Exception {
        JsonNode summaryJson = mapper.readTree(CUSTOMER_SUMMARY_JSON);
        when(runService.dispatch(eq(ReportType.CUSTOMER_SUMMARY), eq(2024), anyString(), anyBoolean()))
                .thenReturn(summaryJson);

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "customer_summary")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp/statements"))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("Next actions")))
                .andExpect(content().string(
                        org.hamcrest.Matchers.containsString("File Form 1065 for fiscal year 2024.")));
    }

    // ── Error cases ──────────────────────────────────────────────────────────

    @Test
    @DisplayName("POST /run with unknown report type shows error banner")
    void runUnknownReport_showsErrorBanner() throws Exception {
        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "not_a_real_report")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp"))
                .andExpect(status().isOk())
                .andExpect(view().name("results"))
                .andExpect(model().attribute("success", false))
                .andExpect(content().string(
                        org.hamcrest.Matchers.containsString("Report failed")));
    }

    @Test
    @DisplayName("POST /run bridge exception renders error page")
    void runBridgeException_showsErrorPage() throws Exception {
        when(runService.dispatch(any(), anyInt(), anyString(), anyBoolean()))
                .thenThrow(new BridgeException("Python bridge unreachable"));

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "balance_sheet")
                        .param("fiscalYear", "2024")
                        .param("folder", "/tmp"))
                .andExpect(status().isOk())
                .andExpect(view().name("results"))
                .andExpect(model().attribute("success", false));
    }

    @Test
    @DisplayName("POST /run invalid year shows error")
    void runInvalidYear_showsError() throws Exception {
        when(runService.dispatch(any(), anyInt(), anyString(), anyBoolean()))
                .thenThrow(new IllegalArgumentException("fiscalYear out of range"));

        mvc.perform(post("/run")
                        .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                        .param("report", "balance_sheet")
                        .param("fiscalYear", "1900")
                        .param("folder", "/tmp"))
                .andExpect(status().isOk())
                .andExpect(model().attribute("success", false));
    }

    // ── GET /healthz ──────────────────────────────────────────────────────────

    @Test
    @DisplayName("GET /healthz returns ok when bridge is up")
    void healthzUp() throws Exception {
        when(bridge.ping()).thenReturn(true);
        mvc.perform(get("/healthz"))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("\"status\":\"ok\"")));
    }

    @Test
    @DisplayName("GET /healthz returns degraded when bridge is down")
    void healthzDegraded() throws Exception {
        when(bridge.ping()).thenReturn(false);
        mvc.perform(get("/healthz"))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("\"status\":\"degraded\"")));
    }
}
