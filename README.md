# Google Photo Manager

## M1

M1 provides offline, link-safe filesystem scanning, streaming SHA-256 hashing,
centralized write-path validation, and a WAL-mode SQLite state store with
compare-and-set stage transitions.

Run the M1 tests with:

```text
python -m pytest -q
```

This repository intentionally stops at M1. Metadata matching, media
verification, archive copying, reports, and graceful shutdown are later
milestones and are not included.
