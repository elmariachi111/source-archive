"""source-archive: capture and preserve web content at the moment of citation.

Phase 1 provides local capture (raw HTML, rendered single-file HTML, PDF,
screenshot, extracted article text), a JSON manifest, and a SQLite index for
querying past captures. Future phases will add IPFS pinning and TLS signature
proofs.
"""

__version__ = "0.1.0"