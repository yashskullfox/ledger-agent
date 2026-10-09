#!/usr/bin/env python3
"""Read audit JSONL and print event summary."""
import json
from collections import Counter
from pathlib import Path

audit_dir = Path("ledger_agent/data/audit")
if not audit_dir.exists():
    audit_dir = Path(__file__).resolve().parent.parent / "ledger_agent" / "data" / "audit"
if not audit_dir.exists():
    audit_dir = Path("data/audit")

logs = sorted(audit_dir.glob("run-*.jsonl"))
events = Counter()
skipped_lines = 0
for log in logs:
    for line in log.read_text().splitlines():
        try:
            events[json.loads(line).get("event", "")] += 1
        except (json.JSONDecodeError, KeyError):
            # Ignore malformed or unreadable lines in audit logs
            skipped_lines += 1

print(f"=== Audit Summary ({len(logs)} runs, {sum(events.values())} events) ===")
for event, count in events.most_common():
    print(f"  {count:4d}  {event}")
if skipped_lines:
    print(f"WARNING: Skipped {skipped_lines} malformed audit line(s).")
