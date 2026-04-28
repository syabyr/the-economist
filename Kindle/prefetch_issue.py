#!/usr/bin/env python3
import json
import os
import sys
import gzip
import socket
import ssl
import time
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


def fetch(url):
    headers = dict(HEADERS)
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
    with open(cache_path(cache_dir, 'editions', key), 'wb') as fh:
        fh.write(raw)
    try:
        return json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError:
        return json.loads(gzip.decompress(raw).decode('utf-8'))


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


def fetch_image(cache_dir, url):
    path = image_cache_path(cache_dir, url)
    if os.path.exists(path):
        return
    raw = fetch(url)
    with open(path, 'wb') as fh:
        fh.write(raw)


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
        print('No edition found for ' + issue_date, file=sys.stderr)
        return 1

    urls = []
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
        lead = article.get('leadComponent') or {}
        collect_image_urls(lead, image_urls)
        for node in article.get('body') or ():
            collect_image_urls(node, image_urls)

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
