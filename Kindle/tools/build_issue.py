#!/usr/bin/env python3
"""Phase 3: Build mobi/pdf/epub via calibre.

Usage:
    python3 tools/build_issue.py YYYY-MM-DD ISSUE_ID [--format mobi,pdf,epub]

The calibre bridge exports a temporary directory that matches the old file-cache
format, then runs ebook-convert against the existing economist.recipe.
"""

import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Allow running as `python3 tools/build_issue.py` from anywhere.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import db
import utils


def export_temp_cache(conn, edition, articles, issue_date, issue_id):
    """Export SQLite data to a temp directory matching the old file-cache format.

    Returns path to the temp directory.
    """
    tmpdir = tempfile.mkdtemp(prefix=f'ec-{issue_date}-{issue_id}-')
    editions_dir = os.path.join(tmpdir, 'editions')
    articles_dir = os.path.join(tmpdir, 'articles')
    images_dir = os.path.join(tmpdir, 'images')
    os.makedirs(editions_dir)
    os.makedirs(articles_dir)
    os.makedirs(images_dir)

    # Edition JSON (same format as old editions/*.json)
    ed_key = 'FindEditionByDate\n' + json.dumps(
        {'issueDate': issue_date, 'editionType': 'WEEKLY'}, sort_keys=True
    )
    ed_hash = hashlib.sha256(ed_key.encode('utf-8')).hexdigest()
    ed_payload = {
        'data': {
            'findEditionByDate': json.loads(edition.get('manifest_json', '{}'))
        }
    }
    with open(os.path.join(editions_dir, ed_hash + '.json'), 'w', encoding='utf-8') as fh:
        json.dump(ed_payload, fh, ensure_ascii=False)

    # Article JSONs (same format as old articles/*.json)
    for article in articles:
        if not article['body_json']:
            continue
        art_hash = hashlib.sha256(article['url'].encode('utf-8')).hexdigest()
        article_data = db.decompress_article_body(article)
        article_payload = {
            'data': {
                'findArticleByUrl': article_data,
            }
        }
        with open(os.path.join(articles_dir, art_hash + '.json'), 'wb') as fh:
            fh.write(json.dumps(article_payload, ensure_ascii=False).encode('utf-8'))

    # Resize and copy images to temp dir (instead of symlink to full-res)
    max_width = int(os.environ.get('ECONOMIST_IMAGE_WIDTH', '600'))
    quality = int(os.environ.get('ECONOMIST_IMAGE_QUALITY', '80'))
    src_images = os.path.join(REPO_ROOT, 'images', f'{issue_date}-{issue_id}')
    if os.path.isdir(src_images):
        try:
            from PIL import Image
        except ImportError:
            Image = None
        for fname in os.listdir(src_images):
            src = os.path.join(src_images, fname)
            dst = os.path.join(images_dir, fname)
            if not os.path.isfile(src):
                continue
            if Image:
                try:
                    img = Image.open(src)
                    if img.width > max_width:
                        ratio = max_width / img.width
                        new_size = (max_width, int(img.height * ratio))
                        img = img.resize(new_size, Image.LANCZOS)
                    img.save(dst, quality=quality)
                except Exception:
                    shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)

    # Export translate.json with recipe-compatible keys
    # The recipe keys its translation cache by (model + fragment). Use the
    # model recorded on the cached translations, otherwise every fragment
    # misses when translate.py ran with a different model than the default.
    translations = db.get_translations_for_edition(conn, edition['id'])
    model = dominant_translation_model(translations)
    if translations:
        cache = {}
        for t in translations:
            key = hashlib.sha256((model + '\n' + t['source_text']).encode('utf-8')).hexdigest()
            cache[key] = t['translated_text']
        with open(os.path.join(tmpdir, 'translate.json'), 'w', encoding='utf-8') as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2, sort_keys=True)

    return tmpdir, model


def dominant_translation_model(translations):
    """Model used for most cached translations of this edition."""
    counts = {}
    for t in translations:
        name = t.get('model')
        if name:
            counts[name] = counts.get(name, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    return (
        os.environ.get('ECONOMIST_TRANSLATE_MODEL')
        or 'nvidia/riva-translate-4b-instruct-v1.1'
    )


def build_calibre(issue_date, issue_id, tmpdir, cover_path, formats, translate_model=None):
    """Run ebook-convert via calibre recipe."""
    year = issue_date[:4]
    recipe_file = f'TheEconomist-{issue_date}-{issue_id}.recipe'
    output_base = f'TheEconomist-{issue_date}-{issue_id}'

    # Generate temp recipe from economist.recipe
    recipe_src = os.path.join(REPO_ROOT, 'economist.recipe')
    with open(recipe_src, 'r', encoding='utf-8') as fh:
        recipe_content = fh.read()
    recipe_content = re.sub(
        r"edition_date\s*=\s*'.*'",
        f"edition_date = '{issue_date}'",
        recipe_content
    )
    with open(recipe_file, 'w', encoding='utf-8') as fh:
        fh.write(recipe_content)

    # Build environment
    env = os.environ.copy()
    env['ECONOMIST_CONTENT_CACHE_DIR'] = tmpdir
    env['ECONOMIST_CACHE_ONLY'] = '1'

    if os.path.isfile(os.path.join(tmpdir, 'translate.json')):
        env['ECONOMIST_TRANSLATE_CACHE'] = os.path.join(tmpdir, 'translate.json')
        env['ECONOMIST_TRANSLATE_ZH'] = '1'
        env['ECONOMIST_TRANSLATE_PROVIDER'] = (
            os.environ.get('ECONOMIST_TRANSLATE_PROVIDER') or 'nvidia'
        )
        env['ECONOMIST_TRANSLATE_MODEL'] = (
            translate_model
            or os.environ.get('ECONOMIST_TRANSLATE_MODEL')
            or 'nvidia/riva-translate-4b-instruct-v1.1'
        )
        env['ECONOMIST_TRANSLATE_SOURCE_LANGUAGE'] = (
            os.environ.get('ECONOMIST_TRANSLATE_SOURCE_LANGUAGE') or 'English'
        )
        env['ECONOMIST_TRANSLATE_TARGET_LANGUAGE'] = (
            os.environ.get('ECONOMIST_TRANSLATE_TARGET_LANGUAGE') or 'Simplified Chinese'
        )

    cover_args = []
    if cover_path and os.path.isfile(cover_path):
        env['ECONOMIST_SKIP_RECIPE_COVER'] = '1'
        cover_args = ['--cover', cover_path]

    results = []

    if 'mobi' in formats:
        print('Building .mobi ...', flush=True)
        built = os.path.join(REPO_ROOT, f'{output_base}.mobi')
        subprocess.run([
            'ebook-convert', recipe_file, built,
            '--output-profile=kindle_oasis',
            f'--pubdate={issue_date}',
            '-vv',
            '--mobi-file-type=new',
            '--authors=TheEconomist',
            f'--title=TheEconomist-{issue_date}-{issue_id}',
        ] + cover_args, env=env, check=True)
        dest_dir = os.path.join(REPO_ROOT, 'output', year)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(built, os.path.join(dest_dir, f'{output_base}.mobi'))
        results.append('mobi')
        print(f'  -> output/{year}/{output_base}.mobi', flush=True)

    if 'pdf' in formats:
        print('Building .pdf ...', flush=True)
        built = os.path.join(REPO_ROOT, f'{output_base}.pdf')
        subprocess.run([
            'ebook-convert', recipe_file, built,
            '--paper-size=a4',
            '--pdf-page-numbers',
            '--pdf-page-margin-bottom=42',
            '--pdf-page-margin-top=42',
            '--pdf-page-margin-left=42',
            '--pdf-page-margin-right=42',
            '-vv',
            f'--title=TheEconomist-{issue_date}-{issue_id}',
        ] + cover_args, env=env, check=True)
        dest_dir = os.path.join(REPO_ROOT, 'output', 'pdf', year)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(built, os.path.join(dest_dir, f'{output_base}.pdf'))
        results.append('pdf')
        print(f'  -> output/pdf/{year}/{output_base}.pdf', flush=True)

    if 'epub' in formats:
        print('Building .epub ...', flush=True)
        built = os.path.join(REPO_ROOT, f'{output_base}.epub')
        subprocess.run([
            'ebook-convert', recipe_file, built,
            '--output-profile=tablet',
            f'--pubdate={issue_date}',
            '-vv',
            '--authors=TheEconomist',
            f'--title=TheEconomist-{issue_date}-{issue_id}',
        ] + cover_args, env=env, check=True)
        dest_dir = os.path.join(REPO_ROOT, 'output', 'epub', year)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(built, os.path.join(dest_dir, f'{output_base}.epub'))
        results.append('epub')
        print(f'  -> output/epub/{year}/{output_base}.epub', flush=True)

    # Cleanup
    if os.path.isfile(recipe_file):
        os.remove(recipe_file)

    return results



# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Build mobi/pdf from SQLite cache')
    parser.add_argument('issue_date', help='YYYY-MM-DD')
    parser.add_argument('issue_id', help='Issue ID')
    parser.add_argument('--format', default='mobi,pdf,epub',
                        help='Comma-separated: mobi,pdf,epub (default: mobi,pdf,epub)')
    args = parser.parse_args()

    formats = [f.strip() for f in args.format.split(',')]
    issue_date = args.issue_date
    issue_id = args.issue_id

    conn = db.get_connection()
    db.ensure_schema(conn)

    edition = db.get_edition(conn, issue_date, issue_id)
    if not edition:
        print(f'Error: edition {issue_date}-{issue_id} not found. Run prefetch.py first.',
              file=sys.stderr)
        return 1

    articles = db.get_articles_for_edition(conn, edition['id'])
    if not articles:
        print('No articles found for this edition.', file=sys.stderr)
        return 1

    calibre_formats = [f for f in formats if f in ('mobi', 'pdf', 'epub')]
    if not calibre_formats:
        print('No valid format specified (mobi, pdf, epub).', file=sys.stderr)
        return 1

    # Find cover image
    cover_path = edition.get('cover_path') or ''
    if cover_path and not os.path.isabs(cover_path):
        cover_path = os.path.join(REPO_ROOT, cover_path)
    if not cover_path or not os.path.isfile(cover_path):
        # Try to find from images dir
        src_images = os.path.join(REPO_ROOT, 'images', f'{issue_date}-{issue_id}')
        if os.path.isdir(src_images):
            candidates = sorted(glob.glob(os.path.join(src_images, '*.jpg')))
            if candidates:
                cover_path = candidates[0]

    tmpdir, translate_model = export_temp_cache(conn, edition, articles, issue_date, issue_id)
    try:
        results = build_calibre(issue_date, issue_id, tmpdir, cover_path,
                                calibre_formats, translate_model)
        print(f'Built: {", ".join(results)}', flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
