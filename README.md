# source-archive

Capture and preserve web content at the moment of citation — durable, verifiable, eventually decentralized.

## Problem

We publish climate change articles that link to external sources. Those sources are subject to link rot, content drift, or takedowns. We need to freeze the content at the time we cite it, keep a local copy in durable formats, and progressively add layers of durability (IPFS pinning, TLS signature proof).

## Survey results (see SURVEY.md)

No existing single tool does all of this:
- **ArchiveBox** is the closest for local capture (HTML/PDF/WARC/text) but has no IPFS or TLS signature features
- **Internet Archive Wayback Machine** is the canonical public redundancy layer (SPN2 API)
- **Perma.cc** is the right concept (link-rot-proof citations) but is gated to academic institutions
- **WACZ-on-IPFS spec** exists but no standalone CLI tool implements it
- **NAAN / IPARO** are research-grade, not production tools

## Architecture

### Phase 1 — Local Capture (current)
- CLI tool: `source-archive capture <url>`
- Captures HTML pages as a standards-compliant WARC file (page + sub-resources), plus a full-page screenshot PNG and extracted article text
- Downloads binary URLs (PDFs, images, etc.) as `raw.<ext>`
- Computes SHA-256 hash of each artifact
- Creates a manifest JSON per capture (URL, timestamp, formats, hashes, original headers)
- Optionally pushes to Internet Archive Wayback Machine (SPN2 API) for public redundancy
- All captures stored in a local SQLite index for querying

### Phase 2 — IPFS Pinning (future)
- Pin all captured artifacts to IPFS
- Record CIDs in the manifest
- Use a pinning service (Pinata, local node, or Filecoin deal for long-term)
- WACZ-on-IPFS chunking for dedup across captures

### Phase 3 — TLS Signature Proof (future)
- Capture the TLS certificate chain from the HTTPS connection at fetch time
- Store the server certificate, chain, and TLS session metadata
- Optionally anchor a hash of the captured content + certificate to a blockchain or OpenTimestamps for tamper-evidence

## Tech stack

- Python CLI (click or typer)
- ArchiveBox as optional capture backend (or direct: requests + playwright + readability)
- SQLite for the local index
- Internet Archive SPN2 API for public archive redundancy
- (Phase 2) IPFS Kubo / pinning service API
- (Phase 3) TLS connection metadata capture

## Usage (planned)

```bash
# Capture a single URL
source-archive capture https://example.com/article

# Capture all URLs from a list file
source-archive batch urls.txt

# Also push to Wayback Machine
source-archive capture https://example.com/article --wayback

# List all captures
source-archive list

# Find what we captured for a URL
source-archive lookup https://example.com/article
```

## License

MIT