# Survey: Durable Web Content Preservation Tools

Conducted July 2026. Goal: find existing tools that capture web content at citation time, store it durably (ideally decentralized), and serve the common good — before deciding to build our own.

## Centralized public archives

### Internet Archive / Wayback Machine
- **What:** Snapshots web pages, publicly replayable indefinitely
- **API:** SPN2 (Save Page Now 2) — POST to `web.archive.org/save`, poll for job status, get Wayback URL back. Auth via S3-style keys (free account)
- **Pros:** Free, public, durable, widely trusted, has API
- **Cons:** Single organization dependency, no local copy, no content hash verification, can rate-limit
- **Verdict:** Use as redundancy layer, not primary capture

### archive.today (archive.ph)
- **What:** On-demand page snapshots, captures JS-rendered content
- **API:** No official API. Unofficial wrappers exist (npm `archivetoday`, Go `jaytaylor/archive.today`)
- **Pros:** Captures JS-rendered pages well, fast
- **Cons:** No API, rate-limited, fragile to automate, single org
- **Verdict:** Optional secondary backup, not reliable for automation

### Perma.cc (Harvard Library Innovation Lab)
- **What:** Designed exactly for link-rot-proof citations — captures WACZ + PNG screenshot, provides permanent URL
- **API:** REST API available
- **Pros:** Purpose-built for citation preservation, library-backed perpetuity
- **Cons:** Free tier extremely limited (individual accounts no longer get recurring free links), institutional accounts required for meaningful use, centralized
- **Verdict:** Right concept but gated for academic use, not suitable for our volume

## Self-hosted open-source

### ArchiveBox (27K stars)
- **What:** Self-hosted web archiving. Takes URLs → saves HTML, singlefile HTML, PDF, PNG, WARC, TXT, article text, favicon. CLI + REST API + web UI
- **IPFS:** No native IPFS support
- **Pros:** Comprehensive capture, CLI-driven, standard durable formats, pushes to archive.org by default, Python, active development
- **Cons:** Heavy dependencies (Chrome, wget, node, yt-dlp, git, singlefile), WARC quality issues (wget-based, not standards-compliant), no IPFS, no TLS signature capture
- **Verdict:** Best available capture engine for Phase 1, but we need a purpose-built CLI wrapper

### Webrecorder / archiveweb.page
- **What:** Browser extension + desktop app for high-fidelity WACZ/WARC capture. Has experimental IPFS sharing
- **IPFS:** Yes — WACZ-on-IPFS spec, used in the extension
- **Pros:** Highest fidelity capture, WACZ format, IPFS support, open source
- **Cons:** Interactive/browser-based, not a CLI pipeline tool, TypeScript/JS ecosystem
- **Verdict:** Reference implementation for WACZ-on-IPFS, but not a CLI tool we can script

### pywb / Browsertrix
- **What:** Python web archiving toolkit (replay + recording), Browsertrix is the hosted/crawler version
- **Verdict:** Overkill for single-URL citation captures

### WebArchiver
- **What:** Newer WARC 1.1 tool, Docker, REST API, replay
- **Verdict:** Solid but less mature, smaller community than ArchiveBox

## Decentralized / IPFS-native

### IPWB (InterPlanetary Wayback)
- **What:** Stores WARC in IPFS, replays from IPFS
- **Status:** Academic project, depends on local indexes, limited maintenance
- **Verdict:** Reference, not production tool

### IPARO (2025 paper)
- **What:** Embeds version-linked references in IPFS objects, uses IPNS for discovery — no local index needed
- **Status:** Research paper (JCDL 2025), no production implementation
- **Verdict:** Future direction, not available today

### WACZ-on-IPFS spec (Webrecorder)
- **What:** Content-aware chunking of WACZ/WARC into IPFS UnixFS, dedup across archives
- **Status:** Spec published, implemented in archiveweb.page extension, no standalone CLI
- **Verdict:** Use this spec for Phase 2 IPFS pinning

### NAAN / nostr-web-archiver
- **What:** Nostr + Blossom decentralized archive nodes, publishes archive receipts as Nostr events
- **Status:** Very early, niche ecosystem, requires Nostr infrastructure
- **Verdict:** Interesting but too early and too niche for our use case

## Gap analysis

| Requirement | Best existing tool | Gap |
|---|---|---|
| URL → local capture (HTML/PDF/text/WARC) | ArchiveBox | Needs wrapper CLI for our workflow |
| Push to public durable archive | ArchiveBox (archive.org) | Works, but no fallback if IA is down |
| Pin capture to IPFS | archiveweb.page (WACZ-on-IPFS) | No CLI tool, browser-only |
| Capture & verify TLS certificate chain | Nobody | Complete gap |
| Batch capture for article sources | ArchiveBox CLI | Works but not integrated with our workflow |
| Content hash manifest for verification | ArchiveBox (partial) | No standardized manifest format |
| Query captures by URL later | ArchiveBox SQLite | Works but not purpose-built for citation tracking |

## Conclusion

No existing single tool does the full job. ArchiveBox is the best capture engine but lacks IPFS and TLS signature features. We should build a purpose-built CLI that:

1. **Phase 1:** Captures URLs locally in durable formats + creates a content manifest + optionally pushes to Wayback Machine
2. **Phase 2:** Pins captures to IPFS and records CIDs in the manifest
3. **Phase 3:** Captures TLS certificate chain as proof of delivery

This is worth building as an internal coding project.