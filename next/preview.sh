#!/usr/bin/env bash
# Preview the HandBrake workbench (next/). Uses a real HandBrakeCLI when
# present, otherwise falls back to the
# interface mock (clearly announced) so the UI can be inspected.
set -euo pipefail

cd "$(dirname "$0")/.."          # repo root
REPO="$PWD/next"
PORT="${CUTE_CAT_HB_PORT:-18084}"
STATE="${CUTE_CAT_HB_STATE:-$HOME/.cache/cute-cat-handbrake-preview}"
MEDIA="$STATE/media"
OUT="$STATE/out"

mkdir -p "$MEDIA/converted" "$OUT"
touch "$MEDIA/.cute-cat-mounted"
[ -f "$MEDIA/movie.mp4" ] || printf 'PREVIEW-INPUT\n' > "$MEDIA/movie.mp4"

ENGINE=""
if command -v HandBrakeCLI >/dev/null 2>&1; then
  ENGINE="$(command -v HandBrakeCLI)"
  echo "[preview] using REAL engine: $ENGINE"
else
  ENGINE="$STATE/HandBrakeCLI"
  cat > "$ENGINE" <<MOCK
#!/usr/bin/env bash
exec python3 "$REPO/tools/mock_handbrakecli.py" "\$@"
MOCK
  chmod +x "$ENGINE"
  echo "[preview] WARNING: real HandBrakeCLI not found — using the interface mock."
  echo "[preview] The mock only exercises wiring; it does NOT transcode."
fi

cat > "$STATE/config.toml" <<CONFIG
[server]
listen = "127.0.0.1:$PORT"
database = "$STATE/cute-cat.db"

[security]
api_token = ""

[engine]
handbrake_bin = "$ENGINE"
ffprobe_bin = ""

[[storage_roots]]
id = "media"
label = "Preview media"
path = "$MEDIA"
read_only = false
mount_marker = ".cute-cat-mounted"

[[storage_roots]]
id = "out"
label = "Preview output"
path = "$OUT"
read_only = false
CONFIG

export PYTHONPATH="$REPO"
echo "Cute Cat (HandBrake) preview: http://127.0.0.1:$PORT"
echo "State: $STATE"
exec python3 -m cutecat --config "$STATE/config.toml"
