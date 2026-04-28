#!/bin/bash
set -e

issue_date="$1"
issue_id="$2"

if [ -z "$issue_date" ] || [ -z "$issue_id" ]; then
    echo "Usage: ./scrap.sh YYYY-MM-DD ISSUE_ID" >&2
    exit 1
fi

./download-issue.sh "$issue_date" "$issue_id"

if [ "${ECONOMIST_TRANSLATE_ZH:-0}" = "1" ]; then
    ./translate-zh-nvidia.sh "$issue_date" "$issue_id"
fi

./build-issue.sh "$issue_date" "$issue_id"
