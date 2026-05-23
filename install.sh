#!/bin/bash
# claude-tier-maximizer one-liner installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ferrygutnow/claude-tier-maximizer/main/install.sh | bash

set -e

REPO="ferrygutnow/claude-tier-maximizer"
INSTALL_DIR="${CTM_DIR:-/opt/claude-tier-maximizer}"
LOG_DIR="/var/log/claude-tier-maximizer"

echo "=== claude-tier-maximizer installer ==="

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Linux)   OS_TYPE="linux" ;;
  Darwin)  OS_TYPE="macos" ;;
  *)       echo "Unsupported OS: $OS"; exit 1 ;;
esac

# Check Python
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it first."
  exit 1
fi

# Install pip deps
echo "[1/4] Installing dependencies..."
"$PY" -m pip install pyyaml --quiet 2>/dev/null || "$PY" -m pip install pyyaml

# Clone or pull
echo "[2/4] Downloading..."
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || true
else
  git clone "https://github.com/$REPO.git" "$INSTALL_DIR"
fi

# Make scripts executable
chmod +x "$INSTALL_DIR"/*.py "$INSTALL_DIR"/*.sh 2>/dev/null || true

# Setup service
echo "[3/4] Installing service..."
mkdir -p "$LOG_DIR"

if [ "$OS_TYPE" = "linux" ]; then
  cp "$INSTALL_DIR/systemd/claude-tier-maximizer.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now claude-tier-maximizer
  # Optional: scrubber
  cp "$INSTALL_DIR/systemd/claude-scrubber.service" /etc/systemd/system/
  systemctl enable --now claude-scrubber 2>/dev/null || true
  echo "  Proxy:  systemctl status claude-tier-maximizer"
  echo "  Scrub: systemctl status claude-scrubber"
elif [ "$OS_TYPE" = "macos" ]; then
  cat > /tmp/com.ctm.proxy.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.ctm.proxy</string>
<key>ProgramArguments</key><array><string>$PY</string><string>$INSTALL_DIR/proxy.py</string></array>
<key>KeepAlive</key><true/>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>$LOG_DIR/stdout.log</string>
<key>StandardErrorPath</key><string>$LOG_DIR/stderr.log</string>
</dict></plist>
PLIST
  cp /tmp/com.ctm.proxy.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.ctm.proxy.plist
  echo "  Proxy running via launchd"
fi

# Ollama model (optional)
echo "[4/4] Optional: pull local LLM for better classification?"
echo "  Run later: ollama pull qwen2.5:3b"
echo ""

# Auto-personalize from existing logs
echo "=== Generating personal rules from your agent logs ==="
"$PY" "$INSTALL_DIR/personalize.py" --out "$INSTALL_DIR/rules/personal.yaml" 2>/dev/null || \
  echo "  (no agent logs found — will auto-generate after first sessions)"

echo ""
echo "=== Done ==="
echo "Point your agent at localhost:5281"
echo "  Claude Code: export ANTHROPIC_BASE_URL=http://localhost:5281"
echo "  Codex CLI:   export OPENAI_BASE_URL=http://localhost:5281/v1"
echo "  Gemini CLI:  export GEMINI_BASE_URL=http://localhost:5281"
echo ""
echo "View logs: tail -f $LOG_DIR/decisions.log"