#!/usr/bin/env python3
import json
import os
import re
import sys
import gzip
import socket
import ssl
import time
from datetime import datetime, timedelta
from html import unescape
from hashlib import sha256
from urllib.parse import quote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


HEADERS = {
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


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def cache_path(cache_dir, group, key):
    return os.path.join(
        ensure_dir(os.path.join(cache_dir, group)),
        sha256(key.encode('utf-8')).hexdigest() + '.json',
    )


def maybe_decompress(payload):
    if payload[:2] == b'\x1f\x8b':
        return gzip.decompress(payload)
    return payload


def image_cache_path(cache_dir, url):
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1] or '.bin'
    return os.path.join(
        ensure_dir(os.path.join(cache_dir, 'images')),
        sha256(url.encode('utf-8')).hexdigest() + ext,
    )


def fetch(url, extra_headers=None):
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    headers = {k: v for k, v in headers.items() if v}
    headers['x-app-trace-id'] = str(uuid4())
    req = Request(url, headers=headers)
    attempts = int(os.environ.get('ECONOMIST_FETCH_RETRIES', '5'))
    base_delay = float(os.environ.get('ECONOMIST_FETCH_RETRY_DELAY', '1.5'))
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=120) as resp:
                return resp.read()
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt == attempts:
                raise
        except (URLError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            last_exc = exc
            if attempt == attempts:
                raise
        delay = base_delay * attempt
        print(
            f'retry {attempt}/{attempts} for {url}: {last_exc}',
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    raise last_exc


def fetch_graphql(cache_dir, operation_name, query, variables):
    params = {
        'operationName': operation_name,
        'variables': json.dumps(variables, separators=(',', ':')),
        'query': query,
    }
    key = operation_name + '\n' + json.dumps(variables, sort_keys=True)
    url = 'https://cp2-graphql-gateway.p.aws.economist.com/graphql?' + urlencode(
        params, safe='()!', quote_via=quote
    )
    raw = fetch(url)
    write_cached_payload(cache_dir, 'editions', key, raw)
    try:
        return json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError:
        return json.loads(gzip.decompress(raw).decode('utf-8'))


def write_cached_payload(cache_dir, group, key, payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    with open(cache_path(cache_dir, group, key), 'wb') as fh:
        fh.write(payload)


def fetch_html(url):
    raw = fetch(
        url,
        {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'content-type': '',
        },
    )
    return maybe_decompress(raw).decode('utf-8', 'replace')


def fetch_article(cache_dir, url):
    params = {
        'operationName': 'ArticleDeeplinkQuery',
        'variables': json.dumps({'ref': url}, separators=(',', ':')),
        'query': ARTICLE_QUERY,
    }
    deep_url = 'https://cp2-graphql-gateway.p.aws.economist.com/graphql?' + urlencode(
        params, safe='()!', quote_via=quote
    )
    raw = fetch(deep_url)
    with open(cache_path(cache_dir, 'articles', url), 'wb') as fh:
        fh.write(raw)
    return json.loads(maybe_decompress(raw).decode('utf-8'))


def collect_image_urls(node, out):
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


def collect_audio_links(article):
    links = []
    narration = article.get('narration') or {}
    narration_url = narration.get('url') or ''
    if narration_url:
        links.append(
            {
                'type': 'narration',
                'url': narration_url,
                'duration': narration.get('duration'),
                'filename': narration.get('filename'),
                'provider': narration.get('provider'),
                'is_ai_generated': narration.get('isAiGenerated'),
                'file_hash': narration.get('fileHash'),
            }
        )

    podcast_audio = ((article.get('podcast') or {}).get('audio') or {})
    podcast_url = podcast_audio.get('url') or ''
    if podcast_url:
        links.append(
            {
                'type': 'podcast',
                'url': podcast_url,
                'duration': podcast_audio.get('durationInSeconds'),
            }
        )
    return links


def fetch_image(cache_dir, url):
    path = image_cache_path(cache_dir, url)
    if os.path.exists(path):
        return
    raw = fetch(url)
    with open(path, 'wb') as fh:
        fh.write(raw)


def parse_next_data(html):
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        flags=re.S,
    )
    if not match:
        raise ValueError('missing __NEXT_DATA__')
    return json.loads(unescape(match.group(1)))


def article_url_date(url):
    match = re.search(r'/(20\d\d)/(\d\d)/(\d\d)/', url or '')
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).date()
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


def normalize_topic_article(article):
    return {
        'headline': article.get('headline') or '',
        'url': article.get('url') or '',
        'rubric': article.get('rubric'),
        'flyTitle': article.get('flyTitle') or article.get('flyTitleToDisplay'),
    }


def build_topic_fallback_edition(cache_dir, issue_date):
    issue_day = datetime.strptime(issue_date, '%Y-%m-%d').date()
    start_day = issue_day - timedelta(days=8)
    seen_urls = set()
    sections = []

    for section_name, topic_url, section_slug in TOPIC_FALLBACKS:
        print(f'No edition yet; scanning topic {section_name}: {topic_url}', flush=True)
        try:
            html = fetch_html(topic_url)
            data = parse_next_data(html)
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
            normalized = normalize_topic_article(article)
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

    key = 'FindEditionByDate\n' + json.dumps(
        {'issueDate': issue_date, 'editionType': 'WEEKLY'}, sort_keys=True
    )
    synthetic_payload = {'data': {'findEditionByDate': edition}}
    write_cached_payload(
        cache_dir,
        'editions',
        key,
        json.dumps(synthetic_payload, separators=(',', ':')),
    )
    print(
        f'Built fallback edition from topics: {article_count} articles '
        f'from {start_day.isoformat()} through {issue_day.isoformat()}',
        flush=True,
    )
    return edition


def main():
    if len(sys.argv) != 2:
        print('Usage: prefetch_issue.py YYYY-MM-DD', file=sys.stderr)
        return 1

    issue_date = sys.argv[1]
    cache_dir = ensure_dir(
        os.environ.get(
            'ECONOMIST_CONTENT_CACHE_DIR',
            os.path.join(os.getcwd(), 'economist_content_cache', issue_date),
        )
    )

    payload = fetch_graphql(
        cache_dir,
        'FindEditionByDate',
        EDITION_QUERY,
        {'issueDate': issue_date, 'editionType': 'WEEKLY'},
    )
    edition = ((payload.get('data') or {}).get('findEditionByDate') or {})
    if not edition:
        edition = build_topic_fallback_edition(cache_dir, issue_date)
    if not edition:
        print('No edition found for ' + issue_date, file=sys.stderr)
        return 1

    urls = []
    audio_items = []
    image_urls = set()
    cover_url = ((edition.get('cover') or {}).get('url') or '')
    if cover_url:
        image_urls.add(cover_url)
    for section in edition.get('sections') or ():
        for article in section.get('articles') or ():
            url = article.get('url') or ''
            if not url:
                continue
            if url.startswith('/'):
                url = 'https://www.economist.com' + url
            urls.append(url)

    with open(os.path.join(cache_dir, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(
            {
                'issue_date': issue_date,
                'headline': edition.get('headline'),
                'article_count': len(urls),
                'urls': urls,
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    for index, url in enumerate(urls, start=1):
        print(f'[{index}/{len(urls)}] {url}', flush=True)
        payload = fetch_article(cache_dir, url)
        article = ((payload.get('data') or {}).get('findArticleByUrl') or {})
        audio_links = collect_audio_links(article)
        if audio_links:
            audio_items.append(
                {
                    'title': article.get('headline') or '',
                    'url': url,
                    'audio': audio_links,
                }
            )
        lead = article.get('leadComponent') or {}
        collect_image_urls(lead, image_urls)
        for node in article.get('body') or ():
            collect_image_urls(node, image_urls)

    with open(os.path.join(cache_dir, 'audio.json'), 'w', encoding='utf-8') as fh:
        json.dump(
            {
                'issue_date': issue_date,
                'audio_count': sum(len(item.get('audio') or ()) for item in audio_items),
                'article_count': len(audio_items),
                'items': audio_items,
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    image_list = sorted(image_urls)
    for index, url in enumerate(image_list, start=1):
        print(f'[img {index}/{len(image_list)}] {url}', flush=True)
        try:
            fetch_image(cache_dir, url)
        except Exception as exc:
            print(f'failed image {url}: {exc}', file=sys.stderr, flush=True)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
