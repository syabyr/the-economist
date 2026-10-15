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
import sys
import time
from hashlib import sha256
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.9',
    'sec-ch-ua': '"Chromium";v="150", "Google Chrome";v="150", "Not-A.Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"macOS"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
}

# GraphQL API 专用 headers (app-style)
GRAPHQL_HEADERS = {
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

# Playwright 可选依赖（真实浏览器，用于绕过 DataDome / Cloudflare）
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
    sync_playwright = None

_PLAYWRIGHT = None  # sync_playwright 实例
_PW_BROWSER = None
_PW_CONTEXT = None


def _get_playwright_context():
    """Lazy-create a Playwright browser context with anti-detection + cookies."""
    global _PLAYWRIGHT, _PW_BROWSER, _PW_CONTEXT
    if _PW_CONTEXT is not None:
        return _PW_CONTEXT
    if not _HAS_PLAYWRIGHT:
        return None

    _PLAYWRIGHT = sync_playwright().start()

    headless = env_flag('ECONOMIST_PLAYWRIGHT_HEADLESS', False)
    _PW_BROWSER = _PLAYWRIGHT.chromium.launch(
        headless=headless,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-dev-shm-usage',
        ],
    )

    _PW_CONTEXT = _PW_BROWSER.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
        viewport={'width': 1440, 'height': 900},
        locale='en-US',
        timezone_id='America/New_York',
    )

    # 反检测：移除 webdriver 标记等
    _PW_CONTEXT.add_init_script('''
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    ''')

    # 注入 cookies
    cookie_str = os.environ.get('ECONOMIST_COOKIE', '')
    if cookie_str:
        cookies = []
        for part in cookie_str.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                cookies.append({
                    'name': k.strip(),
                    'value': v.strip(),
                    'domain': '.economist.com',
                    'path': '/',
                })
        if cookies:
            _PW_CONTEXT.add_cookies(cookies)
            print(f'Injected {len(cookies)} cookies into Playwright browser', flush=True)

    return _PW_CONTEXT


def close_playwright():
    """Close the Playwright browser (call at program exit."""
    global _PLAYWRIGHT, _PW_BROWSER, _PW_CONTEXT
    if _PW_CONTEXT:
        try:
            _PW_CONTEXT.close()
        except Exception:
            pass
        _PW_CONTEXT = None
    if _PW_BROWSER:
        try:
            _PW_BROWSER.close()
        except Exception:
            pass
        _PW_BROWSER = None
    if _PLAYWRIGHT:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
        _PLAYWRIGHT = None


def playwright_fetch_html(url, timeout=30000):
    """Fetch an HTML page using Playwright (real browser). Returns HTML string.

    Most reliable for pages protected by DataDome / Cloudflare.
    """
    ctx = _get_playwright_context()
    if ctx is None:
        raise RuntimeError('Playwright not available')

    page = ctx.new_page()
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        # 等一下让 JS 渲染完 __NEXT_DATA__
        try:
            page.wait_for_selector('#__NEXT_DATA__', state='attached', timeout=5000)
        except Exception:
            # 没有 __NEXT_DATA__ 也没关系，直接返回内容
            pass
        # 稍微等一下页面渲染
        page.wait_for_timeout(1500)
        content = page.content()
        return content
    finally:
        page.close()


# curl_cffi 可选依赖 + 全局 Session
# On macOS (esp. Python 3.14 / Darwin 27), curl_cffi's _wrapper.abi3.so fails
# to resolve the CoreFoundation symbol '_CFArrayAppendValue' unless
# CoreFoundation is already loaded into the process.  Preload it via ctypes.
try:
    if sys.platform == 'darwin':
        import ctypes
        ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
except Exception:
    pass

try:
    from curl_cffi import requests as curl_cffi_requests
    from curl_cffi.requests import Session as CurlSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False
    CurlSession = None

_CURL_SESSION = None
_CURL_IMPERSONATE = 'chrome150'


def _get_curl_session():
    """Lazy-create a global curl_cffi Session with impersonation."""
    global _CURL_SESSION
    if _CURL_SESSION is None and _HAS_CURL_CFFI:
        _CURL_SESSION = CurlSession(impersonate=_CURL_IMPERSONATE)
        # 初始加载 cookies
        cookies = _get_cookies_dict()
        if cookies:
            for k, v in cookies.items():
                _CURL_SESSION.cookies.set(k, v, domain='.economist.com')
    return _CURL_SESSION


def _sync_cookies_to_env():
    """把 session 里的 cookies 同步回 ECONOMIST_COOKIE 环境变量。"""
    sess = _CURL_SESSION
    if sess is None:
        return
    parts = []
    for k, v in sess.cookies.items():
        parts.append(f'{k}={v}')
    if parts:
        os.environ['ECONOMIST_COOKIE'] = '; '.join(parts)


def load_cookie_file(path='cookie.txt'):
    """Load cookies from a cookie.txt file into ECONOMIST_COOKIE env var.

    Supports both raw "key=value; key2=value2" format and Netscape cookies.txt format.
    """
    if not path or not os.path.exists(path):
        return 0
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read().strip()

    if not raw:
        return 0

    # 如果是 Netscape 格式（以 # 开头或包含制表符分隔的 7 列）
    lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith('#')]
    if lines and all('\t' in l for l in lines[:5]):
        # Netscape format: domain\tflag\tpath\tsecure\texpiration\tname\tvalue
        cookies = []
        for l in lines:
            parts = l.split('\t')
            if len(parts) >= 7:
                cookies.append(f'{parts[5]}={parts[6]}')
        cookie_str = '; '.join(cookies)
    else:
        # 原始 cookie header 格式
        cookie_str = raw

    os.environ['ECONOMIST_COOKIE'] = cookie_str
    # 数一下有多少个 cookie
    count = len([c for c in cookie_str.split(';') if '=' in c.strip()])
    print(f'Loaded {count} cookies from {path}', flush=True)
    return count


def _get_cookies_dict():
    """Parse ECONOMIST_COOKIE env var into a dict for requests/curl_cffi."""
    cookie_str = os.environ.get('ECONOMIST_COOKIE', '')
    if not cookie_str:
        return {}
    cookies = {}
    for part in cookie_str.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            cookies[k.strip()] = v.strip()
    return cookies


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

def fetch(url, extra_headers=None, timeout=120):
    """HTTP GET with retry/backoff. Returns raw bytes.

    Uses curl_cffi (TLS fingerprint impersonation) if available to bypass Cloudflare,
    falls back to standard urllib otherwise.
    """
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    headers = {k: v for k, v in headers.items() if v}

    attempts = env_int('ECONOMIST_FETCH_RETRIES', 5)
    base_delay = float(os.environ.get('ECONOMIST_FETCH_RETRY_DELAY', '1.5'))
    request_delay = float(os.environ.get('ECONOMIST_FETCH_DELAY', '0'))
    if request_delay > 0:
        time.sleep(request_delay)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            if _HAS_CURL_CFFI:
                sess = _get_curl_session()
                resp = sess.get(url, headers=headers, timeout=timeout)
                # 同步最新 cookies 回环境变量
                _sync_cookies_to_env()
                # 对 403 也返回 body —— 有时 Cloudflare 返回 403 但 body 是实际内容
                if resp.status_code == 403 and resp.content:
                    return resp.content
                resp.raise_for_status()
                return resp.content
            else:
                # Fallback: urllib
                cookie = os.environ.get('ECONOMIST_COOKIE', '')
                if cookie and 'Cookie' not in headers:
                    headers['Cookie'] = cookie
                req = Request(url, headers=headers)
                with urlopen(req, timeout=timeout) as resp:
                    return resp.read()
        except Exception as exc:
            last_exc = exc
            # 检查是否是不可重试的状态码
            status_code = None
            if hasattr(exc, 'code'):
                status_code = exc.code
            elif hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
                status_code = exc.response.status_code

            if status_code is not None and status_code not in RETRYABLE_HTTP_STATUS:
                raise
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
    """Fetch an HTML page, return decoded string.

    Priority:
      1. Playwright (real browser) — best for DataDome/Cloudflare-protected pages
      2. curl_cffi (TLS fingerprint impersonation) — fast, good for most pages
      3. urllib — fallback
    """
    # 优先用 Playwright（如果可用且不是简单资源请求）
    if _HAS_PLAYWRIGHT and env_flag('ECONOMIST_USE_PLAYWRIGHT', True):
        try:
            return playwright_fetch_html(url)
        except Exception as exc:
            print(f'  Playwright fetch failed, falling back: {exc}',
                  file=__import__('sys').stderr, flush=True)

    # Fallback: curl_cffi / urllib
    raw = fetch(url)
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


# ── Next.js _next/data JSON fetcher (bypasses DataDome) ──────────────────────
#
# The Economist's www.economist.com is a Next.js app.  Its pages embed the
# server-rendered data in <script id="__NEXT_DATA__">, but the HTML pages are
# gated by DataDome (a bot-detection layer that blocks curl_cffi and even
# Playwright once it fingerprints automation).
#
# However, Next.js also exposes the *same* SSR data as JSON at:
#     /_next/data/<buildId>/<page-path>.json
# This route is served from the static/CDN layer and is NOT protected by
# DataDome, so it can be fetched with a plain curl_cffi request — no browser
# required.  The paywall is enforced client-side (JS), so the full article
# body is present in the JSON even for anonymous requests.
#
# `buildId` rotates whenever the site redeploys.  We discover it by fetching a
# deliberately-bogus _next/data URL: Next.js serves its HTML 404 page, whose
# own __NEXT_DATA__ contains the current buildId.

_NEXT_BUILD_ID = None
_NEXT_BUILD_ID_TS = 0.0
_NEXT_BUILD_ID_TTL = 3600  # cache buildId for 1 hour


def _discover_build_id(timeout=30):
    """Discover and cache the current Next.js buildId.

    Returns the cached value if still fresh, otherwise refetches.  The buildId
    is read from the __NEXT_DATA__ of a bogus _next/data 404 page (which is not
    DataDome-protected).  Returns None if curl_cffi is unavailable or discovery
    fails (caller should fall back to the Playwright/HTML path).
    """
    global _NEXT_BUILD_ID, _NEXT_BUILD_ID_TS
    if _NEXT_BUILD_ID and (time.time() - _NEXT_BUILD_ID_TS) < _NEXT_BUILD_ID_TTL:
        return _NEXT_BUILD_ID
    if not _HAS_CURL_CFFI:
        return _NEXT_BUILD_ID
    sess = _get_curl_session()
    last_exc = None
    for attempt in range(3):
        try:
            resp = sess.get(
                'https://www.economist.com/_next/data/x/discover-build-id.json',
                headers=HEADERS, timeout=timeout,
            )
            _sync_cookies_to_env()
            m = re.search(r'"buildId":"([^"]+)"', resp.text)
            if m:
                _NEXT_BUILD_ID = m.group(1)
                _NEXT_BUILD_ID_TS = time.time()
                print(f'Next.js buildId: {_NEXT_BUILD_ID}', flush=True)
                return _NEXT_BUILD_ID
            last_exc = RuntimeError('buildId not found in 404 page')
        except Exception as exc:
            last_exc = exc
        time.sleep(2 * attempt + 1)
    print(f'buildId discovery failed: {last_exc}', file=__import__('sys').stderr, flush=True)
    return _NEXT_BUILD_ID


def _invalidate_build_id():
    """Force the next _discover_build_id() call to refetch."""
    global _NEXT_BUILD_ID, _NEXT_BUILD_ID_TS
    _NEXT_BUILD_ID = None
    _NEXT_BUILD_ID_TS = 0.0


def fetch_next_data(path, timeout=30):
    """Fetch Next.js _next/data JSON for a page path. Returns pageProps dict.

    Bypasses DataDome entirely.  The returned pageProps contains `content`
    (the edition or article data) in the same shape that __NEXT_DATA__ scraping
    produced, so downstream consumers are unchanged.

    Args:
        path: page path without leading/trailing slash, e.g.
              'weeklyedition/2026-10-10' or 'leaders/2026/10/14/some-slug'.
    Raises:
        RuntimeError if the data cannot be fetched after a buildId refresh.
    """
    build_id = _discover_build_id(timeout=timeout)
    if not build_id:
        raise RuntimeError('could not discover Next.js buildId (curl_cffi unavailable?)')
    path = path.strip('/')
    last_exc = None
    for attempt in range(2):
        url = f'https://www.economist.com/_next/data/{build_id}/{path}.json'
        try:
            sess = _get_curl_session()
            resp = sess.get(url, headers=HEADERS, timeout=timeout)
            _sync_cookies_to_env()
            text = resp.text
            # A valid _next/data response is JSON.  When buildId has rotated,
            # Next.js serves its HTML 404 page instead (starts with '<').
            if text.lstrip().startswith('{'):
                data = json.loads(text)
                # The _next/data JSON route returns pageProps at the top level
                # (unlike the __NEXT_DATA__ embedded in HTML, which wraps it
                # under props.pageProps).  Handle both shapes.
                return data.get('pageProps') or (data.get('props') or {}).get('pageProps') or {}
        except Exception as exc:
            last_exc = exc
        # Either non-JSON (buildId rotated) or an exception — rediscover once.
        _invalidate_build_id()
        build_id = _discover_build_id(timeout=timeout)
        if not build_id:
            break
    if last_exc:
        raise RuntimeError(f'_next/data fetch failed for {path}: {last_exc}') from last_exc
    raise RuntimeError(f'_next/data fetch failed for {path}')


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
