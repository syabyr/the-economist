#!/usr/bin/env python3
"""Import full article bodies from an Economist EPUB into economist.sqlite.

prefetch.py stores whatever body GraphQL returns. For anonymous requests that is
usually a two-paragraph teaser, while the EPUB holds the whole article. This
script rebuilds `article.body_json` from the EPUB, copies inline images that
prefetch never saw, and refreshes word counts.

Usage:
    python3 tools/import_epub.py temp/TheEconomist.2026.10.24.epub --dry-run
    python3 tools/import_epub.py temp/TheEconomist.2026.10.24.epub --apply

Mapping: EPUB article pages are matched to DB rows by the GraphQL article id in
the page filename, then by the canonical URL in the page's link_navbar, then by
headline. Articles that already contain at least as many paragraphs as the EPUB
are left alone unless --force is given.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import zipfile
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Allow running as `python3 tools/import_epub.py [--isolated]` from anywhere.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import db
import utils

SYNTHETIC_IMAGE_BASE = 'https://www.economist.com/epub-assets'
VALID_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


# ── Small helpers ───────────────────────────────────────────────────────────

def norm_space(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def issue_date_from_path(path):
    m = re.search(r'(\d{4})[.\-_](\d{2})[.\-_](\d{2})', os.path.basename(path))
    if not m:
        return ''
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'


def url_key(url):
    if not url:
        return ''
    parsed = urlparse(utils.canonical_article_url(url))
    return parsed.path.rstrip('/')


def html_text(fragment):
    return norm_space(BeautifulSoup(fragment or '', 'html.parser').get_text(' '))


def count_words(text):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text or ''))


# ── EPUB pages ──────────────────────────────────────────────────────────────

def read_epub_pages(epub_path):
    """Return metadata for every article page in the EPUB."""
    pages = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(('.html', '.xhtml')):
                continue
            if os.path.basename(name) in ('nav.xhtml', 'toc.xhtml'):
                continue
            try:
                raw = zf.read(name)
            except KeyError:
                continue
            soup = BeautifulSoup(raw.decode('utf-8', 'replace'), 'html.parser')
            h1 = soup.find('h1', class_='te_article_title')
            if not h1:
                continue
            nav = soup.find('p', class_='link_navbar')
            origin = ''
            if nav:
                link = nav.find('a', class_='origin_link')
                if link:
                    origin = link.get('href') or ''
            if not origin:
                for link in soup.find_all('a', href=True):
                    if 'economist.com/' in link['href']:
                        origin = link['href']
                        break
            section = soup.find('span', class_='te_section_title')
            fly = soup.find('span', class_='te_fly_span')
            rubric = soup.find('h3', class_='te_article_rubric')
            head_img = soup.find('img', class_='te_head_image')
            date_span = soup.find('span', class_='te_article_datePublished')
            pages.append({
                'name': name,
                'soup': soup,
                'title': norm_space(h1.get_text(' ')),
                'url': utils.canonical_article_url(origin),
                'section': norm_space(section.get_text(' ')) if section else '',
                'fly_title': norm_space(fly.get_text(' ')).lstrip('|').strip() if fly else '',
                'rubric': norm_space(rubric.get_text(' ')) if rubric else '',
                'date_published': norm_space(date_span.get_text(' ')) if date_span else '',
                'head_image_src': head_img.get('src') if head_img else '',
            })
    return pages


class ImageImporter:
    """Resolve EPUB <img> elements to DB image URLs, copying missing assets."""

    def __init__(self, zf, issue_date, issue_id, md5_to_url, dry_run, skip_images,
                 base_dir=None):
        self.zf = zf
        self.issue_date = issue_date
        self.issue_id = issue_id
        self.md5_to_url = md5_to_url
        self.dry_run = dry_run
        self.skip_images = skip_images
        self.base_dir = base_dir or REPO_ROOT
        self.stats = {'reused': 0, 'repaired': 0, 'created': 0, 'missing': 0}
        self._seen = set()

    def _dest(self, url):
        return os.path.join(
            self.base_dir,
            db.image_local_path(self.issue_date, self.issue_id, url),
        )

    def resolve(self, page_name, img_el):
        """Return an image URL usable by db.image_local_path, or '' to skip."""
        if self.skip_images:
            return ''
        src = img_el.get('src') or ''
        if not src:
            return ''
        member = posixpath.normpath(posixpath.join(posixpath.dirname(page_name), src))
        stem, ext = os.path.splitext(os.path.basename(src))
        ext = ext.lower()
        if ext not in VALID_IMAGE_EXTS:
            ext = '.jpg'

        url = self.md5_to_url.get(stem.lower())
        if url:
            key = ('url', url)
            dest = self._dest(url)
            action = 'repaired'
        else:
            url = f'{SYNTHETIC_IMAGE_BASE}/{self.issue_date}/{stem}{ext}'
            key = ('synthetic', stem)
            dest = self._dest(url)
            action = 'created'

        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            if key not in self._seen:
                self.stats['reused'] += 1
                self._seen.add(key)
            return url

        if key in self._seen:
            return url
        self._seen.add(key)

        try:
            payload = self.zf.read(member)
        except KeyError:
            self.stats['missing'] += 1
            return url

        self.stats[action] += 1
        if not self.dry_run:
            os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            utils.write_file_atomic(dest, payload)
        return url


# ── Article body extraction ─────────────────────────────────────────────────

def extract_body_nodes(page, image_importer):
    """Convert an EPUB article page into GraphQL-style body nodes."""
    soup = page['soup']
    body = soup.body
    if body is None:
        return []

    children = body.find_all(recursive=False)
    date_span = body.find('span', class_='te_article_datePublished')
    if date_span is not None and date_span.parent is body:
        children = children[children.index(date_span) + 1:]

    nodes = []
    for el in children:
        if el.name == 'br':
            continue
        if el.name == 'p' and 'link_navbar' in (el.get('class') or []):
            break

        if el.name == 'p':
            nodes.extend(_paragraph_nodes(el, page['name'], image_importer))
        elif el.name in ('h4', 'h5'):
            text_html = el.decode_contents().strip()
            nodes.append({
                'type': 'CROSSHEAD',
                'text': norm_space(el.get_text(' ')),
                'textHtml': text_html,
            })
        elif el.name == 'blockquote':
            text_html = el.decode_contents().strip()
            nodes.append({
                'type': 'BLOCK_QUOTE',
                'text': norm_space(el.get_text(' ')),
                'textHtml': text_html,
            })
        elif el.name in ('ul', 'ol'):
            items = []
            for li in el.find_all('li', recursive=False):
                item_html = li.decode_contents().strip()
                if html_text(item_html):
                    items.append({
                        'text': norm_space(li.get_text(' ')),
                        'textHtml': item_html,
                    })
            if items:
                nodes.append({
                    'type': 'UNORDERED_LIST' if el.name == 'ul' else 'ORDERED_LIST',
                    'items': items,
                })
        elif el.name == 'hr':
            continue
        elif el.find(['img']) and not html_text(el.get_text(' ')):
            for img in el.find_all('img'):
                nodes.extend(_image_nodes(img, page['name'], image_importer))
        elif html_text(el.get_text(' ')):
            # Unknown content element: keep its text rather than lose it.
            text_html = el.decode_contents().strip()
            nodes.append({
                'type': 'PARAGRAPH',
                'text': norm_space(el.get_text(' ')),
                'textHtml': text_html,
            })
    return nodes


def _paragraph_nodes(el, page_name, image_importer):
    images = el.find_all('img')
    text = norm_space(el.get_text(' '))
    if not images:
        if not text:
            return []
        return [{
            'type': 'PARAGRAPH',
            'text': text,
            'textHtml': el.decode_contents().strip(),
        }]

    if not text:
        nodes = []
        for img in images:
            nodes.extend(_image_nodes(img, page_name, image_importer))
        return nodes

    # Paragraph and image share one <p>: keep the text, emit images after it.
    clone = BeautifulSoup(str(el), 'html.parser').find('p')
    for img in clone.find_all('img'):
        img.extract()
    nodes = []
    if html_text(clone.decode_contents()):
        nodes.append({
            'type': 'PARAGRAPH',
            'text': norm_space(clone.get_text(' ')),
            'textHtml': clone.decode_contents().strip(),
        })
    for img in images:
        nodes.extend(_image_nodes(img, page_name, image_importer))
    return nodes


def _image_nodes(img, page_name, image_importer):
    url = image_importer.resolve(page_name, img)
    if not url:
        return []
    return [{
        'type': 'IMAGE',
        'url': url,
        'altText': img.get('alt') or None,
        'caption': {'textHtml': ''},
    }]


def body_word_count(nodes):
    words = 0
    for node in nodes:
        if node.get('type') in ('PARAGRAPH', 'CROSSHEAD', 'BLOCK_QUOTE'):
            words += count_words(node.get('text'))
        elif node.get('type') in ('ORDERED_LIST', 'UNORDERED_LIST'):
            for item in node.get('items') or ():
                words += count_words(item.get('text'))
    return words


# ── DB article records ──────────────────────────────────────────────────────

def load_records(conn, edition_id):
    records = []
    for art in db.get_articles_for_edition(conn, edition_id):
        data = db.decompress_article_body(art) if art.get('body_json') else {}
        records.append({'row': art, 'data': data or {}})
    return records


def collect_image_urls(record):
    """All remote image URLs known for an article, in rough reading order."""
    urls = []
    seen = set()

    def add(url):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    try:
        for url in json.loads(record['row'].get('image_urls_json') or '[]'):
            add(url)
    except (json.JSONDecodeError, TypeError):
        pass

    for key in ('leadComponent', 'teaserImage'):
        add((record['data'].get(key) or {}).get('url'))

    def walk(value):
        if isinstance(value, dict):
            typename = value.get('__typename') or ''
            if value.get('type') == 'IMAGE' or 'ImageComponent' in typename:
                add(value.get('url'))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(record['data'].get('body'))
    return urls


def build_match_index(records):
    by_id, by_url, by_title = {}, {}, {}
    for record in records:
        data = record['data']
        article_id = data.get('id')
        if article_id:
            by_id[article_id] = record
        by_url.setdefault(url_key(record['row'].get('url')), record)
        by_title.setdefault(norm_space(record['row'].get('headline')), record)
    return by_id, by_url, by_title


def match_record(page, by_id, by_url, by_title):
    stem = os.path.splitext(os.path.basename(page['name']))[0]
    record = by_id.get(stem)
    if record:
        return record, 'id'
    record = by_url.get(url_key(page['url']))
    if record:
        return record, 'url'
    record = by_title.get(norm_space(page['title']))
    if record:
        return record, 'headline'
    return None, ''


def body_is_complete(existing_nodes, new_nodes):
    """True if the stored body already holds at least the EPUB content."""
    old_paragraphs = [n for n in (existing_nodes or ()) if n.get('type') == 'PARAGRAPH']
    new_paragraphs = [n for n in new_nodes if n.get('type') == 'PARAGRAPH']
    old_images = len([n for n in (existing_nodes or ()) if n.get('type') == 'IMAGE'])
    new_images = len([n for n in new_nodes if n.get('type') == 'IMAGE'])
    if len(old_paragraphs) < len(new_paragraphs) or old_images < new_images:
        return False
    if not new_paragraphs:
        return True
    old_text = ' ' + ' '.join(
        html_text(n.get('textHtml') or n.get('text')) for n in old_paragraphs
    ) + ' '
    for probe in (new_paragraphs[0], new_paragraphs[-1]):
        probe_text = norm_space(probe.get('text'))
        if probe_text and probe_text not in old_text:
            return False
    return True


# ── Backup ──────────────────────────────────────────────────────────────────

def write_backup(path, issue_date, edition_id, records):
    payload = {
        'issue_date': issue_date,
        'edition_id': edition_id,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'articles': {},
    }
    for record in records:
        row = record['row']
        blob = row.get('body_json')
        payload['articles'][row['article_key']] = {
            'url': row.get('url'),
            'word_count': row.get('word_count'),
            'image_urls_json': row.get('image_urls_json'),
            'body_json_b64': base64.b64encode(blob).decode('ascii') if blob else None,
        }
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Import full article bodies from an Economist EPUB into SQLite'
    )
    parser.add_argument('epub', help='Path to TheEconomist.YYYY.MM.DD.epub')
    parser.add_argument('--issue-date', help='Issue date (default: from filename)')
    parser.add_argument('--issue-id', help='Issue id (default: looked up by date)')
    parser.add_argument('--apply', action='store_true',
                        help='Write changes (default is a dry run)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Explicitly request a dry run (the default)')
    parser.add_argument('--force', action='store_true',
                        help='Re-import articles whose body already looks complete')
    parser.add_argument('--skip-images', action='store_true',
                        help='Do not copy EPUB images into images/')
    parser.add_argument('--backup', default='',
                        help='Backup path (default: temp/import-epub-backup-<date>-<ts>.json.gz)')
    parser.add_argument('--no-backup', action='store_true', help='Skip the backup file')
    parser.add_argument('--verbose', action='store_true', help='Print every article')
    args = parser.parse_args()
    if args.dry_run:
        args.apply = False

    if not os.path.isfile(args.epub):
        print(f'Error: EPUB not found: {args.epub}', file=sys.stderr)
        return 1
    if not args.apply:
        print('DRY RUN - no database or image changes. Add --apply to write.')

    issue_date = args.issue_date or issue_date_from_path(args.epub)
    if not issue_date:
        print('Error: cannot infer issue date from filename; pass --issue-date',
              file=sys.stderr)
        return 1

    conn = db.get_connection()
    db.ensure_schema(conn)
    edition = db.get_edition(conn, issue_date, args.issue_id)
    if not edition:
        print(f'Error: edition {issue_date} not found. Run prefetch.py first.',
              file=sys.stderr)
        return 1
    issue_id = str(edition['id'])

    records = load_records(conn, edition['id'])
    if not records:
        print('Error: no articles found for this edition.', file=sys.stderr)
        return 1

    by_id, by_url, by_title = build_match_index(records)

    md5_to_url = {}
    image_urls = set()
    for record in records:
        image_urls.update(collect_image_urls(record))
    if edition.get('cover_url'):
        image_urls.add(edition['cover_url'])
    for url in image_urls:
        md5_to_url[hashlib.md5(url.encode('utf-8')).hexdigest()] = url

    pages = read_epub_pages(args.epub)
    print(f'EPUB: {len(pages)} article pages; DB edition {issue_id} '
          f'({issue_date}): {len(records)} articles')

    matched = []
    unmatched_pages = []
    for page in pages:
        record, how = match_record(page, by_id, by_url, by_title)
        if record:
            matched.append((page, record, how))
        else:
            unmatched_pages.append(page)

    updated = []
    skipped = []
    with zipfile.ZipFile(args.epub) as zf:
        importer = ImageImporter(zf, issue_date, issue_id, md5_to_url,
                                 dry_run=not args.apply,
                                 skip_images=args.skip_images)
        for page, record, how in matched:
            nodes = extract_body_nodes(page, importer)
            if not nodes:
                skipped.append((page, record, 'no content extracted'))
                continue
            data = record['data']
            if body_is_complete(data.get('body'), nodes) and not args.force:
                skipped.append((page, record, 'already complete'))
                continue

            new_data = dict(data)
            new_data['body'] = nodes
            new_data['wordCount'] = body_word_count(nodes)
            if page['head_image_src'] and not (new_data.get('leadComponent') or {}).get('url'):
                head_url = importer.resolve(page['name'],
                                            page['soup'].find('img', class_='te_head_image'))
                if head_url:
                    new_data['leadComponent'] = {
                        'type': 'IMAGE',
                        'url': head_url,
                        'altText': None,
                        'caption': {'textHtml': ''},
                    }

            new_image_urls = []
            seen_urls = set()
            lead_url = (new_data.get('leadComponent') or {}).get('url')
            if lead_url:
                seen_urls.add(lead_url)
                new_image_urls.append(lead_url)
            for url in collect_image_urls(record):
                if url not in seen_urls:
                    seen_urls.add(url)
                    new_image_urls.append(url)
            for node in nodes:
                if node.get('type') == 'IMAGE' and node.get('url') not in seen_urls:
                    seen_urls.add(node['url'])
                    new_image_urls.append(node['url'])

            old_paragraphs = len([n for n in (data.get('body') or ())
                                  if n.get('type') == 'PARAGRAPH'])
            new_paragraphs = len([n for n in nodes if n.get('type') == 'PARAGRAPH'])
            new_images = len([n for n in nodes if n.get('type') == 'IMAGE'])
            updated.append({
                'page': page,
                'record': record,
                'how': how,
                'data': new_data,
                'word_count': new_data['wordCount'],
                'image_urls_json': json.dumps(new_image_urls, ensure_ascii=False),
                'old_paragraphs': old_paragraphs,
                'new_paragraphs': new_paragraphs,
                'new_images': new_images,
            })

    unmatched_records = []
    matched_keys = {id(rec['row']) for _, rec, _ in matched}
    for record in records:
        if id(record['row']) not in matched_keys:
            unmatched_records.append(record)

    print(f'Matched: {len(matched)} '
          f'(by id {sum(1 for _, _, h in matched if h == "id")}, '
          f'by url {sum(1 for _, _, h in matched if h == "url")}, '
          f'by headline {sum(1 for _, _, h in matched if h == "headline")})')
    print(f'Updated: {len(updated)}, skipped: {len(skipped)}')
    for page, record, reason in skipped:
        print(f'  skip {reason}: {record["row"].get("headline")}')
    if unmatched_pages:
        print(f'EPUB pages not in this edition ({len(unmatched_pages)}):')
        for page in unmatched_pages:
            print(f'  {page["title"]}  {page["url"]}')
    if unmatched_records:
        print(f'DB articles not in this EPUB ({len(unmatched_records)}):')
        for record in unmatched_records:
            print(f'  {record["row"].get("headline")}  {record["row"].get("url")}')

    if updated:
        print('Changes:')
        for item in updated:
            if args.verbose or item['new_paragraphs'] - item['old_paragraphs'] > 2:
                print(f'  {item["record"]["row"].get("headline")}: '
                      f'{item["old_paragraphs"]} -> {item["new_paragraphs"]} paragraphs, '
                      f'{item["new_images"]} images, {item["word_count"]} words')
        print(f'  ... {len(updated)} article(s) total')

    print('Images: {reused} reused, {repaired} repaired, {created} copied, '
          '{missing} missing'.format(**importer.stats))

    if not args.apply:
        print('Dry run complete. Re-run with --apply to write changes.')
        return 0
    if not updated:
        print('Nothing to update.')
        return 0

    backup_path = args.backup
    if not args.no_backup:
        if not backup_path:
            backup_path = os.path.join(
                REPO_ROOT,
                'temp',
                f'import-epub-backup-{issue_date}-{time.strftime("%Y%m%d%H%M%S")}.json.gz',
            )
        write_backup(backup_path, issue_date, edition['id'],
                     [item['record'] for item in updated])
        print(f'Backup: {backup_path}')

    for item in updated:
        blob = gzip.compress(
            json.dumps(item['data'], ensure_ascii=False).encode('utf-8')
        )
        conn.execute(
            'UPDATE article SET body_json=?, word_count=?, image_urls_json=? '
            'WHERE id=?',
            (blob, item['word_count'], item['image_urls_json'],
             item['record']['row']['id']),
        )
    conn.commit()
    print(f'Updated {len(updated)} article(s) in economist.sqlite.')
    print('Run tools/translate.py for this edition to translate the new paragraphs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
