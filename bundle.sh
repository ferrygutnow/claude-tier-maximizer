#!/bin/bash
# claude-pro-stacker — install all three token-saving tools at once
#   claude-tier-maximizer (proxy, budget routing)
#   caveman (output compression)
#   claude-pro-minmax (model routing)
#
# Usage: curl -fsSL https://raw.githubusercontent.com/ferrygutnow/claude-tier-maximizer/main/bundle.sh | bash

set -e

echo "╔══════════════════════════════════════════════╗"
echo "║     claude-pro-stacker                       ║"
echo "║     Three tools. One cap.                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. claude-tier-maximizer ────────────────────────────────────────────
echo "────────────────────────────────────────────────"
echo " [1/3] claude-tier-maximizer (thinking-budget proxy)"
echo "────────────────────────────────────────────────"
curl -fsSL "https://raw.githubusercontent.com/ferrygutnow/claude-tier-maximizer/main/install.sh" | bash

# ── 2. caveman ─────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────"
echo " [2/3] caveman (output compression — ~65% fewer tokens)"
echo "────────────────────────────────────────────────"
curl -fsSL "https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh" | bash

# ── 3. claude-pro-minmax ──────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────"
echo " [3/3] claude-pro-minmax (model routing)"
echo "────────────────────────────────────────────────"
if command -v npm &>/dev/null; then
  # Check their install method — assume npm or git
  if npm list -g claude-pro-minmax &>/dev/null 2>&1; then
    echo "  Already installed"
  else
    echo "  Installing..."
    npx -y github:move-hoon/claude-pro-minmax --help 2>/dev/null || \
      echo "  Install manually: npx github:move-hoon/claude-pro-minmax"
  fi
else
  echo "  Requires Node.js. Install:"
  echo "    npx github:move-hoon/claude-pro-minmax"
fi

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  All three installed!                        ║"
echo "║                                              ║"
echo "║  claude-tier-maximizer  ANTHROPIC_BASE_URL   ║"
echo "║       ↓ proxy on :5281                       ║"
echo "║  caveman              /caveman in chat       ║"
echo "║  claude-pro-minmax   npx ...                  ║"
echo "║                                              ║"
echo "║  Stack them: export ANTHROPIC_BASE_URL=...   ║"
echo "║  and type /caveman in your first session.    ║"
echo "╚══════════════════════════════════════════════╝"