#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source "$HOME/.cargo/env" 2>/dev/null || true

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo is required in WSL. Install Rust with rustup first." >&2
  exit 127
fi

PORT="${CUTE_CAT_PORT:-18083}"
STATE_DIR="${CUTE_CAT_STATE_DIR:-$HOME/.cache/cute-cat-preview}"
MEDIA_DIR="${CUTE_CAT_MEDIA_DIR:-$STATE_DIR/media}"
TOOLS_DIR="$STATE_DIR/tools"
BUNDLE_DIR="$STATE_DIR/bundle"
CONFIG_FILE="$STATE_DIR/config.toml"
DB_FILE="$STATE_DIR/cute-cat.db"

mkdir -p "$MEDIA_DIR/incoming" "$MEDIA_DIR/converted" "$BUNDLE_DIR/bin"
touch "$MEDIA_DIR/.cute-cat-mounted"
mkdir -p "$STATE_DIR"

FFMPEG_SOURCE="${CUTE_CAT_FFMPEG_SOURCE:-}"
FFPROBE_SOURCE="${CUTE_CAT_FFPROBE_SOURCE:-}"
if [[ -z "$FFMPEG_SOURCE" ]] && command -v ffmpeg >/dev/null 2>&1; then FFMPEG_SOURCE="$(command -v ffmpeg)"; fi
if [[ -z "$FFPROBE_SOURCE" ]] && command -v ffprobe >/dev/null 2>&1; then FFPROBE_SOURCE="$(command -v ffprobe)"; fi
if [[ -n "$FFMPEG_SOURCE" && -n "$FFPROBE_SOURCE" ]]; then
  cp "$FFMPEG_SOURCE" "$BUNDLE_DIR/bin/ffmpeg"
  cp "$FFPROBE_SOURCE" "$BUNDLE_DIR/bin/ffprobe"
  chmod 755 "$BUNDLE_DIR/bin/ffmpeg" "$BUNDLE_DIR/bin/ffprobe"
fi

cat > "$CONFIG_FILE" <<CONFIG
[server]
listen = "127.0.0.1:${PORT}"
database_url = "sqlite://${DB_FILE}?mode=rwc"

[security]
api_token = ""

[[storage_roots]]
id = "media"
label = "Preview media"
path = "${MEDIA_DIR}"
read_only = false
mount_marker = ".cute-cat-mounted"

[transcoding]
ffmpeg_bin = "${CUTE_CAT_FFMPEG_BIN:-ffmpeg}"
ffprobe_bin = "${CUTE_CAT_FFPROBE_BIN:-ffprobe}"
max_concurrent_jobs = 1

[transcoding.installer]
enabled = true
install_dir = "${TOOLS_DIR}"
source_dir = "${BUNDLE_DIR}"
version = "preview-bundle"
CONFIG

export CUTE_CAT_CONFIG="$CONFIG_FILE"
export RUST_LOG="${RUST_LOG:-cute_cat=info,tower_http=info}"

echo "Cute Cat Preview: http://127.0.0.1:${PORT}"
echo "State: ${STATE_DIR}"
exec cargo run --bin cute-cat
