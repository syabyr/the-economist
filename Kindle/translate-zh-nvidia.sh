#!/bin/bash
set -e

issue_date="$1"
issue_id="$2"

if [ -z "$issue_date" ] || [ -z "$issue_id" ]; then
    echo "Usage: ./translate-zh-nvidia.sh YYYY-MM-DD ISSUE_ID" >&2
    exit 1
fi

if [ -z "$NVIDIA_API_KEY" ] && [ -z "$ECONOMIST_TRANSLATE_API_KEY" ]; then
    echo "Set NVIDIA_API_KEY (or ECONOMIST_TRANSLATE_API_KEY) before running this script." >&2
    exit 1
fi

cache_dir="$(pwd)/economist_content_cache/$issue_date-$issue_id"
translate_cache="$(pwd)/economist_translate_cache/$issue_date-$issue_id.json"

if [ ! -d "$cache_dir" ]; then
    echo "Missing content cache at $cache_dir. Run ./download-issue.sh first." >&2
    exit 1
fi

mkdir -p "$(dirname "$translate_cache")"

export ECONOMIST_CONTENT_CACHE_DIR="$cache_dir"
export ECONOMIST_TRANSLATE_CACHE="$translate_cache"
export ECONOMIST_CACHE_ONLY=1
export ECONOMIST_TRANSLATE_ZH=1
export ECONOMIST_TRANSLATE_PROVIDER=nvidia
export ECONOMIST_TRANSLATE_MODEL="${ECONOMIST_TRANSLATE_MODEL:-nvidia/riva-translate-4b-instruct-v1.1}"
export ECONOMIST_TRANSLATE_SOURCE_LANGUAGE="${ECONOMIST_TRANSLATE_SOURCE_LANGUAGE:-English}"
export ECONOMIST_TRANSLATE_TARGET_LANGUAGE="${ECONOMIST_TRANSLATE_TARGET_LANGUAGE:-Simplified Chinese}"

python3 translate_issue_cache.py "$cache_dir"
