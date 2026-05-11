#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
from hashlib import sha256
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HEADERS = {
    'User-Agent': 'TheEconomist-Liskov-android',
    'accept': 'audio/mpeg,audio/*,*/*;q=0.8',
    'x-economist-consumer': 'TheEconomist-Liskov-android',
    'x-teg-client-name': 'Economist-Android',
    'x-teg-client-os': 'Android',
    'x-teg-client-version': '4.40.0',
}

RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def issue_date_compact(issue_date):
    return issue_date.replace('-', '')


def slugify(value, max_len=90):
    value = re.sub(r'[^A-Za-z0-9]+', '-', value or '').strip('-')
    value = re.sub(r'-+', '-', value)
    return (value or 'audio')[:max_len].strip('-') or 'audio'


def extension_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext and re.fullmatch(r'\.[a-z0-9]{1,8}', ext):
        return ext
    return '.mp3'


def target_filename(index, item, audio):
    title = item.get('title') or os.path.basename(urlparse(item.get('url') or '').path)
    digest = sha256((audio.get('url') or '').encode('utf-8')).hexdigest()[:16]
    ext = extension_from_url(audio.get('url') or '')
    return f'{index:03d}-{slugify(title)}-{digest}{ext}'


def fetch(url, retries=5, retry_delay=1.5):
    req = Request(url, headers=HEADERS)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(req, timeout=120) as resp:
                return resp.read()
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt == retries:
                raise
        except (URLError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            last_exc = exc
            if attempt == retries:
                raise
        delay = retry_delay * attempt
        print(
            f'retry {attempt}/{retries} for {url}: {last_exc}',
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    raise last_exc


def write_file_atomic(path, payload):
    tmp_path = path + '.tmp'
    with open(tmp_path, 'wb') as fh:
        fh.write(payload)
    os.replace(tmp_path, path)


def load_audio_manifest(cache_dir):
    path = os.path.join(cache_dir, 'audio.json')
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def download_audio(cache_dir, library_root=None, retries=5, retry_delay=1.5):
    manifest = load_audio_manifest(cache_dir)
    issue_date = manifest.get('issue_date') or ''
    issue_compact = issue_date_compact(issue_date)
    output_dir = ensure_dir(os.path.join(cache_dir, 'audio'))
    library_dir = ensure_dir(os.path.join(library_root, issue_compact)) if library_root else ''
    records = []
    download_index = 0

    for item in manifest.get('items') or []:
        for audio in item.get('audio') or []:
            url = audio.get('url') or ''
            if not url:
                continue
            download_index += 1
            filename = target_filename(download_index, item, audio)
            output_path = os.path.join(output_dir, filename)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                status = 'skipped'
            else:
                print(f'[{download_index}] {filename}', flush=True)
                payload = fetch(url, retries=retries, retry_delay=retry_delay)
                write_file_atomic(output_path, payload)
                status = 'downloaded'

            library_path = ''
            if library_dir:
                library_path = os.path.join(library_dir, filename)
                if not os.path.exists(library_path):
                    shutil.copy2(output_path, library_path)

            records.append(
                {
                    'title': item.get('title') or '',
                    'article_url': item.get('url') or '',
                    'audio_type': audio.get('type') or '',
                    'audio_url': url,
                    'filename': filename,
                    'path': output_path,
                    'library_path': library_path,
                    'status': status,
                }
            )

    playlist_path = os.path.join(output_dir, f'{issue_compact}.m3u')
    with open(playlist_path, 'w', encoding='utf-8') as fh:
        fh.write('#EXTM3U\n')
        for record in records:
            fh.write(f'#EXTINF:-1,{record["title"]}\n')
            fh.write(record['filename'] + '\n')

    with open(os.path.join(output_dir, 'download_manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(
            {
                'issue_date': issue_date,
                'audio_count': len(records),
                'output_dir': output_dir,
                'library_dir': library_dir,
                'items': records,
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    return output_dir, library_dir, records


def main():
    parser = argparse.ArgumentParser(
        description='Download Economist article MP3 files from a cache audio.json.'
    )
    parser.add_argument('cache_dir', help='Issue cache directory containing audio.json')
    parser.add_argument(
        '--library-root',
        default='',
        help='Optional root directory to copy files into ROOT/YYYYMMDD',
    )
    parser.add_argument('--retries', type=int, default=5)
    parser.add_argument('--retry-delay', type=float, default=1.5)
    args = parser.parse_args()

    cache_dir = os.path.abspath(args.cache_dir)
    output_dir, library_dir, records = download_audio(
        cache_dir,
        library_root=args.library_root or None,
        retries=max(1, args.retries),
        retry_delay=args.retry_delay,
    )
    print(f'Done: {len(records)} audio files in {output_dir}')
    if library_dir:
        print(f'Copied to: {library_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
