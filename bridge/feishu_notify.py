#!/usr/bin/env python3
"""Send a one-way task report to a Feishu group via custom bot webhook.

Usage:
  feishu_notify.py --type TYPE --status STATUS --severity action --body "..."
  feishu_notify.py --type TYPE --status STATUS --severity info --body-file /path

Severity mapping:
  urgent -> red, action -> yellow, info -> green.
Every message starts with the Muse marker line so it contains the keyword
"Muse" (passes the Feishu bot keyword check when enabled). The webhook URL
is read from webhook.json in the same directory as this script (keep it
chmod 600, never commit it). The URL is never printed.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

SEVERITY = {
    "urgent": "\U0001f534 \u7d27\u6025\uff08\u9700\u8981\u4f60\u5c3d\u5feb\u5904\u7406\uff09",
    "action": "\U0001f7e1 \u5f85\u5904\u7406\uff08\u7b49\u4f60\u6279\u51c6\u6216\u51b3\u5b9a\uff09",
    "info": "\U0001f7e2 \u4e00\u822c\uff08\u901a\u77e5\uff09",
}

CONFIG = Path(__file__).resolve().parent / "webhook.json"
HEADER = "\u3010\u53d1\u9001\u81ea Muse\u3011"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--severity", required=True, choices=sorted(SEVERITY))
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    args = parser.parse_args()

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8").strip()

    text = HEADER + "\n"
    text += "\u4efb\u52a1\u7c7b\u578b\uff1a" + args.type + "\n"
    text += "\u72b6\u6001\uff1a" + args.status + "\n"
    text += "\u4e25\u91cd\u7b49\u7ea7\uff1a" + SEVERITY[args.severity]
    if body:
        text += "\n" + body

    url = json.loads(CONFIG.read_text(encoding="utf-8"))["url"]
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        print("FEISHU_ERROR code=%s msg=%s" % (result.get("code"), result.get("msg")), file=sys.stderr)
        return 1
    print("FEISHU_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
