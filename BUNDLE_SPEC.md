# Merged WARC Bundle — Option B

## Goal

Modify the `source-archive article` command to produce a **single merged WARC file** containing:
1. The article HTML itself as the first record (the "index" record)
2. All remote source captures as subsequent records

Plus the article HTML saved as a standalone file alongside it.

## Current State

The existing `article.py` captures each source URL into individual `captures/<hash>/capture.warc.gz` files. We want to change this to produce ONE merged `bundle.warc.gz` instead.

## New Output Structure

```
<article_dir>/
  article.html              ← our article HTML (standalone, readable)
  bundle.warc.gz            ← ONE merged WARC: article + all sources
  sources.json              ← index listing all source URLs, their status, and WARC record positions
```

No more `captures/` subdirectory with individual WARCs. One file, sealed, publishable.

## Implementation Changes

### 1. Modify `capture_article_sources()` in `article.py`

Instead of calling `capture_url()` (which writes individual WARC files), the function should:

1. **Fetch the article HTML** (already done — keep this)
2. **Extract and filter remote URLs** (already done — keep this)
3. **For each remote URL:**
   - Fetch the raw HTTP response (headers + body) using `requests.get()`
   - Classify as HTML or binary (reuse `classify_content_type()`)
   - If HTML: optionally render with Playwright to capture sub-resources (CSS, JS, images) — collect all HTTP response pairs
   - If binary: just the single HTTP response
   - Collect all response records into a list
4. **Write one merged WARC file** (`bundle.warc.gz`) containing:
   - Record 0: metadata record (software, format, captured_at, article_url)
   - Record 1: `response` record for the article HTML itself (the "index" page)
   - Records 2..N: `response` records for each source URL (and their sub-resources if HTML)
5. **Save `article.html`** as a standalone file
6. **Write `sources.json`** with the list of source URLs, their capture status, and any errors

### 2. WARC Writing

Reuse the existing `_write_warc()` function from `capture.py`, but extend it to accept multiple URLs worth of responses. The function already takes a `responses` list — we just need to pass it a larger list containing all source responses concatenated.

Alternatively, write a new `_write_merged_warc()` function in `article.py` that:
- Takes a list of `(url, status, headers, body)` tuples
- Writes them all into one `.warc.gz` file
- Includes a metadata record at the start

### 3. Handling Playwright sub-resources

For HTML source URLs, we want to capture not just the page HTML but also its CSS/JS/images (same as the existing `capture_url` does via Playwright). The approach:

- For each HTML source URL: run Playwright, intercept all network responses (same as `_render_and_capture()` in capture.py), collect them
- For each binary source URL: just fetch with `requests.get()`
- Collect ALL response records from ALL sources into one big list
- Write them all into the single `bundle.warc.gz`

### 4. Error Handling

- If a source URL fails to fetch (timeout, 404, connection error): record the error in `sources.json` but continue with the next URL
- The merged WARC should contain all successfully captured sources
- `sources.json` should list every source URL with its status (ok/failed) and error message if any

### 5. Article HTML as first record

The article HTML itself should be the first `response` record in the WARC, with:
- WARC-Target-URI: the article URL
- The raw HTML bytes as the body
- HTTP headers from the article fetch

This makes the WARC self-describing: open it, first record is the article, remaining records are the sources it cites.

### 6. sources.json format

```json
{
  "article_url": "https://thegoodclimate.substack.com/p/...",
  "article_domain": "thegoodclimate.substack.com",
  "captured_at": "2026-07-20T23:45:00Z",
  "total_links_found": 66,
  "excluded": {
    "internal": 12,
    "social": 0,
    "duplicates": 5
  },
  "bundle_warc": "bundle.warc.gz",
  "article_html": "article.html",
  "sources": [
    {
      "url": "https://example.com/report.pdf",
      "status": "ok",
      "content_type": "application/pdf",
      "warc_records": 1,
      "error": null
    },
    {
      "url": "https://example.com/news-article",
      "status": "ok",
      "content_type": "text/html",
      "warc_records": 24,
      "error": null
    },
    {
      "url": "https://example.com/broken",
      "status": "failed",
      "content_type": null,
      "warc_records": 0,
      "error": "404 Not Found"
    }
  ],
  "summary": {
    "total_sources": 49,
    "succeeded": 46,
    "failed": 3,
    "total_warc_records": 542,
    "bundle_size_bytes": 12345678
  }
}
```

### 7. CLI changes

The `source-archive article` command should work the same way from the user's perspective:
```bash
source-archive article <url> [--dry-run] [--output-dir DIR] [--timeout N] [--wayback]
```

The only difference is the output: one `bundle.warc.gz` instead of many individual WARCs.

### 8. Testing

- Update existing tests in `test_article.py` to expect `bundle.warc.gz` instead of individual capture dirs
- Test with `--dry-run` (should still work the same)
- Test with a real URL capturing 2-3 sources
- Verify the merged WARC is valid using `warcio` — read back all records, confirm article HTML is first, confirm source records follow
- Run full test suite

### 9. Git

Commit with message: "Merge all source captures into single bundle.warc.gz with article as index record"

## Technical Notes

- The existing `_render_and_capture()` in capture.py already intercepts Playwright network responses — we can reuse or refactor it to return the response list instead of writing its own WARC
- The existing `_write_warc()` in capture.py accepts a `responses` list — we can call it once with the full merged list
- Keep the 1-second delay between source fetches to avoid rate-limiting
- The merged WARC will be larger than individual WARCs but compresses well (gzip)
- `warcio` handles multi-record WARC files natively — no size limit