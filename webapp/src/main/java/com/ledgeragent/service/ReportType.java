package com.ledgeragent.service;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Canonical set of reports Form D supports. Wire names match the Python bridge
 * method keys and the HTML {@code <select>} option values so the three transports
 * (HTML form, REST API, bridge) share one source of truth.
 */
public enum ReportType {

    BALANCE_SHEET("balance_sheet"),
    FORM1065("form1065"),
    K1_YASH("k1_yash"),
    K1_PARIN("k1_parin"),
    TAX_ESTIMATE("tax_estimate"),
    RECONCILE("reconcile"),
    IMPORT("import");

    private final String wire;

    ReportType(String wire) {
        this.wire = wire;
    }

    /** The wire string used in HTTP requests / bridge method names. */
    public String wire() {
        return wire;
    }

    /**
     * Parse a wire string to a {@link ReportType}.
     *
     * @throws IllegalArgumentException if the string does not map to a known report
     */
    public static ReportType fromWire(String s) {
        if (s == null || s.isBlank()) {
            throw new IllegalArgumentException("report must not be blank");
        }
        return Arrays.stream(values())
                .filter(r -> r.wire.equals(s.trim().toLowerCase()))
                .findFirst()
                .orElseThrow(() -> new IllegalArgumentException("unknown report: " + s));
    }

    /** All wire names, ordered as in this enum (for API discovery). */
    public static List<String> allWireNames() {
        return Arrays.stream(values())
                .map(ReportType::wire)
                .collect(Collectors.toList());
    }
}
