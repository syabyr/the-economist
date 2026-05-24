#!/usr/bin/env python3
"""Shared utilities for the Economist cache system.

Functions extracted from prefetch_issue.py, translate_issue_cache.py,
find_original_fragment.py, and download_audio.py.
"""

import gzip
import json
import os
import re
import shlex
import socket
import ssl
import time
from hashlib import sha256
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

HEADERS = {
    'User-Agent': 'TheEconomist-Liskov-android',
    'accept': 'multipart/mixed; deferSpec=20220824, application/json',
    'accept-encoding': 'gzip',
    'content-type': 'application/json',
    'x-economist-consumer': 'TheEconomist-Liskov-android',
    'x-teg-client-name': 'Economist-Android',
    'x-teg-client-os': 'Android',
    'x-teg-client-version': '4.40.0',
}

RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_dotenv(path='.env', override=False):
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Supports lines in either form:
      KEY=value
      export KEY=value
    """
    if not path or not os.path.exists(path):
        return 0

    loaded = 0
    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export '):].strip()
            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                continue

            value = value.strip()
            try:
                if value and (value[0] == value[-1]) and value[0] in {'"', "'"}:
                    value = shlex.split(value)[0]
            except Exception:
                pass

            if not override and key in os.environ:
                continue
            os.environ[key] = value
            loaded += 1

    return loaded


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def maybe_decompress(payload):
    if payload[:2] == b'\x1f\x8b':
        return gzip.decompress(payload)
    return payload


def slugify(value, max_len=90):
    value = re.sub(r'[^A-Za-z0-9]+', '-', value or '').strip('-')
    value = re.sub(r'-+', '-', value)
    return (value or 'audio')[:max_len].strip('-') or 'audio'


def extension_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext and re.fullmatch(r'\.[a-z0-9]{1,8}', ext):
        return ext
    return '.bin'


def strip_html(fragment):
    text = re.sub(r'<[^>]+>', ' ', fragment or '')
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def normalize_text_key(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def keys_match(left, right):
    a = normalize_text_key(left)
    b = normalize_text_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.replace('and', '') == b.replace('and', '')


def canonical_article_url(url):
    if not url:
        return ''
    if url.startswith('/'):
        url = 'https://www.economist.com' + url
    if url.endswith('/print'):
        url = url.rpartition('/')[0]
    return url


# ── HTTP fetch ──────────────────────────────────────────────────────────────

def fetch(url, extra_headers=None):
    """HTTP GET with retry/backoff. Returns raw bytes."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    headers = {k: v for k, v in headers.items() if v}
    headers['x-app-trace-id'] = str(uuid4())

    req = Request(url, headers=headers)
    attempts = env_int('ECONOMIST_FETCH_RETRIES', 5)
    base_delay = float(os.environ.get('ECONOMIST_FETCH_RETRY_DELAY', '1.5'))
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=120) as resp:
                return resp.read()
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt == attempts:
                raise
        except (URLError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            last_exc = exc
            if attempt == attempts:
                raise
        delay = base_delay * attempt
        print(
            f'retry {attempt}/{attempts} for {url}: {last_exc}',
            file=__import__('sys').stderr,
            flush=True,
        )
        time.sleep(delay)
    raise last_exc


def fetch_html(url):
    """Fetch an HTML page, return decoded string."""
    raw = fetch(
        url,
        {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'content-type': '',
        },
    )
    return maybe_decompress(raw).decode('utf-8', 'replace')


def parse_next_data(html):
    """Extract __NEXT_DATA__ JSON from an HTML page."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        flags=re.S,
    )
    if not match:
        raise ValueError('missing __NEXT_DATA__')
    return json.loads(unescape(match.group(1)))


# ── textJson → HTML rendering ───────────────────────────────────────────────

def parse_txt(ty):
    """Render a single textJson node tree to HTML string list."""
    typ = ty.get('type', '')
    children = ty.get('children', [])
    href = '#'
    for attr in ty.get('attributes') or ():
        if attr.get('name') == 'href':
            href = attr.get('value', href)
            break

    if typ == 'text':
        return [ty.get('value', '')]
    if typ == 'scaps':
        return [
            f'<span style="text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.05em;">'
            f'{"".join(parse_txt(c))}</span>'
            for c in children
        ]
    if typ in {'bold', 'drop_caps'}:
        return [f'<b>{"".join(parse_txt(c))}</b>' for c in children]
    if typ == 'italic':
        return [f'<i>{"".join(parse_txt(c))}</i>' for c in children]
    if typ == 'linebreak':
        return ['<br>']
    if typ in {'external_link', 'internal_link'}:
        return [f'<a href="{href}">{"".join(parse_txt(c))}</a>' for c in children]
    if typ == 'ufinish':
        return [text for c in children for text in parse_txt(c)]
    if typ == 'subscript':
        return [f'<sub>{"".join(parse_txt(c))}</sub>' for c in children]
    if typ == 'superscript':
        return [f'<sup>{"".join(parse_txt(c))}</sup>' for c in children]
    return []


def parse_textjson(nt):
    """Render an array of textJson nodes to a single HTML string."""
    return ''.join(''.join(parse_txt(node)) for node in nt)


# ── Fragment collection for translation ─────────────────────────────────────

def collect_fragments(node, out):
    """Walk an article body node and append translatable HTML fragments to `out`.

    Each fragment is a plain HTML string (with inline tags preserved).
    Callers should pair each fragment with a fragment_type label.
    """
    ntype = node.get('type', '')
    if ntype == 'CROSSHEAD':
        out.append(('crosshead', node.get('textHtml') or node.get('text') or ''))
    elif ntype in {'PARAGRAPH', 'BOOK_INFO', 'PULL_QUOTE'}:
        html = (
            node.get('textHtml')
            or (parse_textjson(node['textJson']) if node.get('textJson') else node.get('text', ''))
        )
        out.append(('paragraph' if ntype == 'PARAGRAPH' else
                    'pull_quote' if ntype == 'PULL_QUOTE' else 'paragraph', html))
    elif ntype == 'BLOCK_QUOTE':
        content = (
            node.get('textHtml')
            or (parse_textjson(node['textJson']) if node.get('textJson') else node.get('text', ''))
        )
        out.append(('block_quote', f'<i>{content}</i>'))
    elif ntype == 'IMAGE' or node.get('__typename', '') == 'ImageComponent':
        caption = node.get('caption') or {}
        if caption.get('textHtml'):
            out.append(('caption', caption['textHtml']))
        elif caption.get('textJson'):
            out.append(('caption', parse_textjson(caption['textJson'])))
        elif caption.get('text'):
            out.append(('caption', caption['text']))
    elif ntype in {'ORDERED_LIST', 'UNORDERED_LIST'}:
        for item in node.get('items') or ():
            html = (
                item.get('textHtml')
                or (parse_textjson(item['textJson']) if item.get('textJson') else item.get('text', ''))
            )
            out.append(('list_item', html))
    elif ntype == 'INFOBOX':
        for child in node.get('components') or ():
            collect_fragments(child, out)
    elif ntype == 'INFOGRAPHIC' and node.get('fallback'):
        collect_fragments(node['fallback'], out)


# ── Image / audio collection ────────────────────────────────────────────────

def collect_image_urls(node, out):
    """Recursively collect image URLs from an article node tree into `out` set."""
    if not isinstance(node, dict):
        return
    ntype = node.get('type', '')
    if (ntype == 'IMAGE') or (node.get('__typename') == 'ImageComponent'):
        url = node.get('url')
        if url:
            out.add(url)
    if ntype == 'INFOGRAPHIC' and isinstance(node.get('fallback'), dict):
        collect_image_urls(node['fallback'], out)
    for value in node.values():
        if isinstance(value, dict):
            collect_image_urls(value, out)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    collect_image_urls(item, out)


def collect_image_urls_ordered(node, out, seen=None):
    """Collect image URLs in traversal order, deduplicated.

    Args:
        node: article node dict/list tree
        out: list to append discovered image URLs
        seen: set used for deduplication (optional)
    """
    if seen is None:
        seen = set()

    if not isinstance(node, dict):
        return

    ntype = node.get('type', '')
    if (ntype == 'IMAGE') or (node.get('__typename') == 'ImageComponent'):
        url = node.get('url')
        if url and url not in seen:
            seen.add(url)
            out.append(url)

    if ntype == 'INFOGRAPHIC' and isinstance(node.get('fallback'), dict):
        collect_image_urls_ordered(node['fallback'], out, seen)

    for value in node.values():
        if isinstance(value, dict):
            collect_image_urls_ordered(value, out, seen)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    collect_image_urls_ordered(item, out, seen)


def collect_audio_links(article):
    """Extract narration and podcast audio links from an article dict."""
    links = []
    narration = article.get('narration') or {}
    narration_url = narration.get('url') or ''
    if narration_url:
        links.append({
            'type': 'narration',
            'url': narration_url,
            'duration': narration.get('duration'),
            'filename': narration.get('filename'),
            'provider': narration.get('provider'),
            'is_ai_generated': narration.get('isAiGenerated'),
            'file_hash': narration.get('fileHash'),
        })
    podcast_audio = ((article.get('podcast') or {}).get('audio') or {})
    podcast_url = podcast_audio.get('url') or ''
    if podcast_url:
        links.append({
            'type': 'podcast',
            'url': podcast_url,
            'duration': podcast_audio.get('durationInSeconds'),
        })
    return links


# ── File helpers ────────────────────────────────────────────────────────────

def write_file_atomic(path, payload):
    """Write bytes to path atomically (tmp + rename)."""
    tmp_path = path + '.tmp'
    with open(tmp_path, 'wb') as fh:
        fh.write(payload)
    os.replace(tmp_path, path)


def image_hash_filename(url):
    """Return 'sha256hex.ext' for an image URL (no directory)."""
    ext = os.path.splitext(urlparse(url).path)[1]
    if not ext or not re.fullmatch(r'\.[a-z0-9]{1,8}', ext.lower()):
        ext = '.jpg'
    return sha256(url.encode('utf-8')).hexdigest() + ext
