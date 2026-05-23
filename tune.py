#!/usr/bin/env python3
"""tune — analyze usage.jsonl, propose rule changes, optionally auto-apply.

Always writes to:
  rules/pending.yaml  -- candidates awaiting human review (use ctm-review)
  /var/log/.../tune-report.md  -- markdown summary

If config.auto_tune.enabled is true, ALSO appends very-high-confidence
patterns to rules/auto.yaml and logs the change.

Confidence = (occurrences, consistency).
Patterns on the blocklist are never suggested again.

Usage:
  python3 tune.py [--config PATH] [--report PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_CONFIG = "/opt/claude-tier-maximizer/config.yaml"
DEFAULT_MD = "/var/log/claude-tier-maximizer/tune-report.md"


def load_yaml(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def save_yaml(path, data, header=""):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        if header:
            f.write(header)
        yaml.safe_dump(data, f, sort_keys=False)


def thinking_tokens(rec):
    u = rec.get("usage") or {}
    for k in ("thinking_tokens", "thinking_output_tokens", "reasoning_tokens"):
        v = u.get(k)
        if isinstance(v, int):
            return v
    return 0


def ngrams(text, n=3):
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", text.lower())
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def load_records(path):
    recs = []
    p = Path(path)
    if not p.exists():
        return recs
    with open(p) as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return recs


def ngram_to_regex(ng: str) -> str:
    """Turn a literal n-gram into a word-boundary regex."""
    words = ng.split()
    return r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"


def is_blocked(ng: str, blocklist_patterns: list) -> bool:
    """Returns True if ng (literal n-gram) matches any blocklist regex."""
    for bp in blocklist_patterns:
        try:
            if re.search(bp, ng, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--report", default=DEFAULT_MD)
    args = ap.parse_args()

    cfg = load_yaml(args.config)
    usage_log = (cfg.get("logging") or {}).get("usage", "/var/log/claude-tier-maximizer/usage.jsonl")
    autocfg = cfg.get("auto_tune") or {}
    pending_path = autocfg.get("pending_file", "/opt/claude-tier-maximizer/rules/pending.yaml")
    blocklist_path = autocfg.get("blocklist_file", "/opt/claude-tier-maximizer/rules/blocklist.yaml")
    auto_target = autocfg.get("target_file", "/opt/claude-tier-maximizer/rules/auto.yaml")
    changelog = autocfg.get("changelog", "/var/log/claude-tier-maximizer/auto-tune.log")
    auto_enabled = bool(autocfg.get("enabled", False))
    min_occ_auto = int(autocfg.get("min_occurrences", 10))
    min_consistency_auto = float(autocfg.get("min_consistency", 0.9))

    recs = load_records(usage_log)
    print(f"loaded {len(recs)} records from {usage_log}")
    if not recs:
        sys.exit(0)

    # Load blocklist regex patterns (so we never re-suggest things the user rejected)
    blocklist = load_yaml(blocklist_path)
    block_patterns = (blocklist.get("patterns") or [])

    # Build candidates: prompts that hit the budget cap → likely needed higher level
    cap_ratio = 0.9
    under_high_candidates = []  # tagged low/medium but ~hit cap
    over_high_examples = []     # tagged high but used very little thinking
    for r in recs:
        lvl = r.get("level")
        budget = r.get("budget_set")
        thinking = thinking_tokens(r)
        if not lvl or budget is None or thinking <= 0:
            continue
        if lvl in ("low", "medium") and thinking >= cap_ratio * budget:
            under_high_candidates.append(r)
        if lvl == "high" and thinking < 1500:
            over_high_examples.append(r)

    # Per-ngram tally with consistency check
    ngram_counts = Counter()
    ngram_levels = defaultdict(lambda: Counter())  # ngram -> Counter({observed_level: n})
    for r in under_high_candidates:
        prev = r.get("preview", "")
        for ng in ngrams(prev, n=3):
            if is_blocked(ng, block_patterns):
                continue
            ngram_counts[ng] += 1
            ngram_levels[ng][r.get("level")] += 1

    pending_high = []     # list of {pattern, ngram, occurrences, consistency}
    auto_applied_high = []
    for ng, count in ngram_counts.most_common(100):
        if count < 3:
            continue
        # 'consistency' = fraction of occurrences agreeing on the dominant misclassified level
        dom_level, dom_count = ngram_levels[ng].most_common(1)[0]
        consistency = dom_count / count if count else 0
        entry = {
            "ngram": ng,
            "pattern": ngram_to_regex(ng),
            "suggested_level": "high",
            "occurrences": count,
            "consistency": round(consistency, 3),
        }
        pending_high.append(entry)
        if auto_enabled and count >= min_occ_auto and consistency >= min_consistency_auto:
            auto_applied_high.append(entry)

    # Write pending suggestions
    pending_data = {
        "_meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "records_analyzed": len(recs),
            "under_classified_used_for_mining": len(under_high_candidates),
            "over_classified_examples": len(over_high_examples),
        },
        "suggestions": pending_high,
    }
    save_yaml(pending_path, pending_data,
              "# pending.yaml -- ctm-review consumes this and writes approvals/rejections.\n\n")

    # Auto-apply (opt-in)
    if auto_applied_high:
        auto = load_yaml(auto_target) or {}
        existing_high = set(auto.get("high") or [])
        new_high = [e["pattern"] for e in auto_applied_high if e["pattern"] not in existing_high]
        if new_high:
            auto.setdefault("high", []).extend(new_high)
            save_yaml(auto_target, {"high": auto["high"], "low": auto.get("low", [])},
                      "# auto.yaml -- auto-tuned patterns. Delete to reset.\n\n")
            Path(changelog).parent.mkdir(parents=True, exist_ok=True)
            with open(changelog, "a") as f:
                ts = datetime.now(timezone.utc).isoformat()
                for e in auto_applied_high:
                    if e["pattern"] in new_high:
                        f.write(f"{ts}\tAUTO_APPEND_HIGH\toccurrences={e['occurrences']}\tconsistency={e['consistency']}\tpattern={e['pattern']!r}\tngram={e['ngram']!r}\n")
            print(f"auto-applied {len(new_high)} high patterns to {auto_target}")
            print(f"changelog: {changelog}")
            print("RESTART required: systemctl restart claude-tier-maximizer")

    # Markdown report
    lines = [
        f"# claude-tier-maximizer tune report",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Records analyzed: **{len(recs)}**",
        f"- Under-classified prompts (mined for suggestions): **{len(under_high_candidates)}**",
        f"- Over-classified high prompts (review candidates): **{len(over_high_examples)}**",
        f"- Auto-tune enabled: **{auto_enabled}** (min_occ={min_occ_auto}, min_consistency={min_consistency_auto})",
        f"- Auto-applied this run: **{len(auto_applied_high)}**",
        "",
    ]
    if pending_high:
        lines.append("## Pending suggestions (run `ctm-review` to triage)\n")
        lines.append("| n-gram | level | occurrences | consistency |")
        lines.append("|---|---|---:|---:|")
        for e in pending_high[:30]:
            lines.append(f"| `{e['ngram']}` | {e['suggested_level']} | {e['occurrences']} | {e['consistency']} |")
        lines.append("")
    if over_high_examples:
        lines.append("## Over-classified — high patterns firing on low-thinking prompts\n")
        lines.append("Consider tightening these reason tags:\n")
        for reason, n in Counter(r.get("reason") for r in over_high_examples).most_common():
            lines.append(f"- `{reason}` — fired on {n} prompts that used <1500 thinking tokens")
        lines.append("")

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.report}")
    print(f"wrote {pending_path}")
    print(f"pending={len(pending_high)} auto_applied={len(auto_applied_high)}")


if __name__ == "__main__":
    main()
