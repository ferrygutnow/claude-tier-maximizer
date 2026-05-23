"""Classifier — rule-driven prompt classification.

Loads regex patterns from up to 3 layered YAML files (default + auto + personal),
each appended. Returns (level, reason) so the log shows which rule fired.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("ctm.classifier")


@dataclass
class CompiledRules:
    strip_patterns: list[re.Pattern] = field(default_factory=list)
    force_low: list[re.Pattern] = field(default_factory=list)
    explicit_high: list[re.Pattern] = field(default_factory=list)
    explicit_low: list[re.Pattern] = field(default_factory=list)
    high: list[re.Pattern] = field(default_factory=list)
    low: list[re.Pattern] = field(default_factory=list)


def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.IGNORECASE | re.DOTALL))
        except re.error as e:
            log.warning("invalid regex %r: %s", p, e)
    return out


def load_rules(default_path: str, auto_path: Optional[str] = None,
               personal_path: Optional[str] = None) -> CompiledRules:
    """Load default rules + optional auto + personal overlays (all appended)."""
    rules = CompiledRules()
    paths = [default_path]
    if auto_path and Path(auto_path).exists():
        paths.append(auto_path)
    if personal_path and Path(personal_path).exists():
        paths.append(personal_path)
    for p in paths:
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            log.warning("rules file not found: %s", p)
            continue
        rules.strip_patterns.extend(_compile((data.get("clean") or {}).get("strip_patterns")))
        rules.force_low.extend(_compile((data.get("force_low") or {}).get("patterns")))
        rules.explicit_high.extend(_compile((data.get("explicit") or {}).get("high")))
        rules.explicit_low.extend(_compile((data.get("explicit") or {}).get("low")))
        rules.high.extend(_compile(data.get("high")))
        rules.low.extend(_compile(data.get("low")))
    log.info(
        "loaded rules from %d files: high=%d low=%d explicit_high=%d explicit_low=%d strip=%d force_low=%d",
        len(paths),
        len(rules.high), len(rules.low),
        len(rules.explicit_high), len(rules.explicit_low),
        len(rules.strip_patterns), len(rules.force_low),
    )
    return rules


def clean_text(text: str, rules: CompiledRules) -> str:
    cleaned = text
    for p in rules.strip_patterns:
        cleaned = p.sub("", cleaned)
    return cleaned.strip()


def classify(text: str, rules: CompiledRules, llm_fallback=None, context: str = "") -> tuple[str, str]:
    """Return (level, reason). Level is one of: low, medium, high."""
    if not text:
        return ("medium", "empty_text_default")
    for pat in rules.explicit_high:
        if pat.search(text):
            return ("high", "explicit_high")
    for pat in rules.explicit_low:
        if pat.search(text):
            return ("low", "explicit_low")
    for pat in rules.force_low:
        if pat.search(text):
            if context and llm_fallback is not None and llm_fallback.enabled:
                # Force_low matched, but context exists — ask Qwen to verify
                try:
                    level = llm_fallback.classify(text, context)
                    if level in ("low", "medium", "high") and level != "low":
                        return (level, "llm_override_force_low")
                except Exception:
                    pass
            return ("low", "force_low")
    for pat in rules.high:
        if pat.search(text):
            return ("high", "regex_high")
    for pat in rules.low:
        if pat.search(text):
            return ("low", "regex_low")
    if llm_fallback is not None and llm_fallback.enabled:
        try:
            level = llm_fallback.classify(text, context)
            if level in ("low", "medium", "high"):
                return (level, "llm")
            log.warning("llm fallback returned invalid level: %r", level)
        except Exception as e:
            log.warning("llm fallback error: %s", e)
    return ("medium", "default")
