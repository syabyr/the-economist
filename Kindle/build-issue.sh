#!/bin/bash
set -e

issue_date="$1"
issue_id="$2"
year=$(echo "$issue_date" | cut -d "-" -f 1)

if [ -z "$issue_date" ] || [ -z "$issue_id" ]; then
    echo "Usage: ./build-issue.sh YYYY-MM-DD ISSUE_ID" >&2
    exit 1
fi

cache_dir="$(pwd)/economist_content_cache/$issue_date-$issue_id"
translate_cache="$(pwd)/economist_translate_cache/$issue_date-$issue_id.json"
recipe_file="TheEconomist-$issue_date-$issue_id.recipe"
cover_path="$(python3 - "$cache_dir" <<'PY'
import glob
import gzip
import hashlib
import json
import os
import sys
from urllib.parse import urlparse

cache_dir = sys.argv[1]
edition_files = glob.glob(os.path.join(cache_dir, "editions", "*.json"))
for edition_file in edition_files:
    raw = open(edition_file, "rb").read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    edition = (
        data.get("data", {}).get("findEditionByDate")
        or data.get("data", {}).get("findLatestEdition")
        or {}
    )
    url = ((edition.get("cover") or {}).get("url") or "").strip()
    if not url:
        continue
    ext = os.path.splitext(urlparse(url).path)[1] or ".bin"
    path = os.path.join(
        cache_dir,
        "images",
        hashlib.sha256(url.encode("utf-8")).hexdigest() + ext,
    )
    if os.path.exists(path):
        print(path)
        break
PY
)"

if [ ! -d "$cache_dir" ]; then
    echo "Missing content cache at $cache_dir. Run ./download-issue.sh first." >&2
    exit 1
fi

mkdir -p "$year" "pdf/$year"

export ECONOMIST_CONTENT_CACHE_DIR="$cache_dir"
export ECONOMIST_CACHE_ONLY=1
if [ -n "$cover_path" ] && [ -f "$cover_path" ]; then
    export ECONOMIST_SKIP_RECIPE_COVER=1
fi
if [ -f "$translate_cache" ]; then
    export ECONOMIST_TRANSLATE_CACHE="$translate_cache"
    export ECONOMIST_TRANSLATE_ZH=1
    export ECONOMIST_TRANSLATE_PROVIDER="${ECONOMIST_TRANSLATE_PROVIDER:-nvidia}"
    export ECONOMIST_TRANSLATE_MODEL="${ECONOMIST_TRANSLATE_MODEL:-nvidia/riva-translate-4b-instruct-v1.1}"
    export ECONOMIST_TRANSLATE_SOURCE_LANGUAGE="${ECONOMIST_TRANSLATE_SOURCE_LANGUAGE:-English}"
    export ECONOMIST_TRANSLATE_TARGET_LANGUAGE="${ECONOMIST_TRANSLATE_TARGET_LANGUAGE:-Simplified Chinese}"
fi

sed "s/edition_date = .*/edition_date = '$issue_date'/" economist.recipe > "$recipe_file"
cover_args=()
if [ -n "$cover_path" ] && [ -f "$cover_path" ]; then
    cover_args=(--cover "$cover_path")
fi

ebook-convert "$recipe_file" .mobi --output-profile=kindle_oasis --pubdate="$issue_date" -vv --mobi-file-type=new --authors="TheEconomist" --title="TheEconomist-$issue_date-$issue_id" "${cover_args[@]}"
ebook-convert "$recipe_file" .pdf --paper-size=a4 --pdf-page-numbers --pdf-page-margin-bottom=42 --pdf-page-margin-top=42 --pdf-page-margin-left=42 --pdf-page-margin-right=42 -vv --title="TheEconomist-$issue_date-$issue_id.pdf" "${cover_args[@]}"
rm -f "$recipe_file"
mv "TheEconomist-$issue_date-$issue_id.mobi" "$year"
mv "TheEconomist-$issue_date-$issue_id.pdf" "pdf/$year"
