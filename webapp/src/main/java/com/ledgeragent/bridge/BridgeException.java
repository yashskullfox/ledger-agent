package com.ledgeragent.bridge;

/**
 * Thrown when the Java↔Python bridge encounters an error:
 * process startup failure, I/O error, JSON-RPC error response, or timeout.
 *
 * <p>ARCH-08
 */
public class BridgeException extends Exception {

    public BridgeException(String message) {
        super(message);
    }

    public BridgeException(String message, Throwable cause) {
        super(message, cause);
    }
}
