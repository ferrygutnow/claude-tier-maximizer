"""LLM fallback classifier — for prompts the regex layer can't decide.

Providers:
- anthropic  : Claude API (e.g. claude-haiku-4-5)
- ollama     : local Ollama server (e.g. gemma:2b)
- openai     : (not implemented)
- deepseek   : (not implemented)

All providers return one of: 'low' | 'medium' | 'high'.
Bounded by timeout_ms; API key (if needed) read from env var.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.request

log = logging.getLogger("ctm.llm")

CLASSIFIER_PROMPT = """You classify Claude Code prompts. Reply with ONE WORD: low, medium, or high.

low    = lookup, rename, list, show, status, version, simple yes/no
medium = implement one function, small refactor, fix one bug
high   = debug unknown bug, design, architecture, multi-file refactor, security review

IMPORTANT: This is a FOLLOW-UP prompt (continuation of a conversation).
Classify by INTENT, not by the words alone:
- Follow-up approval: "yes", "ok", "continue", "go ahead" after being asked to proceed = low
- Status check: "still working?", "check", "status?" = low
- Selection: "do 1", "do all", "do 5 and 6" (picking from options) = low
- Small follow-up fix/repair indicated by assistant response = medium
- New investigation or debugging = high

Previous assistant response: {context}

Current user prompt: {prompt}
Answer:"""


def _extract_level(text: str) -> str:
    t = (text or "").strip().lower()
    for w in ("low", "high", "medium"):
        if w in t:
            return w
    return "medium"


class LLMFallback:
    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled"))
        self.provider = cfg.get("provider", "anthropic")
        self.model = cfg.get("model", "claude-haiku-4-5")
        self.timeout = (cfg.get("timeout_ms", 1500)) / 1000.0
        self.trigger = cfg.get("trigger", "medium_only")
        self.ollama_url = cfg.get("ollama_url", "http://localhost:11434")
        api_key_env = cfg.get("api_key_env", "")
        self.api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if self.enabled and self.provider == "anthropic" and not self.api_key:
            log.warning("llm_fallback anthropic enabled but env %s is empty; disabling", api_key_env)
            self.enabled = False

    def classify(self, text: str, context: str = "") -> str:
        if self.provider == "anthropic":
            return self._classify_anthropic(text, context)
        if self.provider == "ollama":
            return self._classify_ollama(text, context)
        raise NotImplementedError(f"provider {self.provider!r} not implemented")

    def _classify_anthropic(self, text: str, context: str = "") -> str:
        ctx = f"Previous assistant response: {context[:500]}" if context else ""
        body = {
            "model": self.model,
            "max_tokens": 4,
            "messages": [
                {"role": "user", "content": CLASSIFIER_PROMPT.format(prompt=text[:1500], context=ctx)}
            ],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        for block in data.get("content") or []:
            if block.get("type") == "text":
                return _extract_level(block.get("text", ""))
        return "medium"

    def _classify_ollama(self, text: str, context: str = "") -> str:
        ctx = f"Previous assistant response: {context[:500]}" if context else ""
        body = {
            "model": self.model,
            "prompt": CLASSIFIER_PROMPT.format(prompt=text[:1500], context=ctx),
            "stream": False,
            "options": {"num_predict": 3, "temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.ollama_url.rstrip('/')}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return _extract_level(data.get("response", ""))
