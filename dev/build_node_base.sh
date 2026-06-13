#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR/.."

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)
    TARGET_PLATFORM=linux/amd64
    TARGET_TAG=node-base:latest-amd64
    ;;
  arm64|aarch64)
    TARGET_PLATFORM=linux/arm64
    TARGET_TAG=node-base:latest-arm64
    ;;
  *)
    echo "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

# Build only for the native architecture to avoid cross-build/emulation requirements.
docker image build . -f services/node-base.Dockerfile \
  --tag "$TARGET_TAG" \
  --tag node-base:latest \
  --platform "$TARGET_PLATFORM"
