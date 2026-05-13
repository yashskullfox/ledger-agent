package com.ledgeragent.runtime;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.*;
import java.net.URL;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.zip.GZIPInputStream;

/**
 * Extracts the bundled Python runtime from the fat jar's classpath resources
 * and unpacks it to a stable temp-dir location.  Idempotent: re-running with
 * the same jar version hits the already-extracted directory and returns
 * immediately.
 *
 * <p>The runtime tarball is bundled at:
 * {@code src/main/resources/runtime/<OS-ARCH>/cpython.tar.gz}
 * where {@code <OS-ARCH>} is one of:
 * <ul>
 *   <li>{@code linux-x86_64}</li>
 *   <li>{@code darwin-aarch64}</li>
 *   <li>{@code darwin-x86_64}</li>
 *   <li>{@code windows-x86_64}</li>
 * </ul>
 *
 * <p>Extracted directory layout: {@code ${java.io.tmpdir}/ledger-agent/py-<sha8>/}
 *
 * <p>If no embedded runtime is found (i.e. running from source), returns
 * {@code null} and the {@link com.ledgeragent.bridge.PythonBridge} falls back
 * to searching {@code PATH}.
 *
 * <p>ARCH-10
 */
public final class PythonRuntimeExtractor {

    private static final Logger log = LoggerFactory.getLogger(PythonRuntimeExtractor.class);
    private static final String BASE_TEMP = System.getProperty("java.io.tmpdir")
            + File.separator + "ledger-agent";

    private PythonRuntimeExtractor() {}

    /**
     * Extract the bundled Python runtime to a temp directory.
     *
     * @return Path to the extracted runtime root (e.g.
     *         {@code /tmp/ledger-agent/py-a1b2c3d4/}), or {@code null} if no
     *         bundled runtime was found (source-checkout mode).
     * @throws IOException if extraction fails
     */
    public static Path extractToTempDir() throws IOException {
        String resourcePath = "runtime/" + osArch() + "/cpython.tar.gz";
        URL resource = PythonRuntimeExtractor.class.getClassLoader().getResource(resourcePath);
        if (resource == null) {
            log.debug("No bundled Python runtime at classpath:{} — using PATH", resourcePath);
            return null;
        }

        // Derive a short hash from the URL to use as a cache key
        String sha8 = sha8(resource.toString());
        Path dest = Paths.get(BASE_TEMP, "py-" + sha8);

        if (Files.isDirectory(dest) && Files.exists(dest.resolve("bin/python3"))) {
            log.debug("Python runtime already extracted at {}", dest);
            return dest;
        }

        log.info("Extracting Python runtime to {} …", dest);
        Files.createDirectories(dest);

        try (InputStream is = resource.openStream();
             GZIPInputStream gz = new GZIPInputStream(new BufferedInputStream(is))) {
            extractTar(gz, dest);
        }

        log.info("Python runtime extracted ({} bytes) to {}", Files.walk(dest).count(), dest);
        return dest;
    }

    // ── OS/arch detection ─────────────────────────────────────────────────────

    static String osArch() {
        String os = System.getProperty("os.name", "").toLowerCase();
        String arch = System.getProperty("os.arch", "").toLowerCase();

        String archKey;
        if (arch.contains("aarch64") || arch.contains("arm64")) {
            archKey = "aarch64";
        } else {
            archKey = "x86_64";
        }

        if (os.contains("mac") || os.contains("darwin")) {
            return "darwin-" + archKey;
        } else if (os.contains("win")) {
            return "windows-x86_64";
        } else {
            return "linux-x86_64";
        }
    }

    // ── TAR extraction (no external dependency) ───────────────────────────────

    private static void extractTar(InputStream tarStream, Path destDir) throws IOException {
        byte[] header = new byte[512];
        while (true) {
            int read = readFully(tarStream, header);
            if (read < 512) break;

            // Check for end-of-archive (two zero blocks)
            boolean allZero = true;
            for (byte b : header) { if (b != 0) { allZero = false; break; } }
            if (allZero) break;

            String name = readString(header, 0, 100);
            if (name.isEmpty()) break;

            long size = readOctal(header, 124, 12);
            int type = header[156];  // '0' or '\0' = regular file, '5' = directory

            Path target = destDir.resolve(name).normalize();
            if (!target.startsWith(destDir)) {
                // Zip-slip protection
                throw new IOException("Tar entry escapes destination: " + name);
            }

            if (type == '5' || name.endsWith("/")) {
                Files.createDirectories(target);
            } else if (type == '0' || type == 0) {
                Files.createDirectories(target.getParent());
                try (OutputStream out = Files.newOutputStream(target)) {
                    long remaining = size;
                    byte[] buf = new byte[4096];
                    while (remaining > 0) {
                        int toRead = (int) Math.min(remaining, buf.length);
                        int n = tarStream.read(buf, 0, toRead);
                        if (n < 0) break;
                        out.write(buf, 0, n);
                        remaining -= n;
                    }
                }
                // Set executable bit on bin/* files
                if (name.contains("/bin/") || name.contains("\\bin\\")) {
                    target.toFile().setExecutable(true, false);
                }
            }
            // Skip padding to 512-byte boundary
            long padded = (size + 511) & ~511;
            long skip = padded - size;
            if (skip > 0) tarStream.skip(skip);
        }
    }

    private static int readFully(InputStream is, byte[] buf) throws IOException {
        int total = 0;
        while (total < buf.length) {
            int n = is.read(buf, total, buf.length - total);
            if (n < 0) break;
            total += n;
        }
        return total;
    }

    private static String readString(byte[] buf, int offset, int len) {
        int end = offset;
        while (end < offset + len && buf[end] != 0) end++;
        return new String(buf, offset, end - offset).trim();
    }

    private static long readOctal(byte[] buf, int offset, int len) {
        String s = readString(buf, offset, len).trim();
        if (s.isEmpty()) return 0;
        try { return Long.parseLong(s, 8); } catch (NumberFormatException e) { return 0; }
    }

    private static String sha8(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] hash = md.digest(input.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash).substring(0, 8);
        } catch (Exception e) {
            return Integer.toHexString(input.hashCode());
        }
    }
}
