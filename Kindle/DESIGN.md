# Economist Cache System Redesign

## 1. Overview

Replace the current file-based JSON cache (`economist_content_cache/<date>-<id>/`) with a **SQLite database** for all text/string content, while keeping **file-system storage** for binary assets (images, audio).

### Goals
1. **Unified cache** for PDF/mobi generation and web viewing
2. **SQL-based querying** — filter by date, section, article, translation status
3. **Online web viewer** — browse editions, read articles side-by-side (EN/ZH), play audio, view images
4. **Clean separation** — text in SQLite, binaries in `audio/` and `images/` directories

---

## 2. SQLite Schema

### 2.1 `edition` — Weekly edition manifest

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Issue number, e.g. `9500` |
| `issue_date` | TEXT NOT NULL | `"2026-05-23"` |
| `headline` | TEXT | Edition headline |
| `cover_url` | TEXT | Remote cover image URL |
| `cover_path` | TEXT | Local cached image path (relative) |
| `sections_json` | TEXT | Full sections array with article URLs (JSON) |
| `manifest_json` | TEXT | Full edition metadata (JSON) |
| `created_at` | TEXT | ISO timestamp |
| `updated_at` | TEXT | ISO timestamp |

- **UNIQUE** constraint on `issue_date`

### 2.2 `article` — Individual article

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `edition_id` | INTEGER FK → edition.id | Parent edition |
| `url` | TEXT NOT NULL | Canonical economist.com URL |
| `article_key` | TEXT NOT NULL UNIQUE | `sha256(url)` stable article key |
| `headline` | TEXT | Article headline |
| `fly_title` | TEXT | "Fly title" / overline |
| `rubric` | TEXT | Section rubric (e.g., "Leaders") |
| `section_name` | TEXT | Section display name |
| `section_order` | INTEGER | Position within section |
| `article_order` | INTEGER | Global position within edition |
| `body_json` | TEXT | Full GraphQL article JSON (compressed) |
| `audio_url` | TEXT | Narration/podcast audio URL |
| `audio_path` | TEXT | Local cached audio path (relative) |
| `audio_duration` | INTEGER | Duration in seconds |
| `image_urls_json` | TEXT | JSON array of image URLs in article |
| `word_count` | INTEGER | Word count |
| `estimated_read_time` | INTEGER | Read time in minutes |
| `date_published` | TEXT | Publication date |
| `created_at` | TEXT | ISO timestamp |

- **UNIQUE** constraint on `(edition_id, url)`
- Index on `(issue_date, section_name)` via JOIN for quick lookups

### 2.3 `translation` — Fragment-level translation cache

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `article_key` | TEXT FK → article.article_key | Parent article |
| `hash_id` | TEXT NOT NULL | SHA256 of source fragment text |
| `source_text` | TEXT | Original English fragment |
| `translated_text` | TEXT | Translated Chinese fragment |
| `model` | TEXT | Model used (e.g., `nvidia/riva-translate-4b-instruct-v1.1`) |
| `source_language` | TEXT | `"English"` |
| `target_language` | TEXT | `"Simplified Chinese"` |
| `fragment_type` | TEXT | `paragraph`, `heading`, `caption`, `fly_title`, `rubric`, `list_item`, `pull_quote`, `crosshead` |
| `fragment_order` | INTEGER | Order within article (for reconstruction) |
| `created_at` | TEXT | ISO timestamp |

- Index on `hash_id` for fast cache lookup
- Index on `(article_key, fragment_order)` for ordered retrieval

### 2.4 SQLite pragmas

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

---

## 3. Directory Structure

```
Kindle/
├── economist.sqlite        # SQLite database (all text/string content)
├── images/
│   ├── 2026-05-23-9500/
│   │   ├── <sha256>.jpg        # Article images, keyed by SHA256 of URL
│   │   └── ...
│   └── 2026-05-30-9501/
│       └── ...
├── audio/
│   ├── 2026-05-23-9500/
│   │   ├── <sha256>.mp3        # Narration audio, keyed by SHA256 of URL
│   │   └── ...
│   └── 2026-05-30-9501/
│       └── ...
├── output/
│   ├── 2026/
│   │   └── TheEconomist-2026-05-23-9500.mobi
│   └── pdf/
│       └── 2026/
│           └── TheEconomist-2026-05-23-9500.pdf
├── prefetch.py                 # Phase 1: fetch & cache
├── translate.py                # Phase 2: translate & cache
├── build_issue.py              # Phase 3: build mobi/pdf + web HTML
├── web/
│   ├── server.py               # Flask web app
│   ├── templates/
│   │   ├── index.html          # Edition list
│   │   ├── edition.html        # Articles in an edition
│   │   └── article.html        # Side-by-side article view
│   └── static/
│       └── style.css           # economist.com-inspired styling
└── economist.recipe            # (kept for calibre compatibility)
```

**Rationale**: `images/` and `audio/` organized by `<date>-<id>` flat directory (same convention as the old `economist_content_cache/<date>-<id>/`), so you can browse/clean them manually. Database paths store relative paths like `images/2026-05-23-9500/<hash>.jpg`.

---

## 4. Core Functions

### 4.1 `prefetch.py`

**Usage**: `python3 prefetch.py YYYY-MM-DD [ISSUE_ID]`

If `ISSUE_ID` is omitted, derive it from the ISO week number.

**Flow**:

```
1. Open economist.sqlite, ensure schema exists
2. Query GraphQL FindEditionByDate(issueDate, WEEKLY)
3. If edition not found → fallback: scan topic pages, build synthetic edition
4. Reorder sections using weeklyedition.com HTML or audio .m3u ordering
5. UPSERT into `edition` table
6. For each article URL in edition:
   a. Check `article` table — if exists with same url+edition_id, skip fetch (use ECONOMIST_CACHE_ONLY)
   b. Fetch via ArticleDeeplinkQuery
   c. Extract: headline, rubric, fly_title, section, word_count, read_time, audio
   d. Store body_json as gzip-compressed JSON string
   e. Collect image URLs from leadComponent + body nodes
   f. INSERT/UPDATE `article` row
   g. Download images → images/<date>-<id>/<sha256(url)>.<ext>
   h. Update article.image_urls_json and article.image_paths
7. Print summary: N articles, M images, K audio links
```

**Key decisions**:
- If `ECONOMIST_CACHE_ONLY=1` is set, skip network fetches for articles that already exist in DB
- Image download can be skipped with `--skip-images` flag
- Audio is NOT downloaded in this phase — only URLs are recorded. Audio download happens separately.

### 4.2 `translate.py`

**Usage**: `python3 translate.py YYYY-MM-DD [ISSUE_ID]`

**Flow**:

```
1. Open economist.sqlite
2. Lookup edition by issue_date[/issue_id]
3. For each article in edition:
   a. Parse body_json to extract translatable fragments
   b. Fragment types: fly_title, headline, rubric, paragraph, block_quote,
      pull_quote, crosshead, image_caption, list_item, infobox_text
   c. For each fragment:
      - Compute hash_id = SHA256(fragment_text)
      - Check `translation` table for hash_id
      - If found → skip (cache hit)
      - If not found → call translation API → INSERT into `translation`
      - Apply rate limiting / retry with backoff
4. Print summary: N fragments, M cache hits, K new translations
```

**Fragment extraction** (ported from `translate_issue_cache.py:collect_fragments`):
- Same logic: walk `body_json` nodes, extract `textHtml` or render `textJson` via `parse_textjson()`
- Preserve inline HTML tags (links, emphasis, superscript, subscript)
- Store both raw HTML fragment and plain-text version

**Translation API** (same as current `Translator` class):
- Provider-agnostic: OpenAI-compatible `/chat/completions` endpoint
- Supports NVIDIA, OpenAI, or any compatible provider
- Configurable via environment variables (same names as current)

### 4.3 `build_issue.py`

**Usage**: `python3 build_issue.py YYYY-MM-DD ISSUE_ID [--format mobi,pdf,web]`

**Flow**:

```
1. Open economist.sqlite, load edition + articles + translations
2. Check that all articles exist (warn if any missing body_json)
3. Generate calibre recipe dynamically:
   a. Write temp .recipe file from template
   b. Set edition_date
   c. Point ECONOMIST_CONTENT_CACHE_DIR to a temp dir with article data
      OR: modify recipe to read directly from SQLite
4. If --format includes mobi:
   ebook-convert <recipe> .mobi --output-profile=kindle_oasis ...
5. If --format includes pdf:
   ebook-convert <recipe> .pdf --paper-size=a4 ...
6. If --format includes web:
   Generate static HTML files in web/static/build/<date>-<id>/
   (alternative to Flask dynamic server — see Section 5)
7. Move outputs to output/<year>/ and output/pdf/<year>/
```

**Web HTML generation** (for `--format web`):
- Generate `index.html` (all editions list)
- Generate per-edition `edition.html` (article list by section)
- Generate per-article `article.html` (side-by-side bilingual view)
- All HTML is self-contained (inline CSS) and can be opened directly in browser
- Images referenced via relative paths to `../../images/<date>-<id>/`
- Audio referenced via relative paths to `../../audio/<date>-<id>/`

---

## 5. Web Viewer

Two modes, both supported:

### 5.1 Dynamic (Flask) — `web/server.py`

```
python3 web/server.py          # starts http://localhost:8080
python3 web/server.py --port 9090
```

**Routes**:

| Route | Description |
|-------|-------------|
| `/` | List all editions (date, headline, article count, translation status) |
| `/weeklyedition/<issue_date>` | Edition detail — articles grouped by section |
| `/<section_slug>/<year>/<month>/<day>/<article_slug>` | Article view — side-by-side EN/ZH |

**Article page layout** (mimicking economist.com):
```
┌──────────────────────────────────────────────────────┐
│  The Economist — Edition 2026-05-23                  │
│  ← Back to edition                                   │
├──────────────────────────────────────────────────────┤
│  [Section Rubric]                                    │
│  Article Headline                                    │
│  Fly title | Date | Read time | Audio player ▶️      │
├─────────────────────────┬────────────────────────────┤
│  English (Original)     │  中文 (Simplified Chinese)  │
│                         │                            │
│  Paragraph 1 text...    │  段落1翻译文本...            │
│                         │                            │
│  [Image]                │  [Image]                   │
│  Image caption text     │  图片说明翻译               │
│                         │                            │
│  Paragraph 2 text...    │  段落2翻译文本...            │
│                         │                            │
├─────────────────────────┴────────────────────────────┤
│  ← Previous Article  |  Next Article →               │
└──────────────────────────────────────────────────────┘
```

**Styling** (economist.com-inspired):
- Font: Georgia / "Noto Serif SC" (for Chinese)
- Body width: max 1200px (side-by-side needs more space)
- Red accent: `#E3120B`
- Section rubrics in small red caps
- Clean typography, generous whitespace
- Responsive: stacks vertically on narrow screens

**Audio**: HTML5 `<audio>` player at top of article if `audio_path` exists.

### 5.2 Static HTML (generated by `build_issue.py --format web`)

Same HTML structure as the Flask templates, but pre-rendered to static files. Can be opened directly with `file://` or served by any HTTP server:

```
python3 -m http.server 8080 -d web/static/build/
```

---

## 6. Environment Variables

Keep backward compatibility with existing variable names where possible:

| Variable | Used by | Description |
|----------|---------|-------------|
| `ECONOMIST_DB_PATH` | all | Path to economist.sqlite (default: `./economist.sqlite`) |
| `ECONOMIST_IMAGES_DIR` | prefetch, web | Images base dir (default: `./images`) |
| `ECONOMIST_AUDIO_DIR` | prefetch, web | Audio base dir (default: `./audio`) |
| `ECONOMIST_OUTPUT_DIR` | build | Output base dir (default: `./output`) |
| `ECONOMIST_CACHE_ONLY` | prefetch | Skip network fetch if article exists in DB |
| `ECONOMIST_TRANSLATE_ZH` | translate, build | Enable Chinese translation |
| `ECONOMIST_TRANSLATE_PROVIDER` | translate | `openai` or `nvidia` |
| `ECONOMIST_TRANSLATE_MODEL` | translate | Model name |
| `ECONOMIST_TRANSLATE_BASE_URL` | translate | API endpoint |
| `ECONOMIST_TRANSLATE_API_KEY` | translate | API key |
| `ECONOMIST_TRANSLATE_SOURCE_LANGUAGE` | translate | Source lang (default: `English`) |
| `ECONOMIST_TRANSLATE_TARGET_LANGUAGE` | translate | Target lang (default: `Simplified Chinese`) |
| `NVIDIA_API_KEY` | translate | Fallback API key |
| `ECONOMIST_FETCH_RETRIES` | prefetch | Retry count for HTTP (default: 5) |
| `ECONOMIST_FETCH_RETRY_DELAY` | prefetch | Retry base delay in seconds (default: 1.5) |

---

## 7. Migration Path

Existing `economist_content_cache/` directories are NOT deleted automatically. A migration script (`migrate_cache.py`) can import old JSON caches into the new SQLite database:

```
python3 migrate_cache.py economist_content_cache/2026-05-23-9500/
```

This reads the old `manifest.json`, `articles/*.json`, `translate.json` and populates the SQLite tables. Images are symlinked (not copied) to the new directory structure.

---

## 8. Open Questions for Confirmation

1. **Flask vs static HTML**: Should the web viewer be a Flask app (dynamic, searchable) or static HTML files (no server needed)? I recommend **supporting both**: Flask for daily browsing, static HTML generation for archiving/portability.

2. **issue_id auto-derivation**: When `ISSUE_ID` is omitted, should prefetch derive it from the ISO week number? The current convention maps `2026-05-23` → `9500`. I'll add a mapping table/function for this.

3. **Audio download**: Should audio be downloaded during `prefetch.py` or as a separate step? Current design records URLs during prefetch but requires a separate download step. Confirm if you want a `--download-audio` flag in prefetch.

4. **Backward compatibility**: Should the new `build_issue.py` still use `economist.recipe` + `ebook-convert` (calibre), or should it generate ebooks directly? I recommend keeping calibre as the rendering engine since the recipe already handles bilingual layout well.

5. **Database location**: Single `economist.sqlite` at the repo root, or one per edition? I recommend a **single database** — SQLite handles millions of rows easily, and cross-edition queries become trivial.

6. **Compression**: Store `body_json` as gzip-compressed blob (saves ~70% space) or plain JSON text (more queryable)? I recommend **compressed** since we extract the queryable fields (headline, url, section) into separate columns anyway.
