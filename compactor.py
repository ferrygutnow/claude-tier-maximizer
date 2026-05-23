"""compactor — two proxy-level interceptors for inbound messages:

1. Tool-result compactor: large text tool_result blocks → local LLM summary
2. Image downscaler: base64 image blocks → resized before forwarding upstream

Both are applied to the messages array before it reaches Anthropic.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import urllib.request
from typing import Any

log = logging.getLogger("ctm.compactor")

SUMMARIZE_PROMPT = """Summarize this tool output for an AI coding assistant. Be dense and precise.
Preserve: error messages, file paths, line numbers, exit codes, warnings, final status.
Drop: progress bars, repeated lines, verbose stack frame padding, timestamps.
Max 120 words.

Output:
{content}"""


def _ollama_summarize(text: str, model: str, ollama_url: str, timeout: float) -> str:
    body = {
        "model": model,
        "prompt": SUMMARIZE_PROMPT.format(content=text[:6000]),
        "stream": False,
        "options": {"num_predict": 200, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{ollama_url.rstrip('/')}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("response", "").strip()


def _downscale_image(b64_data: str, media_type: str, max_px: int) -> tuple[str, str]:
    """Downscale a base64 image to max_px on its longest side. Returns (b64, media_type)."""
    from PIL import Image
    raw = base64.b64decode(b64_data)
    img = Image.open(io.BytesIO(raw))
    w, h = img.size
    if max(w, h) <= max_px:
        return b64_data, media_type
    scale = max_px / max(w, h)
    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    fmt = "JPEG" if "jpeg" in media_type else "PNG"
    img.save(buf, format=fmt, optimize=True)
    new_b64 = base64.b64encode(buf.getvalue()).decode()
    new_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    reduction = (1 - len(new_b64) / len(b64_data)) * 100
    log.info("image downscaled %dx%d→%dx%d (%.0f%% smaller)",
             w, h, int(w * scale), int(h * scale), reduction)
    return new_b64, new_type


def _text_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(c.get("text", "")) for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return 0


def _gpu_available() -> bool:
    import shutil, subprocess
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=3)
            return r.returncode == 0
        except Exception:
            pass
    return False


class Compactor:
    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled", False))
        if self.enabled and not _gpu_available():
            log.info("compactor disabled — no GPU detected (CPU-only Ollama too slow)")
            self.enabled = False
        self.min_chars = int(cfg.get("min_chars", 2000))
        self.model = cfg.get("model", "qwen2.5:3b")
        self.ollama_url = cfg.get("ollama_url", "http://localhost:11434")
        self.timeout = cfg.get("timeout_ms", 8000) / 1000.0
        self.max_summary_chars = int(cfg.get("max_summary_chars", 600))
        img_cfg = cfg.get("image", {})
        self.image_enabled = bool(img_cfg.get("enabled", True))
        self.image_max_px = int(img_cfg.get("max_px", 768))

    def process(self, messages: list) -> tuple[list, dict]:
        """Return (modified_messages, stats)."""
        if not self.enabled:
            return messages, {}
        stats = {"compacted": 0, "images_scaled": 0, "chars_saved": 0}
        result = []
        for msg in messages:
            msg = dict(msg)
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue
            new_content = []
            for block in content:
                block = self._process_block(block, stats)
                new_content.append(block)
            msg["content"] = new_content
            result.append(msg)
        return result, stats

    def _process_block(self, block: dict, stats: dict) -> dict:
        if not isinstance(block, dict):
            return block

        btype = block.get("type")

        # --- tool_result text compaction ---
        if btype == "tool_result":
            inner = block.get("content")
            # content can be a string or list of blocks
            if isinstance(inner, str) and len(inner) >= self.min_chars:
                summary = self._summarize(inner)
                if summary:
                    saved = len(inner) - len(summary)
                    stats["compacted"] += 1
                    stats["chars_saved"] += saved
                    log.info("tool_result compacted: %d→%d chars (saved %d)", len(inner), len(summary), saved)
                    block = dict(block)
                    block["content"] = f"[compacted by ctm — {saved} chars removed]\n{summary}"
            elif isinstance(inner, list):
                new_inner = []
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        text = sub.get("text", "")
                        if len(text) >= self.min_chars:
                            summary = self._summarize(text)
                            if summary:
                                saved = len(text) - len(summary)
                                stats["compacted"] += 1
                                stats["chars_saved"] += saved
                                log.info("tool_result[text] compacted: %d→%d chars", len(text), len(summary))
                                sub = dict(sub)
                                sub["text"] = f"[compacted by ctm — {saved} chars removed]\n{summary}"
                    new_inner.append(sub)
                block = dict(block)
                block["content"] = new_inner

        # --- image downscaling ---
        if btype == "image" and self.image_enabled:
            src = block.get("source", {})
            if src.get("type") == "base64":
                new_b64, new_type = _downscale_image(
                    src["data"], src.get("media_type", "image/png"), self.image_max_px
                )
                if new_b64 is not src["data"]:
                    stats["images_scaled"] += 1
                    block = dict(block)
                    block["source"] = dict(src)
                    block["source"]["data"] = new_b64
                    block["source"]["media_type"] = new_type

        return block

    def _summarize(self, text: str) -> str | None:
        try:
            summary = _ollama_summarize(text, self.model, self.ollama_url, self.timeout)
            if summary and len(summary) < len(text):
                return summary
        except Exception as e:
            log.warning("compactor summarize failed: %s", e)
        return None
