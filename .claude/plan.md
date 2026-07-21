# Plan: Merge article source captures into a single bundle.warc.gz

## Objective
Change `source-archive article` so it produces one merged `bundle.warc.gz` containing the article HTML as the index record followed by all captured remote source records, plus standalone `article.html` and a new `sources.json` format.

## Files to change
1. `source_archive/capture.py` — small refactor to make response collection reusable without writing artifacts.
2. `source_archive/article.py` — main implementation.
3. `source_archive/cli.py` — update article output formatting.
4. `tests/test_article.py` — update expectations.

## Detailed implementation

### 1. `source_archive/capture.py`
- Add `_collect_rendered_responses(url, timeout)` helper that runs Playwright, intercepts every network response, and returns `(ok, responses, main_status, main_headers, main_content_type, html)` without writing any files.
- Keep `_render_and_capture()` as a thin wrapper around the new helper that also writes `screenshot.png`, preserving existing behavior for the `capture`/`batch` commands.
- Export `_fetch`, `_classify`, `classify_content_type`, `_write_warc`, `_status_reason`, `_user_agent`, `_collect_rendered_responses` for use by `article.py`.

### 2. `source_archive/article.py`
- Replace `_fetch_article_html()` with `_fetch_article(url, timeout)` that returns `(status, content_type, headers, body_bytes, html_text)` via `requests.get()` + `_user_agent()`.
- In `capture_article_sources()`:
  1. Fetch article response and extract/filter remote URLs (unchanged logic).
  2. Create `<output_dir>/article_<hash>/`.
  3. Save `article.html` from the fetched body.
  4. For each remote URL (unless `dry_run`):
     - Classify via `_classify()`; if that fails, record failure.
     - If HTML: try `_collect_rendered_responses()`; on failure fall back to `_fetch()`.
     - If binary: fetch full body with `_fetch()`.
     - Append all successfully fetched responses to a single `all_responses` list, keeping a per-source count.
     - Sleep `delay` between sources.
  5. Build article response record and prepend it to `all_responses`.
  6. Write `bundle.warc.gz` using `_write_warc()` with metadata: `software`, `format`, `captured_at`, `article_url`.
  7. Write `sources.json` in the new format with `article_url`, `article_domain`, `captured_at`, `total_links_found`, `excluded`, `bundle_warc`, `article_html`, `sources` (url/status/content_type/warc_records/error), and `summary`.
- Remove `_capture_remote_urls()` and the call to `capture_url()`.

### 3. `source_archive/cli.py`
- Update `article` command output to reference `bundle_warc` instead of per-source `warc_path`, and print the bundle path/size after capture.
- Keep dry-run output unchanged.

### 4. `tests/test_article.py`
- Keep link-extraction, dry-run, and local-file tests; only adjust assertions that reference per-source WARC paths.
- Update `test_full_capture_calls_capture_url_for_each_source` to test that `bundle.warc.gz` is created and `sources.json` reports `bundle_warc`/`article_html`.
- Update failure test to expect `bundle_warc` and `article_html` plus per-source `warc_records==0`.
- Update CLI mock result in `test_cli_article_capture_exits_nonzero_on_failure` to match new result shape.

## Verification steps
1. Run existing tests and fix any regressions.
2. Build package / install in editable mode if needed.
3. Run `source-archive article <url> --dry-run` on the Substack URL to confirm link extraction works.
4. Run a real capture limiting to 3 sources (e.g., via a small test helper or by temporarily accepting `--limit`) and use `warcio` to read back records, confirming article HTML is the first response record.
5. Run the full test suite.
6. `git add -A && git commit -m 'Merge all source captures into single bundle.warc.gz with article as index record'`.
