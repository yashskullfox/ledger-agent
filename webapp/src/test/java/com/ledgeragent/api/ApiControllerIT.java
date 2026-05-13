package com.ledgeragent.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ledgeragent.bridge.BridgeException;
import com.ledgeragent.bridge.PythonBridge;
import com.ledgeragent.service.ReportType;
import com.ledgeragent.service.RunService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import java.util.List;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/**
 * Unit-level slice test for {@link ApiController} — verifies routing, status codes,
 * and JSON shapes without a real Python bridge (mocked via Mockito).
 *
 * <p>Run via:
 * <pre>
 *   cd webapp && ./mvnw -q test -Dtest=ApiControllerIT
 * </pre>
 */
@WebMvcTest(ApiController.class)
@DisplayName("ApiController — REST surface (ARCH-17)")
class ApiControllerIT {

    @Autowired
    private MockMvc mvc;

    @MockBean
    private RunService runService;

    @MockBean
    private PythonBridge bridge;

    private final ObjectMapper mapper = new ObjectMapper();

    @BeforeEach
    void setUp() throws Exception {
        when(bridge.ping()).thenReturn(true);
        when(runService.availableReports()).thenReturn(ReportType.allWireNames());
    }

    // ── GET /api/v1/reports ───────────────────────────────────────────────────

    @Test
    @DisplayName("GET /api/v1/reports returns all 7 wire names")
    void getReports() throws Exception {
        mvc.perform(get("/api/v1/reports"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(7))
                .andExpect(jsonPath("$[0]").value("balance_sheet"));
    }

    // ── GET /api/v1/healthz ───────────────────────────────────────────────────

    @Test
    @DisplayName("GET /api/v1/healthz returns ok when bridge is alive")
    void healthzOk() throws Exception {
        mvc.perform(get("/api/v1/healthz"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ok"))
                .andExpect(jsonPath("$.bridge").value(true));
    }

    @Test
    @DisplayName("GET /api/v1/healthz returns degraded when bridge is down")
    void healthzDegraded() throws Exception {
        when(bridge.ping()).thenReturn(false);
        mvc.perform(get("/api/v1/healthz"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("degraded"))
                .andExpect(jsonPath("$.bridge").value(false));
    }

    // ── POST /api/v1/run — happy path ─────────────────────────────────────────

    @Test
    @DisplayName("POST /api/v1/run balance_sheet 2024 returns 200 with bridge payload")
    void runBalanceSheet() throws Exception {
        JsonNode fakeResult = mapper.readTree("{\"total_assets\":30139.00}");
        when(runService.dispatch(eq(ReportType.BALANCE_SHEET), eq(2024), any(), eq(false)))
                .thenReturn(fakeResult);

        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"balance_sheet\",\"fiscalYear\":2024,\"folder\":\"/tmp\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.total_assets").value(30139.00));
    }

    @Test
    @DisplayName("POST /api/v1/run with allowPii=true propagates the flag")
    void runWithAllowPii() throws Exception {
        JsonNode fakeResult = mapper.readTree("{\"entity_name\":\"SYNCED LLC\"}");
        when(runService.dispatch(eq(ReportType.FORM1065), eq(2024), any(), eq(true)))
                .thenReturn(fakeResult);

        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"form1065\",\"fiscalYear\":2024,\"folder\":\"\",\"allowPii\":true}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.entity_name").value("SYNCED LLC"));
    }

    // ── POST /api/v1/run — error cases ────────────────────────────────────────

    @Test
    @DisplayName("POST /api/v1/run unknown report → 400")
    void unknownReport() throws Exception {
        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"bogus\",\"fiscalYear\":2024}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").exists());
    }

    @Test
    @DisplayName("POST /api/v1/run fiscalYear out of range → 400")
    void fiscalYearOutOfRange() throws Exception {
        when(runService.dispatch(any(), eq(1900), any(), anyBoolean()))
                .thenThrow(new IllegalArgumentException("fiscalYear must be between 2020 and 2099"));

        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"balance_sheet\",\"fiscalYear\":1900}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").exists());
    }

    @Test
    @DisplayName("POST /api/v1/run bridge exception → 422")
    void bridgeException() throws Exception {
        when(runService.dispatch(eq(ReportType.BALANCE_SHEET), anyInt(), any(), anyBoolean()))
                .thenThrow(new BridgeException("Python process crashed"));

        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"balance_sheet\",\"fiscalYear\":2024}"))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(jsonPath("$.error").value("Python process crashed"));
    }

    @Test
    @DisplayName("POST /api/v1/run when bridge is down → 503")
    void bridgeDown() throws Exception {
        when(bridge.ping()).thenReturn(false);

        mvc.perform(post("/api/v1/run")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"report\":\"balance_sheet\",\"fiscalYear\":2024}"))
                .andExpect(status().isServiceUnavailable());
    }
}
