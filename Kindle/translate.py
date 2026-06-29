#!/usr/bin/env python3
"""Phase 2: Extract translatable fragments from cached articles → translate → SQLite.

Usage:
    python3 translate.py YYYY-MM-DD [ISSUE_ID]

Translation is only performed when ECONOMIST_TRANSLATE_ZH=1.
Set ECONOMIST_CACHE_ONLY=1 to skip API calls (only check cache).
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


def _load_runtime_env():
    """Load .env variables for direct script execution without shell sourcing."""
    cwd_env = os.path.join(os.getcwd(), '.env')
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    loaded = utils.load_dotenv(cwd_env)
    if script_env != cwd_env:
        loaded += utils.load_dotenv(script_env)
    return loaded


class SqliteTranslator:
    """Translation engine that reads/writes the `translation` SQLite table."""

    def __init__(self, conn):
        self.conn = conn
        self.enabled = utils.env_flag('ECONOMIST_TRANSLATE_ZH')
        self.provider = self._resolve_provider()
        self.source_language = os.environ.get('ECONOMIST_TRANSLATE_SOURCE_LANGUAGE', 'English')
        self.target_language = os.environ.get('ECONOMIST_TRANSLATE_TARGET_LANGUAGE', 'Simplified Chinese')
        self.base_url = self._get_base_url()
        self.api_key = self._get_api_key()
        self.model = os.environ.get('ECONOMIST_TRANSLATE_MODEL', self._default_model())
        self.timeout = int(os.environ.get('ECONOMIST_TRANSLATE_TIMEOUT', '120'))
        self.retries = int(os.environ.get('ECONOMIST_TRANSLATE_RETRIES', '8'))
        self.retry_delay = float(os.environ.get('ECONOMIST_TRANSLATE_RETRY_DELAY', '2'))
        self._cache_hits = 0
        self._api_calls = 0
        self._failures = 0

    def _resolve_provider(self):
        configured = (os.environ.get('ECONOMIST_TRANSLATE_PROVIDER') or '').strip().lower()
        if configured:
            return configured

        nvidia_key = (os.environ.get('NVIDIA_API_KEY') or '').strip()
        openai_key = (os.environ.get('OPENAI_API_KEY') or '').strip()
        if nvidia_key and (not openai_key or nvidia_key.startswith('nvapi-')):
            return 'nvidia'
        return 'openai'

    def _get_base_url(self):
        explicit = os.environ.get('ECONOMIST_TRANSLATE_BASE_URL')
        if explicit:
            return explicit.rstrip('/')
        if self.provider == 'nvidia':
            return 'https://integrate.api.nvidia.com/v1'
        return (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').rstrip('/')

    def _get_api_key(self):
        return (os.environ.get('ECONOMIST_TRANSLATE_API_KEY')
                or os.environ.get('NVIDIA_API_KEY')
                or os.environ.get('OPENAI_API_KEY')
                or '')

    def _default_model(self):
        if self.provider == 'nvidia':
            return 'nvidia/riva-translate-4b-instruct-v1.1'
        return 'gpt-4.1-mini'

    def _system_prompt(self):
        dropcap_rule = (
            'If a fragment begins with a decorative drop cap pattern like '
            '<b>L</b><span ...>argely because</span>, treat it as one phrase. '
            'Do not keep the Latin initial letter untranslated. Translate the combined '
            'meaning naturally in Chinese while preserving valid inline HTML structure.'
        )
        if self.provider == 'nvidia':
            return (
                f'You are an expert at translating HTML fragments from '
                f'{self.source_language} to {self.target_language}. '
                'Preserve all inline HTML tags, links, emphasis, superscripts, '
                'subscripts, and text order. '
                + dropcap_rule + ' '
                'Return only the translated HTML fragment.'
            )
        return (
            f'Translate the provided HTML fragment into {self.target_language}. '
            'Preserve inline HTML tags, links, emphasis, superscripts, subscripts, '
            'and text order. '
            + dropcap_rule + ' '
            'Return only the translated HTML fragment.'
        )

    @staticmethod
    def _has_drop_cap_pattern(fragment):
        return bool(re.match(r'^\s*<b>[A-Za-z]</b>\s*<span\b[^>]*>', fragment or ''))

    @staticmethod
    def _looks_untranslated_drop_cap(translated):
        return bool(re.match(r'^\s*<b>[A-Za-z]</b>', translated or ''))

    def _normalize_drop_cap_translation(self, fragment, translated_text):
        if not self._has_drop_cap_pattern(fragment):
            return translated_text
        if not self._looks_untranslated_drop_cap(translated_text):
            return translated_text
        # Some models keep the decorative Latin initial; strip it to keep a natural Chinese phrase.
        return re.sub(r'^\s*<b>[A-Za-z]</b>\s*', '', translated_text or '', count=1)

    def _should_retranslate_drop_cap(self, fragment, translated_text):
        return self._has_drop_cap_pattern(fragment) and self._looks_untranslated_drop_cap(translated_text)

    def _user_prompt(self, fragment):
        if self.provider == 'nvidia':
            return (
                f'What is the {self.target_language} translation of the following HTML '
                f'fragment from {self.source_language}?\n\n{fragment}'
            )
        return fragment

    def translate_html(self, article_key, fragment, fragment_type='paragraph', fragment_order=0):
        """Look up or translate a single HTML fragment.  Returns '' if disabled/skipped."""
        if not self.enabled:
            return ''
        source_text = utils.strip_html(fragment)
        if len(source_text) < 2:
            return ''

        hash_id = hashlib.sha256(fragment.encode('utf-8')).hexdigest()
        cached = db.get_translation(self.conn, hash_id, article_key=article_key)
        if cached:
            # If this translation belongs to a different article_key,
            # copy it to the current article_key so build_issue.py can find it.
            if cached['article_key'] != article_key:
                db.upsert_translation(
                    self.conn, article_key, hash_id, fragment, cached['translated_text'],
                    model=cached.get('model') or self.model,
                    source_language=cached.get('source_language') or self.source_language,
                    target_language=cached.get('target_language') or self.target_language,
                    fragment_type=fragment_type,
                    fragment_order=fragment_order,
                )
            normalized = self._normalize_drop_cap_translation(fragment, cached['translated_text'])
            if normalized != cached['translated_text']:
                db.upsert_translation(
                    self.conn, article_key, hash_id, fragment, normalized,
                    model=cached.get('model') or self.model,
                    source_language=cached.get('source_language') or self.source_language,
                    target_language=cached.get('target_language') or self.target_language,
                    fragment_type=fragment_type,
                    fragment_order=fragment_order,
                )
                self._cache_hits += 1
                return normalized
            if not self._should_retranslate_drop_cap(fragment, cached['translated_text']):
                self._cache_hits += 1
                return cached['translated_text']

        shared_cached = db.get_translation(self.conn, hash_id)
        if shared_cached:
            normalized = self._normalize_drop_cap_translation(fragment, shared_cached['translated_text'])
            if normalized != shared_cached['translated_text']:
                db.upsert_translation(
                    self.conn, article_key, hash_id, fragment, normalized,
                    model=shared_cached.get('model') or self.model,
                    source_language=shared_cached.get('source_language') or self.source_language,
                    target_language=shared_cached.get('target_language') or self.target_language,
                    fragment_type=fragment_type,
                    fragment_order=fragment_order,
                )
                self._cache_hits += 1
                return normalized
            if not self._should_retranslate_drop_cap(fragment, shared_cached['translated_text']):
                db.upsert_translation(
                    self.conn, article_key, hash_id, fragment, shared_cached['translated_text'],
                    model=shared_cached.get('model') or self.model,
                    source_language=shared_cached.get('source_language') or self.source_language,
                    target_language=shared_cached.get('target_language') or self.target_language,
                    fragment_type=fragment_type,
                    fragment_order=fragment_order,
                )
                self._cache_hits += 1
                return shared_cached['translated_text']

        if not self.api_key:
            raise RuntimeError(
                'ECONOMIST_TRANSLATE_ZH is enabled but no translation API key is set.'
            )

        if utils.env_flag('ECONOMIST_CACHE_ONLY'):
            return ''

        translated = self._normalize_drop_cap_translation(fragment, self._call_api(fragment))
        db.upsert_translation(
            self.conn, article_key, hash_id, fragment, translated,
            model=self.model,
            source_language=self.source_language,
            target_language=self.target_language,
            fragment_type=fragment_type,
            fragment_order=fragment_order,
        )
        self._api_calls += 1
        return translated

    def _call_api(self, fragment):
        payload = {
            'model': self.model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': self._system_prompt()},
                {'role': 'user', 'content': self._user_prompt(fragment)},
            ],
        }
        req = Request(
            self.base_url + '/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': 'Bearer ' + self.api_key,
                'Content-Type': 'application/json',
            },
        )
        raw = self._request_with_retry(req)
        return raw['choices'][0]['message']['content'].strip()

    def _request_with_retry(self, req):
        attempts = max(1, self.retries)
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except HTTPError as exc:
                if exc.code < 500 and exc.code != 429:
                    raise
                last_error = exc
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt == attempts:
                raise last_error
            delay = self.retry_delay * attempt
            print(
                f'Translation retry {attempt}/{attempts}: {last_error}; waiting {delay:g}s',
                file=sys.stderr, flush=True,
            )
            time.sleep(delay)

    @property
    def stats(self):
        return {'cache_hits': self._cache_hits, 'api_calls': self._api_calls,
                'failures': self._failures}


def extract_fragments_from_article(article_data):
    """Extract all translatable fragments from a decompressed article dict.

    Returns list of (fragment_type, html_text) tuples in document order.
    """
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


def main():
    _load_runtime_env()

    parser = argparse.ArgumentParser(description='Translate cached Economist articles')
    parser.add_argument('issue_date', help='YYYY-MM-DD')
    parser.add_argument('issue_id', nargs='?', default=None, help='Issue ID (auto-derived if omitted)')
    args = parser.parse_args()

    issue_date = args.issue_date
    issue_id = args.issue_id or db.derive_issue_id(issue_date)

    conn = db.get_connection()
    db.ensure_schema(conn)

    edition = db.get_edition(conn, issue_date, issue_id)
    if not edition:
        print(f'Error: edition {issue_date}-{issue_id} not found in database. Run prefetch.py first.',
              file=sys.stderr)
        return 1

    articles = db.get_articles_for_edition(conn, edition['id'])
    if not articles:
        print('No articles found for this edition.', file=sys.stderr)
        return 1

    translator = SqliteTranslator(conn)
    if not translator.enabled:
        print('Translation disabled (set ECONOMIST_TRANSLATE_ZH=1 to enable).', flush=True)
        return 0

    total_fragments = 0
    translated_articles = 0

    for article in articles:
        if not article['body_json']:
            continue

        try:
            article_data = db.decompress_article_body(article)
        except Exception as exc:
            print(f'  ERROR decompressing article {article["id"]}: {exc}', file=sys.stderr)
            continue

        fragments = extract_fragments_from_article(article_data)
        if not fragments:
            continue

        article_had_new = False
        for order, (ftype, html_text) in enumerate(fragments):
            if not html_text:
                continue
            total_fragments += 1
            before_hits = translator._cache_hits
            translator.translate_html(article['article_key'], html_text,
                                      fragment_type=ftype, fragment_order=order)
            if translator._cache_hits == before_hits and translator._api_calls > 0:
                article_had_new = True

        if article_had_new:
            translated_articles += 1
        print(f'  article {article["id"]}: {len(fragments)} fragments', flush=True)

    stats = translator.stats
    print(f'\nDone: {len(articles)} articles, {total_fragments} fragments, '
          f'{stats["cache_hits"]} cache hits, {stats["api_calls"]} new translations',
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
