#!/usr/bin/env python3
"""personalize — generate a personal.yaml overlay from your own session history.

Walks ~/.claude/projects/**/*.jsonl, extracts user prompts, applies the current
classifier to each, surfaces prompts that landed in the "medium" default bucket
(no rule fired), groups them by leading n-gram, and writes the top patterns to
rules/personal.yaml for you to review/edit.

Run:
  python3 personalize.py [--out PATH] [--top-ngrams N] [--label-interactive]

Without --label-interactive it produces a draft you can edit by hand.
With it, you triage each top phrase as low/medium/high in the terminal.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from classifier import classify, clean_text, load_rules


DEFAULT_RULES = "/opt/claude-tier-maximizer/rules/default.yaml"
DEFAULT_OUT = "/opt/claude-tier-maximizer/rules/personal.yaml"
DEFAULT_SESSIONS = str(Path.home() / ".claude" / "projects")


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts)
    return ""


def first_ngram(text: str, n: int = 4) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", text.lower())
    return " ".join(words[:n])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=DEFAULT_SESSIONS, help="Path to ~/.claude/projects")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--top-ngrams", type=int, default=30)
    ap.add_argument("--label-interactive", action="store_true")
    args = ap.parse_args()

    rules = load_rules(args.rules, None)
    files = glob.glob(f"{args.sessions}/**/*.jsonl", recursive=True)
    print(f"scanning {len(files)} session files...", file=sys.stderr)

    ngram_to_prompts = {}
    total_user = 0
    ambiguous = 0
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") != "user":
                        continue
                    msg = rec.get("message", {})
                    content = msg.get("content")
                    if isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
                    ):
                        continue
                    raw = extract_text(content)
                    cleaned = clean_text(raw, rules)
                    if not cleaned:
                        continue
                    total_user += 1
                    level, reason = classify(cleaned, rules, None)
                    if reason == "default":  # current rules don't catch this prompt
                        ambiguous += 1
                        ng = first_ngram(cleaned, 4)
                        if ng:
                            ngram_to_prompts.setdefault(ng, []).append(cleaned[:140])
        except Exception as e:
            print(f"err {path}: {e}", file=sys.stderr)

    counter = Counter({ng: len(p) for ng, p in ngram_to_prompts.items()})
    top = counter.most_common(args.top_ngrams)

    print(f"\nTotal user prompts: {total_user}")
    print(f"Ambiguous (no rule fired, defaulted to medium): {ambiguous}")
    print(f"Top {args.top_ngrams} starting phrases in the ambiguous bucket:\n")

    decisions = []  # list of (level, pattern, sample_count, samples)
    for ng, count in top:
        samples = ngram_to_prompts[ng][:3]
        print(f"[{count:>4}x]  '{ng}'")
        for s in samples:
            print(f"           > {s}")
        if args.label_interactive:
            while True:
                resp = input("  label (l=low, m=medium, h=high, s=skip): ").strip().lower()
                if resp in ("l", "low"):
                    decisions.append(("low", ng, count, samples)); break
                if resp in ("h", "high"):
                    decisions.append(("high", ng, count, samples)); break
                if resp in ("m", "medium", "s", "skip"):
                    break
        print()

    out = {"high": [], "low": []}
    for level, ng, _count, _samples in decisions:
        # Build a regex from the n-gram (word-boundaries, escaped)
        words = ng.split()
        pat = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
        if level == "high":
            out["high"].append(pat)
        elif level == "low":
            out["low"].append(pat)

    if decisions:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            f.write("# personal.yaml — generated by personalize.py.\n")
            f.write("# Patterns are APPENDED to default.yaml — review and trim before going live.\n\n")
            yaml.safe_dump(out, f, sort_keys=False)
        print(f"\nWrote {len(decisions)} patterns to {args.out}")
        print(f"Restart the proxy: systemctl restart claude-tier-maximizer")
    else:
        print("\nNo decisions made — re-run with --label-interactive to triage.")


if __name__ == "__main__":
    main()
