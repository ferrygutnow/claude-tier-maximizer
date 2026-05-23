#!/usr/bin/env python3
"""claude-tier-maximizer — HTTP proxy between Claude Code and Anthropic API."""

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
    upstream: str
    budgets: dict
    usage_log_path: str


def extract_context(body: dict) -> str:
    """Extract the last assistant response for context (truncated)."""
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content[:500]
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            return " ".join(parts)[:500]
    return ""


def extract_last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            return " ".join(parts)
    return ""


def apply_budget(body: dict, level: str, budgets: dict) -> None:
    body["thinking"] = {"type": "enabled", "budget_tokens": int(budgets[level])}


def write_usage(record: dict) -> None:
    """Append one JSONL record to the usage log."""
    if not State.usage_log_path:
        return
    try:
        Path(State.usage_log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(State.usage_log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.warning("usage log write failed: %s", e)


def _merge_usage(accumulated: dict, u: dict) -> None:
    """Merge a usage dict into accumulated, summing numeric fields."""
    for k, v in u.items():
        if isinstance(v, (int, float)):
            accumulated[k] = accumulated.get(k, 0) + v
        else:
            accumulated[k] = v


def _parse_sse_line(line: bytes, accumulated: dict) -> None:
    """Extract usage from a single SSE data line."""
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
    """Parse complete SSE lines from buffer, updating accumulated usage info.

    Returns the remaining (incomplete) buffer.
    Pass flush=True after the stream ends to process any trailing partial line.
    """
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        _parse_sse_line(line, accumulated)
    if flush and buffer:
        _parse_sse_line(buffer, accumulated)
        buffer = b""
    return buffer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _forward(self, method: str, body_bytes: bytes, classification_meta: dict | None = None) -> None:
        url = State.upstream + self.path
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
                        wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                        logging.warning("HTTP %d from Anthropic, retry %d/3 in %ds", e.code, attempt + 1, wait)
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
            logging.exception("upstream error: %s", e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        self._forward("GET", b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(n) if n else b""

        if "/v1/messages" not in self.path:
            return self._forward("POST", body_bytes)

        new_body = body_bytes
        meta = None
        try:
            body = json.loads(body_bytes) if body_bytes else {}
            raw = extract_last_user_text(body)
            cleaned = clean_text(raw, State.rules)
            if not cleaned:
                logging.info(
                    "level=PASSTHROUGH reason=empty_after_clean raw_len=%d model=%s stream=%s",
                    len(raw), body.get("model"), body.get("stream", False),
                )
                meta = {
                    "level": "PASSTHROUGH", "reason": "empty_after_clean",
                    "preview": "", "model": body.get("model"),
                    "stream": body.get("stream", False), "budget_set": None,
                }
            else:
                ctx = extract_context(body)
                level, reason = classify(cleaned, State.rules, State.fallback, context=ctx)
                apply_budget(body, level, State.budgets)
                body["messages"], inject_stats = State.detector.process_messages(
                    body.get("messages", [])
                )
                if inject_stats.get("sanitized"):
                    logging.warning("injection_detector: %s", inject_stats)
                body["messages"], compact_stats = State.compactor.process(body.get("messages", []))
                if compact_stats.get("compacted") or compact_stats.get("images_scaled"):
                    logging.info("compactor: %s", compact_stats)
                new_body = json.dumps(body).encode()
                logging.info(
                    "level=%s reason=%s preview=%r model=%s stream=%s",
                    level, reason, cleaned[:120].replace("\n", " "),
                    body.get("model"), body.get("stream", False),
                )
                meta = {
                    "level": level, "reason": reason,
                    "preview": cleaned[:200].replace("\n", " "),
                    "model": body.get("model"),
                    "stream": body.get("stream", False),
                    "budget_set": State.budgets[level],
                }
        except Exception as e:
            logging.exception("classify error, passing through: %s", e)
            new_body = body_bytes

        self._forward("POST", new_body, meta)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/claude-tier-maximizer/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.get("logging") or {})

    State.config = cfg
    State.upstream = cfg.get("upstream", "https://api.anthropic.com")
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
        "claude-tier-maximizer listening on %s:%d upstream=%s budgets=%s llm_fallback=%s usage_log=%s",
        host, port, State.upstream, State.budgets, State.fallback.enabled, State.usage_log_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")


if __name__ == "__main__":
    main()
