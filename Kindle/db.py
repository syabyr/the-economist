#!/usr/bin/env python3
"""SQLite database layer for the Economist cache system.

Single database file: economist.sqlite
Tables: edition, article, translation
"""

import os
import re
import sqlite3
import gzip
import json
from datetime import datetime, date
from hashlib import sha256
from urllib.parse import urlparse, unquote

import utils


def _default_db_candidates():
    module_dir = os.path.dirname(os.path.abspath(__file__))
    cwd_path = os.path.join(os.getcwd(), 'economist.sqlite')
    module_path = os.path.join(module_dir, 'economist.sqlite')

    candidates = [cwd_path]
    if module_path != cwd_path:
        candidates.append(module_path)
    return candidates


def _has_expected_schema(db_path):
    if not os.path.exists(db_path):
        return False

    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edition'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_db_path():
    env_path = os.environ.get('ECONOMIST_DB_PATH')
    if env_path:
        return env_path

    for candidate in _default_db_candidates():
        if _has_expected_schema(candidate):
            return candidate

    return _default_db_candidates()[0]


def get_connection(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema ──────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS edition (
    id              INTEGER PRIMARY KEY,
    issue_date      TEXT NOT NULL,
    headline        TEXT,
    cover_url       TEXT,
    cover_path      TEXT,
    sections_json   TEXT,
    manifest_json   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(issue_date)
);

CREATE TABLE IF NOT EXISTS article (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id          INTEGER NOT NULL REFERENCES edition(id),
    url                 TEXT NOT NULL,
    article_key         TEXT NOT NULL UNIQUE,
    headline            TEXT,
    fly_title           TEXT,
    rubric              TEXT,
    section_name        TEXT,
    section_order       INTEGER,
    article_order       INTEGER,
    body_json           BLOB,
    audio_url           TEXT,
    audio_path          TEXT,
    audio_duration      INTEGER,
    image_urls_json     TEXT,
    word_count          INTEGER,
    estimated_read_time INTEGER,
    date_published      TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(edition_id, url)
);

CREATE INDEX IF NOT EXISTS idx_article_edition ON article(edition_id);
CREATE INDEX IF NOT EXISTS idx_article_section ON article(edition_id, section_name, section_order);

CREATE TABLE IF NOT EXISTS translation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_key     TEXT NOT NULL REFERENCES article(article_key),
    hash_id         TEXT NOT NULL,
    source_text     TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    model           TEXT,
    source_language TEXT,
    target_language TEXT,
    fragment_type   TEXT,
    fragment_order  INTEGER,
    created_at      TEXT NOT NULL,
    UNIQUE(article_key, hash_id)
);

CREATE INDEX IF NOT EXISTS idx_translation_hash ON translation(hash_id);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    conn.commit()


def _table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def article_key_for_url(url):
    return sha256((url or '').encode('utf-8')).hexdigest()


def _migrate_edition_issue_id_to_id(conn):
    columns = _table_columns(conn, 'edition')
    if 'issue_id' not in columns:
        return

    duplicate = conn.execute(
        "SELECT issue_id FROM edition GROUP BY issue_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate:
        raise RuntimeError(f"Cannot migrate duplicate issue_id value: {duplicate['issue_id']}")

    invalid = conn.execute(
        "SELECT issue_id FROM edition WHERE issue_id IS NULL OR issue_id = '' OR issue_id GLOB '*[^0-9]*' LIMIT 1"
    ).fetchone()
    if invalid:
        raise RuntimeError(f"Cannot migrate non-numeric issue_id value: {invalid['issue_id']}")

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE edition_new (
                id              INTEGER PRIMARY KEY,
                issue_date      TEXT NOT NULL,
                headline        TEXT,
                cover_url       TEXT,
                cover_path      TEXT,
                sections_json   TEXT,
                manifest_json   TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE(issue_date)
            )
        """)
        conn.execute("""
            INSERT INTO edition_new (
                id, issue_date, headline, cover_url, cover_path,
                sections_json, manifest_json, created_at, updated_at
            )
            SELECT
                CAST(issue_id AS INTEGER), issue_date, headline, cover_url, cover_path,
                sections_json, manifest_json, created_at, updated_at
            FROM edition
            ORDER BY issue_date
        """)
        conn.execute("""
            UPDATE article
            SET edition_id = (
                SELECT CAST(e.issue_id AS INTEGER)
                FROM edition e
                WHERE e.id = article.edition_id
            )
            WHERE EXISTS (
                SELECT 1 FROM edition e WHERE e.id = article.edition_id
            )
        """)
        conn.execute("DROP TABLE edition")
        conn.execute("ALTER TABLE edition_new RENAME TO edition")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _ensure_article_key_column(conn):
    columns = _table_columns(conn, 'article')
    if 'article_key' not in columns:
        conn.execute("ALTER TABLE article ADD COLUMN article_key TEXT")
        conn.commit()

    rows = conn.execute(
        "SELECT id, url FROM article WHERE article_key IS NULL OR article_key = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE article SET article_key=? WHERE id=?",
            (article_key_for_url(row['url']), row['id'])
        )
    if rows:
        conn.commit()

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_article_article_key ON article(article_key)")
    conn.commit()


def _migrate_translation_article_id_to_article_key(conn):
    columns = _table_columns(conn, 'translation')
    if 'article_id' not in columns:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_translation_article_key ON translation(article_key, fragment_order)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_translation_hash ON translation(hash_id)")
        conn.commit()
        return

    _ensure_article_key_column(conn)

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE translation_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                article_key     TEXT NOT NULL REFERENCES article(article_key),
                hash_id         TEXT NOT NULL,
                source_text     TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                model           TEXT,
                source_language TEXT,
                target_language TEXT,
                fragment_type   TEXT,
                fragment_order  INTEGER,
                created_at      TEXT NOT NULL,
                UNIQUE(article_key, hash_id)
            )
        """)
        conn.execute("""
            INSERT INTO translation_new (
                article_key, hash_id, source_text, translated_text, model,
                source_language, target_language, fragment_type, fragment_order, created_at
            )
            SELECT
                a.article_key, t.hash_id, t.source_text, t.translated_text, t.model,
                t.source_language, t.target_language, t.fragment_type, t.fragment_order, t.created_at
            FROM translation t
            JOIN article a ON a.id = t.article_id
            ORDER BY t.id
        """)
        conn.execute("DROP TABLE translation")
        conn.execute("ALTER TABLE translation_new RENAME TO translation")
        conn.execute("CREATE INDEX idx_translation_hash ON translation(hash_id)")
        conn.execute("CREATE INDEX idx_translation_article_key ON translation(article_key, fragment_order)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def run_migrations(conn):
    """Idempotent migrations for columns added after initial schema."""
    _migrate_edition_issue_id_to_id(conn)
    _ensure_article_key_column(conn)
    _migrate_translation_article_id_to_article_key(conn)

    migrations = [
        "ALTER TABLE article ADD COLUMN section_slug TEXT",
        "ALTER TABLE article ADD COLUMN slug TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_slug ON article(section_slug, slug, date_published)"
    )
    conn.commit()


# ── ISO week derivation ─────────────────────────────────────────────────────

def derive_issue_id(issue_date_str):
    """Derive issue_id from ISO week number.

    Formula: issue_id = BASE + iso_week_number
    BASE defaults to 9479 (configurable via ECONOMIST_ISSUE_ID_BASE).
    Example: 2026-05-23 (ISO week 21) -> 9479 + 21 = 9500
    """
    base = int(os.environ.get('ECONOMIST_ISSUE_ID_BASE', '9479'))
    d = date.fromisoformat(issue_date_str)
    iso_week = d.isocalendar()[1]
    return str(base + iso_week)


# ── Path helpers ────────────────────────────────────────────────────────────

def _image_hash_filename(url):
    ext = os.path.splitext(urlparse(url).path)[1]
    if not ext or not re.fullmatch(r'\.[a-z0-9]{1,8}', ext.lower()):
        ext = '.jpg'
    return sha256(url.encode('utf-8')).hexdigest() + ext


def image_local_path(issue_date, issue_id, url):
    """Relative path: images/<date>-<id>/<sha256>.<ext>"""
    return os.path.join('images', f'{issue_date}-{issue_id}', _image_hash_filename(url))


def _audio_filename(url, index=None):
    """Return just the filename for an audio URL."""
    raw_name = os.path.basename(urlparse(url).path or '')
    decoded = unquote(raw_name or '').strip()
    decoded = decoded.replace('/', '_').replace('\\', '_')
    decoded = re.sub(r'[\x00-\x1f\x7f]', '', decoded)
    decoded = re.sub(r'[<>:"|?*]', '_', decoded)
    decoded = re.sub(r'\s+', '-', decoded).strip('-')

    if not decoded:
        decoded = 'audio.mp3'

    base, ext = os.path.splitext(decoded)
    if not ext or not re.fullmatch(r'\.[a-z0-9]{1,8}', ext.lower()):
        decoded = f"{(base or 'audio')}.mp3"

    prefix = f'{index:03d}-' if index is not None else ''
    return f'{prefix}{decoded}'


def audio_local_path(issue_date, issue_id, url, index=None):
    """Relative path: audio/<date>-<id>/<decoded-original-filename>"""
    return os.path.join('audio', f'{issue_date}-{issue_id}',
                        _audio_filename(url, index))


def ensure_asset_dir(base_dir, issue_date, issue_id, asset_type):
    """Create and return images/ or audio/ subdirectory for an edition."""
    d = os.path.join(base_dir, asset_type, f'{issue_date}-{issue_id}')
    os.makedirs(d, exist_ok=True)
    return d


# ── Edition CRUD ────────────────────────────────────────────────────────────

def upsert_edition(conn, issue_date, issue_id, headline=None, cover_url=None,
                   cover_path=None, sections_json=None, manifest_json=None):
    now = datetime.utcnow().isoformat() + 'Z'
    edition_id = int(issue_id)
    conn.execute("""
        INSERT INTO edition (id, issue_date, headline, cover_url, cover_path,
                             sections_json, manifest_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            issue_date=excluded.issue_date,
            headline=excluded.headline,
            cover_url=excluded.cover_url,
            cover_path=excluded.cover_path,
            sections_json=excluded.sections_json,
            manifest_json=excluded.manifest_json,
            updated_at=excluded.updated_at
    """, (edition_id, issue_date, headline, cover_url, cover_path,
          sections_json, manifest_json, now, now))
    conn.commit()
    return edition_id


def get_edition(conn, issue_date, issue_id=None):
    if issue_id:
        row = conn.execute(
            "SELECT * FROM edition WHERE issue_date=? AND id=?",
            (issue_date, int(issue_id))
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM edition WHERE issue_date=? ORDER BY id DESC LIMIT 1",
            (issue_date,)
        ).fetchone()
    return dict(row) if row else None


def list_editions(conn, limit=50):
    rows = conn.execute(
        "SELECT * FROM edition ORDER BY issue_date DESC, id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    editions = []
    for row in rows:
        ed = dict(row)
        ed['article_count'] = conn.execute(
            "SELECT COUNT(*) FROM article WHERE edition_id=?", (ed['id'],)
        ).fetchone()[0]
        ed['translated_count'] = conn.execute("""
            SELECT COUNT(DISTINCT article_key) FROM translation
            WHERE article_key IN (SELECT article_key FROM article WHERE edition_id=?)
        """, (ed['id'],)).fetchone()[0]
        editions.append(ed)
    return editions


# ── Article CRUD ────────────────────────────────────────────────────────────

def upsert_article(conn, edition_id, url, headline=None, fly_title=None,
                   rubric=None, section_name=None, section_order=None,
                   article_order=None, body_json_blob=None,
                   audio_url=None, audio_path=None, audio_duration=None,
                   image_urls_json=None, word_count=None,
                   estimated_read_time=None, date_published=None,
                   section_slug=None, slug=None):
    now = datetime.utcnow().isoformat() + 'Z'
    article_key = article_key_for_url(url)
    conn.execute("""
        INSERT INTO article (edition_id, url, article_key, headline, fly_title, rubric,
                             section_name, section_order, article_order,
                             body_json, audio_url, audio_path, audio_duration,
                             image_urls_json, word_count, estimated_read_time,
                             date_published, section_slug, slug, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(edition_id, url) DO UPDATE SET
            article_key=excluded.article_key,
            headline=excluded.headline,
            fly_title=excluded.fly_title,
            rubric=excluded.rubric,
            section_name=excluded.section_name,
            section_order=excluded.section_order,
            article_order=excluded.article_order,
            body_json=COALESCE(excluded.body_json, article.body_json),
            audio_url=COALESCE(excluded.audio_url, article.audio_url),
            audio_path=COALESCE(excluded.audio_path, article.audio_path),
            audio_duration=COALESCE(excluded.audio_duration, article.audio_duration),
            image_urls_json=COALESCE(excluded.image_urls_json, article.image_urls_json),
            word_count=COALESCE(excluded.word_count, article.word_count),
            estimated_read_time=COALESCE(excluded.estimated_read_time, article.estimated_read_time),
            date_published=COALESCE(excluded.date_published, article.date_published),
            section_slug=COALESCE(excluded.section_slug, article.section_slug),
            slug=COALESCE(excluded.slug, article.slug)
    """, (edition_id, url, article_key, headline, fly_title, rubric,
          section_name, section_order, article_order,
          body_json_blob, audio_url, audio_path, audio_duration,
          image_urls_json, word_count, estimated_read_time,
          date_published, section_slug, slug, now))
    conn.commit()
    row = conn.execute(
        "SELECT id FROM article WHERE edition_id=? AND url=?",
        (edition_id, url)
    ).fetchone()
    return row['id']


def get_article(conn, edition_id, url):
    row = conn.execute(
        "SELECT * FROM article WHERE edition_id=? AND url=?",
        (edition_id, url)
    ).fetchone()
    return dict(row) if row else None


def get_article_by_id(conn, article_id):
    row = conn.execute("SELECT * FROM article WHERE id=?", (article_id,)).fetchone()
    return dict(row) if row else None


def get_articles_for_edition(conn, edition_id):
    rows = conn.execute(
        "SELECT * FROM article WHERE edition_id=? ORDER BY article_order, section_order",
        (edition_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def article_exists(conn, edition_id, url):
    row = conn.execute(
        "SELECT 1 FROM article WHERE edition_id=? AND url=?", (edition_id, url)
    ).fetchone()
    return row is not None


def decompress_article_body(article_row):
    blob = article_row.get('body_json')
    if not blob:
        return None
    return json.loads(gzip.decompress(blob).decode('utf-8'))


def update_article_audio_path(conn, article_id, audio_path):
    conn.execute("UPDATE article SET audio_path=? WHERE id=?", (audio_path, article_id))
    conn.commit()


# ── URL slug helpers ─────────────────────────────────────────────────────────

def parse_article_url_path(url):
    """Extract (section_slug, year, month, day, article_slug) from article URL.

    URL pattern: /<section>/<YYYY>/<MM>/<DD>/<slug>
    Example: /culture/2026/05/20/gamified-novels-are-a-winning-format
    Returns: ('culture', 2026, 5, 20, 'gamified-novels-are-a-winning-format')
    Returns (None, None, None, None, None) on parse failure.
    """
    path = urlparse(url).path.strip('/')
    match = re.match(
        r'^([a-z0-9][-a-z0-9]*)/(20\d\d)/(\d\d)/(\d\d)/([-a-z0-9]+)$', path
    )
    if match:
        return (match.group(1), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), match.group(5))
    return (None, None, None, None, None)


def slugify_section(name):
    """Convert section name to URL slug.

    Uses predefined mapping for known sections, falls back to generic slugify.
    """
    mapping = {
        'the world this week': 'the-world-this-week',
        'leaders': 'leaders',
        'letters': 'letters',
        'by invitation': 'by-invitation',
        'briefing': 'briefing',
        'britain': 'britain',
        'europe': 'europe',
        'united states': 'united-states',
        'middle east & africa': 'middle-east-and-africa',
        'the americas': 'the-americas',
        'asia': 'asia',
        'china': 'china',
        'international': 'international',
        '1843': '1843',
        'business': 'business',
        'finance & economics': 'finance-and-economics',
        'science & technology': 'science-and-technology',
        'culture': 'culture',
        'economic & financial indicators': 'economic-and-financial-indicators',
        'obituary': 'obituary',
        'essay': 'essay',
    }
    key = (name or '').strip().lower()
    if key in mapping:
        return mapping[key]
    return utils.slugify(name)


def backfill_article_slugs(conn):
    """Populate section_slug and slug for all articles missing them."""
    rows = conn.execute(
        "SELECT id, url, headline, section_name FROM article "
        "WHERE section_slug IS NULL OR slug IS NULL"
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    for row in rows:
        section_slug, _, _, _, article_slug = parse_article_url_path(row['url'] or '')
        if not section_slug:
            section_slug = slugify_section(row['section_name'])
        if not article_slug:
            article_slug = utils.slugify(row['headline'] or 'untitled')[:80]
        conn.execute(
            "UPDATE article SET section_slug=?, slug=? WHERE id=?",
            (section_slug, article_slug, row['id'])
        )
        updated += 1
    conn.commit()
    return updated


def get_article_by_slug(conn, section_slug, year, month, day, article_slug):
    """Find article by URL components. Matches on section_slug + slug (the date
    components exist for URL structure but may drift from date_published)."""
    row = conn.execute("""
        SELECT * FROM article
        WHERE section_slug=? AND slug=?
        LIMIT 1
    """, (section_slug, article_slug)).fetchone()
    return dict(row) if row else None


def get_article_nav(conn, article_id):
    """Return (prev_article_dict, next_article_dict) for navigation within an edition.
    Each dict has keys: section_slug, slug, date_published (or None if no prev/next).
    """
    art = conn.execute("SELECT edition_id, article_order FROM article WHERE id=?",
                       (article_id,)).fetchone()
    if not art:
        return None, None
    edition_id = art['edition_id']
    order = art['article_order']

    prev_row = conn.execute("""
        SELECT section_slug, slug, date_published FROM article
        WHERE edition_id=? AND article_order < ? ORDER BY article_order DESC LIMIT 1
    """, (edition_id, order)).fetchone()

    next_row = conn.execute("""
        SELECT section_slug, slug, date_published FROM article
        WHERE edition_id=? AND article_order > ? ORDER BY article_order ASC LIMIT 1
    """, (edition_id, order)).fetchone()

    def _nav_dict(row):
        if not row:
            return None
        d = dict(row)
        if not d.get('section_slug') or not d.get('slug'):
            return None
        return d

    return _nav_dict(prev_row), _nav_dict(next_row)


def get_latest_edition(conn):
    """Return the most recent edition for redirect purposes."""
    row = conn.execute(
        "SELECT * FROM edition ORDER BY issue_date DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_edition_by_id(conn, edition_id):
    """Get edition by primary key id."""
    row = conn.execute("SELECT * FROM edition WHERE id=?", (edition_id,)).fetchone()
    return dict(row) if row else None


def get_edition_by_date(conn, issue_date):
    """Get edition by date, returning the latest id match."""
    row = conn.execute(
        "SELECT * FROM edition WHERE issue_date=? ORDER BY id DESC LIMIT 1",
        (issue_date,)
    ).fetchone()
    return dict(row) if row else None


# ── Translation CRUD ────────────────────────────────────────────────────────

def get_translation(conn, hash_id, article_key=None):
    row = None
    if article_key:
        row = conn.execute(
            "SELECT * FROM translation WHERE article_key=? AND hash_id=?",
            (article_key, hash_id)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM translation WHERE hash_id=? LIMIT 1", (hash_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_translation(conn, article_key, hash_id, source_text, translated_text,
                       model=None, source_language=None, target_language=None,
                       fragment_type=None, fragment_order=None):
    now = datetime.utcnow().isoformat() + 'Z'
    conn.execute("""
        INSERT INTO translation
            (article_key, hash_id, source_text, translated_text, model,
             source_language, target_language, fragment_type, fragment_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_key, hash_id) DO UPDATE SET
            translated_text=excluded.translated_text,
            model=COALESCE(excluded.model, translation.model),
            source_language=COALESCE(excluded.source_language, translation.source_language),
            target_language=COALESCE(excluded.target_language, translation.target_language),
            fragment_type=COALESCE(excluded.fragment_type, translation.fragment_type),
            fragment_order=COALESCE(excluded.fragment_order, translation.fragment_order)
    """, (article_key, hash_id, source_text, translated_text, model,
          source_language, target_language, fragment_type, fragment_order, now))
    conn.commit()


def get_translations_for_article(conn, article_key):
    rows = conn.execute(
        "SELECT * FROM translation WHERE article_key=? ORDER BY fragment_order",
        (article_key,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_translations_for_edition(conn, edition_id):
    rows = conn.execute("""
        SELECT t.* FROM translation t
        JOIN article a ON a.article_key = t.article_key
        WHERE a.edition_id = ?
        ORDER BY a.article_order, t.fragment_order
    """, (edition_id,)).fetchall()
    return [dict(r) for r in rows]


def translation_count_for_edition(conn, edition_id):
    total = conn.execute("""
        SELECT COUNT(*) FROM article WHERE edition_id=? AND body_json IS NOT NULL
    """, (edition_id,)).fetchone()[0]
    translated = conn.execute("""
        SELECT COUNT(DISTINCT article_key) FROM translation
        WHERE article_key IN (SELECT article_key FROM article WHERE edition_id=?)
    """, (edition_id,)).fetchone()[0]
    return total, translated
