#!/bin/sh
set -eu

# Optional, explicit host/device setup hook. The application never runs package
# manager commands and never receives Docker socket or mount capabilities.
case "${DRIVER_SETUP:-off}" in
  off) exec "$@" ;;
  auto)
    echo "DRIVER_SETUP=auto is a deployment hook; use a purpose-built image with preinstalled userspace drivers." >&2
    exec "$@"
    ;;
  *) echo "Unsupported DRIVER_SETUP value: ${DRIVER_SETUP}" >&2; exit 64 ;;
esac
