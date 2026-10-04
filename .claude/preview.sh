#!/usr/bin/env bash
# Default preview: the Python / HandBrake workbench.
set -euo pipefail

cd "$(dirname "$0")/.."
exec bash next/preview.sh
