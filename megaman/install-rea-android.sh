#!/usr/bin/env bash
# Fetch the one Android engine REA can't run without: jadx-headless-mcp 0.7.1.
# REA deliberately never downloads it. Pinned by version AND sha256 — a mismatch
# deletes the file. Run as yourself (no root needed):  bash install-rea-android.sh
# Projects created afterwards pick it up; existing RE projects: /revive them
# after re-running trust (or delete their `rea` entry and let the bot re-add).
set -euo pipefail
DIR="$HOME/tools/rea-android"
JAR="$DIR/jadx-headless-mcp-0.7.1-all.jar"
SHA=6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75
mkdir -p "$DIR"
if [ -f "$JAR" ] && echo "$SHA  $JAR" | sha256sum -c - >/dev/null 2>&1; then echo "already installed: $JAR"; exit 0; fi
curl -fL --retry 3 -o "$JAR" \
  https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/jadx-headless-mcp-0.7.1-all.jar
echo "$SHA  $JAR" | sha256sum -c - || { rm -f "$JAR"; echo "checksum mismatch — removed" >&2; exit 1; }
echo "installed: $JAR"
