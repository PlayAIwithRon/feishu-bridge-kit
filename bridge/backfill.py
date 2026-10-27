#!/usr/bin/env python3
"""Feishu two-way bridge: history backfill (disconnect recovery).

Why this exists
---------------
The long connection (websocket) is a real-time push channel: if the
listener is disconnected when a message is sent, Feishu does not replay
that event after reconnecting. The message itself still exists in the
chat history, though — so this module periodically asks the Feishu IM
API for recent history of every chat the bridge already knows about,
and appends anything missing to inbox.jsonl.

Design
------
- Source of truth for "already have it" is inbox.jsonl itself
  (message_id set). Backfilled rows carry ``"source": "backfill"`` and
  ``"recovered": true`` so downstream consumers can tell them apart.
- Only messages sent by a *user* are recovered (sender_type == "user");
  the bot's own replies are skipped, otherwise it would loop.
- Deleted messages are skipped.
- A small overlap window (default 5 minutes) plus message_id dedupe
  makes repeated runs idempotent: running it twice recovers 0 the
  second time.
- State is kept in bridge-state.json next to this file: known chat_ids
  (learned from inbox.jsonl), last run time, last result. The state is
  informational — losing it only widens the next lookback window.
- Can run standalone (cron / manual), and listener.py also runs it in
  a background thread: once shortly after startup, then on an interval
  (default 120s, env FEISHU_BACKFILL_INTERVAL_SECS, 0 = disabled).

Usage
-----
    python3 backfill.py                 # incremental (state-aware window)
    python3 backfill.py --hours 48      # look back 48 hours
    python3 backfill.py --chat-id oc_xxx --hours 24
    python3 backfill.py --dry-run       # report only, touch nothing

Credentials come from config.json in the same directory
({"app_id": "...", "app_secret": "..."}), same as listener.py/reply.py.
Exit code is non-zero if the Feishu API call itself fails.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
INBOX_PATH = BASE_DIR / "inbox.jsonl"
STATE_PATH = BASE_DIR / "bridge-state.json"

API_BASE = "https://open.feishu.cn/open-apis"
OVERLAP_SECS = 300            # re-scan the last 5 min of every window
DEFAULT_FIRST_LOOKBACK_SECS = 24 * 3600
MAX_PAGES_PER_CHAT = 10
PAGE_SIZE = 50


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{ts}] backfill: {msg}", flush=True)


# --- config / API ----------------------------------------------------------

def load_config(base_dir: Path = BASE_DIR) -> dict:
    cfg_path = base_dir / "config.json"
    if not cfg_path.exists():
        print(f"ERROR: config.json not found at {cfg_path}", file=sys.stderr)
        sys.exit(2)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: cannot read config.json ({exc})", file=sys.stderr)
        sys.exit(2)
    app_id = str(cfg.get("app_id", "")).strip()
    app_secret = str(cfg.get("app_secret", "")).strip()
    if not app_id or not app_secret or "xxx" in app_id or "xxx" in app_secret:
        print("ERROR: config.json has empty/placeholder app_id or app_secret", file=sys.stderr)
        sys.exit(2)
    return {"app_id": app_id, "app_secret": app_secret}


def _request(url: str, token: str | None = None, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"code": -1, "msg": f"http {exc.code}"}
    except urllib.error.URLError as exc:
        return {"code": -1, "msg": f"network failure: {exc.reason}"}


def get_tenant_access_token(cfg: dict) -> str:
    body = _request(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        payload={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
    )
    if body.get("code") != 0 or not body.get("tenant_access_token"):
        raise RuntimeError(f"tenant_access_token failed: code={body.get('code')} msg={body.get('msg', '')}")
    return body["tenant_access_token"]


def list_chat_messages(token: str, chat_id: str, start_secs: int, end_secs: int) -> list[dict]:
    """All history items in [start_secs, end_secs] for one chat, oldest first."""
    items: list[dict] = []
    page_token = ""
    for _ in range(MAX_PAGES_PER_CHAT):
        qs = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_secs),
            "end_time": str(end_secs),
            "page_size": str(PAGE_SIZE),
            "sort_type": "ByCreateTimeAsc",
        }
        if page_token:
            qs["page_token"] = page_token
        url = f"{API_BASE}/im/v1/messages?{urllib.parse.urlencode(qs)}"
        body = _request(url, token=token)
        if body.get("code") != 0:
            raise RuntimeError(
                f"list messages failed for chat {chat_id}: code={body.get('code')} msg={body.get('msg', '')}"
            )
        data = body.get("data") or {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return items


# --- inbox / state ---------------------------------------------------------

def read_inbox(base_dir: Path = BASE_DIR) -> tuple[set[str], list[str]]:
    """Return (message_ids already in inbox, known chat_ids in first-seen order)."""
    ids: set[str] = set()
    chats: list[str] = []
    inbox = base_dir / "inbox.jsonl"
    if inbox.exists():
        for line in inbox.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("message_id"):
                ids.add(rec["message_id"])
            cid = rec.get("chat_id")
            if cid and cid not in chats:
                chats.append(cid)
    return ids, chats


def load_state(base_dir: Path = BASE_DIR) -> dict:
    state_path = base_dir / "bridge-state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict, base_dir: Path = BASE_DIR) -> None:
    (base_dir / "bridge-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def item_to_record(item: dict) -> dict | None:
    """Convert one history API item to an inbox record, or None to skip it."""
    if item.get("deleted"):
        return None
    sender = item.get("sender") or {}
    if sender.get("sender_type") != "user":
        return None  # never recover the bot's own messages
    msg_type = item.get("msg_type") or ""
    text = ""
    if msg_type == "text":
        try:
            content = json.loads((item.get("body") or {}).get("content") or "{}")
            text = content.get("text", "") or ""
        except (json.JSONDecodeError, AttributeError):
            text = ""
    create_ms = int(item.get("create_time") or 0)
    ts = (
        datetime.fromtimestamp(create_ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        if create_ms
        else datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    )
    return {
        "ts": ts,
        "message_id": item.get("message_id") or "",
        "chat_id": item.get("chat_id") or "",
        "sender_open_id": sender.get("id") or "",
        "msg_type": msg_type,
        "text": text,
        "source": "backfill",
        "recovered": True,
    }


# --- main recovery pass ----------------------------------------------------

def run_backfill(
    base_dir: Path = BASE_DIR,
    chat_ids: list[str] | None = None,
    lookback_secs: int | None = None,
    dry_run: bool = False,
) -> dict:
    """One recovery pass. Returns {"recovered": n, "checked_chats": m, "candidates": k}."""
    cfg = load_config(base_dir)
    token = get_tenant_access_token(cfg)

    known_ids, inbox_chats = read_inbox(base_dir)
    state = load_state(base_dir)
    chats = list(dict.fromkeys((chat_ids or []) + inbox_chats + state.get("known_chat_ids", [])))

    now = int(time.time())
    if lookback_secs is not None:
        start = now - lookback_secs
    elif state.get("last_backfill_at"):
        start = int(state["last_backfill_at"]) - OVERLAP_SECS
    else:
        start = now - DEFAULT_FIRST_LOOKBACK_SECS

    recovered_records: list[dict] = []
    for cid in chats:
        for item in list_chat_messages(token, cid, start, now + 60):
            rec = item_to_record(item)
            if rec is None or not rec["message_id"]:
                continue
            if rec["message_id"] in known_ids:
                continue
            known_ids.add(rec["message_id"])
            recovered_records.append(rec)

    recovered_records.sort(key=lambda r: r["ts"])
    if recovered_records and not dry_run:
        with (base_dir / "inbox.jsonl").open("a", encoding="utf-8") as fh:
            for rec in recovered_records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if not dry_run:
        state["known_chat_ids"] = chats
        state["last_backfill_at"] = now
        state["last_backfill_result"] = {
            "recovered": len(recovered_records),
            "checked_chats": len(chats),
            "window_start": start,
            "window_end": now,
        }
        save_state(state, base_dir)

    result = {
        "recovered": len(recovered_records),
        "checked_chats": len(chats),
        "candidates": [r["message_id"] for r in recovered_records],
    }
    log(
        f"checked {len(chats)} chat(s), window {start}..{now}, "
        f"recovered {len(recovered_records)} message(s)" + (" (dry-run)" if dry_run else "")
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover messages missed while the listener was offline.")
    parser.add_argument("--chat-id", action="append", dest="chat_ids", help="Chat to check (repeatable); default: all known chats")
    parser.add_argument("--hours", type=float, help="Look back this many hours (default: since last run, or 24h first time)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be recovered, change nothing")
    args = parser.parse_args()

    try:
        result = run_backfill(
            chat_ids=args.chat_ids,
            lookback_secs=int(args.hours * 3600) if args.hours else None,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"recovered={result['recovered']} checked_chats={result['checked_chats']}")
    for mid in result["candidates"]:
        print(f"  recovered message_id={mid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
