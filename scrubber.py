#!/usr/bin/env python3
"""scrubber — watch Claude Code session logs and redact secrets in-place.

Monitors ~/.claude/projects/**/*.jsonl for API keys, passwords, tokens, and
other sensitive patterns, replacing them with [REDACTED] as new lines are
appended.

When a GPU is available, also runs a semantic scan via local LLM to catch
secrets that don't match regex patterns (e.g. unusual variable names,
encoded credentials, custom tokens).

Usage:
  python3 scrubber.py [--watch-dir DIR] [--dry-run]
  python3 scrubber.py --scrub-file FILE   # one-shot scrub

Run as a service: see systemd/claude-scrubber.service
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("ctm.scrubber")

# ── GPU gate ──────────────────────────────────────────────────────────────────

def _gpu_available() -> bool:
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=3)
            return r.returncode == 0
        except Exception:
            pass
    return False

SEMANTIC_PROMPT = """You are a secret-finder. Scan the text below for ANY string that looks like a credential, token, API key, password, or private key — even if it has an unusual name or encoding.

Rules:
- DO flag: things assigned to variables named key, secret, token, pass, credential, auth, api, pwd, cipher, salt
- DO flag: encoded/base64 strings that decode to credentials
- DO flag: any 20+ character alphanumeric string assigned to a security-related variable
- DO flag: connection strings or URLs containing credentials
- DO NOT flag: example/placeholder values like "your-key-here", "xxxx", "YOUR_API_KEY"
- DO NOT flag: environment variable references like process.env.X or $VAR
- DO NOT flag: documentation or comments about where to put keys

Return a JSON array of objects with "value" (the secret string) and "context" (10 chars before and after). Return [] if nothing suspicious.

Text:
{text}"""

# ── Patterns ─────────────────────────────────────────────────────────────────

DEFAULT_WATCH_DIRS = [
    str(Path.home() / ".claude" / "projects"),      # Claude Code
    str(Path.home() / ".codex" / "projects"),        # OpenAI Codex CLI
    str(Path.home() / ".opencode" / "projects"),     # OpenCode
    str(Path.home() / ".gemini" / "projects"),       # Google Gemini CLI
    str(Path.home() / ".copilot" / "projects"),      # GitHub Copilot CLI
]

KNOWN_AGENT_DIRS = {
    "claude":   str(Path.home() / ".claude" / "projects"),
    "codex":    str(Path.home() / ".codex" / "projects"),
    "opencode": str(Path.home() / ".opencode" / "projects"),
    "gemini":   str(Path.home() / ".gemini" / "projects"),
    "copilot":  str(Path.home() / ".copilot" / "projects"),
}

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key",   re.compile(r'sk-ant-[A-Za-z0-9\-_]{20,}', re.ASCII)),
    ("openai_key",      re.compile(r'sk-[A-Za-z0-9]{32,}', re.ASCII)),
    ("aws_access_key",  re.compile(r'AKIA[0-9A-Z]{16}', re.ASCII)),
    ("aws_secret",      re.compile(r'(?i)aws.{0,20}secret.{0,5}["\']?\s*[:=]\s*["\']?([A-Za-z0-9/+]{40})')),
    ("github_token",    re.compile(r'gh[pousr]_[A-Za-z0-9]{36,}', re.ASCII)),
    ("generic_bearer",  re.compile(r'(?i)bearer\s+([A-Za-z0-9\-_.~+/]{20,})')),
    ("private_key",     re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ("password_assign", re.compile(r'(?i)(password|passwd|pwd)\s*[:=]\s*["\']?([^\s"\']{6,})')),
    ("api_key_assign",  re.compile(r'(?i)(api[_-]?key|apikey|api[_-]?secret)\s*[:=]\s*["\']?([^\s"\']{8,})')),
    ("db_url",          re.compile(r'(?i)(postgres|mysql|mongodb|redis)://[^@\s]+:[^@\s]+@')),
    ("hex_secret",      re.compile(r'(?i)(secret|token|key)\s*[:=]\s*["\']?([0-9a-f]{32,})["\']?')),
]


def _ollama_semantic_scan(text: str, model: str, ollama_url: str, timeout: float) -> list[str]:
    """Use local LLM to find secrets that regex patterns miss."""
    # Only scan content that looks code-ish or config-ish — skip long prose
    if len(text) > 5000:
        lines = text.split("\n")
        candidates = []
        for i, line in enumerate(lines):
            if re.search(r'(?i)(key|secret|token|pass|credential|auth|api|pwd|cipher|salt)\s*[:=]', line):
                block = "\n".join(lines[max(0,i-2):i+3])
                candidates.append(block)
        if not candidates:
            return []
        scan_text = "\n---\n".join(candidates)
    else:
        scan_text = text

    body = {
        "model": model,
        "prompt": SEMANTIC_PROMPT.format(text=scan_text[:3000]),
        "stream": False,
        "options": {"num_predict": 200, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{ollama_url.rstrip('/')}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        result = data.get("response", "")
        found = json.loads(result)
        if isinstance(found, list):
            secrets = [item.get("value", "") for item in found if isinstance(item, dict) and item.get("value")]
            return secrets
    except Exception:
        pass
    return []


def scrub_text(text: str, semantic_cfg: dict | None = None) -> tuple[str, int]:
    """Replace secrets in text. Returns (scrubbed_text, count_replaced)."""
    count = 0
    for name, pattern in PATTERNS:
        def replacer(m: re.Match) -> str:
            nonlocal count
            full = m.group(0)
            groups = m.groups()
            if groups:
                secret = groups[-1]
                if secret and len(secret) >= 4:
                    count += 1
                    return full.replace(secret, "[REDACTED]", 1)
            count += 1
            return "[REDACTED]"
        text = pattern.sub(replacer, text)
    # ── Semantic pass (GPU-gated) ─────────────────────────────────────────────
    if semantic_cfg and _gpu_available():
        try:
            semantic_secrets = _ollama_semantic_scan(
                text,
                model=semantic_cfg.get("model", "qwen2.5:3b"),
                ollama_url=semantic_cfg.get("ollama_url", "http://localhost:11434"),
                timeout=semantic_cfg.get("timeout_ms", 5000) / 1000.0,
            )
            for secret in semantic_secrets:
                if len(secret) >= 4 and secret not in ("[REDACTED]",):
                    text = text.replace(secret, "[REDACTED]", 1)
                    count += 1
                    log.info("semantic redact: %r", secret[:30])
        except Exception as e:
            log.warning("semantic scan error: %s", e)
    return text, count


def scrub_jsonl_line(line: str, semantic_cfg: dict | None = None) -> tuple[str, int]:
    """Scrub a JSONL line — scrubs the raw string to avoid JSON parse overhead."""
    scrubbed, n = scrub_text(line, semantic_cfg=semantic_cfg)
    return scrubbed, n


def scrub_file(path: Path, dry_run: bool = False, semantic_cfg: dict | None = None) -> int:
    """Scrub an entire file in-place. Returns total replacements made."""
    try:
        original = path.read_text(errors="replace")
    except Exception as e:
        log.warning("cannot read %s: %s", path, e)
        return 0
    scrubbed, total = scrub_text(original, semantic_cfg=semantic_cfg)
    if total == 0:
        return 0
    if dry_run:
        log.info("[dry-run] %s: would redact %d secret(s)", path, total)
        return total
    path.write_text(scrubbed)
    log.info("scrubbed %s: redacted %d secret(s)", path, total)
    return total


class TailWatcher:
    """Watch a file for new lines and scrub them in-place."""

    def __init__(self, path: Path, semantic_cfg: dict | None = None):
        self.path = path
        self.semantic_cfg = semantic_cfg
        self.offset = path.stat().st_size if path.exists() else 0

    def check(self, dry_run: bool = False) -> int:
        if not self.path.exists():
            return 0
        size = self.path.stat().st_size
        if size <= self.offset:
            if size < self.offset:
                self.offset = 0
            return 0
        try:
            with open(self.path, "r+", errors="replace") as f:
                f.seek(self.offset)
                new_content = f.read()
                scrubbed, count = scrub_text(new_content, semantic_cfg=self.semantic_cfg)
                if count > 0 and not dry_run:
                    f.seek(self.offset)
                    f.write(scrubbed)
                    f.flush()
                    log.info("live-scrubbed %s: %d secret(s) redacted in new content", self.path, count)
        except Exception as e:
            log.warning("watcher error on %s: %s", self.path, e)
            count = 0
        self.offset = self.path.stat().st_size
        return count


class Watcher:
    def __init__(self, watch_dirs: list[Path], dry_run: bool = False, semantic_cfg: dict | None = None):
        self.watch_dirs = watch_dirs
        self.dry_run = dry_run
        self.semantic_cfg = semantic_cfg
        self._watchers: dict[Path, TailWatcher] = {}

    def _discover(self) -> list[Path]:
        files = []
        for d in self.watch_dirs:
            if d.exists():
                files.extend(d.rglob("*.jsonl"))
        return files

    def run_once(self) -> int:
        total = 0
        for path in self._discover():
            if path not in self._watchers:
                scrub_file(path, self.dry_run, semantic_cfg=self.semantic_cfg)
                self._watchers[path] = TailWatcher(path, semantic_cfg=self.semantic_cfg)
            else:
                total += self._watchers[path].check(self.dry_run)
        return total

    def run_forever(self, interval: float = 5.0):
        dirs_str = ", ".join(str(d) for d in self.watch_dirs if d.exists())
        log.info("scrubber watching %s (interval=%.1fs dry_run=%s gpu=%s)",
                 dirs_str, interval, self.dry_run, _gpu_available())
        while True:
            try:
                self.run_once()
            except Exception as e:
                log.warning("watcher loop error: %s", e)
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Claude Code log scrubber")
    parser.add_argument("--watch-dir", action="append", default=None,
                        help="Directory to watch (can repeat for multiple agents, default: all known)")
    parser.add_argument("--agents", nargs="*", default=None,
                        choices=list(KNOWN_AGENT_DIRS.keys()),
                        help="Which agents to watch (e.g. --agents claude codex)")
    parser.add_argument("--scrub-file", help="One-shot: scrub a single file and exit")
    parser.add_argument("--dry-run", action="store_true", help="Report but don't modify files")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    parser.add_argument("--log-file", default="/var/log/claude-tier-maximizer/scrubber.log")
    parser.add_argument("--ollama-url", default="http://localhost:11434",
                        help="Ollama URL for semantic scan")
    parser.add_argument("--semantic-model", default="qwen2.5:3b",
                        help="Local model for semantic secret detection")
    parser.add_argument("--semantic-timeout", type=int, default=5000,
                        help="Timeout ms for semantic scan")
    args = parser.parse_args()

    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    semantic_cfg = {
        "model": args.semantic_model,
        "ollama_url": args.ollama_url,
        "timeout_ms": args.semantic_timeout,
    } if _gpu_available() else None

    if semantic_cfg:
        log.info("GPU detected — semantic secret detection enabled (model=%s)", args.semantic_model)
    else:
        log.info("No GPU — regex-only secret detection")

    # ── Resolve watch directories ──────────────────────────────────────────
    if args.watch_dir:
        watch_dirs = [Path(d) for d in args.watch_dir]
    elif args.agents:
        watch_dirs = [Path(KNOWN_AGENT_DIRS[a]) for a in args.agents]
    else:
        watch_dirs = [Path(d) for d in DEFAULT_WATCH_DIRS]

    for d in watch_dirs:
        d.mkdir(parents=True, exist_ok=True)
    watch_dirs = [d for d in watch_dirs if d.exists()]

    if args.scrub_file:
        n = scrub_file(Path(args.scrub_file), args.dry_run, semantic_cfg=semantic_cfg)
        print(f"Redacted {n} secret(s) in {args.scrub_file}")
        return

    watcher = Watcher(watch_dirs, args.dry_run, semantic_cfg=semantic_cfg)
    watcher.run_forever(args.interval)


if __name__ == "__main__":
    main()
