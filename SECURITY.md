# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| main    | ✅ Yes     |
| < 0.1.0 | ❌ No      |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

To report a security vulnerability, open a
[GitHub Security Advisory](https://github.com/yashskullfox/ledger-agent/security/advisories/new) <!-- # redaction: allow -->
in this repository. Maintainers will respond within 72 hours.

## Privacy-specific security concerns

This project processes partnership financial data. If you discover a defect that
could cause:

- PII (partner names, account numbers, financial figures) to be written to
  tracked files or sent to external services
- The `allow_pii=True` firewall to be bypassed without explicit opt-in
- Redaction scanner false-negatives (real names passing the scanner)

Please report these as security advisories — they are treated as P0 incidents
per `AGENTS.md §1 rule 3`.

## Scope

In scope: the Python core (`ledger_agent/core/`), CLI, MCP server, Spring Boot webapp.

Out of scope: third-party dependencies (report to the upstream project directly).
