#!/usr/bin/env bash
# Feishu bridge inbox watcher: wakes an agent when the owner has sent Feishu
# messages that nobody has processed yet.
# Source of truth for "processed" is the bridge's watch-state.json, which is
# updated by whichever agent handles the messages. This script only reads.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

RESULT="$(python3 - <<'PY'
import json
from pathlib import Path

base = Path.home() / "workspace" / "feishu-bridge"
inbox = base / "inbox.jsonl"
state = base / "watch-state.json"

processed = set()
if state.exists():
    try:
        processed = set(json.loads(state.read_text(encoding="utf-8")).get("processed_message_ids", []))
    except Exception:
        processed = set()

new = []
if inbox.exists():
    for line in inbox.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        mid = ev.get("message_id")
        if mid and mid not in processed:
            new.append({"message_id": mid, "ts": ev.get("ts"), "text": (ev.get("text") or "")[:80]})

print(json.dumps({"count": len(new), "new": new[-5:]}, ensure_ascii=False))
PY
)"

COUNT="$(printf '%s' "$RESULT" | python3 -c 'import sys, json; print(json.load(sys.stdin)["count"])')"

log "inbox_check" "{\"unprocessed\": $COUNT}"

if [ "$COUNT" -gt 0 ]; then
  wake "飞书有新的未处理消息" "$RESULT"
else
  silent "暂无未处理的新消息" "$RESULT"
fi
