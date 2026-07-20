# Batch Source Capture Workflow

## Goal

Build a new CLI command `source-archive article <url-or-file>` that:
1. Fetches an article (from URL or reads local HTML file)
2. Extracts all **remote** external links (source material we cite)
3. Filters out internal/navigation/self-referential links
4. Batch-captures each remote URL into WARC files using the existing `capture_url()` function
5. Produces a summary report

## Context

The existing tool is at `~/source-archive/`. It has:
- `source_archive/capture.py` — `capture_url(url, output_dir=, wayback=, timeout=)` function
- `source_archive/cli.py` — click-based CLI with `capture`, `batch`, `list`, `lookup` commands
- `source_archive/manifest.py` — manifest building
- `source_archive/db.py` — SQLite index

The CLI is built with `click`. The venv is at `.venv/`.

## New Command: `source-archive article`

```
source-archive article <url-or-file-path> [--output-dir DIR] [--timeout N] [--wayback] [--dry-run]
```

### Input
- If argument starts with `http://` or `https://`: fetch the HTML from the URL
- Otherwise: treat as a local file path and read the HTML

### Link Extraction & Filtering

Parse the HTML and extract all `<a href="...">` links. Then filter:

**INCLUDE** (these are the remote sources we want to capture):
- Any URL on a **different domain** than the source article
- HTTP and HTTPS links
- PDFs, news articles, reports, studies, datasets — anything external

**EXCLUDE** (do NOT capture these):
- Links to the same domain as the article (e.g., other Substack posts, about page, archive page)
- Links to `thegoodclimate.substack.com` or `substack.com` (internal/platform links)
- Social media share links (facebook.com, twitter.com, x.com, threads.com, linkedin.com, reddit.com, whatsapp.com, telegram.org, mailto:, tel:)
- Anchor links (`#...`)
- Relative links without a domain
- Duplicate URLs (capture each unique URL only once)
- Links that are just the article URL itself

### Domain extraction
Extract the registrable domain from both the article URL and each link URL. Use Python's `urllib.parse` to get the hostname. Compare the base domain (last two parts for common TLDs, or use `tldextract` if available — but prefer stdlib only, so a simple approach: compare the full hostname, and also strip common subdomains like `www.`).

### Output

1. Print a summary of what was found:
```
Article: https://thegoodclimate.substack.com/p/example
Domain: thegoodclimate.substack.com

Found 47 total links
  - 12 internal (excluded)
  - 5 social media (excluded)  
  - 3 duplicates (excluded)
  - 27 remote sources to capture

Capturing 27 remote sources...
  [1/27] https://example.com/report.pdf → OK (capture.warc.gz, 1.2 MB)
  [2/27] https://news.bbc.co.uk/article → OK (capture.warc.gz, 0.8 MB)
  [3/27] https://doi.org/10.1234/example → FAIL (404)
  ...
  
Done: 24/27 captured successfully, 3 failed
Captures stored in: ./archive/article_<hash>/
```

2. Store all captures in a subdirectory: `<output-dir>/article_<sha256(article_url)[:8]>/`
3. Write a `sources.json` manifest in that directory listing all captured URLs, their status, and paths to their WARC files
4. If `--dry-run`: just list the URLs that would be captured, don't actually fetch them

### Implementation

Add a new function `capture_article_sources()` in a new file `source_archive/article.py`, and a new CLI command in `cli.py`.

The function should:
1. Fetch/read the HTML
2. Parse with `trafilatura` or `beautifulsoup4` (check what's already installed in the venv — `trafilatura` is a dependency, and `bs4` might be too; if not, use `html.parser` from stdlib)
3. Extract all `<a>` tags with `href` attributes
4. Filter per the rules above
5. Call `capture_url()` for each remaining URL
6. Collect results and write `sources.json`

### Testing

After building:
1. Test with `--dry-run` on a real Substack article URL
2. Test with a local HTML file
3. Test actual capture on 2-3 URLs from a real article
4. Run existing tests to ensure nothing broke
5. Add a test for the article command

### Git

Commit with message: "Add article batch capture: extract remote sources from article HTML, capture each to WARC"

## Technical Notes

- Use stdlib `urllib.parse` for URL parsing
- Use `html.parser` or `trafilatura` for HTML parsing (avoid adding new dependencies if possible)
- The existing `capture_url()` function handles both HTML and binary (PDF) URLs
- Add a small delay (1 second) between captures to avoid rate-limiting
- Handle failures gracefully — one failed URL shouldn't stop the batch
- The `--dry-run` flag is important for previewing before committing to a long batch run