from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SERVER_NAME = "ledger-agent"
SERVER_VERSION = "2.1.0"
PROTOCOL_VERSION = "2024-11-05"


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


# ---------------------------------------------------------------------------
# Privacy firewall (ARCH-07 / ARCH-22)
# ---------------------------------------------------------------------------

class PrivacyRedactionError(RuntimeError):
    """Raised when redaction fails and allow_pii=False — fail-closed."""


def _redact_struct(obj, redact_fn):
    """
    SMELL-M4 fix: walk a JSON-compatible object recursively, redacting all
    string leaf values in place.  Avoids the json.dumps → regex → json.loads
    round-trip that can silently corrupt JSON when a redaction token or the
    original value contains characters that change JSON structure (e.g. `"`).

    Preserves dict insertion order (Python 3.7+ guarantee) for deterministic
    downstream consumers.  Non-string scalars (int, float, bool, None) pass
    through unchanged.
    """
    if isinstance(obj, str):
        result, _ = redact_fn(obj, scope="mcp_response")
        return result
    if isinstance(obj, dict):
        return {k: _redact_struct(v, redact_fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_struct(item, redact_fn) for item in obj]
    return obj  # int, float, bool, None — pass through unchanged


def _redact_response(payload: dict, *, allow_pii: bool = False) -> dict:
    try:
        from core.audit import audit
    except Exception:
        audit = None  # type: ignore

    # Always sweep for API keys — even allow_pii=True must not leak credentials
    # (ARCH-22 / BUG-M1).
    try:
        from core.privacy import _detect_api_keys  # type: ignore[import]
        api_hits = _detect_api_keys(json.dumps(payload))
        if api_hits:
            raise PrivacyRedactionError(
                "API key detected in payload — blocked even with allow_pii=True")
    except PrivacyRedactionError:
        raise
    except Exception:
        pass  # privacy module unavailable — fall through to full redaction path

    if allow_pii:
        if audit:
            audit("mcp.redaction.skipped", reason="allow_pii=true")
        return payload

    try:
        from core.privacy import redact as _redact  # type: ignore[import]
        raw_size = len(json.dumps(payload))  # size metric only — not used for redaction
        # SMELL-M4 fix: structural walk instead of json.dumps → regex → json.loads.
        # The old approach could silently corrupt the response if a redaction token
        # or original value contained JSON-significant characters (e.g. `"`).
        redacted = _redact_struct(payload, _redact)
        if audit:
            audit("mcp.redaction.applied",
                  payload_size=raw_size,
                  tokens_issued=0)  # structural walk doesn't return a unified mapping
        return redacted
    except Exception as exc:
        # FAIL-CLOSED: redaction failure means we cannot prove the response is
        # safe to emit. The response is withheld and the operator is alerted.
        if audit:
            audit("mcp.redaction.failed",
                  error_type=type(exc).__name__,
                  error_message=str(exc)[:200])
        log.error("Privacy redaction FAILED (fail-closed): %s", exc)
        raise PrivacyRedactionError(f"redaction_failed: {exc}") from exc


# ---------------------------------------------------------------------------
# MCP dispatcher (transport-agnostic JSON-RPC handler)
# ---------------------------------------------------------------------------

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
                return _err(-32000, "redaction_failed")
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
        except Exception:
            tb = traceback.format_exc()
            # SMELL-M3: never return the full traceback to the client — it
            # leaks file paths, module structure, and internal state.
            # Full tb is logged server-side; client gets a sanitised message.
            log.error("Tool %r raised:\n%s", tool_name, tb)
            return _ok({
                "content": [{"type": "text", "text": "Internal error (see server log)"}],
                "isError": True,
            })

    if msg_id is not None:
        return _err(-32601, f"Method not found: {method!r}")
    return None


# ---------------------------------------------------------------------------
# Transport: stdio (single canonical loop — ARCH-22)
# ---------------------------------------------------------------------------

def serve_stdio() -> None:
    _bootstrap()
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


# ---------------------------------------------------------------------------
# Transport: HTTP
# ---------------------------------------------------------------------------

def serve_http(host: str = "127.0.0.1", port: int = 7337) -> None:
    _bootstrap()
    from ledger_agent.mcp.transport_http import serve_http as _http

    def _dispatch_sync(request: dict) -> dict:
        response = _dispatch(request)
        if response is None:
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}
        return response

    _http(host=host, port=port, dispatch_fn=_dispatch_sync)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ledger-agent-mcp",
        description="ledger-agent MCP server",
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
    )
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
