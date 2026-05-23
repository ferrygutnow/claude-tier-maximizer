#!/usr/bin/env python3
"""calibrator — analyze usage.jsonl and surface mis-classifications.

Reads /var/log/claude-tier-maximizer/usage.jsonl and reports:
- prompts classified high that used very little thinking (over-classified)
- prompts classified low or medium that hit their thinking budget cap (under-classified)
- distribution per (reason) tag so you can see which rules are pulling weight

Usage:
  python3 calibrator.py [--usage-log PATH] [--top N]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_LOG = "/var/log/claude-tier-maximizer/usage.jsonl"


def load_records(path: str):
    recs = []
    p = Path(path)
    if not p.exists():
        print(f"no usage log at {path}", file=sys.stderr)
        return recs
    with open(p) as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return recs


def thinking_tokens(rec: dict) -> int:
    """Extract whatever Anthropic returned for thinking. Tolerant of field-name drift."""
    u = rec.get("usage") or {}
    for key in ("thinking_tokens", "thinking_output_tokens", "reasoning_tokens"):
        v = u.get(key)
        if isinstance(v, int):
            return v
    return 0


def analyze(recs, top=10):
    print(f"Analyzing {len(recs)} records from usage log\n")

    by_level = Counter(r.get("level") for r in recs)
    print("Distribution by classified level:")
    for lvl, n in by_level.most_common():
        pct = n / len(recs) * 100 if recs else 0
        print(f"  {lvl:<14} {n:>6}  ({pct:5.1f}%)")
    print()

    by_reason = Counter(r.get("reason") for r in recs)
    print("Distribution by reason tag:")
    for r, n in by_reason.most_common():
        print(f"  {r:<22} {n:>6}")
    print()

    # Calibration signals
    over_classified = []  # level=high but used little thinking
    under_classified = []  # level in (low, medium) but hit cap

    for rec in recs:
        lvl = rec.get("level")
        budget = rec.get("budget_set")
        thinking = thinking_tokens(rec)
        if not lvl or budget is None or thinking <= 0:
            continue
        if lvl == "high" and thinking < 1500:
            over_classified.append((thinking, rec))
        if lvl in ("low", "medium") and thinking >= 0.9 * budget:
            under_classified.append((thinking, rec))

    if over_classified:
        print(f"OVER-CLASSIFIED ({len(over_classified)} entries):")
        print("  Tagged high but used < 1.5k thinking tokens — candidates to downshift to medium")
        for thinking, rec in sorted(over_classified, key=lambda x: x[0])[:top]:
            print(f"  thinking={thinking:<5} reason={rec.get('reason'):<14} preview={rec.get('preview','')[:90]!r}")
        print()

    if under_classified:
        print(f"UNDER-CLASSIFIED ({len(under_classified)} entries):")
        print("  Tagged low/medium but hit budget cap — candidates to upshift")
        for thinking, rec in sorted(under_classified, key=lambda x: -x[0])[:top]:
            budget = rec.get("budget_set")
            print(f"  thinking={thinking:<5}/{budget:<5} reason={rec.get('reason'):<14} preview={rec.get('preview','')[:90]!r}")
        print()

    # Sum savings vs always-high baseline (16k)
    classified_total = sum(1 for r in recs if r.get("budget_set") is not None)
    if classified_total:
        baseline = classified_total * 16000
        actual = sum(r.get("budget_set", 0) for r in recs if r.get("budget_set") is not None)
        saved = baseline - actual
        pct = saved / baseline * 100
        print(f"Budget allocation vs always-high (16k) baseline:")
        print(f"  baseline:        {baseline:>12,} tokens")
        print(f"  router-actual:   {actual:>12,} tokens")
        print(f"  saved:           {saved:>12,} tokens  ({pct:5.1f}%)")
        actual_thinking_total = sum(thinking_tokens(r) for r in recs)
        if actual_thinking_total > 0:
            print(f"  actual thinking used by model: {actual_thinking_total:,} tokens")

    print("\nNext: edit rules/personal.yaml (or default.yaml) to add patterns")
    print("for mis-classified prompts. Then `systemctl restart claude-tier-maximizer`.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage-log", default=DEFAULT_LOG)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    recs = load_records(args.usage_log)
    if not recs:
        sys.exit(1)
    analyze(recs, top=args.top)


if __name__ == "__main__":
    main()
