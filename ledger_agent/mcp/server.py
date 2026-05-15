from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Optional

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
        raw = json.dumps(payload)
        redacted_text, _mapping = _redact(raw, scope="mcp_response")
        result = json.loads(redacted_text)
        if audit:
            audit("mcp.redaction.applied",
                  payload_size=len(raw),
                  tokens_issued=len(_mapping))
        return result
    except Exception as exc:
        if audit:
            audit("mcp.redaction.failed",
                  error_type=type(exc).__name__,
                  error_message=str(exc)[:200])
        log.error("Privacy redaction FAILED (fail-closed): %s", exc)
        raise PrivacyRedactionError(f"redaction_failed: {exc}") from exc


def _dispatch(msg: dict) -> Optional[dict]:
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
                return _err(-32000, f"redaction_failed: {pii_err}")
            return _ok({
                "content": [{"type": "text", "text": json.dumps(redacted, indent=2)}],
            })
        except ValueError as exc:
            if "Unknown tool" in str(exc):
                return _err(-32601, str(exc))
            return _ok({
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Tool %r raised:\n%s", tool_name, tb)
            import hashlib
            import time
            err_id = hashlib.sha1(
                f"{tool_name}{time.time()}".encode()
            ).hexdigest()[:8]
            return _ok({
                "content": [{"type": "text",
                              "text": f"Internal error (ref:{err_id}) — see server logs"}],
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
        except Exception:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": traceback.format_exc()}})


def serve_http(host: str = "127.0.0.1", port: int = 7337) -> None:
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7337)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--method", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cli", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.transport == "http":
        serve_http(host=args.host, port=args.port)
    else:
        serve_stdio()


if __name__ == "__main__":
    main()
