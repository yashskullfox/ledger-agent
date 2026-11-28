from __future__ import annotations

import json
import logging
import re
import sys

log = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

_TOOL_METHODS = frozenset({
    "import_statements",
    "generate_balance_sheet",
    "generate_form_1065",
    "generate_k1",
    "pte_estimate",
    "reconcile_year",
    "customer_summary",
    "audit_year",
})


def _respond(msg_id, result):
    payload = {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _error(msg_id, code: int, message: str):
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class BridgeRedactionError(RuntimeError):
    """Redaction or the egress check failed; the response is withheld.

    ``message`` is safe to send to the client: a fixed prefix, an optional list of
    leak CATEGORIES (from ``privacy.audit_egress``) and an opaque ``ref:`` id.  It
    never carries a value from the payload or from the underlying exception.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _new_ref() -> str:
    """Opaque ``ref:`` id: 8 hex chars with at least one letter (P-15)."""
    from ledger_agent.core.privacy import new_error_ref
    return new_error_ref()


def _egress_strings(obj) -> list[str]:
    """Every dict key and string value of ``obj`` (the text that can carry PII).

    Numbers, booleans and null are typed values (amounts, counts, years, flags),
    not free text: they are left out of the egress scan so that a large integer
    is not mistaken for a bare account-number digit run (P-11).
    """
    out: list[str] = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                out.append(str(k))
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str):
            out.append(cur)
    return out


_CATEGORY_RE = re.compile(r"'([A-Za-z_]{1,40})'")
# Categories privacy.audit_egress can report.  Anything else in the exception
# text is dropped, so a value can never reach the client through this path.
_LEAK_CATEGORIES = frozenset({
    "SSN", "EIN", "ITIN", "credit_card", "routing", "IBAN", "URL_credentials",
    "private_key_block", "account_reference", "digit_run", "protected_name",
})


def _leak_categories(exc: BaseException) -> str:
    """Known category names from a PrivacyLeakError message, else ``"pii"``."""
    cats = [c for c in _CATEGORY_RE.findall(str(exc)) if c in _LEAK_CATEGORIES]
    return ",".join(dict.fromkeys(cats)) or "pii"


def _redact_bridge_response(payload: dict, *, allow_pii: bool = False) -> dict:
    """
    Redact a tool result before it is sent to the Java client (BUG-B2 / P-11).

    Reuses ``ledger_agent.mcp.server._redact_response`` so Forms C and D redact the
    same way: the payload is walked structurally and only string VALUES are passed
    to ``privacy.redact(scope="mcp_response")``; numbers, booleans, null and keys
    are untouched, and non-ASCII text is redacted as text (never as ``\\uXXXX``
    escapes).  The API-key sweep runs unconditionally, even with ``allow_pii``.

    Unless ``allow_pii`` is set, ``privacy.audit_egress`` then runs on every key
    and string value of the redacted result.

    Fail-closed: any error — an API key, a redaction failure, an egress block or a
    failing egress check — raises ``BridgeRedactionError`` whose message holds only
    a fixed prefix, leak categories and an opaque ``ref:``.  Logs and audit events
    carry the exception type and the ref, never a value.
    """
    try:
        from ledger_agent.core.audit import audit as _audit
    except Exception:
        _audit = None  # type: ignore

    def _record(event: str, **fields) -> None:
        if _audit:
            try:
                _audit(event, **fields)
            except Exception:  # noqa: BLE001 — auditing must not mask the result
                pass

    try:
        from ledger_agent.core import privacy
        from ledger_agent.mcp.server import _redact_response
    except Exception as exc:  # noqa: BLE001 — no redactor → nothing leaves
        ref = _new_ref()
        _record("bridge.redaction.failed", kind="import_failed",
                error_type=type(exc).__name__, ref=ref)
        log.error("Bridge: redaction unavailable (%s) — response withheld (ref:%s)",
                  type(exc).__name__, ref)
        raise BridgeRedactionError(f"bridge_redaction_failed (ref:{ref})") from None

    try:
        redacted = _redact_response(payload, allow_pii=allow_pii)
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        ref = _new_ref()
        kind = "api_key_in_response" if "api_key" in str(exc) else "redaction_failed"
        _record("bridge.redaction.failed", kind=kind,
                error_type=type(exc).__name__, ref=ref)
        log.error("Bridge: %s (%s) — response withheld (ref:%s)",
                  kind, type(exc).__name__, ref)
        raise BridgeRedactionError(f"bridge_redaction_failed (ref:{ref})") from None

    if allow_pii:
        _record("bridge.redaction.skipped", reason="allow_pii=true")
        return redacted

    try:
        privacy.audit_egress(_egress_strings(redacted))
    except Exception as exc:  # noqa: BLE001 — fail closed on anything
        ref = _new_ref()
        if isinstance(exc, privacy.PrivacyLeakError):
            cats = _leak_categories(exc)
            _record("bridge.egress.blocked", categories=cats, ref=ref)
            log.error("Bridge: audit_egress blocked the response [%s] (ref:%s)", cats, ref)
            raise BridgeRedactionError(
                f"bridge_egress_blocked: {cats} (ref:{ref})"
            ) from None
        _record("bridge.egress.check_failed", error_type=type(exc).__name__, ref=ref)
        log.error("Bridge: audit_egress check failed (%s) — response withheld (ref:%s)",
                  type(exc).__name__, ref)
        raise BridgeRedactionError(f"bridge_egress_check_failed (ref:{ref})") from None

    _record("bridge.redaction.applied", egress_audit="pass")
    return redacted


def _internal_error(msg_id, exc: BaseException, *, where: str) -> None:
    """Send an opaque ``Internal error (ref:…)`` (JSON-RPC -32603) for ``exc`` (P-13).

    The client (and, through ``RunController``/``ApiController``, the browser)
    gets only the fixed text and a random ref.  The exception message and the
    traceback can carry raw values, so they never reach the client: stderr
    (inherited by the Java process) gets the exception TYPE and ref at WARNING,
    the traceback only at DEBUG, and the audit log gets the type and ref.
    """
    ref = _new_ref()
    error_type = type(exc).__name__
    log.warning("Bridge: internal error in %s (%s) (ref:%s)", where, error_type, ref)
    log.debug("Bridge: internal error traceback (ref:%s)", ref, exc_info=exc)
    try:
        from ledger_agent.core.audit import audit
        audit("bridge.internal_error", where=where, error_type=error_type, ref=ref)
    except Exception:  # noqa: BLE001 — auditing must not mask the error response
        pass
    _error(msg_id, -32603, f"Internal error (ref:{ref})")


def _dispatch(msg: dict) -> None:
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "ping":
        _respond(msg_id, {"pong": True})
        return

    if method == "server_info":
        _respond(msg_id, {
            "name": "ledger-agent-bridge",
            "version": "2.1.0",
            "methods": sorted(_TOOL_METHODS) + ["ping", "server_info"],
        })
        return

    if method in _TOOL_METHODS:
        try:
            from ledger_agent.mcp.tools import call_tool
            allow_pii = bool((params.get("_meta") or {}).get("allow_pii", False))
            clean_params = {k: v for k, v in params.items()
                            if not k.startswith("_")}
            try:
                from ledger_agent.core.audit import audit
                from ledger_agent.core.cleanup import run_cycle
                audit("bridge.tool_call", method=method, allow_pii=allow_pii)
                with run_cycle(f"bridge:{method}"):
                    # BUG-B1 fix: capture the error type here, inside the
                    # run_cycle body, before run_cycle.__exit__ can raise a
                    # secondary exception that would replace exc in the outer
                    # handler and produce a misleading audit event.
                    try:
                        raw_json = call_tool(method, clean_params, allow_pii=allow_pii)
                    except Exception as _tool_exc:
                        try:
                            audit("bridge.tool_error", method=method,
                                  error_type=type(_tool_exc).__name__)
                        except Exception:
                            pass
                        raise
            except ImportError:
                raw_json = call_tool(method, clean_params, allow_pii=allow_pii)

            raw_dict = json.loads(raw_json)
            try:
                redacted = _redact_bridge_response(raw_dict, allow_pii=allow_pii)
            except BridgeRedactionError as red_err:
                _error(msg_id, -32000, red_err.message)
                return
            _respond(msg_id, redacted)

        except Exception as exc:  # noqa: BLE001 — fail closed, never echo the error
            _internal_error(msg_id, exc, where=f"tool:{method}")
        return

    _error(msg_id, -32601, f"Method not found: {method!r}")


def _background_init() -> None:
    """
    Run boot_cleanup + audit initialisation in a daemon thread so the bridge
    enters the stdin readline loop immediately.

    Without this, audit._ensure_initialised() calls mkdir() on the data/audit
    directory.  On NFS-backed filesystems (CI, some dev machines) that mkdir()
    blocks indefinitely while waiting for the NFS lock server — which means the
    bridge never reaches its readline loop, Java's readLine() blocks until the
    test harness kills the process, and the ping fails.

    Running in a daemon thread means:
    - The bridge is responsive immediately (ping works before this completes).
    - Audit still initialises normally when the filesystem is fast.
    - If mkdir hangs forever the daemon thread is silently reaped on exit.
    """
    try:
        from ledger_agent.core.audit import audit, current_run_id
        from ledger_agent.core.cleanup import boot_cleanup
        boot_cleanup()
        audit("bridge.session.start", transport="jsonrpc_stdio")
        log.info("ledger-agent JSON-RPC bridge ready on stdio (run_id=%s)",
                 current_run_id())
    except Exception:
        log.info("ledger-agent JSON-RPC bridge ready on stdio")


def _configure_bridge_logging() -> None:
    """Set up bridge logging on stderr, which the Java process inherits (P-14).

    * Every root handler gets ``LogRedactionFilter`` (message + args, traceback,
      stack and extras are redacted after rendering); ``configure_logging``, which
      later replaces these handlers when a core module calls ``get_logger``,
      attaches the same filter to its own handler.
    * ``FI_LOG_LEVEL=DEBUG`` is refused unless ``FI_BRIDGE_DEBUG=1`` is also set:
      a level floor of INFO is installed (it also holds against the later
      ``configure_logging``) and one WARNING is logged, without values.  With
      ``FI_BRIDGE_DEBUG=1`` DEBUG is enabled, still redacted.
    """
    import os

    from ledger_agent.core import logging_setup

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logging_setup.install_log_redaction()

    debug_requested = os.environ.get("FI_LOG_LEVEL", "").strip().upper() == "DEBUG"
    debug_allowed = os.environ.get("FI_BRIDGE_DEBUG", "").strip() == "1"
    if not debug_requested:
        return
    if not debug_allowed:
        logging_setup.set_level_floor(logging.INFO)
        log.warning("Bridge: FI_LOG_LEVEL=DEBUG refused (set FI_BRIDGE_DEBUG=1 to "
                    "allow it); bridge logging capped at INFO")
        return
    logging.getLogger().setLevel(logging.DEBUG)
    log.warning("Bridge: DEBUG logging enabled (FI_BRIDGE_DEBUG=1); "
                "log output is still redacted")


def serve() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    _configure_bridge_logging()

    import threading
    _init_thread = threading.Thread(target=_background_init, daemon=True,
                                    name="bridge-init")
    _init_thread.start()

    try:
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                _dispatch(msg)
            except EOFError:
                break
            except json.JSONDecodeError as exc:
                _error(None, -32700, f"Parse error: {exc}")
            except Exception as exc:  # noqa: BLE001 — fail closed, never echo the error
                _internal_error(None, exc, where="main_loop")
    finally:
        try:
            from ledger_agent.core.audit import audit, shutdown_audit
            audit("bridge.session.end", transport="jsonrpc_stdio")
            shutdown_audit()
        except Exception:
            pass


if __name__ == "__main__":
    serve()
