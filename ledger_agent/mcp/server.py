from __future__ import annotations

import json
import logging
import sys

log = logging.getLogger(__name__)

SERVER_NAME = "ledger-agent"
SERVER_VERSION = "2.1.0"
PROTOCOL_VERSION = "2024-11-05"


class PrivacyRedactionError(RuntimeError):
    pass


def _redact_response(payload: dict, *, allow_pii: bool = False) -> dict:
    try:
        from ledger_agent.core.audit import audit
    except Exception:
        audit = None  # type: ignore

    try:
        from ledger_agent.core.privacy import _detect_api_keys
        raw_for_keys = json.dumps(payload)
        if _detect_api_keys(raw_for_keys):
            if audit:
                audit("mcp.redaction.api_key_hit",
                      payload_size=len(raw_for_keys),
                      allow_pii=allow_pii)
            log.error("MCP: API key detected in tool response payload — response withheld")
            raise PrivacyRedactionError("api_key_in_response")
    except ImportError:
        pass

    if allow_pii:
        if audit:
            audit("mcp.redaction.skipped", reason="allow_pii=true")
        return payload

    try:
        from ledger_agent.core.privacy import redact as _redact

        # SMELL-M4 fix: walk the dict structurally and redact each string
        # value individually.  The previous json.dumps → regex → json.loads
        # round-trip could silently corrupt JSON if a redacted token
        # introduced JSON-breaking characters (e.g. a token containing \").
        _combined_mapping: dict = {}

        def _walk(obj):
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(v) for v in obj]
            if isinstance(obj, str):
                redacted_str, m = _redact(obj, scope="mcp_response")
                _combined_mapping.update(m)
                return redacted_str
            return obj

        result = _walk(payload)
        if audit:
            audit("mcp.redaction.applied",
                  payload_size=len(json.dumps(payload)),
                  tokens_issued=len(_combined_mapping))
        return result
    except Exception as exc:
        if audit:
            audit("mcp.redaction.failed",
                  error_type=type(exc).__name__,
                  error_message=str(exc)[:200])
        # P-13: the message can carry a value from the payload; it stays on the
        # exception (and in the redacted audit log), stderr gets the type only.
        log.error("Privacy redaction FAILED (fail-closed): %s", type(exc).__name__)
        raise PrivacyRedactionError(f"redaction_failed: {exc}") from exc


def _new_ref() -> str:
    """Opaque ``ref:`` id: 8 hex chars with at least one letter (P-15)."""
    from ledger_agent.core.privacy import new_error_ref
    return new_error_ref()


def _record_internal_error(event: str, where: str, exc: BaseException) -> str:
    """Log + audit ``exc`` by TYPE and a fresh ref only; return the ref (P-13).

    The exception message and the traceback can carry raw values: the traceback
    goes to DEBUG only, the message is never logged above DEBUG or sent to a client.
    """
    ref = _new_ref()
    error_type = type(exc).__name__
    log.warning("MCP: internal error in %s (%s) (ref:%s)", where, error_type, ref)
    log.debug("MCP: internal error traceback (ref:%s)", ref, exc_info=exc)
    try:
        from ledger_agent.core.audit import audit
        audit(event, where=where, error_type=error_type, ref=ref)
    except Exception:  # noqa: BLE001 — auditing must not mask the error response
        pass
    return ref


# P-13: modules whose own ``raise ValueError(...)`` statements build the message
# from fixed text plus the caller's own arguments (fiscal year, period, folder,
# tool name) — never from statement or DB data.  Those messages are useful to an
# MCP client; any other ValueError gets an opaque ``ref:`` instead.
_CLIENT_SAFE_VALUE_ERROR_MODULES = frozenset({
    "ledger_agent.core.api",
    "ledger_agent.mcp.tools",
})


def _client_safe_value_error(exc: ValueError) -> bool:
    """True only for a plain ``ValueError`` raised by an explicit ``raise``
    statement in one of ``_CLIENT_SAFE_VALUE_ERROR_MODULES``.

    A ValueError from a builtin conversion (``int()``, ``float()``,
    ``json.loads``…) or from deeper in core can quote data, so it fails closed.
    """
    if type(exc) is not ValueError or exc.__traceback__ is None:
        return False
    tb = exc.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    if tb.tb_frame.f_globals.get("__name__") not in _CLIENT_SAFE_VALUE_ERROR_MODULES:
        return False
    import linecache
    line = linecache.getline(tb.tb_frame.f_code.co_filename, tb.tb_lineno).strip()
    return line.startswith("raise ValueError(")


def _dispatch(msg: dict) -> dict | None:
    from ledger_agent.mcp.tools import TOOL_SCHEMAS, call_tool

    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    def _ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return _ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _ok({}) if msg_id is not None else None

    if method == "tools/list":
        return _ok({"tools": TOOL_SCHEMAS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        meta = params.get("_meta") or {}
        allow_pii = bool(meta.get("allow_pii", False))

        try:
            raw_json = call_tool(tool_name, tool_args, allow_pii=allow_pii)
            raw_dict = json.loads(raw_json)
            try:
                redacted = _redact_response(raw_dict, allow_pii=allow_pii)
            except PrivacyRedactionError as pii_err:
                ref = _record_internal_error("mcp.redaction.withheld", "tools/call", pii_err)
                return _err(-32000, f"redaction_failed (ref:{ref})")
            return _ok({
                "content": [{"type": "text", "text": json.dumps(redacted, indent=2)}],
            })
        except Exception as exc:  # noqa: BLE001 — fail closed, never echo the error
            if isinstance(exc, ValueError) and _client_safe_value_error(exc):
                if str(exc).startswith("Unknown tool"):
                    return _err(-32601, str(exc))
                try:
                    text = _redact_response({"message": str(exc)},
                                            allow_pii=allow_pii)["message"]
                except PrivacyRedactionError as pii_err:
                    exc = pii_err
                else:
                    return _ok({"content": [{"type": "text", "text": text}],
                                "isError": True})
            ref = _record_internal_error("mcp.internal_error", "tools/call", exc)
            return _ok({
                "content": [{"type": "text",
                              "text": f"Internal error (ref:{ref}) — see server logs"}],
                "isError": True,
            })

    if msg_id is not None:
        return _err(-32601, f"Method not found: {method!r}")
    return None


def serve_stdio() -> None:
    log.info("ledger-agent MCP stdio server ready (protocol %s)", PROTOCOL_VERSION)

    import sys as _sys

    def _write(obj):
        _sys.stdout.write(json.dumps(obj) + "\n")
        _sys.stdout.flush()

    while True:
        try:
            line = _sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            response = _dispatch(msg)
            if response is not None:
                _write(response)
        except EOFError:
            break
        except json.JSONDecodeError:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}})
        except Exception as exc:  # noqa: BLE001 — fail closed, never echo the error
            ref = _record_internal_error("mcp.internal_error", "main_loop", exc)
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": f"Internal error (ref:{ref})"}})


def serve_http(host: str = "127.0.0.1", port: int = 7337) -> None:
    """Start the HTTP transport.  Requires FI_MCP_HTTP_TOKEN (see transport_http)."""
    from ledger_agent.mcp.transport_http import serve_http as _http

    def _dispatch_sync(request: dict) -> dict:
        response = _dispatch(request)
        if response is None:
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}
        return response

    _http(host=host, port=port, dispatch_fn=_dispatch_sync)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ledger-agent-mcp",
        description="ledger-agent MCP server",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="HTTP bind address (default 127.0.0.1; non-loopback needs "
             "FI_MCP_HTTP_ALLOW_REMOTE=1)",
    )
    parser.add_argument("--port", type=int, default=7337)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--method", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cli", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(levelname)s %(name)s: %(message)s")
    # stderr is inherited by callers (P-14/P-15): redact before configure_logging
    # replaces this handler, including at DEBUG with --debug.
    from ledger_agent.core.logging_setup import install_log_redaction
    install_log_redaction()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.transport == "http":
        from ledger_agent.mcp.transport_http import HTTPTransportConfigError
        try:
            serve_http(host=args.host, port=args.port)
        except HTTPTransportConfigError as exc:
            sys.stderr.write(f"ledger-agent-mcp: {exc}\n")
            raise SystemExit(2) from None
    else:
        serve_stdio()


if __name__ == "__main__":
    main()
