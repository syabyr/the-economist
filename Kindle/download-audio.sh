#!/bin/bash
set -e

cache_dir="$1"
library_root="$2"

if [ -z "$cache_dir" ]; then
    echo "Usage: ./download-audio.sh CACHE_DIR [AUDIO_LIBRARY_ROOT]" >&2
    exit 1
fi

args=("$cache_dir")
if [ -n "$library_root" ]; then
    args+=(--library-root "$library_root")
fi

python3 "$(dirname "$0")/download_audio.py" "${args[@]}"
