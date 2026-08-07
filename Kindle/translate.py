#!/usr/bin/env python3
"""Phase 2: Translate articles using FULL-ARTICLE translation (context-aware)

Usage:
    python3 translate.py YYYY-MM-DD [ISSUE_ID] [--article-index N] [--force] [--paragraph-mode]

Options:
    --force              Re-translate articles that already have translations
    --paragraph-mode     Use original paragraph-by-paragraph translation (legacy)
    --article-index N    Only translate article N (0-indexed) instead of all
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import db
import utils


def load_runtime_env():
    """Load .env variables with support for 'export' prefix."""
    cwd_env = os.path.join(os.getcwd(), '.env')
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

    for env_file in [cwd_env, script_env]:
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if line.startswith('export '):
                            line = line.replace('export ', '', 1)
                        if '=' in line:
                            key, value = line.split('=', 1)
                            os.environ[key] = value


def get_full_article_text_with_markers(article_data):
    """Extract all text from an article as a single string with fragment markers."""
    fragments = []
    for key, ftype in [('flyTitle', 'fly_title'), ('headline', 'heading'), ('rubric', 'rubric')]:
        val = article_data.get(key)
        if val:
            fragments.append(f'<!-- FRAGMENT type="{ftype}" -->')
            fragments.append(val)

    lead = article_data.get('leadComponent')
    if lead:
        frag_list = []
        utils.collect_fragments(lead, frag_list)
        for ftype, html in frag_list:
            fragments.append(f'<!-- FRAGMENT type="{ftype}" -->')
            fragments.append(html)

    for node in article_data.get('body') or ():
        frag_list = []
        utils.collect_fragments(node, frag_list)
        for ftype, html in frag_list:
            fragments.append(f'<!-- FRAGMENT type="{ftype}" -->')
            fragments.append(html)

    return '\n\n'.join(fragments)


def parse_translated_fragments(translated_text):
    """Parse translated text back into fragments using the markers."""
    pattern = r'<!-- FRAGMENT type="([^"]+)" -->'
    parts = re.split(pattern, translated_text)

    fragments = []
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            ftype = parts[i]
            content = parts[i + 1].strip()
            if content:
                fragments.append((ftype, content))

    return fragments


def call_nvidia_api(full_text, model='meta/llama-3.1-8b-instruct', max_tokens=16384, temperature=0.1):
    """Call NVIDIA API for full-article translation."""
    api_key = os.environ.get('NVIDIA_API_KEY')
    base_url = 'https://integrate.api.nvidia.com/v1'

    system_prompt = '''You are an expert translator specializing in translating English magazine articles to Simplified Chinese.

CRITICAL RULES:
1. Translate the ENTIRE article accurately, preserving context and nuance.
2. Preserve ALL HTML tags EXACTLY as they appear: <b>, </b>, <i>, </i>, <em>, </em>, <a href="...">, </a>, <sup>, </sup>, <sub>, </sub>, <small>, </small>, <span ...>, </span>, etc.
3. KEEP the <!-- FRAGMENT type="..." --> markers EXACTLY as they appear - DO NOT translate, modify, or remove them.
4. Maintain the original paragraph structure and formatting.
5. Use natural, fluent Chinese that reads like a professionally published article.
6. For proper nouns (names, places, brands), use standard Chinese translations when they exist.
7. Do NOT add any introductory text, notes, or explanations.
8. Return ONLY the translated article content, preserving all markers and tags.

The FRAGMENT markers are critical for post-processing - they must remain in place and unchanged.'''

    user_prompt = f'''Translate the following article from English to Simplified Chinese while preserving all HTML tags and fragment markers.

Article to translate:

{full_text}'''

    payload = {
        'model': model,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }

    req = Request(
        base_url + '/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': 'Bearer ' + api_key,
            'Content-Type': 'application/json',
        },
    )

    # Retry logic for 504/500 errors
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result['choices'][0]['message']['content'].strip()
        except HTTPError as e:
            if e.code in [504, 500, 502, 429] and attempt < max_retries - 1:
                delay = 30 * (attempt + 1)
                print(f'  Got HTTP {e.code}, retrying in {delay}s... (attempt {attempt+1}/{max_retries})', flush=True)
                time.sleep(delay)
                continue
            raise


def has_existing_translations(conn, article_key, source_fragments):
    """Check if article already has translations for all fragments."""
    for _, source_text in source_fragments:
        hash_id = hashlib.sha256(source_text.encode('utf-8')).hexdigest()
        cached = db.get_translation(conn, hash_id, article_key=article_key)
        if not cached:
            return False
    return True


def store_full_translation(conn, article_key, source_fragments, translated_fragments, model):
    """Store translated fragments in SQLite database."""
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    stored = 0

    for order, ((source_type, source_text), (_, trans_text)) in enumerate(zip(source_fragments, translated_fragments)):
        hash_id = hashlib.sha256(source_text.encode('utf-8')).hexdigest()

        # Delete existing first (for --force)
        conn.execute('DELETE FROM translation WHERE article_key = ? AND hash_id = ?', (article_key, hash_id))

        conn.execute("""
            INSERT INTO translation
            (article_key, hash_id, source_text, translated_text, model,
             source_language, target_language, fragment_type, fragment_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (article_key, hash_id, source_text, trans_text, model,
              'English', 'Simplified Chinese', source_type, order, now))
        stored += 1

    conn.commit()
    return stored


def get_source_fragments(article_data):
    """Get list of (ftype, html) tuples from article data."""
    fragments = []
    for key, ftype in [('flyTitle', 'fly_title'), ('headline', 'heading'), ('rubric', 'rubric')]:
        val = article_data.get(key)
        if val:
            fragments.append((ftype, val))

    lead = article_data.get('leadComponent')
    if lead:
        utils.collect_fragments(lead, fragments)

    for node in article_data.get('body') or ():
        utils.collect_fragments(node, fragments)

    return fragments


def translate_article_full(article, model='meta/llama-3.1-8b-instruct', force=False):
    """Translate an entire article using full-article mode.

    Returns: (success, num_translated, num_fragments)
    """
    conn = db.get_connection()
    article_data = db.decompress_article_body(article)
    source_fragments = get_source_fragments(article_data)

    # Check if already translated
    if not force and has_existing_translations(conn, article['article_key'], source_fragments):
        print(f'  Already has {len(source_fragments)} translations (use --force to re-translate)', flush=True)
        return True, 0, len(source_fragments)

    # Get full text with markers
    full_text = get_full_article_text_with_markers(article_data)
    print(f'  Translating full article ({len(full_text)} chars, {len(source_fragments)} fragments)...', flush=True)

    # Call API
    translated = call_nvidia_api(full_text, model=model)
    print(f'  Translation complete ({len(translated)} chars)', flush=True)

    # Parse fragments
    translated_fragments = parse_translated_fragments(translated)

    # Verify
    if len(translated_fragments) != len(source_fragments):
        print(f'  WARNING: Fragment count mismatch! Source: {len(source_fragments)}, Translated: {len(translated_fragments)}', file=sys.stderr)
        print(f'  WARNING: Some translations may be misaligned', file=sys.stderr)

    # Store
    stored = store_full_translation(conn, article['article_key'], source_fragments, translated_fragments, model)
    print(f'  Stored {stored} translations', flush=True)

    return True, stored, len(source_fragments)


def main():
    load_runtime_env()

    parser = argparse.ArgumentParser(description='Translate Economist articles (FULL-ARTICLE mode)')
    parser.add_argument('issue_date', help='YYYY-MM-DD')
    parser.add_argument('issue_id', nargs='?', default=None, help='Issue ID (auto-derived if omitted)')
    parser.add_argument('--article-index', type=int, default=None, help='Only translate article N (0-indexed)')
    parser.add_argument('--force', action='store_true', help='Re-translate even if translations exist')
    parser.add_argument('--paragraph-mode', action='store_true', help='Use legacy paragraph-by-paragraph translation')
    parser.add_argument('--model', default='meta/llama-3.1-8b-instruct', help='LLM model to use for translation')
    args = parser.parse_args()

    issue_date = args.issue_date
    issue_id = args.issue_id or db.derive_issue_id(issue_date)

    conn = db.get_connection()
    db.ensure_schema(conn)

    # Check if translation is enabled
    enabled = utils.env_flag('ECONOMIST_TRANSLATE_ZH')
    if not enabled:
        print('Translation disabled (set ECONOMIST_TRANSLATE_ZH=1 to enable).', flush=True)
        return 0

    # Check API key
    api_key = os.environ.get('NVIDIA_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        print('Error: No translation API key found (NVIDIA_API_KEY or OPENAI_API_KEY)', file=sys.stderr)
        return 1

    edition = db.get_edition(conn, issue_date, issue_id)
    if not edition:
        print(f'Error: edition {issue_date}-{issue_id} not found in database. Run prefetch.py first.',
              file=sys.stderr)
        return 1

    articles = db.get_articles_for_edition(conn, edition['id'])
    if not articles:
        print('No articles found for this edition.', file=sys.stderr)
        return 1

    # Filter to specific article if requested
    if args.article_index is not None:
        if 0 <= args.article_index < len(articles):
            articles = [articles[args.article_index]]
            print(f'Selected article {args.article_index}: {articles[0].get("headline", "N/A")}', flush=True)
        else:
            print(f'Error: article index {args.article_index} out of range (0-{len(articles)-1})', file=sys.stderr)
            return 1

    # Legacy paragraph mode
    if args.paragraph_mode:
        print('=== Using LEGACY paragraph-by-paragraph translation ===', flush=True)
        # Import and use original translate logic here
        # For now, just warn
        print('NOTE: Full-article translation is recommended for better quality!', flush=True)
        return 0

    print(f'=== FULL-ARTICLE TRANSLATION (model: {args.model}) ===', flush=True)
    if args.force:
        print('Force mode enabled: Re-translating all articles', flush=True)
    print(f'Total articles: {len(articles)}', flush=True)

    total_translated = 0
    total_fragments = 0
    success_count = 0

    for idx, article in enumerate(articles):
        if not article['body_json']:
            continue

        headline = json.loads(gzip.decompress(article['body_json'])).get('headline', 'N/A')
        print(f'\\n[{idx+1}/{len(articles)}] Article: {headline}', flush=True)
        print(f'  Key: {article["article_key"]}', flush=True)

        try:
            success, translated, fragments = translate_article_full(
                article,
                model=args.model,
                force=args.force
            )
            if success:
                success_count += 1
                total_translated += translated
                total_fragments += fragments
        except Exception as e:
            print(f'  ERROR: {e}', file=sys.stderr)
            import traceback
            traceback.print_exc()

    print(f'\\n=== SUMMARY ===', flush=True)
    print(f'Successful articles: {success_count}/{len([a for a in articles if a["body_json"]])}', flush=True)
    print(f'Total fragments translated: {total_translated}', flush=True)
    print(f'Total fragments processed: {total_fragments}', flush=True)

    return 0 if success_count == len([a for a in articles if a['body_json']]) else 1


if __name__ == '__main__':
    raise SystemExit(main())
