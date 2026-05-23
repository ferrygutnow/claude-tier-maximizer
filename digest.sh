#!/bin/sh
# Weekly digest — runs calibrator + tuner, writes a dated markdown digest.
# Wired via /etc/cron.d/claude-tier-maximizer.
set -e
STAMP=$(date +%Y%m%d)
DIR=/var/log/claude-tier-maximizer
DIGEST="$DIR/digest-$STAMP.md"
mkdir -p "$DIR"

{
  echo "# claude-tier-maximizer weekly digest — $(date -Is)"
  echo
  echo "## Calibrator"
  echo '```'
  python3 /opt/claude-tier-maximizer/calibrator.py 2>&1 || true
  echo '```'
  echo
  echo "## Auto-tuning suggestions"
  python3 /opt/claude-tier-maximizer/tune.py --out-md "$DIR/tune-report-$STAMP.md" 2>&1 || true
  echo
  if [ -f "$DIR/tune-report-$STAMP.md" ]; then
    cat "$DIR/tune-report-$STAMP.md"
  fi
} > "$DIGEST"

# Symlink "latest" for easy viewing
ln -sf "$DIGEST" "$DIR/digest-latest.md"
echo "Wrote $DIGEST"
