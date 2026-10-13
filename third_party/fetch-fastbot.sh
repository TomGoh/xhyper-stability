#!/bin/bash
# Download the prebuilt Fastbot_Android files at a pinned commit (no build).
# Usage: ./fetch-fastbot.sh   (runs anywhere with curl; set https_proxy if needed)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
COMMIT=$(grep -E '^fastbot ' "$HERE/versions.lock" | awk '{print $4}')
OUT=$HERE/out/fastbot
mkdir -p "$OUT"
FILES="monkeyq.jar framework.jar fastbot-thirdpart.jar libs/arm64-v8a/libfastbot_native.so"
for f in $FILES; do
  mkdir -p "$OUT/$(dirname "$f")"
  curl -fsSL --retry 3 -o "$OUT/$f" "https://raw.githubusercontent.com/bytedance/Fastbot_Android/$COMMIT/$f"
done
( cd "$OUT" && shasum -a 256 $FILES > SHA256SUMS && echo "commit $COMMIT" > COMMIT.txt && ls -l $FILES && cat SHA256SUMS )
