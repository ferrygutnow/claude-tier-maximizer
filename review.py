#!/usr/bin/env python3
"""review — interactive approve/reject loop with human-readable suggestions.

Shows you:
- the PHRASE that triggers the rule (in plain English, not regex)
- real sample prompts from your usage history
- the current vs proposed thinking budget
- average thinking-tokens those prompts actually used (so you can see WHY it's
  being flagged)
- how many future prompts this likely affects

Press 'r' if you want to see the underlying regex.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import yaml


# -------- yaml helpers --------

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


def append_pattern(path, level, pattern, header_if_new):
    data = load_yaml(path) or {}
    data.setdefault(level, [])
    if pattern not in data[level]:
        data[level].append(pattern)
    save_yaml(path, data, header_if_new if not Path(path).exists() else "")


def append_blocklist(path, ngram):
    data = load_yaml(path) or {}
    patterns = data.get("patterns") or []
    if ngram not in patterns:
        patterns.append(ngram)
    save_yaml(path, {"patterns": patterns},
              "# blocklist.yaml -- rejected via ctm-review. Never suggested again.\n\n")


# -------- usage.jsonl analysis --------

def thinking_tokens(rec):
    u = rec.get("usage") or {}
    for k in ("thinking_tokens", "thinking_output_tokens", "reasoning_tokens"):
        v = u.get(k)
        if isinstance(v, int):
            return v
    return 0


def matching_records(usage_log: str, ngram: str, k_samples: int = 5):
    """Find records whose preview contains the n-gram. Return (records, all_samples).
    Stops collecting samples at k_samples but keeps counting records and stats."""
    p = Path(usage_log)
    if not p.exists():
        return [], []
    needle = ngram.lower()
    samples = []
    matches = []
    with open(p) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            prev = (rec.get("preview") or "").lower()
            if needle in prev:
                matches.append(rec)
                if len(samples) < k_samples:
                    samples.append(rec.get("preview"))
    return matches, samples


def summarize_matches(matches, proposed_level: str, budgets: dict):
    """Compute human-friendly stats for a set of matched records."""
    if not matches:
        return None
    levels = [r.get("level") for r in matches]
    most_common_level = max(set(levels), key=levels.count) if levels else "?"
    thinking_values = [thinking_tokens(r) for r in matches if thinking_tokens(r) > 0]
    avg_thinking = int(statistics.mean(thinking_values)) if thinking_values else 0
    current_budget = budgets.get(most_common_level, 0)
    proposed_budget = budgets.get(proposed_level, 0)
    capped_count = sum(
        1 for r in matches
        if thinking_tokens(r) > 0 and r.get("budget_set")
        and thinking_tokens(r) >= 0.9 * r.get("budget_set", 0)
    )
    return {
        "count": len(matches),
        "current_level": most_common_level,
        "current_budget": current_budget,
        "proposed_budget": proposed_budget,
        "avg_thinking_used": avg_thinking,
        "capped_count": capped_count,
    }


# -------- display --------

def show_suggestion(idx, total, suggestion, summary, samples):
    ng = suggestion.get("ngram", "")
    level = suggestion.get("suggested_level", "high")
    occ = suggestion.get("occurrences", 0)
    cons = suggestion.get("consistency", 0)

    print(f"\n=== Suggestion {idx}/{total} ===")
    print(f"  When you write     : \"{ng}\" (as separate words anywhere in a prompt)")
    print(f"  Proposed routing   : -> {level.upper()}")
    if summary:
        print(f"  Matches in history : {summary['count']} prompts")
        print(f"  Currently classified as: {summary['current_level']} "
              f"(budget {summary['current_budget']} tokens)")
        print(f"  If approved        : {level} (budget {summary['proposed_budget']} tokens)")
        if summary["avg_thinking_used"] > 0:
            print(f"  Avg thinking used  : {summary['avg_thinking_used']} tokens")
        if summary["capped_count"] > 0:
            print(f"  Hit budget cap     : {summary['capped_count']}/{summary['count']} times"
                  f" (this is the signal it needs more budget)")
    else:
        print(f"  Matches in history : (no usage data yet; based on {occ} historical occurrences,"
              f" consistency {cons})")

    if samples:
        print(f"\n  Examples from your prompts:")
        for i, s in enumerate(samples, 1):
            text = s.strip().replace("\n", " ")
            print(f"    {i}. {text[:150]}")
    print()


def help_keys():
    print("  Decide:")
    print("    y - approve  (route matching prompts to the proposed level)")
    print("    n - reject   (never suggest this phrase again)")
    print("    s - skip     (decide next time)")
    print("    r - show the underlying regex pattern")
    print("    e - edit the phrase before approving")
    print("    q - quit     (saves remaining as still-pending)")


# -------- modes --------

def interactive_review(cfg):
    rules_cfg = cfg.get("rules") or {}
    auto_cfg = cfg.get("auto_tune") or {}
    pending_path = auto_cfg.get("pending_file", "/opt/claude-tier-maximizer/rules/pending.yaml")
    personal_path = rules_cfg.get("personal", "/opt/claude-tier-maximizer/rules/personal.yaml")
    blocklist_path = auto_cfg.get("blocklist_file", "/opt/claude-tier-maximizer/rules/blocklist.yaml")
    usage_log = (cfg.get("logging") or {}).get("usage", "/var/log/claude-tier-maximizer/usage.jsonl")
    budgets = cfg.get("budgets") or {"low": 1024, "medium": 4000, "high": 16000}

    pending = load_yaml(pending_path)
    suggestions = pending.get("suggestions") or []
    if not suggestions:
        print(f"No pending suggestions in {pending_path}")
        print("(run `ctm-tune` first to generate them)")
        return

    print(f"\nReviewing {len(suggestions)} suggestion(s).")
    help_keys()

    approved = rejected = skipped = edited = 0
    remaining = []

    for i, s in enumerate(suggestions, 1):
        ng = s.get("ngram", "")
        matches, samples = matching_records(usage_log, ng, k_samples=5)
        summary = summarize_matches(matches, s.get("suggested_level", "high"), budgets)
        show_suggestion(i, len(suggestions), s, summary, samples)

        while True:
            choice = input("  > ").strip().lower()
            if choice in ("y", "yes"):
                append_pattern(personal_path, s.get("suggested_level", "high"),
                               s.get("pattern"),
                               "# personal.yaml -- approved via ctm-review.\n\n")
                approved += 1
                print(f"  approved. added to {personal_path}")
                break
            if choice in ("n", "no"):
                append_blocklist(blocklist_path, ng)
                rejected += 1
                print(f"  rejected. added to {blocklist_path}")
                break
            if choice in ("s", "skip", ""):
                remaining.append(s)
                skipped += 1
                print("  skipped.")
                break
            if choice == "r":
                print(f"  regex: {s.get('pattern')}")
                continue
            if choice == "e":
                new_phrase = input(f"  new phrase (current: '{ng}'): ").strip()
                if new_phrase:
                    import re as _re
                    words = new_phrase.split()
                    new_regex = r"\b" + r"\s+".join(_re.escape(w) for w in words) + r"\b"
                    s["ngram"] = new_phrase
                    s["pattern"] = new_regex
                    edited += 1
                    # re-fetch matches with new phrase
                    matches, samples = matching_records(usage_log, new_phrase, k_samples=5)
                    summary = summarize_matches(matches, s.get("suggested_level", "high"), budgets)
                    show_suggestion(i, len(suggestions), s, summary, samples)
                    continue
            if choice in ("q", "quit"):
                remaining.extend(suggestions[suggestions.index(s) + 1:])
                pending["suggestions"] = remaining
                save_yaml(pending_path, pending,
                          "# pending.yaml -- ctm-review unfinished.\n\n")
                print(f"\nstopped. {len(remaining)} unreviewed remain.")
                print(f"approved={approved} rejected={rejected} skipped={skipped} edited={edited}")
                return
            if choice in ("h", "?"):
                help_keys(); continue
            print("  ? type y, n, s, r, e, or q  (h for help)")

    pending["suggestions"] = remaining
    save_yaml(pending_path, pending,
              "# pending.yaml -- ctm-review processed batch.\n\n")
    print(f"\nDone. approved={approved} rejected={rejected} skipped={skipped} edited={edited}")
    if approved or edited:
        print("\nRestart proxy to load new patterns:")
        print("  systemctl restart claude-tier-maximizer")


def to_markdown(cfg, out_path):
    """Editable markdown — also human-readable, no raw regex."""
    auto_cfg = cfg.get("auto_tune") or {}
    pending_path = auto_cfg.get("pending_file", "/opt/claude-tier-maximizer/rules/pending.yaml")
    usage_log = (cfg.get("logging") or {}).get("usage", "/var/log/claude-tier-maximizer/usage.jsonl")
    budgets = cfg.get("budgets") or {"low": 1024, "medium": 4000, "high": 16000}
    pending = load_yaml(pending_path)
    suggestions = pending.get("suggestions") or []
    lines = [
        "# claude-tier-maximizer review queue",
        "",
        "For each suggestion below, EITHER tick `[x] approve` OR `[x] reject` (not both).",
        "Leave both unticked to skip. Then save and run `ctm-review --apply <this file>`.",
        "",
    ]
    for s in suggestions:
        ng = s.get("ngram", "")
        level = s.get("suggested_level", "high")
        matches, samples = matching_records(usage_log, ng, k_samples=5)
        summary = summarize_matches(matches, level, budgets)
        lines.append(f"## When prompt contains \"{ng}\"")
        lines.append(f"- Proposed routing: **{level.upper()}**")
        if summary:
            lines.append(f"- Matches in your history: **{summary['count']} prompts**")
            lines.append(f"- Currently classified as: {summary['current_level']} "
                         f"(budget {summary['current_budget']} tokens)")
            lines.append(f"- If approved: {level} (budget {summary['proposed_budget']} tokens)")
            if summary["avg_thinking_used"] > 0:
                lines.append(f"- Average thinking used: {summary['avg_thinking_used']} tokens")
            if summary["capped_count"] > 0:
                lines.append(f"- Hit budget cap: {summary['capped_count']}/{summary['count']} times")
        if samples:
            lines.append("- Examples:")
            for sm in samples:
                lines.append(f"  - {sm.strip()[:150]}")
        lines.append("- Decision: `[ ] approve` `[ ] reject`")
        lines.append("")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out_path}  ({len(suggestions)} suggestions)")


def apply_markdown(cfg, md_path):
    auto_cfg = cfg.get("auto_tune") or {}
    rules_cfg = cfg.get("rules") or {}
    pending_path = auto_cfg.get("pending_file", "/opt/claude-tier-maximizer/rules/pending.yaml")
    personal_path = rules_cfg.get("personal", "/opt/claude-tier-maximizer/rules/personal.yaml")
    blocklist_path = auto_cfg.get("blocklist_file", "/opt/claude-tier-maximizer/rules/blocklist.yaml")

    pending = load_yaml(pending_path)
    suggestions = pending.get("suggestions") or []
    if not suggestions:
        print(f"no pending suggestions in {pending_path}")
        return
    with open(md_path) as f:
        md = f.read()
    sections = md.split("## When prompt contains \"")
    approved = rejected = 0
    remaining = []
    for s in suggestions:
        ng = s.get("ngram", "")
        matched = None
        for sec in sections[1:]:
            if sec.startswith(f"{ng}\""):
                matched = sec; break
        if not matched:
            remaining.append(s); continue
        body = matched.lower()
        if "[x] approve" in body:
            append_pattern(personal_path, s.get("suggested_level", "high"),
                           s.get("pattern"),
                           "# personal.yaml -- approved via ctm-review.\n\n")
            approved += 1
        elif "[x] reject" in body:
            append_blocklist(blocklist_path, ng); rejected += 1
        else:
            remaining.append(s)
    pending["suggestions"] = remaining
    save_yaml(pending_path, pending,
              "# pending.yaml -- after --apply.\n\n")
    print(f"applied: approved={approved} rejected={rejected} remaining={len(remaining)}")
    if approved:
        print("\nRestart proxy: systemctl restart claude-tier-maximizer")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/claude-tier-maximizer/config.yaml")
    ap.add_argument("--markdown", metavar="FILE")
    ap.add_argument("--apply", metavar="FILE")
    args = ap.parse_args()
    cfg = load_yaml(args.config)
    if args.apply:
        apply_markdown(cfg, args.apply)
    elif args.markdown:
        to_markdown(cfg, args.markdown)
    else:
        interactive_review(cfg)


if __name__ == "__main__":
    main()
