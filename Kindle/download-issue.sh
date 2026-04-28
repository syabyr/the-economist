#!/bin/bash
set -e

issue_date="$1"
issue_id="$2"

if [ -z "$issue_date" ] || [ -z "$issue_id" ]; then
    echo "Usage: ./download-issue.sh YYYY-MM-DD ISSUE_ID" >&2
    exit 1
fi

cache_dir="$(pwd)/economist_content_cache/$issue_date-$issue_id"
mkdir -p "$cache_dir"
export ECONOMIST_CONTENT_CACHE_DIR="$cache_dir"

python3 prefetch_issue.py "$issue_date"
