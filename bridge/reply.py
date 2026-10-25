#!/usr/bin/env python3
"""Feishu two-way bridge: send or reply to a message from the CLI.

Usage:
  python3 reply.py --message-id <id> --text "..."   # reply to a message
  python3 reply.py --chat-id <id>    --text "..."   # new message to a chat

Credentials come from config.json next to this file (app_id / app_secret).
The tenant_access_token and app secret are never printed.
Exit code is non-zero on any failure; the API response code is printed.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
API_BASE = "https://open.feishu.cn/open-apis"


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
    if not app_id or not app_secret or "xxx" in app_id or "xxx" in app_secret:
        print(
            "ERROR: credentials not configured yet: config.json has empty/placeholder "
            "app_id or app_secret.",
            file=sys.stderr,
        )
        sys.exit(2)
    return {"app_id": app_id, "app_secret": app_secret}


def post_json(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}
    except urllib.error.URLError as exc:
        print(f"ERROR: network failure: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def get_tenant_access_token(cfg: dict) -> str:
    _, body = post_json(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        {"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
    )
    code = body.get("code", -1)
    if code != 0 or not body.get("tenant_access_token"):
        print(f"tenant_access_token failed: api code={code} msg={body.get('msg', '')}")
        sys.exit(1)
    return body["tenant_access_token"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Send or reply to a Feishu message.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--message-id", help="Reply to this message_id")
    group.add_argument("--chat-id", help="Send a new message to this chat_id")
    parser.add_argument("--text", required=True, help="Message text to send")
    args = parser.parse_args()

    cfg = load_config()
    token = get_tenant_access_token(cfg)

    content = json.dumps({"text": args.text}, ensure_ascii=False)
    if args.message_id:
        url = f"{API_BASE}/im/v1/messages/{args.message_id}/reply"
        payload = {"content": content, "msg_type": "text"}
    else:
        url = f"{API_BASE}/im/v1/messages?receive_id_type=chat_id"
        payload = {"receive_id": args.chat_id, "msg_type": "text", "content": content}

    http_status, body = post_json(url, payload, token=token)
    code = body.get("code", -1)
    print(f"http_status={http_status} api_code={code} msg={body.get('msg', '')}")
    if code != 0:
        return 1
    data = body.get("data") or {}
    if data.get("message_id"):
        print(f"sent message_id={data['message_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
