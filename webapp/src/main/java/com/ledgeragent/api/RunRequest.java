package com.ledgeragent.api;

/**
 * Request body for {@code POST /api/v1/run}.
 *
 * <p>{@code allowPii} is nullable — omitting it is equivalent to {@code false}.
 * The HTML form transport never sets this field; it always defaults to deny (R-46).
 */
public record RunRequest(
        String report,
        int fiscalYear,
        String folder,
        Boolean allowPii
) {
    /** Null-safe accessor — treats a missing value as {@code false}. */
    public boolean effectiveAllowPii() {
        return Boolean.TRUE.equals(allowPii);
    }
}
