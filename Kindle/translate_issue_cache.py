#!/usr/bin/env python3
import gzip
import hashlib
import json
import os
import re
import sys
from html import unescape
from urllib.request import Request, urlopen


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def strip_html(fragment):
    text = re.sub(r'<[^>]+>', ' ', fragment or '')
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def maybe_decompress(payload):
    if payload[:2] == b'\x1f\x8b':
        return gzip.decompress(payload)
    return payload


def parse_txt(ty):
    typ = ty.get('type', '')
    children = ty.get('children', [])
    href = '#'
    attributes = ty.get('attributes') or ()
    for attr in attributes:
        if attr.get('name') == 'href':
            href = attr.get('value', href)
            break

    if typ == 'text':
        return [ty.get('value', '')]
    if typ == 'scaps':
        return [
            f'<span style="text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.05em;">{"".join(parse_txt(c))}</span>'
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
    return ''.join(''.join(parse_txt(node)) for node in nt)


class Translator:
    def __init__(self):
        self.enabled = env_flag('ECONOMIST_TRANSLATE_ZH')
        self.provider = os.environ.get('ECONOMIST_TRANSLATE_PROVIDER', 'openai').strip().lower()
        self.source_language = os.environ.get('ECONOMIST_TRANSLATE_SOURCE_LANGUAGE', 'English')
        self.target_language = os.environ.get(
            'ECONOMIST_TRANSLATE_TARGET_LANGUAGE', 'Simplified Chinese'
        )
        self.base_url = self.get_base_url()
        self.api_key = self.get_api_key()
        self.model = os.environ.get('ECONOMIST_TRANSLATE_MODEL', self.default_model())
        self.timeout = int(os.environ.get('ECONOMIST_TRANSLATE_TIMEOUT', '120'))
        self.cache_path = os.environ['ECONOMIST_TRANSLATE_CACHE']
        self.cache = self.load_cache()

    def get_base_url(self):
        explicit = os.environ.get('ECONOMIST_TRANSLATE_BASE_URL')
        if explicit:
            return explicit.rstrip('/')
        if self.provider == 'nvidia':
            return 'https://integrate.api.nvidia.com/v1'
        return (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').rstrip('/')

    def get_api_key(self):
        return (
            os.environ.get('ECONOMIST_TRANSLATE_API_KEY')
            or os.environ.get('NVIDIA_API_KEY')
            or os.environ.get('OPENAI_API_KEY')
            or ''
        )

    def default_model(self):
        if self.provider == 'nvidia':
            return 'nvidia/riva-translate-4b-instruct-v1.1'
        return 'gpt-4.1-mini'

    def system_prompt(self):
        if self.provider == 'nvidia':
            return (
                f'You are an expert at translating HTML fragments from '
                f'{self.source_language} to {self.target_language}. '
                'Preserve all inline HTML tags, links, emphasis, superscripts, '
                'subscripts, and text order. Return only the translated HTML fragment.'
            )
        return (
            f'Translate the provided HTML fragment into {self.target_language}. '
            'Preserve inline HTML tags, links, emphasis, superscripts, subscripts, '
            'and text order. Return only the translated HTML fragment.'
        )

    def user_prompt(self, fragment):
        if self.provider == 'nvidia':
            return (
                f'What is the {self.target_language} translation of the following HTML '
                f'fragment from {self.source_language}?\n\n{fragment}'
            )
        return fragment

    def load_cache(self):
        try:
            with open(self.cache_path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_cache(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, 'w', encoding='utf-8') as fh:
            json.dump(self.cache, fh, ensure_ascii=False, indent=2, sort_keys=True)

    def translate_html(self, fragment):
        if not self.enabled:
            return ''
        source_text = strip_html(fragment)
        if len(source_text) < 2:
            return ''
        cache_key = hashlib.sha256((self.model + '\n' + fragment).encode('utf-8')).hexdigest()
        if cache_key in self.cache:
            return self.cache[cache_key]
        payload = {
            'model': self.model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': self.system_prompt()},
                {'role': 'user', 'content': self.user_prompt(fragment)},
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
        with urlopen(req, timeout=self.timeout) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        translated = raw['choices'][0]['message']['content'].strip()
        self.cache[cache_key] = translated
        self.save_cache()
        return translated


def collect_fragments(node, out):
    ntype = node.get('type', '')
    if ntype == 'CROSSHEAD':
        out.append(node.get('textHtml') or node.get('text') or '')
    elif ntype in {'PARAGRAPH', 'BOOK_INFO', 'PULL_QUOTE'}:
        out.append(node.get('textHtml') or parse_textjson(node['textJson']) if node.get('textJson') else node.get('text', ''))
    elif ntype == 'BLOCK_QUOTE':
        content = node.get('textHtml') or parse_textjson(node['textJson']) if node.get('textJson') else node.get('text', '')
        out.append(f'<i>{content}</i>')
    elif ntype == 'IMAGE' or node.get('__typename', '') == 'ImageComponent':
        caption = node.get('caption') or {}
        if caption.get('textHtml'):
            out.append(caption['textHtml'])
        elif caption.get('textJson'):
            out.append(parse_textjson(caption['textJson']))
        elif caption.get('text'):
            out.append(caption['text'])
    elif ntype in {'ORDERED_LIST', 'UNORDERED_LIST'}:
        for item in node.get('items') or ():
            out.append(item.get('textHtml') or parse_textjson(item['textJson']) if item.get('textJson') else item.get('text', ''))
    elif ntype == 'INFOBOX':
        for child in node.get('components') or ():
            collect_fragments(child, out)
    elif ntype == 'INFOGRAPHIC' and node.get('fallback'):
        collect_fragments(node['fallback'], out)


def extract_article_data(raw):
    payload = json.loads(maybe_decompress(raw).decode('utf-8'))
    data = (payload.get('data') or {}).get('findArticleByUrl')
    return data or {}


def main():
    if len(sys.argv) != 2:
        print('Usage: translate_issue_cache.py CACHE_DIR', file=sys.stderr)
        return 1

    cache_dir = sys.argv[1]
    article_dir = os.path.join(cache_dir, 'articles')
    if not os.path.isdir(article_dir):
        print('Missing article cache at ' + article_dir, file=sys.stderr)
        return 1

    translator = Translator()
    for name in sorted(os.listdir(article_dir)):
        path = os.path.join(article_dir, name)
        if not os.path.isfile(path):
            continue
        data = extract_article_data(open(path, 'rb').read())
        fragments = []
        for key in ('flyTitle', 'headline', 'rubric'):
            if data.get(key):
                fragments.append(data[key])
        lead = data.get('leadComponent')
        if lead:
            collect_fragments(lead, fragments)
        for node in data.get('body') or ():
            collect_fragments(node, fragments)
        for fragment in fragments:
            if fragment:
                translator.translate_html(fragment)
        print(name, flush=True)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
