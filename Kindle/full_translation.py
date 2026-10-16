#!/usr/bin/env python3
"""
Full Article Translation Module

This module implements:
1. Full-article translation instead of paragraph-by-paragraph
2. Better context preservation with the full article
3. HTML fragment marker parsing to split translation back
4. SQLite integration for storing full-article translations

NVIDIA LLM Context Windows:
- meta/llama-3.3-70b-instruct: 128K tokens
- nvidia/llama-3.1-nemotron-70b-instruct: 128K tokens
- nvidia/nemotron-4-340b-instruct: 128K tokens

An average Economist article is ~2000-5000 tokens,
so full-article translation is well within limits.
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
    """Load environment variables from .env files."""
    cwd_env = os.path.join(os.getcwd(), '.env')
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

    # Parse .env files, handling 'export' prefix
    for env_file in [cwd_env, script_env]:
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Handle 'export KEY=value' format
                        if line.startswith('export '):
                            line = line.replace('export ', '', 1)
                        if '=' in line:
                            key, value = line.split('=', 1)
                            os.environ[key] = value


def call_nvidia_api(full_text, model='meta/llama-3.1-70b-instruct', max_tokens=8192, temperature=0.1):
    """Call NVIDIA API for full-article translation."""
    api_key = os.environ.get('NVIDIA_API_KEY')
    base_url = 'https://integrate.api.nvidia.com/v1'

    system_prompt = '''You are an expert translator specializing in translating English magazine articles to Simplified Chinese.

CRITICAL RULES (VIOLATING WILL RESULT IN POOR QUALITY):
1. Translate the ENTIRE article accurately, preserving context and nuance.
2. Preserve ALL HTML tags EXACTLY as they appear: <b>, </b>, <i>, </i>, <em>, </em>, <a href="...">, </a>, <sup>, </sup>, <sub>, </sub>, <small>, </small>, <span ...>, </span>, etc.
3. KEEP the <!-- FRAGMENT type="..." --> markers EXACTLY as they appear - DO NOT translate, modify, or remove them.
4. Maintain the original paragraph structure and formatting.
5. Use natural, fluent Chinese that reads like a professionally published article in a reputable Chinese magazine.
6. For proper nouns (names, places, brands), use standard Chinese translations when they exist.
7. Do NOT add any introductory text, notes, explanations, or disclaimers.
8. Return ONLY the translated article content, preserving all markers and tags.

The FRAGMENT markers are critical for post-processing - they must remain in place and unchanged.
Your translation will be automatically parsed using these markers, so their integrity is essential.'''

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

    with urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read().decode('utf-8'))
        return result['choices'][0]['message']['content'].strip()


def get_full_article_text_with_markers(article_data):
    """
    Extract all text from an article as a single string with fragment markers.

    The markers use HTML comment format to be invisible when rendered,
    and provide a way to split the translated content back into fragments.

    Format: <!-- FRAGMENT type="paragraph" -->
    """
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
    """
    Parse translated text back into fragments using the markers.

    Returns list of (fragment_type, translated_html) tuples.
    """
    # Pattern to match fragment markers
    # Matches: <!-- FRAGMENT type="..." -->
    pattern = r'<!-- FRAGMENT type="([^"]+)" -->'

    parts = re.split(pattern, translated_text)

    fragments = []
    # parts[0] is anything before first marker (should be empty or whitespace)
    # Then pattern is: type, content, type, content, ...
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            ftype = parts[i]
            content = parts[i + 1].strip()
            if content:
                fragments.append((ftype, content))

    return fragments


def translate_article_full(article_key, article_data, model='meta/llama-3.3-70b-instruct', use_cache=True):
    """
    Translate an entire article at once for better context preservation.

    Args:
        article_key: Unique article identifier
        article_data: Decompressed article JSON data
        model: LLM model to use
        use_cache: Whether to check for cached full-article translations

    Returns:
        List of (fragment_type, translated_html) tuples
    """
    # Get full text with markers
    full_text = get_full_article_text_with_markers(article_data)

    # Check for cached full-article translation (using article_key + 'FULL' as marker)
    if use_cache:
        full_hash = hashlib.sha256(full_text.encode('utf-8')).hexdigest()
        cached = db.get_translation(db.get_connection(), full_hash, article_key=article_key + 'FULL')
        if cached:
            print(f'  Using cached full-article translation', flush=True)
            return parse_translated_fragments(cached['translated_text'])

    # Call API
    print(f'  Translating full article ({len(full_text)} chars)...', flush=True)
    translated = call_nvidia_api(full_text, model=model)
    print(f'  Translation complete ({len(translated)} chars)', flush=True)

    # Parse fragments
    fragments = parse_translated_fragments(translated)

    # Store the full translated text with fragments for cache and reference
    full_hash = hashlib.sha256(full_text.encode('utf-8')).hexdigest()
    conn = db.get_connection()
    now = datetime_now()
    try:
        conn.execute("""
            INSERT INTO translation
            (article_key, hash_id, source_text, translated_text, model,
             source_language, target_language, fragment_type, fragment_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (article_key + 'FULL', full_hash, full_text, translated, model,
              'English', 'Simplified Chinese', 'full_article', 0, now))
        conn.commit()
    except Exception:
        # Ignore if already exists
        conn.rollback()

    return fragments


def datetime_now():
    """Get current timestamp string."""
    return time.strftime('%Y-%m-%d %H:%M:%S')


def store_full_translation_fragments(article_key, source_fragments, translated_fragments, model):
    """
    Store translated fragments in SQLite database.

    Args:
        article_key: Unique article identifier
        source_fragments: List of (ftype, original_html) tuples
        translated_fragments: List of (ftype, translated_html) tuples
        model: Model used for translation
    """
    conn = db.get_connection()
    now = datetime_now()

    # Create a mapping for translated fragments by type
    # Note: There could be multiple fragments with same type,
    # so we match by order assuming types are in same sequence
    stored = 0
    for order, ((source_type, source_text), (trans_type, trans_text)) in enumerate(zip(source_fragments, translated_fragments)):
        if source_type != trans_type:
            print(f'  Warning: Type mismatch at position {order}: {source_type} vs {trans_type}', file=sys.stderr)

        hash_id = hashlib.sha256(source_text.encode('utf-8')).hexdigest()

        try:
            conn.execute("""
                INSERT OR REPLACE INTO translation
                (article_key, hash_id, source_text, translated_text, model,
                 source_language, target_language, fragment_type, fragment_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (article_key, hash_id, source_text, trans_text, model,
                  'English', 'Simplified Chinese', source_type, order, now))
            stored += 1
        except Exception as e:
            print(f'  Error storing fragment {order}: {e}', file=sys.stderr)

    conn.commit()
    print(f'  Stored {stored} translated fragments in database', flush=True)


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


def get_paragraph_translations(article_key, source_fragments):
    """Get paragraph-by-paragraph translations from cache."""
    conn = db.get_connection()
    results = []

    for ftype, source_text in source_fragments:
        hash_id = hashlib.sha256(source_text.encode('utf-8')).hexdigest()
        cached = db.get_translation(conn, hash_id, article_key=article_key)
        if cached:
            results.append((ftype, source_text, cached['translated_text']))
        else:
            results.append((ftype, source_text, ''))

    return results


def generate_comparison_html(article_title, paragraph_results, full_fragments, output_file):
    """
    Generate HTML comparison page showing:
    - Original English
    - Paragraph-by-paragraph translation
    - Full-article translation
    """
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>翻译方式对比: {article_title}</title>
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans SC', sans-serif;
            line-height: 1.8;
            color: #333;
            max-width: 1600px;
            margin: 0 auto;
            padding: 30px 20px;
            background: #f5f5f5;
        }}

        h1 {{
            text-align: center;
            margin-bottom: 20px;
            color: #2c3e50;
            font-size: 2em;
            font-weight: 600;
        }}

        .legend {{
            text-align: center;
            margin-bottom: 30px;
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .legend-item {{
            display: inline-block;
            margin: 0 15px;
            padding: 8px 20px;
            border-radius: 20px;
            color: white;
            font-size: 0.95em;
            font-weight: 500;
        }}

        .legend-original {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }}

        .legend-paragraph {{
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }}

        .legend-full {{
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }}

        .stats {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            text-align: center;
        }}

        .stat-item {{
            display: inline-block;
            margin: 0 30px;
        }}

        .stat-value {{
            font-size: 2em;
            font-weight: 700;
            color: #2c3e50;
        }}

        .stat-label {{
            font-size: 0.85em;
            color: #7f8c8d;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-top: 5px;
        }}

        .comparison-container {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 20px;
        }}

        .column {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .column h2 {{
            text-align: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 3px solid;
            font-size: 1.2em;
            font-weight: 600;
        }}

        .column h2.original {{
            color: #667eea;
            border-color: #667eea;
        }}

        .column h2.paragraph {{
            color: #f5576c;
            border-color: #f5576c;
        }}

        .column h2.full {{
            color: #4facfe;
            border-color: #4facfe;
        }}

        .fragment {{
            margin-bottom: 20px;
            padding: 18px;
            background: #fafafa;
            border-radius: 6px;
            border-left: 4px solid #ddd;
            transition: all 0.2s;
        }}

        .fragment:hover {{
            background: #f0f0f0;
            transform: translateX(2px);
        }}

        .fragment-type {{
            font-size: 0.75em;
            color: #95a5a6;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
            font-weight: 600;
        }}

        .fragment-content {{
            font-size: 15px;
            line-height: 1.8;
        }}

        .fragment-content b,
        .fragment-content strong {{
            font-weight: 600;
            color: #2c3e50;
        }}

        .fragment-content i,
        .fragment-content em {{
            font-style: italic;
        }}

        .fragment-content small {{
            font-size: 0.85em;
        }}

        .fragment-content sup {{
            font-size: 0.7em;
            vertical-align: super;
        }}

        .fragment-content a {{
            color: #3498db;
            text-decoration: none;
        }}

        .fragment-content a:hover {{
            text-decoration: underline;
        }}

        .no-translation {{
            color: #bdc3c7;
            font-style: italic;
        }}

        .summary {{
            margin-top: 40px;
            padding: 25px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .summary h3 {{
            color: #2c3e50;
            margin-bottom: 15px;
            font-size: 1.3em;
        }}

        .summary ul {{
            margin-left: 25px;
            color: #555;
        }}

        .summary li {{
            margin-bottom: 8px;
        }}

        @media (max-width: 1200px) {{
            .comparison-container {{
                grid-template-columns: 1fr;
            }}

            .column {{
                margin-bottom: 20px;
            }}
        }}
    </style>
</head>
<body>
    <h1>翻译方式对比实验</h1>

    <div class="legend">
        <span class="legend-item legend-original">原文 (English)</span>
        <span class="legend-item legend-paragraph">分段翻译</span>
        <span class="legend-item legend-full">全文翻译</span>
    </div>

    <div class="stats">
        <div class="stat-item">
            <div class="stat-value">{len(paragraph_results)}</div>
            <div class="stat-label">段落数</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{sum(len(s) for _, s, _ in paragraph_results)}</div>
            <div class="stat-label">原文字符数</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{sum(len(s) // 4 for _, s, _ in paragraph_results)}</div>
            <div class="stat-label">原文token数(约)</div>
        </div>
    </div>

    <div class="comparison-container">
        <div class="column">
            <h2 class="original">原文</h2>
'''

    for ftype, original, _ in paragraph_results:
        html += f'''
            <div class="fragment">
                <div class="fragment-type">{ftype}</div>
                <div class="fragment-content">{original}</div>
            </div>'''

    html += '''
        </div>
        <div class="column">
            <h2 class="paragraph">分段翻译</h2>
'''

    for ftype, _, translated in paragraph_results:
        content = translated if translated else '<span class="no-translation">(无翻译)</span>'
        html += f'''
            <div class="fragment">
                <div class="fragment-type">{ftype}</div>
                <div class="fragment-content">{content}</div>
            </div>'''

    html += '''
        </div>
        <div class="column">
            <h2 class="full">全文翻译</h2>
'''

    for ftype, translated in full_fragments:
        content = translated if translated else '<span class="no-translation">(无翻译)</span>'
        html += f'''
            <div class="fragment">
                <div class="fragment-type">{ftype}</div>
                <div class="fragment-content">{content}</div>
            </div>'''

    html += '''
        </div>
    </div>

    <div class="summary">
        <h3>全文翻译的优势</h3>
        <ul>
            <li><strong>上下文连贯</strong>: LLM能够理解整篇文章的语境，避免段落独立翻译导致的前后不一致</li>
            <li><strong>术语统一</strong>: 专有名词、人名、地名在全文中保持一致的翻译</li>
            <li><strong>指代清晰</strong>: 代词（it, they, this等）能够根据上下文准确翻译</li>
            <li><strong>语流自然</strong>: 整篇文章的语气、风格更加统一自然</li>
            <li><strong>减少API调用</strong>: 一次API调用替代多次调用，降低成本和时间</li>
        </ul>
        <h3>实现方法</h3>
        <ul>
            <li>使用HTML注释标记 <code>&lt;!-- FRAGMENT type="..." --&gt;</code> 分隔段落</li>
            <li>将整篇文章连同标记一起发送给LLM进行翻译</li>
            <li>LLM保留标记不变，返回完整的翻译文本</li>
            <li>使用正则表达式根据标记切分翻译结果</li>
            <li>将切分后的片段按顺序写入SQLite数据库</li>
        </ul>
    </div>
</body>
</html>'''

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'Comparison HTML written to: {output_file}', flush=True)


def main():
    load_runtime_env()

    parser = argparse.ArgumentParser(description='Full Article Translation')
    parser.add_argument('issue_date', help='Issue date (YYYY-MM-DD)')
    parser.add_argument('--issue-id', type=int, default=None, help='Issue ID (auto-derived from ISO week if omitted)')
    parser.add_argument('--article-index', type=int, default=0, help='Which article to process (default: 0)')
    parser.add_argument('--model', default='google/gemma-4-31b-it', help='LLM model to use')
    parser.add_argument('--output', default=None, help='Output HTML file path')
    parser.add_argument('--skip-api', action='store_true', help='Skip API call, use existing data only')
    parser.add_argument('--store-db', action='store_true', help='Store full translation fragments in DB')
    args = parser.parse_args()

    conn = db.get_connection()
    issue_id = args.issue_id or db.derive_issue_id(args.issue_date)

    edition = db.get_edition(conn, args.issue_date, issue_id)
    if not edition:
        print(f'Error: edition {args.issue_date}-{issue_id} not found in database.', file=sys.stderr)
        return 1

    articles = db.get_articles_for_edition(conn, edition['id'])
    if not articles:
        print('No articles found for this edition.', file=sys.stderr)
        return 1

    if args.article_index >= len(articles):
        print(f'Error: article index {args.article_index} out of range (0-{len(articles)-1})', file=sys.stderr)
        return 1

    article = articles[args.article_index]
    article_data = db.decompress_article_body(article)
    article_title = article_data.get('headline', 'Unknown Title')

    print(f'Article: {article_title}', flush=True)
    print(f'Article key: {article["article_key"]}', flush=True)

    # Get source fragments
    source_fragments = get_source_fragments(article_data)
    print(f'Fragments: {len(source_fragments)}', flush=True)

    # Get paragraph-by-paragraph translations
    paragraph_results = get_paragraph_translations(article['article_key'], source_fragments)
    print(f'Paragraph translations available: {sum(1 for _, _, t in paragraph_results if t)}', flush=True)

    # Full-article translation
    if args.skip_api:
        print('Skipping API call, using paragraph translations as fallback...', flush=True)
        full_fragments = [(ftype, trans) for ftype, _, trans in paragraph_results]
    else:
        try:
            full_fragments = translate_article_full(
                article['article_key'],
                article_data,
                model=args.model
            )
            print(f'Parsed {len(full_fragments)} fragments from full translation', flush=True)
        except Exception as e:
            print(f'Error in full-article translation: {e}', file=sys.stderr)
            print('Falling back to paragraph translations...', flush=True)
            full_fragments = [(ftype, trans) for ftype, _, trans in paragraph_results]

    # Store in database if requested
    if args.store_db and not args.skip_api:
        store_full_translation_fragments(
            article['article_key'],
            source_fragments,
            full_fragments,
            args.model + '-FULL'
        )

    # Generate comparison HTML
    output_file = args.output or f'translation_comparison_{args.issue_date}_{args.article_index}.html'
    generate_comparison_html(article_title, paragraph_results, full_fragments, output_file)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
