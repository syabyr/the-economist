# Make Multiple Kindle Books

## Bilingual English + Chinese Output

The recipe can now translate each text block to Simplified Chinese and render both versions in the generated ebook.

### NVIDIA Free Translation Model

The easiest way to use NVIDIA's hosted translation model is:

```sh
export NVIDIA_API_KEY=your_nvidia_key
./scrap-zh-nvidia.sh 2026-04-11 9494
```

This wrapper enables:

- `ECONOMIST_TRANSLATE_PROVIDER=nvidia`
- `ECONOMIST_TRANSLATE_MODEL=nvidia/riva-translate-4b-instruct-v1.1`
- English to Simplified Chinese bilingual output

Model availability changes over time. `translate.py` defaults to
`minimaxai/minimax-m3.1-flash`; `google/gemma-4-31b-it` currently hangs and
several catalog models return 404 for accounts that are not provisioned.
Override the model with `--model` or `ECONOMIST_TRANSLATE_MODEL`. The build
step reads the model recorded in the translation cache, so its `translate.json`
keys always match what `translate.py` stored.

### Three-Stage Workflow

If you want to separate network download, translation, and final output generation:

```sh
./download-issue.sh 2026-04-25 9496
export NVIDIA_API_KEY=your_nvidia_key
./translate-zh-nvidia.sh 2026-04-25 9496
./build-issue.sh 2026-04-25 9496
```

This stores:

- raw issue/article payloads under `economist_content_cache/<date>-<issue>/`
- translation cache under `economist_content_cache/<date>-<issue>/translate.json`
- `.mobi` under `<year>/`
- `.pdf` under `pdf/<year>/`

### Import full article bodies from an EPUB

If `prefetch.py` only captured the two-paragraph teaser (anonymous GraphQL
requests), import the complete text from a downloaded EPUB:

```sh
python3 tools/import_epub.py temp/TheEconomist.2026.10.24.epub --dry-run   # preview
python3 tools/import_epub.py temp/TheEconomist.2026.10.24.epub --apply     # write
```

The script matches EPUB pages to DB articles by GraphQL article id, then by URL,
then by headline. It replaces `body_json`, copies inline images that prefetch
never saw into `images/<date>-<id>/`, refreshes word counts, and writes a backup
under `temp/import-epub-backup-*.json.gz`. Re-running is a no-op once the bodies
are complete (use `--force` to re-import). Run `translate.py` afterwards to
translate the new paragraphs.

### Generic OpenAI-Compatible Mode

You can also enable translation manually before running `scrap.sh` or `scrap-pdf.sh`:

```sh
export ECONOMIST_TRANSLATE_ZH=1
export OPENAI_API_KEY=your_api_key
export ECONOMIST_TRANSLATE_MODEL=gpt-4.1-mini
./scrap.sh 2026-04-11 9494
```

Optional variables:

```sh
export OPENAI_BASE_URL=https://api.openai.com/v1
export ECONOMIST_TRANSLATE_BASE_URL=https://your-openai-compatible-endpoint/v1
export ECONOMIST_TRANSLATE_TIMEOUT=120
export ECONOMIST_TRANSLATE_CACHE=economist_content_cache/2026-04-25-9496/translate.json
export ECONOMIST_TRANSLATE_PROVIDER=nvidia
export NVIDIA_API_KEY=your_nvidia_key
export ECONOMIST_TRANSLATE_MODEL=nvidia/riva-translate-4b-instruct-v1.1
```

Notes:

- Translation is off by default.
- The recipe caches translated fragments locally, so reruns do not retranslate the same paragraphs.
- When translation is enabled, the ebook shows English first and Chinese directly underneath.

```sh
./scrap.sh 2025-12-13 9478
./scrap.sh 2025-12-20 9479
./scrap.sh 2025-12-27 9480


```
