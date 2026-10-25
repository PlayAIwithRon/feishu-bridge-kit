#!/usr/bin/env python3
"""Feishu two-way bridge: long-connection (websocket) event listener.

Connects to Feishu with lark-oapi's ws client (no public URL needed),
handles `im.message.receive_v1` events, and appends one JSON line per
event to inbox.jsonl next to this file.

Credentials come from config.json in the same directory:
    {"app_id": "...", "app_secret": "..."}

If config.json is missing or still holds placeholders, this script exits
immediately with a clear error (exit code 2) instead of crash-looping.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Proxy patch -----------------------------------------------------------
# Some hosts reach the internet only through an HTTP egress proxy (env vars
# http_proxy/https_proxy). lark-oapi's ws client explicitly forces a direct
# websocket connection (proxy=None in _ws_connect_kwargs), which fails on
# such hosts. Patch it to pass the env proxy (if any) through to
# websockets.connect instead. On hosts without a proxy this is a no-op.
try:
    import lark_oapi.ws.client as _lark_ws_client

    _orig_ws_connect_kwargs = _lark_ws_client._ws_connect_kwargs

    def _patched_ws_connect_kwargs(connection_headers=None):
        kwargs = _orig_ws_connect_kwargs(connection_headers)
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    _lark_ws_client._ws_connect_kwargs = _patched_ws_connect_kwargs
except Exception as _patch_exc:  # never let the patch itself kill the listener
    print(f"WARNING: proxy patch not applied: {_patch_exc}", file=sys.stderr)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
INBOX_PATH = BASE_DIR / "inbox.jsonl"

PLACEHOLDER_MARKERS = ("", "your_app_id", "your_app_secret", "cli_xxx", "placeholder")


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            "ERROR: credentials not configured yet: config.json not found at "
            f"{CONFIG_PATH}. Copy config.example.json to config.json and fill in "
            "app_id / app_secret first.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: credentials not configured yet: cannot read config.json ({exc}).", file=sys.stderr)
        sys.exit(2)
    app_id = str(cfg.get("app_id", "")).strip()
    app_secret = str(cfg.get("app_secret", "")).strip()
    if app_id.lower() in PLACEHOLDER_MARKERS or app_secret.lower() in PLACEHOLDER_MARKERS \
            or "xxx" in app_id or "xxx" in app_secret:
        print(
            "ERROR: credentials not configured yet: config.json still contains "
            "placeholder/empty app_id or app_secret. Fill in the real values first.",
            file=sys.stderr,
        )
        sys.exit(2)
    return {"app_id": app_id, "app_secret": app_secret}


def on_message_receive(data) -> None:
    """Handle one P2ImMessageReceiveV1 event: record it in inbox.jsonl."""
    try:
        event = data.event
        message = event.message
        sender = event.sender

        sender_open_id = ""
        if sender is not None and sender.sender_id is not None:
            sender_open_id = sender.sender_id.open_id or ""

        msg_type = message.message_type or ""
        raw_content = message.content or ""

        text = ""
        if msg_type == "text":
            try:
                text = json.loads(raw_content).get("text", "") or ""
            except (json.JSONDecodeError, AttributeError):
                text = ""

        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "message_id": message.message_id or "",
            "chat_id": message.chat_id or "",
            "sender_open_id": sender_open_id,
            "msg_type": msg_type,
            "text": text,
        }
        with INBOX_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        log(f"inbox <- {record['message_id']} chat={record['chat_id']} "
            f"from={sender_open_id} type={msg_type} text={text!r}")
    except Exception as exc:  # never let one bad event kill the listener
        log(f"WARN: failed to record event: {exc}")


def main() -> int:
    cfg = load_config()

    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    client = lark.ws.Client(
        cfg["app_id"],
        cfg["app_secret"],
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    log("listener starting (long connection / websocket mode); inbox: %s" % INBOX_PATH)
    client.start()  # blocks; the ws client auto-reconnects
    return 0


if __name__ == "__main__":
    sys.exit(main())
