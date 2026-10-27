#!/usr/bin/env python3
"""Feishu two-way bridge: long-connection (websocket) event listener. v2

Connects to Feishu with lark-oapi's ws client (no public URL needed),
handles `im.message.receive_v1` events, and appends one JSON line per
event to inbox.jsonl next to this file.

v2 adds disconnect recovery (see backfill.py): a background thread runs
a history backfill shortly after startup and then on an interval
(default 120s, env FEISHU_BACKFILL_INTERVAL_SECS, 0 = disabled). Any
user message that was sent while this listener was disconnected is
recovered from the chat-history API and appended to inbox.jsonl with
"source": "backfill". Incoming events are also deduplicated by
message_id (in memory, seeded from inbox.jsonl), so the live push and
the backfill can never write the same message twice.

The thread also refreshes bridge-status.json on every pass
(last_backfill_at / last result), so a watchdog can tell at a glance
whether recovery is healthy.

Credentials come from config.json in the same directory:
    {"app_id": "...", "app_secret": "..."}

If config.json is missing or still holds placeholders, this script exits
immediately with a clear error (exit code 2) instead of crash-looping.
"""

import json
import os
import sys
import threading
import time
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
STATUS_PATH = BASE_DIR / "bridge-status.json"

PLACEHOLDER_MARKERS = ("", "your_app_id", "your_app_secret", "cli_xxx", "placeholder")

BACKFILL_INTERVAL_SECS = int(os.environ.get("FEISHU_BACKFILL_INTERVAL_SECS", "120"))


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


# --- inbox dedupe ------------------------------------------------------------
_seen_lock = threading.Lock()
_seen_ids: set[str] = set()


def _seed_seen_ids() -> None:
    if not INBOX_PATH.exists():
        return
    for line in INBOX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            mid = json.loads(line).get("message_id")
        except json.JSONDecodeError:
            continue
        if mid:
            _seen_ids.add(mid)


def _append_record(record: dict) -> bool:
    """Append to inbox.jsonl unless this message_id is already there."""
    mid = record.get("message_id") or ""
    with _seen_lock:
        if mid and mid in _seen_ids:
            return False
        with INBOX_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if mid:
            _seen_ids.add(mid)
    return True


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
            "source": "websocket",
        }
        if _append_record(record):
            log(f"inbox <- {record['message_id']} chat={record['chat_id']} "
                f"from={sender_open_id} type={msg_type} text={text!r}")
        else:
            log(f"inbox: duplicate event skipped: {record['message_id']}")
    except Exception as exc:  # never let one bad event kill the listener
        log(f"WARN: failed to record event: {exc}")


# --- backfill thread ---------------------------------------------------------

def _write_status(fields: dict) -> None:
    try:
        current = {}
        if STATUS_PATH.exists():
            try:
                current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                current = {}
        current.update(fields)
        STATUS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"WARN: cannot write bridge-status.json: {exc}")


def _backfill_loop() -> None:
    """Recover missed messages forever: first pass after 10s, then periodic.

    Runs in a daemon thread; a failure here must never take the listener
    down, so every pass is wrapped and logged.
    """
    if BACKFILL_INTERVAL_SECS <= 0:
        log("backfill disabled (FEISHU_BACKFILL_INTERVAL_SECS=0)")
        return
    try:
        import backfill
    except Exception as exc:
        log(f"WARN: backfill module unavailable, recovery disabled: {exc}")
        return
    time.sleep(10)
    while True:
        try:
            result = backfill.run_backfill(base_dir=BASE_DIR)
            # Pick up ids the backfill just wrote, so the live handler
            # dedupes against them too.
            with _seen_lock:
                ids, _ = backfill.read_inbox(BASE_DIR)
                _seen_ids.update(ids)
            _write_status({
                "listener": "running",
                "last_backfill_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "last_backfill_result": result,
            })
        except SystemExit:
            log("WARN: backfill pass skipped (config not ready)")
        except Exception as exc:
            log(f"WARN: backfill pass failed: {exc}")
            _write_status({"last_backfill_error": str(exc)})
        time.sleep(BACKFILL_INTERVAL_SECS)


def main() -> int:
    cfg = load_config()
    _seed_seen_ids()

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
    _write_status({
        "listener": "starting",
        "started_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "backfill_interval_secs": BACKFILL_INTERVAL_SECS,
    })
    threading.Thread(target=_backfill_loop, name="backfill", daemon=True).start()
    log("listener starting (long connection / websocket mode, v2 with backfill); inbox: %s" % INBOX_PATH)
    client.start()  # blocks; the ws client auto-reconnects
    return 0


if __name__ == "__main__":
    sys.exit(main())
