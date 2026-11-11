#!/bin/bash
# Sync the SpeakReady cloud repo into the local Mac site.
#
# The cloud repo (GitHub Actions) is the source of truth for content.
# Run this on the Mac whenever it is awake and you want the local site
# (http://localhost:3333/serve/speakready/) to catch up:
#
#     bash ~/Documents/speakready-cloud/scripts/sync_to_mac.sh
#
# First-time setup (once):
#     git clone <your-repo-url> ~/Documents/speakready-cloud
#
set -euo pipefail

CLOUD_DIR="${1:-$HOME/Documents/speakready-cloud}"
SITE_DIR="$HOME/Documents/MyApps/speakready"
DRAFTS_DIR="$HOME/.speakready/newsletter_drafts"

if [ ! -d "$CLOUD_DIR/.git" ]; then
  echo "ERROR: $CLOUD_DIR is not a git clone."
  echo "First run: git clone <your-repo-url> $CLOUD_DIR"
  exit 1
fi

echo "== pulling latest =="
git -C "$CLOUD_DIR" pull --ff-only

echo "== syncing data files =="
cp "$CLOUD_DIR/data/prompts.json" "$SITE_DIR/data/prompts.json"
cp "$CLOUD_DIR/llms.txt"          "$SITE_DIR/llms.txt"
cp "$CLOUD_DIR/index.html"        "$SITE_DIR/index.html"

echo "== syncing audio (new files only) =="
mkdir -p "$SITE_DIR/audio"
# cp -n: never overwrite the Mac's existing mp3s
cp -n "$CLOUD_DIR"/audio/*.mp3 "$SITE_DIR/audio/" 2>/dev/null || true

echo "== syncing newsletter drafts (new files only) =="
mkdir -p "$DRAFTS_DIR"
cp -n "$CLOUD_DIR"/newsletter_drafts/*.md "$DRAFTS_DIR/" 2>/dev/null || true

total=$(python3 -c "import json;print(len(json.load(open('$SITE_DIR/data/prompts.json'))))")
echo "done. local prompts.json now holds $total prompts."
