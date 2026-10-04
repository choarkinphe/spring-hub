#!/bin/sh
# SpringHub (HandBrake edition) container entrypoint.
#
# Responsibilities:
#   * ensure /data exists and is writable by the service user;
#   * generate a config file on first run if none is mounted;
#   * report engine availability clearly (and keep running so the UI can show
#     the reason, rather than crash-looping);
#   * exec the service.
#
# It never installs packages, never mounts network shares, and never touches the
# Docker socket. SMB/NFS mounts are a host / Docker concern (see the Compose
# examples).

set -eu

# New names take precedence; legacy names and persistent paths stay compatible.
DATA_DIR="${SPRINGHUB_DATA_DIR-${CUTE_CAT_DATA_DIR:-/data}}"
CONFIG_FILE="${SPRINGHUB_CONFIG-${CUTE_CAT_CONFIG:-$DATA_DIR/config.toml}}"
ENGINE="${SPRINGHUB_ENGINE-${CUTE_CAT_ENGINE:-HandBrakeCLI}}"

mkdir -p "$DATA_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[entrypoint] no config at $CONFIG_FILE — generating a default one"
  cat > "$CONFIG_FILE" <<CONFIG
[server]
listen = "${SPRINGHUB_LISTEN-${CUTE_CAT_LISTEN:-0.0.0.0:8080}}"
database = "${SPRINGHUB_DATABASE-${CUTE_CAT_DATABASE:-$DATA_DIR/cute-cat.db}}"
media_root = "${SPRINGHUB_MEDIA_ROOT-${CUTE_CAT_MEDIA_ROOT:-/media}}"

[security]
api_token = ""

[engine]
handbrake_bin = "$ENGINE"
ffprobe_bin = "ffprobe"
max_concurrent_jobs = ${SPRINGHUB_MAX_JOBS-${CUTE_CAT_MAX_JOBS:-1}}
refuse_overwrite = true
job_timeout_seconds = 0
extra_args = []

[[storage_roots]]
id = "media"
label = "媒体目录"
path = "${SPRINGHUB_MEDIA_ROOT-${CUTE_CAT_MEDIA_ROOT:-/media}}"
read_only = false
mount_marker = "${SPRINGHUB_MOUNT_MARKER-${CUTE_CAT_MOUNT_MARKER:-.cute-cat-mounted}}"
CONFIG
fi

# Engine diagnostics — reported, never fatal. The web UI surfaces the same
# information so an operator can see *why* encoding is disabled.
if command -v "$ENGINE" >/dev/null 2>&1; then
  echo "[entrypoint] engine: $(command -v "$ENGINE")"
  "$ENGINE" --version 2>&1 | head -1 || true
else
  echo "[entrypoint] WARNING: engine '$ENGINE' not found on PATH." >&2
  echo "[entrypoint] Encoding is disabled until a real HandBrakeCLI is available." >&2
fi

exec "$@"
