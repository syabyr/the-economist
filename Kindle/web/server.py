#!/usr/bin/env python3
"""Flask web viewer for the Economist SQLite cache.

Usage:
    python3 web/server.py [--port 8080]
"""

import argparse
import glob
import hashlib
import json
import os
import sys
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import utils
from flask import Flask, abort, redirect, render_template, send_from_directory, url_for

app = Flask(__name__)

IMAGES_DIR = os.environ.get('ECONOMIST_IMAGES_DIR',
                            os.path.join(os.path.dirname(__file__), '..', 'images'))
AUDIO_DIR = os.environ.get('ECONOMIST_AUDIO_DIR',
                           os.path.join(os.path.dirname(__file__), '..', 'audio'))


def _get_conn():
    return db.get_connection()


def _article_path(article):
    """Return the article's relative URL path.

    Extracts the path from article.url field (most reliable method since Economist
    uses multiple date conventions). Strips https://www.economist.com prefix to get
    a relative path like /business/2026/07/27/article-slug.

    Falls back to constructing from components if url field is missing.
    """
    # Use the exact URL from the article data - extract relative path
    if article and article.get('url'):
        url = article['url']
        # Strip domain prefix if present
        if url.startswith('http'):
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.path
        return url

    # Fallback: construct from components if URL is missing
    section_slug = article.get('section_slug') or 'article' if article else 'article'
    slug = article.get('slug') or 'untitled' if article else 'untitled'
    date_published = article.get('date_published') or '' if article else ''
    try:
        dt = date_published[:10]
        y, m, d = dt.split('-')
        return f'/{section_slug}/{y}/{m}/{d}/{slug}'
    except (ValueError, IndexError):
        return f'/{section_slug}/1970/01/01/{slug}'


def _economist_url(url):
    return urljoin('https://www.economist.com', url or '')


def _extract_saved_content(html_text):
    needle = '"content":'
    start = html_text.find(needle)
    if start == -1:
        return None

    pos = start + len(needle)
    depth = 0
    in_string = False
    escape = False
    end = None

    for idx in range(pos, len(html_text)):
        ch = html_text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = idx + 1
                break

    if end is None:
        return None

    try:
        return json.loads(html_text[pos:end])
    except json.JSONDecodeError:
        return None


def _load_saved_weeklyedition_content(issue_date):
    temp_dir = os.path.join(os.path.dirname(__file__), '..', 'temp')
    for file_path in sorted(glob.glob(os.path.join(temp_dir, '*.html'))):
        try:
            with open(file_path, 'r', encoding='utf-8') as handle:
                html_text = handle.read()
        except Exception:
            continue

        content = _extract_saved_content(html_text)
        if not content:
            continue

        content_issue_date = (content.get('issueDate') or '')[:10]
        if content_issue_date == issue_date:
            return content

    return None


def _load_cached_edition_content(issue_date, issue_id):
    cache_dir = os.path.join(
        os.path.dirname(__file__), '..', 'economist_content_cache', f'{issue_date}-{issue_id}', 'editions'
    )
    for file_path in sorted(glob.glob(os.path.join(cache_dir, '*.json'))):
        try:
            with open(file_path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except Exception:
            continue

        content = ((payload.get('data') or {}).get('findEditionByDate') or {})
        if (content.get('issueDate') or '')[:10] == issue_date:
            return content

    return None


def _section_layout(section_name):
    layouts = {
        'The world this week': 'spotlight',
        'Leaders': 'normalTeaser',
        'Letters': 'minimisedTeaser',
        'By Invitation': 'spotlight',
        'Briefing': 'maximisedTeaser',
        'Economic & financial indicators': 'minimisedTeaser',
    }
    return layouts.get(section_name or '', 'spotlight')


def _reorder_body_sections(sections):
    preferred = ['Leaders', 'Letters', 'By Invitation', 'Briefing']
    ordered = []
    used_names = set()

    by_name = {section.get('name'): section for section in sections}
    for name in preferred:
        section = by_name.get(name)
        if section:
            ordered.append(section)
            used_names.add(name)

    for section in sections:
        name = section.get('name')
        if name not in used_names:
            ordered.append(section)

    return ordered


def _first_image_url(article, issue_date, issue_id):
    # Prefer lead image from article payload so teaser thumbnail matches article primary image.
    body_blob = article.get('body_json')
    if body_blob:
        try:
            article_data = db.decompress_article_body(article)
            lead_url = ((article_data.get('leadComponent') or {}).get('url') or '')
            if lead_url:
                lead_fname = db._image_hash_filename(lead_url)
                return f'/images/{issue_date}-{issue_id}/{lead_fname}'
        except Exception:
            pass

    img_json = article.get('image_urls_json') or ''
    try:
        imgs = json.loads(img_json)
    except (json.JSONDecodeError, TypeError):
        imgs = []

    if not imgs:
        return ''

    fname = db._image_hash_filename(imgs[0])
    return f'/images/{issue_date}-{issue_id}/{fname}'


def _article_teaser(article, issue_date, issue_id):
    return {
        **article,
        'path': _article_path(article),
        'image_url': _first_image_url(article, issue_date, issue_id),
    }


def _spec_teaser(article_spec, matched_article, issue_date, issue_id):
    if matched_article:
        teaser = _article_teaser(matched_article, issue_date, issue_id)
    else:
        teaser = {
            'path': _economist_url(article_spec.get('url') or ''),
            'image_url': ((article_spec.get('image') or {}).get('url') or ''),
        }

    spec_url = article_spec.get('url') or ''
    if spec_url and not matched_article:
        teaser['path'] = _economist_url(spec_url)

    teaser.update({
        'headline': matched_article.get('headline') if matched_article else article_spec.get('headline') or '',
        'fly_title': matched_article.get('fly_title') if matched_article else article_spec.get('flyTitle') or '',
        'rubric': matched_article.get('rubric') if matched_article else article_spec.get('rubric') or '',
    })
    return teaser


def _order_articles_for_section(section_articles, article_specs):
    if not section_articles:
        return []
    if not article_specs:
        return section_articles

    by_url = {}
    for art in section_articles:
        url = utils.canonical_article_url(art.get('url') or '')
        if url:
            by_url[url] = art

    ordered = []
    seen_ids = set()
    for spec in article_specs:
        url = utils.canonical_article_url(spec.get('url') or '')
        art = by_url.get(url)
        if art and art['id'] not in seen_ids:
            ordered.append(art)
            seen_ids.add(art['id'])

    for art in section_articles:
        if art['id'] not in seen_ids:
            ordered.append(art)

    return ordered


def _build_sections_from_saved_content(content, articles, issue_date, issue_id):
    articles_by_url = {}
    for article in articles:
        canonical_url = utils.canonical_article_url(article.get('url') or '')
        if canonical_url:
            articles_by_url[canonical_url] = article

    def build_section(section_spec):
        teasers = []
        for article_spec in section_spec.get('articles') or []:
            canonical_url = utils.canonical_article_url(article_spec.get('url') or '')
            matched_article = articles_by_url.get(canonical_url)
            teasers.append(_spec_teaser(article_spec, matched_article, issue_date, issue_id))

        return {
            'name': section_spec.get('name') or 'Uncategorized',
            'layout': section_spec.get('layout') or 'spotlight',
            'articles': teasers,
        }

    header_sections = [build_section(section) for section in (content.get('headerSections') or []) if section.get('articles')]
    body_sections = [build_section(section) for section in (content.get('sections') or []) if section.get('articles')]
    return header_sections, body_sections


def _build_sections_from_cached_content(content, articles, issue_date, issue_id):
    header_sections = []
    body_sections = []

    articles_by_url = {}
    for article in articles:
        canonical_url = utils.canonical_article_url(article.get('url') or '')
        if canonical_url:
            articles_by_url[canonical_url] = article

    for section_spec in (content.get('sections') or []):
        teasers = []
        for article_spec in section_spec.get('articles') or []:
            canonical_url = utils.canonical_article_url(article_spec.get('url') or '')
            matched_article = articles_by_url.get(canonical_url)
            teasers.append(_spec_teaser(article_spec, matched_article, issue_date, issue_id))

        if not teasers:
            continue

        section = {
            'name': section_spec.get('name') or 'Uncategorized',
            'layout': _section_layout(section_spec.get('name')),
            'articles': teasers,
        }
        if section['name'] == 'The world this week' and not header_sections:
            header_sections.append(section)
        else:
            body_sections.append(section)

    return header_sections, _reorder_body_sections(body_sections)


app.jinja_env.globals['article_path'] = _article_path


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def root_redirect():
    return redirect('/weeklyedition')


@app.route('/weeklyedition')
def weeklyedition_redirect():
    conn = _get_conn()
    latest = db.get_latest_edition(conn)
    if not latest:
        abort(404)
    return redirect(f'/weeklyedition/{latest["issue_date"]}')


@app.route('/weeklyedition/archive')
def archive():
    conn = _get_conn()
    editions = db.list_editions(conn)
    return render_template('archive.html', editions=editions)


@app.route('/weeklyedition/<issue_date>')
def edition_view(issue_date):
    conn = _get_conn()
    edition = db.get_edition_by_date(conn, issue_date)
    if not edition:
        abort(404)

    articles = db.get_articles_for_edition(conn, edition['id'])
    edition_issue_id = str(edition['id'])

    saved_content = _load_saved_weeklyedition_content(issue_date)
    if saved_content:
        saved_header_sections, saved_body_sections = _build_sections_from_saved_content(
            saved_content, articles, issue_date, edition_issue_id
        )
        header_section = saved_header_sections[0] if saved_header_sections else None

        from datetime import datetime
        try:
            parsed_date = datetime.strptime(issue_date, '%Y-%m-%d')
            display_date = parsed_date.strftime('%B %d %Y')
        except ValueError:
            display_date = issue_date

        cover_url = ''
        if edition.get('cover_path'):
            cover_url = '/' + edition['cover_path']
        elif (saved_content.get('cover') or {}).get('url'):
            cover_url = (saved_content.get('cover') or {}).get('url')

        return render_template('edition.html', edition=edition, sections=saved_body_sections,
                               articles=articles, issue_date=issue_date,
                               cover_url=cover_url, display_date=display_date,
                               header_section=header_section)

    cached_content = _load_cached_edition_content(issue_date, edition_issue_id)
    if cached_content:
        cached_header_sections, cached_body_sections = _build_sections_from_cached_content(
            cached_content, articles, issue_date, edition_issue_id
        )
        header_section = cached_header_sections[0] if cached_header_sections else None

        from datetime import datetime
        try:
            parsed_date = datetime.strptime(issue_date, '%Y-%m-%d')
            display_date = parsed_date.strftime('%B %d %Y')
        except ValueError:
            display_date = issue_date

        cover_url = ''
        if edition.get('cover_path'):
            cover_url = '/' + edition['cover_path']
        elif (cached_content.get('cover') or {}).get('url'):
            cover_url = (cached_content.get('cover') or {}).get('url')

        return render_template('edition.html', edition=edition, sections=cached_body_sections,
                               articles=articles, issue_date=issue_date,
                               cover_url=cover_url, display_date=display_date,
                               header_section=header_section)

    # Group articles by section_name, preserving DB order within each section.
    sections_by_name = {}
    for art in articles:
        sn = art.get('section_name') or 'Uncategorized'
        sections_by_name.setdefault(sn, []).append(art)

    section_specs = []
    try:
        section_specs = json.loads(edition.get('sections_json') or '[]')
    except (json.JSONDecodeError, TypeError):
        section_specs = []

    ordered_sections = []
    for spec in section_specs:
        name = spec.get('name') or 'Uncategorized'
        section_articles = sections_by_name.pop(name, [])
        ordered_articles = _order_articles_for_section(section_articles, spec.get('articles') or [])
        teasers = [_article_teaser(art, issue_date, edition_issue_id) for art in ordered_articles]
        if teasers:
            ordered_sections.append({'name': name, 'layout': 'spotlight', 'articles': teasers})

    for name, section_articles in sections_by_name.items():
        teasers = [_article_teaser(art, issue_date, edition_issue_id) for art in section_articles]
        if teasers:
            ordered_sections.append({'name': name, 'layout': 'spotlight', 'articles': teasers})

    # Format the date
    from datetime import datetime
    try:
        parsed_date = datetime.strptime(issue_date, '%Y-%m-%d')
        display_date = parsed_date.strftime('%B %d %Y')
    except ValueError:
        display_date = issue_date

    header_section = None
    body_sections = []
    for section in ordered_sections:
        if section['name'] == 'The world this week' and header_section is None:
            header_section = section
        else:
            body_sections.append(section)

    cover_url = ''
    if edition.get('cover_path'):
        cover_url = '/' + edition['cover_path']

    return render_template('edition.html', edition=edition, sections=body_sections,
                           articles=articles, issue_date=issue_date,
                           cover_url=cover_url, display_date=display_date,
                           header_section=header_section)


@app.route('/<section_slug>/<int:year>/<int:month>/<int:day>/<article_slug>')
def article_view(section_slug, year, month, day, article_slug):
    conn = _get_conn()
    article = db.get_article_by_slug(conn, section_slug, year, month, day, article_slug)
    if not article:
        abort(404)

    edition = db.get_edition_by_id(conn, article['edition_id'])
    if not edition:
        abort(404)

    # Decompress body and extract bilingual fragments
    fragments = []
    if article['body_json']:
        article_data = db.decompress_article_body(article)
        fragments = _extract_bilingual(conn, article, article_data,
                                       edition['issue_date'], str(edition['id']))

    # Ensure audio is downloaded
    _ensure_audio(conn, article, edition['issue_date'], str(edition['id']))

    # Navigation
    prev_info, next_info = db.get_article_nav(conn, article['id'])

    return render_template('article.html',
                           edition=edition, article=article,
                           fragments=fragments,
                           issue_date=edition['issue_date'],
                           issue_id=edition['id'],
                           prev_info=prev_info, next_info=next_info)


# ── Static file serving ─────────────────────────────────────────────────────

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route('/audio/<path:filename>')
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _extract_bilingual(conn, article, article_data, issue_date, issue_id):
    fragments = []
    article_key = article['article_key']

    for key, ftype in [('flyTitle', 'fly_title'), ('headline', 'heading'), ('rubric', 'rubric')]:
        val = article_data.get(key)
        if val:
            fragments.append({'type': ftype, 'en': val,
                              'zh': _lookup(conn, article_key, val)})

    lead = article_data.get('leadComponent') or {}
    if lead.get('url'):
        fragments.append({'type': 'lead_image', 'en': '', 'zh': '',
                          'image_url': _img_src(issue_date, issue_id, lead['url']),
                          'image_alt': lead.get('altText', '')})

    for node in article_data.get('body') or ():
        ntype = node.get('type', '')
        typename = node.get('__typename', '')

        if ntype == 'IMAGE' or typename == 'ImageComponent':
            url = node.get('url', '')
            caption = node.get('caption') or {}
            cap_text = (caption.get('textHtml')
                        or (utils.parse_textjson(caption['textJson']) if caption.get('textJson')
                            else caption.get('text', '')))
            fragments.append({
                'type': 'image', 'en': cap_text or '',
                'zh': _lookup(conn, article_key, cap_text) if cap_text else '',
                'image_url': _img_src(issue_date, issue_id, url),
                'image_alt': node.get('altText', ''),
            })
        elif ntype == 'INFOGRAPHIC' and node.get('fallback'):
            fb = node['fallback']
            fb_url = fb.get('url', '')
            fragments.append({
                'type': 'image', 'en': fb.get('altText', '') or fb.get('title', ''), 'zh': '',
                'image_url': _img_src(issue_date, issue_id, fb_url),
                'image_alt': fb.get('altText', ''),
            })
        elif ntype == 'INFOBOX':
            for child in node.get('components') or ():
                html = _node_html(child)
                if html:
                    fragments.append({'type': 'paragraph', 'en': html,
                                      'zh': _lookup(conn, article_key, html)})
        elif ntype in ('ORDERED_LIST', 'UNORDERED_LIST'):
            items = []
            for item in node.get('items') or []:
                item_html = _node_html(item)
                items.append({'en': item_html, 'zh': _lookup(conn, article_key, item_html)})
            fragments.append({'type': 'list', 'items': items, 'en': '', 'zh': '',
                              'list_tag': 'ol' if ntype == 'ORDERED_LIST' else 'ul'})
        else:
            html = _node_html(node)
            ftype = _fragment_type(ntype)
            if html:
                fragments.append({'type': ftype, 'en': html,
                                  'zh': _lookup(conn, article_key, html)})

    return fragments


def _node_html(node):
    ntype = node.get('type', '')
    if ntype == 'CROSSHEAD':
        return node.get('textHtml') or node.get('text', '')
    if ntype in ('PARAGRAPH', 'BOOK_INFO', 'PULL_QUOTE'):
        return (node.get('textHtml')
                or (utils.parse_textjson(node['textJson']) if node.get('textJson')
                    else node.get('text', '')))
    if ntype == 'BLOCK_QUOTE':
        content = (node.get('textHtml')
                   or (utils.parse_textjson(node['textJson']) if node.get('textJson')
                       else node.get('text', '')))
        return f'<i>{content}</i>'
    return ''


def _fragment_type(ntype):
    return {
        'CROSSHEAD': 'crosshead',
        'PULL_QUOTE': 'pull_quote',
        'BLOCK_QUOTE': 'block_quote',
    }.get(ntype, 'paragraph')


def _lookup(conn, article_key, html_text):
    if not html_text:
        return ''
    hash_id = hashlib.sha256(html_text.encode('utf-8')).hexdigest()
    cached = db.get_translation(conn, hash_id, article_key=article_key)
    return cached['translated_text'] if cached else ''


def _img_src(issue_date, issue_id, url):
    if not url:
        return ''
    return f'/images/{issue_date}-{issue_id}/{db._image_hash_filename(url)}'


def _audio_src(issue_date, issue_id, article):
    """Return the URL path for serving an article's audio file."""
    path = article.get('audio_path') or ''
    if path:
        return '/' + path
    return ''


def _ensure_audio(conn, article, issue_date, issue_id):
    """Download audio file on-demand if article has audio_url but no local copy."""
    import os as _os
    audio_url = article.get('audio_url') or ''
    if not audio_url:
        return
    if article.get('audio_path'):
        return  # already downloaded

    # Build local path
    asset_dir = db.ensure_asset_dir(_os.getcwd(), issue_date, issue_id, 'audio')
    fname = db._audio_filename(audio_url, None)
    dest = _os.path.join(asset_dir, fname)
    rel_path = db.audio_local_path(issue_date, issue_id, audio_url)

    # Download if missing
    if not _os.path.exists(dest) or _os.path.getsize(dest) == 0:
        try:
            print(f'Downloading audio: {audio_url}', flush=True)
            raw = utils.fetch(audio_url)
            utils.write_file_atomic(dest, raw)
            print(f'Audio saved: {rel_path}', flush=True)
        except Exception as exc:
            print(f'Audio download failed: {exc}', file=sys.stderr, flush=True)
            return

    # Update DB
    db.update_article_audio_path(conn, article['id'], rel_path)
    article['audio_path'] = rel_path


# ── Entrypoint ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Start the Economist web viewer')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on (default: 8080)')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind (default: 0.0.0.0)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()

    print(f'Starting at http://localhost:{args.port}/', flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
