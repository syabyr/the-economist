#!/usr/bin/env python3
"""Phase 1: Fetch edition metadata + article content + images → SQLite.

Usage:
    python3 prefetch.py YYYY-MM-DD [ISSUE_ID] [--skip-images]

If ISSUE_ID is omitted it is derived from the ISO week number.
Set ECONOMIST_CACHE_ONLY=1 to skip network fetches for articles already in the DB.
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode, urlparse, quote

import db
import utils

# ── GraphQL queries ─────────────────────────────────────────────────────────

EDITION_QUERY = (
    'query FindEditionByDate($issueDate: Date!, $editionType: EditionType!) { '
    'findEditionByDate(issueDate: $issueDate, editionType: $editionType) { '
    'headline issueDate cover { url } sections { name articles { headline url rubric flyTitle } } '
    '} }'
)

ARTICLE_QUERY = (
    'query ArticleDeeplinkQuery($ref: String!, $includeRelatedArticles: Boolean = true ) { '
    'findArticleByUrl(url: $ref) { __typename ...ArticleDataFragment } }  '
    'fragment ContentIdentityFragment on ContentIdentity { articleType forceAppWebView leadMediaType }  '
    'fragment NarrationFragment on Narration { album bitrate duration filename id provider url isAiGenerated fileHash }  '
    'fragment ImageTeaserFragment on ImageComponent { altText height imageType source url width }  '
    'fragment PodcastAudioFragment on PodcastEpisode { id audio { url durationInSeconds } }  '
    'fragment ArticleTeaserFragment on Article { id tegId url rubric headline flyTitle brand byline dateFirstPublished '
    'dateline dateModified datePublished dateRevised estimatedReadTime wordCount printHeadline contentIdentity '
    '{ __typename ...ContentIdentityFragment } section { tegId name } teaserImage { __typename type '
    '...ImageTeaserFragment } leadComponent { __typename type ...ImageTeaserFragment } '
    'narration(selectionMethod: PREFER_ACTOR_NARRATION) { __typename ...NarrationFragment } '
    'podcast { __typename ...PodcastAudioFragment } }  '
    'fragment AnnotatedTextFragment on AnnotatedText { text textJson annotations { type length index attributes { name value } } }  '
    'fragment ImageComponentFragment on ImageComponent { altText caption { __typename ...AnnotatedTextFragment } credit height imageType mode source url width }  '
    'fragment BlockQuoteComponentFragment on BlockQuoteComponent { text textJson annotations { type length index attributes { name value } } }  '
    'fragment BookInfoComponentFragment on BookInfoComponent { text textJson annotations { type length index attributes { name value } } }  '
    'fragment ParagraphComponentFragment on ParagraphComponent { text textJson annotations { type length index attributes { name value } } }  '
    'fragment PullQuoteComponentFragment on PullQuoteComponent { text textJson annotations { type length index attributes { name value } } }  '
    'fragment CrossheadComponentFragment on CrossheadComponent { text }  '
    'fragment OrderedListComponentFragment on OrderedListComponent { items { __typename ...AnnotatedTextFragment } }  '
    'fragment UnorderedListComponentFragment on UnorderedListComponent { items { __typename ...AnnotatedTextFragment } }  '
    'fragment VideoComponentFragment on VideoComponent { url title thumbnailImage }  '
    'fragment InfoboxComponentFragment on InfoboxComponent { components { __typename type '
    '...BlockQuoteComponentFragment ...BookInfoComponentFragment ...ParagraphComponentFragment '
    '...PullQuoteComponentFragment ...CrossheadComponentFragment ...OrderedListComponentFragment '
    '...UnorderedListComponentFragment ...VideoComponentFragment } }  '
    'fragment InfographicComponentFragment on InfographicComponent { url title width fallback '
    '{ __typename ...ImageComponentFragment } altText height width }  '
    'fragment ArticleDataFragment on Article { id url brand byline rubric headline layout { headerStyle } '
    'contentIdentity { __typename ...ContentIdentityFragment } dateline dateFirstPublished dateModified '
    'datePublished dateRevised estimatedReadTime narration(selectionMethod: PREFER_ACTOR_NARRATION) '
    '{ __typename ...NarrationFragment } printFlyTitle printHeadline printRubric flyTitle wordCount '
    'section { tegId name articles(pagingInfo: { pagingType: OFFSET pageSize: 6 pageNumber: 1 } ) '
    '@include(if: $includeRelatedArticles) { edges { node { __typename ...ArticleTeaserFragment } } } } '
    'teaserImage { __typename type ...ImageComponentFragment } tegId leadComponent { __typename type '
    '...ImageComponentFragment } body { __typename type ...BlockQuoteComponentFragment '
    '...BookInfoComponentFragment ...ParagraphComponentFragment ...PullQuoteComponentFragment '
    '...CrossheadComponentFragment ...OrderedListComponentFragment ...UnorderedListComponentFragment '
    '...InfoboxComponentFragment ...ImageComponentFragment ...VideoComponentFragment ...InfographicComponentFragment } '
    'footer { __typename type ...ParagraphComponentFragment } tags { name } ads { adData } '
    'podcast { __typename ...PodcastAudioFragment } }'
)

TOPIC_FALLBACKS = [
    ('The world this week', 'https://www.economist.com/topics/the-world-this-week', 'the-world-this-week'),
    ('Leaders', 'https://www.economist.com/topics/leaders', 'leaders'),
    ('Letters', 'https://www.economist.com/topics/letters', 'letters'),
    ('By Invitation', 'https://www.economist.com/topics/by-invitation', 'by-invitation'),
    ('Briefing', 'https://www.economist.com/topics/briefing', 'briefing'),
    ('Britain', 'https://www.economist.com/topics/britain', 'britain'),
    ('Europe', 'https://www.economist.com/topics/europe', 'europe'),
    ('United States', 'https://www.economist.com/topics/united-states', 'united-states'),
    ('Middle East & Africa', 'https://www.economist.com/middle-east-and-africa', 'middle-east-and-africa'),
    ('The Americas', 'https://www.economist.com/topics/the-americas', 'the-americas'),
    ('Asia', 'https://www.economist.com/topics/asia', 'asia'),
    ('China', 'https://www.economist.com/topics/china', 'china'),
    ('International', 'https://www.economist.com/topics/international', 'international'),
    ('1843', 'https://www.economist.com/1843', '1843'),
    ('Business', 'https://www.economist.com/topics/business', 'business'),
    ('Finance & economics', 'https://www.economist.com/topics/finance-and-economics', 'finance-and-economics'),
    ('Science & technology', 'https://www.economist.com/topics/science-and-technology', 'science-and-technology'),
    ('Culture', 'https://www.economist.com/topics/culture', 'culture'),
    ('Economic & financial indicators', 'https://www.economist.com/topics/economic-and-financial-indicators', 'economic-and-financial-indicators'),
    ('Obituary', 'https://www.economist.com/topics/obituary', 'obituary'),
]

# ── Data fetch (web scraping replaces decommissioned GraphQL API) ───────────


def fetch_edition_from_web(issue_date):
    """Fetch edition metadata from the weeklyedition web page __NEXT_DATA__.

    The Economist decommissioned their GraphQL API (cp2-graphql-gateway returns
    TLS errors and www.economist.com/graphql returns HTTP 410).  This function
    scrapes the public weeklyedition page instead, returning the same shape as
    the old FindEditionByDate GraphQL response.
    """
    try:
        html = utils.fetch_html('https://www.economist.com/weeklyedition/' + issue_date)
    except Exception as exc:
        print(f'weeklyedition page fetch failed for {issue_date}: {exc}', file=sys.stderr, flush=True)
        return {}
    data = utils.parse_next_data(html)
    content = ((data.get('props') or {}).get('pageProps') or {}).get('content') or {}
    if not content:
        return {}

    # Convert web page components → GraphQL-compatible sections
    sections = []
    for comp in content.get('components') or []:
        if comp.get('type') != 'COLLECTION':
            continue
        name = comp.get('name') or ''
        articles = []
        for art in comp.get('articles') or []:
            headline = art.get('headline') or ''
            url = art.get('url') or ''
            if not headline or not url:
                continue
            articles.append({
                'headline': headline,
                'url': url,
                'rubric': art.get('rubric'),
                'flyTitle': art.get('flyTitle'),
            })
        if articles:
            sections.append({'name': name, 'articles': articles})

    return {
        'headline': content.get('headline'),
        'issueDate': content.get('issueDate') or (issue_date + 'T00:00:00.000Z'),
        'cover': content.get('cover'),
        'sections': sections,
    }


def fetch_article_from_web(url):
    """Fetch article data by scraping the article web page __NEXT_DATA__.

    Returns a dict mimicking the old GraphQL ArticleDeeplinkQuery response:
        {'data': {'findArticleByUrl': article_dict}}

    The web page embeds the same backend data model under
    props.pageProps.content, so the extracted fields (headline, body,
    leadComponent, narration, etc.) are structurally compatible with the
    GraphQL response.
    """
    full_url = utils.canonical_article_url(url)
    if not full_url:
        return {'data': {'findArticleByUrl': {}}}

    # Interactive articles don't embed __NEXT_DATA__ — they need a browser.
    if '/interactive/' in full_url:
        raise RuntimeError('Interactive articles are not supported (requires a browser)')

    try:
        html = utils.fetch_html(full_url)
    except Exception as exc:
        raise RuntimeError(f'Failed to fetch article page {full_url}: {exc}') from exc

    data = utils.parse_next_data(html)
    article_data = (
        ((data.get('props') or {}).get('pageProps') or {}).get('cp2Content')
        or ((data.get('props') or {}).get('pageProps') or {}).get('content')
        or {}
    )

    # Ensure the body nodes are keyed by type (GraphQL convention) for
    # downstream consumers (translate_issue_cache, etc.).  The web page
    # already uses __typename on richer nodes, but the raw 'type' string
    # on simple nodes is all most code paths check.
    return {'data': {'findArticleByUrl': article_data}}


def fetch_article_html_fallback(url):
    """Fetch article content from public HTML page as fallback when GraphQL is down."""
    try:
        html = utils.fetch_html(url)
        data = utils.parse_next_data(html)
        page_props = (data.get('props') or {}).get('pageProps') or {}
        content = page_props.get('content') or {}

        if not content or not content.get('body'):
            return None

        # Map HTML page data structure to match GraphQL response structure
        return {
            'id': content.get('id'),
            'url': url,
            'brand': content.get('brand'),
            'byline': content.get('byline'),
            'rubric': content.get('rubric'),
            'headline': content.get('headline'),
            'flyTitle': content.get('flyTitle') or content.get('flyTitleToDisplay'),
            'dateFirstPublished': content.get('dateFirstPublished'),
            'datePublished': content.get('datePublished') or content.get('dateFirstPublished'),
            'dateModified': content.get('dateModified'),
            'dateRevised': content.get('dateRevised'),
            'estimatedReadTime': content.get('estimatedReadTime'),
            'wordCount': content.get('wordCount'),
            'printHeadline': content.get('printHeadline'),
            'printRubric': content.get('printRubric'),
            'section': content.get('section') or {},
            'teaserImage': content.get('teaserImage') or {},
            'leadComponent': content.get('leadComponent') or {},
            'body': content.get('body') or [],
            'footer': content.get('footer') or [],
            'tags': content.get('tags') or [],
            'narration': content.get('narration') or {},
            'podcast': content.get('podcast') or {},
            'contentIdentity': content.get('contentIdentity') or {},
        }
    except Exception as exc:
        print(f'  HTML fallback failed: {exc}', file=sys.stderr, flush=True)
        return None


# ── Section ordering ────────────────────────────────────────────────────────

def audio_m3u_candidates(issue_date):
    compact = issue_date.replace('-', '')
    candidates = []
    explicit = os.environ.get('ECONOMIST_AUDIO_ORDER_FILE', '').strip()
    if explicit:
        candidates.append(explicit)
    base_dir = os.environ.get('ECONOMIST_AUDIO_BASE_DIR', '').strip()
    if base_dir:
        candidates.append(os.path.join(base_dir, compact, compact + '.m3u'))
    cwd = os.getcwd()
    candidates.extend([
        os.path.join(cwd, 'audio', compact, compact + '.m3u'),
        os.path.join(cwd, '..', 'audio', compact, compact + '.m3u'),
    ])
    return candidates


def parse_audio_m3u_order(issue_date):
    path = ''
    for candidate in audio_m3u_candidates(issue_date):
        if candidate and os.path.isfile(candidate):
            path = candidate
            break
    if not path:
        return []
    sections = []
    section_map = {}
    pending_extinf = ''
    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#EXTINF:'):
                pending_extinf = line
                continue
            if not line.endswith('.mp3'):
                continue
            name = os.path.basename(line)
            match = re.match(r'^\d+-(.+?)---(.+)-[0-9a-f]{32}\.mp3$', name)
            if not match:
                pending_extinf = ''
                continue
            section_name = match.group(1).replace('-', ' ').strip()
            title = match.group(2).replace('-', ' ').replace('_', "'").strip()
            if pending_extinf:
                try:
                    info = pending_extinf.split(',', 1)[1]
                    parts = info.split(' - ')
                    if len(parts) >= 3:
                        parsed_section = re.sub(r'^\d+\s+', '', parts[1]).strip()
                        parsed_title = ' - '.join(parts[2:]).strip()
                        if parsed_section:
                            section_name = parsed_section
                        if parsed_title:
                            title = parsed_title
                except Exception:
                    pass
            pending_extinf = ''
            section_key = utils.normalize_text_key(section_name)
            if not section_key:
                continue
            section_bucket = section_map.get(section_key)
            if section_bucket is None:
                section_bucket = {'name': section_name, 'articles': []}
                section_map[section_key] = section_bucket
                sections.append(section_bucket)
            section_bucket['articles'].append({'headline': title})
    if sections:
        print('Loaded audio m3u ordering from ' + path, flush=True)
    return sections


def fetch_weeklyedition_sections(issue_date):
    try:
        html = utils.fetch_html('https://www.economist.com/weeklyedition/' + issue_date)
        data = utils.parse_next_data(html)
        content = ((data.get('props') or {}).get('pageProps') or {}).get('content') or {}
        return (content.get('headerSections') or []) + (content.get('sections') or [])
    except Exception as exc:
        print(f'failed weeklyedition order fetch for {issue_date}: {exc}', file=sys.stderr, flush=True)
        return []


def reorder_edition_sections(edition, ordered_sections):
    if not ordered_sections:
        return edition
    source_sections = list(edition.get('sections') or ())
    if not source_sections:
        return edition
    used_sections = [False] * len(source_sections)
    result_sections = []
    for ordered in ordered_sections:
        section_name = (ordered.get('name') or '').strip()
        section_key = utils.normalize_text_key(section_name)
        if not section_key:
            continue
        source_index = None
        source_section = None
        for idx, candidate in enumerate(source_sections):
            if used_sections[idx]:
                continue
            if utils.keys_match((candidate.get('name') or '').strip(), section_name):
                source_index = idx
                source_section = candidate
                break
        if source_section is None:
            continue
        source_articles = list(source_section.get('articles') or ())
        used_articles = [False] * len(source_articles)
        ordered_articles = []
        for ordered_article in ordered.get('articles') or ():
            ordered_url = utils.canonical_article_url(ordered_article.get('url') or '')
            ordered_headline = (ordered_article.get('headline') or '').strip()
            picked_index = None
            if ordered_url:
                for idx, article in enumerate(source_articles):
                    if used_articles[idx]:
                        continue
                    if utils.canonical_article_url(article.get('url') or '') == ordered_url:
                        picked_index = idx
                        break
            if picked_index is None and ordered_headline:
                for idx, article in enumerate(source_articles):
                    if used_articles[idx]:
                        continue
                    if utils.keys_match((article.get('headline') or '').strip(), ordered_headline):
                        picked_index = idx
                        break
            if picked_index is not None:
                used_articles[picked_index] = True
                ordered_articles.append(source_articles[picked_index])
        for idx, article in enumerate(source_articles):
            if not used_articles[idx]:
                ordered_articles.append(article)
        merged = dict(source_section)
        merged['articles'] = ordered_articles
        result_sections.append(merged)
        used_sections[source_index] = True
    for idx, section in enumerate(source_sections):
        if not used_sections[idx]:
            result_sections.append(section)
    merged_edition = dict(edition)
    merged_edition['sections'] = result_sections
    return merged_edition


def resolve_ordered_sections(issue_date):
    """Resolve preferred section/article ordering for an issue.

    Priority:
      1) official weeklyedition page order
      2) local audio m3u order as fallback
    """
    ordered_sections = fetch_weeklyedition_sections(issue_date)
    if ordered_sections:
        print('Using weeklyedition page ordering', flush=True)
        return ordered_sections

    ordered_sections = parse_audio_m3u_order(issue_date)
    if ordered_sections:
        print('Using audio m3u ordering fallback', flush=True)
    return ordered_sections


# ── Topic fallback ──────────────────────────────────────────────────────────

def article_url_date(url):
    match = re.search(r'/(20\d\d)/(\d\d)/(\d\d)/', url or '')
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    except ValueError:
        return None


def article_path_section(url):
    parts = [part for part in urlparse(url).path.split('/') if part]
    if parts and parts[0] == 'interactive' and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else ''


def topic_article_allowed(article, section_slug, start_date, end_date):
    url = article.get('url') or ''
    if not url:
        return False
    path = urlparse(url).path
    if path.startswith('/podcasts/') or path.startswith('/in-brief/'):
        return False
    if 'newsletter' in (article.get('headline') or '').lower():
        return False
    url_date = article_url_date(url)
    if not url_date or url_date < start_date or url_date > end_date:
        return False
    return article_path_section(url) == section_slug


def build_topic_fallback_edition(issue_date):
    issue_day = datetime.strptime(issue_date, '%Y-%m-%d').date()
    start_day = issue_day - timedelta(days=8)
    seen_urls = set()
    sections = []
    for section_name, topic_url, section_slug in TOPIC_FALLBACKS:
        print(f'No edition yet; scanning topic {section_name}: {topic_url}', flush=True)
        try:
            html = utils.fetch_html(topic_url)
            data = utils.parse_next_data(html)
        except Exception as exc:
            print(f'failed topic {section_name}: {exc}', file=sys.stderr, flush=True)
            continue
        content = ((data.get('props') or {}).get('pageProps') or {}).get('content') or {}
        articles = []
        for article in content.get('articles') or ():
            if not isinstance(article, dict):
                continue
            if not topic_article_allowed(article, section_slug, start_day, issue_day):
                continue
            normalized = {
                'headline': article.get('headline') or '',
                'url': article.get('url') or '',
                'rubric': article.get('rubric'),
                'flyTitle': article.get('flyTitle') or article.get('flyTitleToDisplay'),
            }
            url = normalized.get('url') or ''
            if not normalized.get('headline') or url in seen_urls:
                continue
            seen_urls.add(url)
            articles.append(normalized)
        if articles:
            sections.append({'name': section_name, 'articles': articles})
    article_count = sum(len(section.get('articles') or ()) for section in sections)
    if article_count == 0:
        return {}
    edition = {
        'headline': 'Current weekly articles',
        'issueDate': issue_date + 'T00:00:00.000Z',
        'cover': None,
        'sections': sections,
    }
    print(
        f'Built fallback edition from topics: {article_count} articles '
        f'from {start_day.isoformat()} through {issue_day.isoformat()}',
        flush=True,
    )
    return edition


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Prefetch Economist edition into SQLite cache')
    parser.add_argument('issue_date', help='YYYY-MM-DD')
    parser.add_argument('issue_id', nargs='?', default=None, help='Issue ID (auto-derived if omitted)')
    parser.add_argument('--skip-images', action='store_true', help='Skip image download')
    args = parser.parse_args()

    issue_date = args.issue_date
    issue_id = args.issue_id or db.derive_issue_id(issue_date)
    cache_only = utils.env_flag('ECONOMIST_CACHE_ONLY')

    conn = db.get_connection()
    db.ensure_schema(conn)

    # ── 1. Fetch edition manifest ───────────────────────────────────────
    print(f'Fetching edition {issue_date} (id={issue_id})...', flush=True)
    edition = fetch_edition_from_web(issue_date)
    if not edition:
        edition = build_topic_fallback_edition(issue_date)
    if not edition:
        print('No edition found for ' + issue_date, file=sys.stderr)
        return 1

    # Reorder sections
    ordered_sections = resolve_ordered_sections(issue_date)
    if ordered_sections:
        edition = reorder_edition_sections(edition, ordered_sections)

    # Cover image
    cover_url = ((edition.get('cover') or {}).get('url') or '')
    cover_path = ''
    if cover_url:
        cover_path = db.image_local_path(issue_date, issue_id, cover_url)

    # Store edition
    sections_json = json.dumps(edition.get('sections') or [], ensure_ascii=False)
    manifest_json = json.dumps(edition, ensure_ascii=False)
    edition_db_id = db.upsert_edition(
        conn, issue_date, issue_id,
        headline=edition.get('headline'),
        cover_url=cover_url,
        cover_path=cover_path,
        sections_json=sections_json,
        manifest_json=manifest_json,
    )
    print(f'Edition saved (db id={edition_db_id}): {edition.get("headline")}', flush=True)

    # ── 2. Collect article URLs ─────────────────────────────────────────
    urls = []
    for section in edition.get('sections') or ():
        for article in section.get('articles') or ():
            url = utils.canonical_article_url(article.get('url') or '')
            if url and url not in urls:
                urls.append(url)
    print(f'{len(urls)} articles to fetch', flush=True)

    # ── 3. Fetch articles ───────────────────────────────────────────────
    all_image_urls = set()
    audio_jobs = []
    article_order = 0

    for section_idx, section in enumerate(edition.get('sections') or ()):
        section_name = section.get('name') or ''
        section_articles = section.get('articles') or ()
        for article_idx, article_meta in enumerate(section_articles):
            url = utils.canonical_article_url(article_meta.get('url') or '')
            if not url:
                continue
            article_order += 1
            print(f'[{article_order}/{len(urls)}] {url}', flush=True)

            # Check cache
            if cache_only and db.article_exists(conn, edition_db_id, url):
                print(f'  (cached, skipping fetch)', flush=True)
                continue

            article_data = None
            try:
                payload = fetch_article_from_web(url)
                article_data = (payload.get('data') or {}).get('findArticleByUrl')
            except Exception as exc:
                print(f'  Web fetch failed, trying HTML fallback: {exc}', file=sys.stderr, flush=True)
                article_data = fetch_article_html_fallback(url)

            if not article_data:
                print(f'  WARNING: empty article data for {url}', flush=True)
                continue

            # Extract fields
            headline = article_data.get('headline') or ''
            fly_title = article_data.get('flyTitle') or ''
            rubric = article_data.get('rubric') or ''
            word_count = article_data.get('wordCount')
            estimated_read_time = article_data.get('estimatedReadTime')
            if estimated_read_time is not None:
                estimated_read_time = int(estimated_read_time)
            date_published = (article_data.get('datePublished')
                              or article_data.get('dateFirstPublished') or '')

            # Audio
            audio_links = utils.collect_audio_links(article_data)
            audio_url = audio_links[0]['url'] if audio_links else ''
            audio_duration = audio_links[0].get('duration') if audio_links else None

            # Images in article (preserve traversal order so first image aligns with lead image)
            article_image_urls = []
            article_image_seen = set()
            lead = article_data.get('leadComponent') or {}
            utils.collect_image_urls_ordered(lead, article_image_urls, article_image_seen)
            for node in article_data.get('body') or ():
                utils.collect_image_urls_ordered(node, article_image_urls, article_image_seen)
            all_image_urls.update(article_image_urls)

            # Extract URL slugs
            section_slug, _, _, _, article_slug = db.parse_article_url_path(url)
            if not section_slug:
                section_slug = db.slugify_section(section_name)
            if not article_slug:
                article_slug = utils.slugify(headline or 'untitled')[:80]

            # Compress body_json
            body_blob = gzip.compress(json.dumps(article_data, ensure_ascii=False).encode('utf-8'))

            article_id = db.upsert_article(
                conn, edition_db_id, url,
                headline=headline,
                fly_title=fly_title,
                rubric=rubric,
                section_name=section_name,
                section_order=article_idx,
                article_order=article_order,
                body_json_blob=body_blob,
                audio_url=audio_url,
                audio_duration=audio_duration,
                image_urls_json=json.dumps(article_image_urls, ensure_ascii=False),
                word_count=word_count,
                estimated_read_time=estimated_read_time,
                date_published=date_published,
                section_slug=section_slug,
                slug=article_slug,
            )

            if audio_url:
                audio_jobs.append({'article_id': article_id, 'audio_url': audio_url})

    # ── 4. Download images ──────────────────────────────────────────────
    image_list = sorted(all_image_urls)
    if cover_url:
        image_list.insert(0, cover_url)

    if args.skip_images:
        print(f'Skipping image download ({len(image_list)} images)', flush=True)
    else:
        asset_dir = db.ensure_asset_dir(os.getcwd(), issue_date, issue_id, 'images')
        downloaded = 0
        skipped = 0
        for idx, img_url in enumerate(image_list, start=1):
            fname = db._image_hash_filename(img_url)
            dest = os.path.join(asset_dir, fname)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                skipped += 1
                continue
            print(f'[img {idx}/{len(image_list)}] {img_url}', flush=True)
            try:
                raw = utils.fetch(img_url)
                utils.write_file_atomic(dest, raw)
                downloaded += 1
            except Exception as exc:
                print(f'  failed image: {exc}', file=sys.stderr, flush=True)
        print(f'Images: {downloaded} downloaded, {skipped} skipped', flush=True)

    # ── 5. Download audio ───────────────────────────────────────────────
    if audio_jobs:
        audio_asset_dir = db.ensure_asset_dir(os.getcwd(), issue_date, issue_id, 'audio')
        audio_downloaded = 0
        audio_skipped = 0
        for idx, job in enumerate(audio_jobs, start=1):
            audio_url = job['audio_url']
            article_id = job['article_id']
            fname = db._audio_filename(audio_url)
            dest = os.path.join(audio_asset_dir, fname)
            rel_path = db.audio_local_path(issue_date, issue_id, audio_url)

            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                audio_skipped += 1
                db.update_article_audio_path(conn, article_id, rel_path)
                continue

            print(f'[audio {idx}/{len(audio_jobs)}] {audio_url}', flush=True)
            try:
                raw = utils.fetch(audio_url)
                utils.write_file_atomic(dest, raw)
                db.update_article_audio_path(conn, article_id, rel_path)
                audio_downloaded += 1
            except Exception as exc:
                print(f'  failed audio: {exc}', file=sys.stderr, flush=True)
        print(f'Audio: {audio_downloaded} downloaded, {audio_skipped} skipped', flush=True)

    # ── 6. Summary ──────────────────────────────────────────────────────
    article_count = conn.execute(
        "SELECT COUNT(*) FROM article WHERE edition_id=?", (edition_db_id,)
    ).fetchone()[0]
    print(f'Done: {article_count} articles cached for {issue_date}-{issue_id}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
