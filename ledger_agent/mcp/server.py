from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

SERVER_NAME = "ledger-agent"
SERVER_VERSION = "2.1.0"
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Bootstrap: add repo root to sys.path so the package is importable when
# run from the source checkout (not just when installed as a wheel).
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


# ---------------------------------------------------------------------------
# Privacy firewall  (ARCH-07)
# ---------------------------------------------------------------------------

def _redact_response(payload: dict, *, allow_pii: bool = False) -> dict:
    if allow_pii:
        return payload

    try:
        from core.privacy import redact as _redact  # type: ignore[import]
        raw = json.dumps(payload)
        redacted_text, _ = _redact(raw, scope="mcp_response")
        return json.loads(redacted_text)
    except Exception as exc:
        # Privacy library not available or failed — log and pass through.
        # This is intentionally non-fatal so the server stays up, but we
        # emit a WARNING so operators know redaction was skipped.
        log.warning("Privacy redaction skipped: %s", exc)
        return payload


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

    # ── Lifecycle ────────────────────────────────────────────────────────────
    if method == "initialize":
        return _ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # notification — no response

    if method == "ping":
        return _ok({}) if msg_id is not None else None

    # ── Tool discovery ───────────────────────────────────────────────────────
    if method == "tools/list":
        return _ok({"tools": TOOL_SCHEMAS})

    # ── Tool invocation ──────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        # Check allow_pii opt-in from the _meta object
        meta = params.get("_meta") or {}
        allow_pii = bool(meta.get("allow_pii", False))

        try:
            raw_json = call_tool(tool_name, tool_args, allow_pii=allow_pii)
            raw_dict = json.loads(raw_json)
            redacted = _redact_response(raw_dict, allow_pii=allow_pii)
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
            log.error("Tool %r raised:\n%s", tool_name, tb)
            return _ok({
                "content": [{"type": "text", "text": f"Internal error:\n{tb}"}],
                "isError": True,
            })

    # ── Unknown method ───────────────────────────────────────────────────────
    if msg_id is not None:
        return _err(-32601, f"Method not found: {method!r}")
    return None  # unknown notification — ignore


# ---------------------------------------------------------------------------
# Transport: stdio
# ---------------------------------------------------------------------------

def serve_stdio() -> None:
    """Run the MCP server over stdin/stdout (newline-delimited JSON-RPC)."""
    _bootstrap()
    log.info("ledger-agent MCP stdio server ready (protocol %s)", PROTOCOL_VERSION)

    from ledger_agent.mcp.transport_stdio import read_message, respond, error as err_out

    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            response = _dispatch(msg)
            if response is not None:
                respond(response.get("id"), response.get("result") or response.get("error"))
                # Re-send as full envelope so the client sees it correctly
                import sys as _sys
                _sys.stdout.write(json.dumps(response) + "\n")
                _sys.stdout.flush()
        except EOFError:
            break
        except json.JSONDecodeError:
            err_out(None, -32700, "Parse error")
        except Exception:
            err_out(None, -32603, traceback.format_exc())


def _serve_stdio_clean() -> None:
    """Cleaner stdio loop that writes full JSON-RPC envelopes directly."""
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
        description="ledger-agent MCP server (ARCH-06)",
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7337,
                        help="HTTP port (default: 7337)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging to stderr")
    # MCP Inspector passes --method / --cli flags — accept and ignore them
    parser.add_argument("--method", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cli", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(levelname)s %(name)s: %(message)s")

    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.transport == "http":
        serve_http(host=args.host, port=args.port)
    else:
        _serve_stdio_clean()


if __name__ == "__main__":
    main()
