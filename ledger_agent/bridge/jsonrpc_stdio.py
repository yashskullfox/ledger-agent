from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

_TOOL_METHODS = frozenset({
    "import_statements",
    "generate_balance_sheet",
    "generate_form_1065",
    "generate_k1",
    "pte_estimate",
    "reconcile_year",
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


def _redact_bridge_response(payload: dict, *, allow_pii: bool = False) -> dict:
    """
    Apply the same PII redaction used by the MCP server before sending any
    tool result back to the Java client (BUG-B2 fix).

    Mirrors ledger_agent.mcp.server._redact_response — fail-closed: if
    redaction itself fails and allow_pii is False, the response is withheld.
    When allow_pii is True the full payload is returned BUT the API-key sweep
    still runs unconditionally (BUG-M1 companion fix).
    """
    try:
        from ledger_agent.core.audit import audit as _audit
    except Exception:
        _audit = None  # type: ignore

    # API-key sweep is UNCONDITIONAL — regardless of allow_pii (BUG-M1 fix).
    try:
        from ledger_agent.core.privacy import PrivacyLeakError, redact as _check
        _check(json.dumps(payload), scope="api_key_only")
    except ImportError:
        pass  # privacy module unavailable — degrade gracefully
    except Exception:
        # API-key pattern hit → fail-closed even for allow_pii callers.
        if _audit:
            _audit("bridge.redaction.api_key_hit", method="<bridge>")
        log.error("Bridge: API key detected in tool response — response withheld")
        raise

    if allow_pii:
        if _audit:
            _audit("bridge.redaction.skipped", reason="allow_pii=true")
        return payload

    try:
        from ledger_agent.core.privacy import redact as _redact
        raw = json.dumps(payload)
        redacted_text, _mapping = _redact(raw, scope="mcp_response")
        if _audit:
            _audit("bridge.redaction.applied",
                   payload_size=len(raw),
                   tokens_issued=len(_mapping))
        return json.loads(redacted_text)
    except Exception as exc:
        if _audit:
            _audit("bridge.redaction.failed",
                   error_type=type(exc).__name__,
                   error_message=str(exc)[:200])
        log.error("Bridge: PII redaction FAILED (fail-closed): %s", exc)
        raise RuntimeError(f"bridge_redaction_failed: {exc}") from exc


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
            except RuntimeError:
                _error(msg_id, -32000, "bridge_redaction_failed")
                return
            _respond(msg_id, redacted)

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Bridge tool %r raised:\n%s", method, tb)
            _error(msg_id, -32603, f"Internal error: {tb}")
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
        from ledger_agent.core.cleanup import boot_cleanup
        from ledger_agent.core.audit import audit, current_run_id
        boot_cleanup()
        audit("bridge.session.start", transport="jsonrpc_stdio")
        log.info("ledger-agent JSON-RPC bridge ready on stdio (run_id=%s)",
                 current_run_id())
    except Exception:
        log.info("ledger-agent JSON-RPC bridge ready on stdio")


def serve() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

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
            except Exception:
                _error(None, -32603, traceback.format_exc())
    finally:
        try:
            from ledger_agent.core.audit import audit, shutdown_audit
            audit("bridge.session.end", transport="jsonrpc_stdio")
            shutdown_audit()
        except Exception:
            pass


if __name__ == "__main__":
    serve()
