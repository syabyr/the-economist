#!/usr/bin/env python3
"""
从 economist_content_cache 中反向查找 translate.json 中的 id 对应的原英文片段。

使用方式：
  python3 find_original_fragment.py cache_dir id
  python3 find_original_fragment.py economist_content_cache/2026-04-11-9494 001cdf7712cb4cc72c03aa5134957b908f015093e0d2b6615e3d0c01772bc5c1
"""

import json
import os
import sys
import gzip
import hashlib


def maybe_decompress(payload):
    if payload[:2] == b'\x1f\x8b':
        return gzip.decompress(payload)
    return payload


def parse_textjson(nt):
    """从 textJson 解析为 HTML 字符串"""
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

    return ''.join(''.join(parse_txt(node)) for node in nt)


def collect_fragments(node, out):
    """从文章节点递归提取所有可翻译 HTML 片段"""
    ntype = node.get('type', '')
    if ntype == 'CROSSHEAD':
        out.append(node.get('textHtml') or node.get('text') or '')
    elif ntype in {'PARAGRAPH', 'BOOK_INFO', 'PULL_QUOTE'}:
        if node.get('textHtml'):
            out.append(node.get('textHtml'))
        elif node.get('textJson'):
            out.append(parse_textjson(node['textJson']))
        else:
            out.append(node.get('text', ''))
    elif ntype == 'BLOCK_QUOTE':
        content = node.get('textHtml') or (parse_textjson(node['textJson']) if node.get('textJson') else node.get('text', ''))
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
            if item.get('textHtml'):
                out.append(item.get('textHtml'))
            elif item.get('textJson'):
                out.append(parse_textjson(item['textJson']))
            else:
                out.append(item.get('text', ''))
    elif ntype == 'INFOBOX':
        for child in node.get('components') or ():
            collect_fragments(child, out)
    elif ntype == 'INFOGRAPHIC' and node.get('fallback'):
        collect_fragments(node['fallback'], out)


def extract_article_fragments(article_json_raw):
    """从 article JSON 提取所有可翻译片段"""
    payload = json.loads(maybe_decompress(article_json_raw).decode('utf-8'))
    data = (payload.get('data') or {}).get('findArticleByUrl')
    if not data:
        return []

    fragments = []
    
    # 提取标题等
    for key in ('flyTitle', 'headline', 'rubric'):
        if data.get(key):
            fragments.append(data[key])
    
    # 提取 leadComponent
    lead = data.get('leadComponent')
    if lead:
        collect_fragments(lead, fragments)
    
    # 提取 body
    for node in data.get('body') or ():
        collect_fragments(node, fragments)
    
    return fragments


def main():
    if len(sys.argv) not in (2, 3):
        print('Usage: find_original_fragment.py cache_dir [id]', file=sys.stderr)
        print('  若指定 id，则查找该 id 对应的原文', file=sys.stderr)
        print('  若不指定 id，则列出所有 id 与原文的映射', file=sys.stderr)
        return 1

    cache_dir = sys.argv[1]
    target_id = sys.argv[2] if len(sys.argv) == 3 else None

    article_dir = os.path.join(cache_dir, 'articles')
    translate_cache_path = os.path.join(cache_dir, 'translate.json')

    # 读取翻译缓存，获取使用的模型名
    model = 'nvidia/riva-translate-4b-instruct-v1.1'  # 默认值
    if os.path.exists(translate_cache_path):
        try:
            with open(translate_cache_path, 'r', encoding='utf-8') as fh:
                # 从 translate.json 无法直接获取 model，需要从环境或代码推断
                pass
        except Exception:
            pass

    # 遍历 articles 目录，提取所有片段并计算 hash
    mapping = {}  # { hash_id: original_fragment }

    if not os.path.isdir(article_dir):
        print(f'Error: article directory not found at {article_dir}', file=sys.stderr)
        return 1

    for article_filename in sorted(os.listdir(article_dir)):
        article_path = os.path.join(article_dir, article_filename)
        if not os.path.isfile(article_path):
            continue

        try:
            with open(article_path, 'rb') as fh:
                raw = fh.read()

            fragments = extract_article_fragments(raw)

            for fragment in fragments:
                if not fragment or len(fragment.strip()) < 2:
                    continue

                # 计算 hash
                cache_key = hashlib.sha256((model + '\n' + fragment).encode('utf-8')).hexdigest()
                mapping[cache_key] = fragment

                # 如果指定了 id 且找到了，直接返回
                if target_id and cache_key == target_id:
                    print('Found match!')
                    print(f'ID:       {target_id}')
                    print(f'Original: {fragment}')
                    return 0

        except Exception as e:
            print(f'Error processing {article_filename}: {e}', file=sys.stderr)

    if target_id:
        print(f'ID {target_id} not found', file=sys.stderr)
        return 1

    # 输出所有映射（可选：保存为 JSON）
    print(f'Found {len(mapping)} fragments')
    print()
    for hash_id in sorted(mapping.keys()):
        print(f'ID: {hash_id}')
        print(f'Original: {mapping[hash_id][:100]}...' if len(mapping[hash_id]) > 100 else f'Original: {mapping[hash_id]}')
        print()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
