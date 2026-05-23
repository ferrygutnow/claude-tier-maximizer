# claude-tier-maximizer

**Stretch your AI coding agent budget by routing thinking/reasoning per prompt.**

An HTTP proxy that sits between your coding agent (Claude Code, Codex CLI, Gemini CLI)
and its upstream API. It classifies every user prompt and rewrites the thinking
or reasoning budget based on actual complexity — so simple prompts don't burn
the same budget as complex debugging.

```
You type prompt
   ↓
Claude Code  (ANTHROPIC_BASE_URL=http://localhost:5281)
   ↓
claude-tier-maximizer proxy
   - extracts last user message
   - strips system-reminders & noise
   - classifies via regex (low / medium / high)
   - optional LLM fallback for ambiguous prompts (Ollama, Anthropic)
   - rewrites thinking.budget_tokens
   ↓
api.anthropic.com
   ↓
response streams back through proxy → Claude Code
```

On a sample of 3,814 real Claude Code prompts:
- **25.8%** → low (1,024 budget tokens)
- **68.6%** → medium (4,000 budget tokens)
- **5.6%** → high (16,000 budget tokens)

Without the proxy, most of those low prompts would burn medium+ budgets — effective session length stretches significantly if you hit daily caps.

## Features

| Feature | What |
|---|---|
| **Thinking-budget routing** | Classify prompts as low/medium/high via layered regex + optional local LLM |
| **Context-aware LLM fallback** | Qwen 2.5 3B (or any Ollama model) classifies ambiguous prompts with access to the last assistant response — catches follow-up intent |
| **Personal rules** | `personal.yaml` learns your actual prompt patterns; force_low catches "ja", "do 1", "still working?", "check again" |
| **Secret redactor** | `scrubber.py` watches all agent log dirs (Claude Code, Codex, OpenCode, Gemini CLI, Copilot CLI) and redacts API keys, tokens, passwords in-place via regex. GPU-gated semantic LLM pass for non-obvious secrets |
| **Prompt injection shield** | `injection_detector.py` scans tool_result content for "ignore instructions", "your new role is", "send data to" — regex + optional LLM layer |
| **Tool-result compactor** | `compactor.py` summarizes large tool outputs (2k+ chars) via local LLM before forwarding, saving input tokens. GPU-gated. Auto-downscales base64 images |
| **Auto-tune** | `tune.py` mines `usage.jsonl` for under-classified prompts, proposes new regex patterns. Optional auto-apply with confidence thresholds |
| **Personalization** | `personalize.py` walks your existing agent session history and generates `personal.yaml` from your actual prompts — run immediately after install, no need to wait |
| **Weekly digest** | Cron runs calibrator + tuner every Monday 09:00 UTC, writes `digest-latest.md` |
| **Precise override markers** | `ultrathink` / `(high thinking)` = force high. `(low thinking)` / `(no thinking)` = force low |

## How classification works

1. **Strip noise** — `<system-reminder>`, pasted-content markers
2. **Explicit overrides** — user-typed markers (`ultrathink`, `(no thinking)`)
3. **Force low** — slash commands, one-word confirmations, option selections, status pings
4. **High patterns** — `debug`, `investigate`, `design`, `security review`, `why is...`
5. **Low patterns** — `rename`, `show`, `list`, `ls`, `cat`, `run`, `status`
6. **LLM fallback** — if regex can't decide and medium-only trigger is hit, ask local model with context
7. **Default** — medium (4k budget)

Force_low prompts that match with conversation context (last assistant response available) are re-checked by the LLM to avoid under-classifying approval prompts.

## Rule files (three layers, all appended at startup)

| Layer | File | Source |
|---|---|---|
| 1 | `rules/default.yaml` | Shipped patterns (47 high, 43 low) |
| 2 | `rules/auto.yaml` | Auto-tuned by `tune.py` when `auto_tune.enabled: true` |
| 3 | `rules/personal.yaml` | Generated from your session history via `personalize.py` |

Plus `rules/blocklist.yaml` — patterns you rejected via `ctm-review`.

## Install

Requires Python 3.9+ and [Ollama](https://ollama.ai) (for LLM features — compactor, injection detection, classifier fallback).

### Linux (systemd)

```bash
git clone https://github.com/ferrygutnow/claude-tier-maximizer.git /opt/claude-tier-maximizer
sudo apt-get install -y python3-yaml

# Install the proxy (thinking-budget routing)
sudo cp /opt/claude-tier-maximizer/systemd/claude-tier-maximizer.service /etc/systemd/system/
sudo mkdir -p /var/log/claude-tier-maximizer
sudo systemctl daemon-reload
sudo systemctl enable --now claude-tier-maximizer

# Install the scrubber (secret redaction — optional but recommended)
sudo cp /opt/claude-tier-maximizer/systemd/claude-scrubber.service /etc/systemd/system/
sudo systemctl enable --now claude-scrubber
```

The scrubber immediately does a full scan of all existing agent logs
(`~/.claude/projects`, `~/.codex/projects`, `~/.opencode/projects`,
`~/.gemini/projects`, `~/.copilot/projects`), then watches in real-time
for new content every 5 seconds.

### macOS / Windows

```bash
git clone https://github.com/ferrygutnow/claude-tier-maximizer.git
cd claude-tier-maximizer
pip install pyyaml
python3 proxy.py
```

All LLM features auto-disable if Ollama is not available or no GPU is detected — the proxy still works for regex-based classification alone.

Point your agent at the proxy. In the same terminal, before starting:

```bash
# Claude Code (Anthropic)
export ANTHROPIC_BASE_URL=http://localhost:5281
claude

# Codex CLI (OpenAI)
export OPENAI_BASE_URL=http://localhost:5281/v1
codex

# Gemini CLI (Google)
export GEMINI_API_KEY=your_key  # needs an API key
export GEMINI_BASE_URL=http://localhost:5281  # most Gemini CLIs support this
```

Or add to your shell profile (`~/.bashrc`, `~/.zshrc`) to always route through the proxy.

```bash
echo 'export ANTHROPIC_BASE_URL=http://localhost:5281' >> ~/.bashrc
```

Watch decisions:

```bash
tail -f /var/log/claude-tier-maximizer/decisions.log
```

## Configuration

Edit `/opt/claude-tier-maximizer/config.yaml`:

```yaml
# Budgets per level
budgets:
  low: 1024
  medium: 4000
  high: 16000

# LLM fallback (for ambiguous prompts)
llm_fallback:
  enabled: true
  provider: ollama           # or anthropic
  model: qwen2.5:3b
  timeout_ms: 5000

# Prompt injection detector
injection_detector:
  enabled: true
  llm:
    enabled: true            # disabled if no GPU
    model: qwen2.5:3b

# Tool-result compactor (auto-disabled without GPU)
tool_compactor:
  enabled: true
  min_chars: 2000
  model: qwen2.5:3b

# Auto-tune (opt-in)
auto_tune:
  enabled: false
  min_occurrences: 10
  min_consistency: 0.9
```

## Scrubber (secret redaction)

Runs as a separate service / cron job. Watches all agent log directories:

```bash
# Watch all known agent dirs (~/.claude/projects, .codex, .opencode, .gemini, .copilot)
python3 scrubber.py

# Watch specific agents only
python3 scrubber.py --agents claude codex

# One-shot: scrub a specific file
python3 scrubber.py --scrub-file ~/.claude/projects/project/session.jsonl

# Dry run: see what would be redacted
python3 scrubber.py --dry-run
```

On GPU machines, the semantic LLM pass catches non-standard secrets. On CPU,
regex-only — still catches the common patterns.

## Companion tools

- **[caveman](https://github.com/JuliusBrussee/caveman)** — compresses output prose (~65% fewer tokens per reply)
- **[claude-pro-minmax](https://github.com/move-hoon/claude-pro-minmax)** — model routing and tool-output budgets

## License

MIT
