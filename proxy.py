#!/usr/bin/env python3
"""claude-tier-maximizer — HTTP proxy between coding agents and their APIs.

Supports:
  - Anthropic (Claude Code):  /v1/messages        → thinking.budget_tokens
  - OpenAI (Codex CLI):       /v1/chat/completions → reasoning_effort
  - Google (Gemini CLI):      /v1/models/...:generateContent → thinkingConfig
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from classifier import classify, clean_text, load_rules
from compactor import Compactor
from injection_detector import InjectionDetector
from llm_fallback import LLMFallback


# ── Provider detection ────────────────────────────────────────────────────────

PROVIDER_PATHS = [
    ("anthropic", "/v1/messages"),
    ("openai",    "/v1/chat/completions"),
    ("google",    "/v1beta/models/"),
    ("google",    "/v1/models/"),
]

def detect_provider(path: str) -> str | None:
    for provider, prefix in PROVIDER_PATHS:
        if prefix in path:
            return provider
    return None


# ── Format-specific extractors ────────────────────────────────────────────────

def _extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts)
    return ""


def extract_last_user_text(body: dict, provider: str) -> str:
    if provider in ("anthropic", "openai"):
        for msg in reversed(body.get("messages", [])):
            if msg.get("role") != "user":
                continue
            return _extract_text_from_content(msg.get("content", ""))
    elif provider == "google":
        for part in reversed(body.get("contents", [])):
            if part.get("role") != "user":
                continue
            parts = part.get("parts", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            if texts:
                return " ".join(texts)
    return ""


def extract_context(body: dict, provider: str) -> str:
    """Extract the last assistant response for context (truncated 500)."""
    if provider in ("anthropic", "openai"):
        for msg in reversed(body.get("messages", [])):
            if msg.get("role") != "assistant":
                continue
            return _extract_text_from_content(msg.get("content", ""))[:500]
    elif provider == "google":
        for part in reversed(body.get("contents", [])):
            if part.get("role") != "model":
                continue
            parts = part.get("parts", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            if texts:
                return " ".join(texts)[:500]
    return ""


# ── Format-specific budget application ────────────────────────────────────────

BUDGET_LEVELS = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
}

REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high"}
GEMINI_THINKING_BUDGET = {"low": 1024, "medium": 4000, "high": 16000}


def apply_budget(body: dict, level: str, budgets: dict, provider: str) -> None:
    tokens = int(budgets.get(level, budgets.get("medium", 4000)))
    if provider == "anthropic":
        body["thinking"] = {"type": "enabled", "budget_tokens": tokens}
    elif provider == "openai":
        if level in REASONING_EFFORT:
            body["reasoning_effort"] = REASONING_EFFORT[level]
    elif provider == "google":
        body["thinkingConfig"] = {"thinkingBudget": GEMINI_THINKING_BUDGET.get(level, 4000)}


# ── Logging & config ──────────────────────────────────────────────────────────

def setup_logging(cfg: dict) -> None:
    lvl = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    decisions_path = cfg.get("decisions", "/var/log/claude-tier-maximizer/decisions.log")
    Path(decisions_path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=decisions_path,
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class State:
    config: dict
    rules: object
    fallback: LLMFallback
    compactor: Compactor
    detector: InjectionDetector
    upstreams: dict[str, str]
    budgets: dict
    usage_log_path: str


def write_usage(record: dict) -> None:
    if not State.usage_log_path:
        return
    try:
        Path(State.usage_log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(State.usage_log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.warning("usage log write failed: %s", e)


def _merge_usage(accumulated: dict, u: dict) -> None:
    for k, v in u.items():
        if isinstance(v, (int, float)):
            accumulated[k] = accumulated.get(k, 0) + v
        else:
            accumulated[k] = v


def _parse_sse_line(line: bytes, accumulated: dict) -> None:
    line = line.strip()
    if not line.startswith(b"data:"):
        return
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return
    try:
        data = json.loads(payload)
    except Exception:
        return
    t = data.get("type")
    if t == "message_start":
        u = (data.get("message") or {}).get("usage") or {}
        _merge_usage(accumulated, u)
    elif t == "message_delta":
        u = data.get("usage") or {}
        _merge_usage(accumulated, u)


def parse_sse_for_usage(buffer: bytes, accumulated: dict, flush: bool = False) -> bytes:
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        _parse_sse_line(line, accumulated)
    if flush and buffer:
        _parse_sse_line(buffer, accumulated)
        buffer = b""
    return buffer


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _forward(self, method: str, body_bytes: bytes,
                 provider: str = "anthropic",
                 classification_meta: dict | None = None) -> None:
        upstream = State.upstreams.get(provider, State.upstreams.get("anthropic", ""))
        url = upstream + self.path
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "accept-encoding")
        }
        headers["Accept-Encoding"] = "identity"
        req = urllib.request.Request(url, data=body_bytes or None, headers=headers, method=method)
        ctx = ssl.create_default_context()
        try:
            t0 = time.time()
            resp = None
            for attempt in range(4):
                try:
                    resp = urllib.request.urlopen(req, context=ctx, timeout=300)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (429, 529) and attempt < 3:
                        wait = 5 * (2 ** attempt)
                        logging.warning("HTTP %d from %s, retry %d/3 in %ds",
                                        e.code, provider, attempt + 1, wait)
                        time.sleep(wait)
                    else:
                        raise
            if resp is None:
                raise RuntimeError("no response after retries")
            self.send_response(resp.status)
            content_type = ""
            for k, v in resp.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                if k.lower() == "content-type":
                    content_type = v
                self.send_header(k, v)
            self.end_headers()

            is_sse = "event-stream" in content_type.lower()
            usage_acc: dict = {}
            full_body = b""
            sse_buf = b""

            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if classification_meta is not None:
                    if is_sse:
                        sse_buf = parse_sse_for_usage(sse_buf + chunk, usage_acc)
                    else:
                        full_body += chunk

            if classification_meta is not None:
                if is_sse and sse_buf:
                    parse_sse_for_usage(sse_buf, usage_acc, flush=True)
                if not is_sse and full_body:
                    try:
                        data = json.loads(full_body)
                        u = data.get("usage") or {}
                        if u:
                            usage_acc.update(u)
                    except Exception:
                        pass
                rec = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "provider": provider,
                    "level": classification_meta.get("level"),
                    "reason": classification_meta.get("reason"),
                    "preview": classification_meta.get("preview"),
                    "model": classification_meta.get("model"),
                    "stream": classification_meta.get("stream"),
                    "budget_set": classification_meta.get("budget_set"),
                    "elapsed_s": round(time.time() - t0, 3),
                    "usage": usage_acc,
                }
                write_usage(rec)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            logging.exception("upstream error [%s]: %s", provider, e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self._forward("GET", b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(n) if n else b""
        provider = detect_provider(self.path)

        if not provider:
            return self._forward("POST", body_bytes)

        new_body = body_bytes
        meta = None
        try:
            body = json.loads(body_bytes) if body_bytes else {}
            raw = extract_last_user_text(body, provider)
            cleaned = clean_text(raw, State.rules)
            if not cleaned:
                logging.info(
                    "provider=%s level=PASSTHROUGH reason=empty_after_clean raw_len=%d model=%s stream=%s",
                    provider, len(raw), body.get("model") or body.get("systemInstruction", {}).get("model"),
                    body.get("stream", False),
                )
                meta = {
                    "provider": provider,
                    "level": "PASSTHROUGH", "reason": "empty_after_clean",
                    "preview": "", "model": body.get("model"),
                    "stream": body.get("stream", False), "budget_set": None,
                }
            else:
                ctx = extract_context(body, provider)
                level, reason = classify(cleaned, State.rules, State.fallback, context=ctx)
                apply_budget(body, level, State.budgets, provider)

                # Injection detector & compactor work on Anthropic/OpenAI message arrays
                msgs = body.get("messages") if provider in ("anthropic", "openai") else []
                if msgs:
                    msgs, inject_stats = State.detector.process_messages(msgs)
                    if inject_stats.get("sanitized"):
                        logging.warning("injection_detector [%s]: %s", provider, inject_stats)
                    msgs, compact_stats = State.compactor.process(msgs)
                    if compact_stats.get("compacted") or compact_stats.get("images_scaled"):
                        logging.info("compactor [%s]: %s", provider, compact_stats)
                    if provider in ("anthropic", "openai"):
                        body["messages"] = msgs

                new_body = json.dumps(body).encode()
                logging.info(
                    "provider=%s level=%s reason=%s preview=%r model=%s stream=%s",
                    provider, level, reason, cleaned[:120].replace("\n", " "),
                    body.get("model") or "google", body.get("stream", False),
                )
                meta = {
                    "provider": provider,
                    "level": level, "reason": reason,
                    "preview": cleaned[:200].replace("\n", " "),
                    "model": body.get("model") or "google",
                    "stream": body.get("stream", False),
                    "budget_set": State.budgets.get(level),
                }
        except Exception as e:
            logging.exception("classify error [%s], passing through: %s", provider, e)
            new_body = body_bytes

        self._forward("POST", new_body, provider=provider, classification_meta=meta)


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/claude-tier-maximizer/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.get("logging") or {})

    State.config = cfg
    raw_upstreams = cfg.get("upstream") or {}
    if isinstance(raw_upstreams, str):
        raw_upstreams = {"anthropic": raw_upstreams}
    State.upstreams = {
        "anthropic": raw_upstreams.get("anthropic", "https://api.anthropic.com"),
        "openai": raw_upstreams.get("openai", "https://api.openai.com"),
        "google": raw_upstreams.get("google", "https://generativelanguage.googleapis.com"),
    }
    State.budgets = cfg.get("budgets") or {"low": 1024, "medium": 4000, "high": 16000}
    State.usage_log_path = (cfg.get("logging") or {}).get("usage", "/var/log/claude-tier-maximizer/usage.jsonl")
    rules_cfg = cfg.get("rules") or {}
    State.rules = load_rules(
        rules_cfg.get("default", "/opt/claude-tier-maximizer/rules/default.yaml"),
        rules_cfg.get("auto"),
        rules_cfg.get("personal"),
    )
    State.fallback = LLMFallback(cfg.get("llm_fallback") or {})
    State.compactor = Compactor(cfg.get("tool_compactor") or {})
    State.detector = InjectionDetector(cfg.get("injection_detector") or {})

    listen = cfg.get("listen") or {}
    host = listen.get("host", "127.0.0.1")
    port = int(listen.get("port", 5281))
    server = ThreadingHTTPServer((host, port), Handler)
    logging.info(
        "ctm listening on %s:%d upstreams=%s budgets=%s llm_fallback=%s usage_log=%s",
        host, port, list(State.upstreams.keys()), State.budgets,
        State.fallback.enabled, State.usage_log_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")


if __name__ == "__main__":
    main()