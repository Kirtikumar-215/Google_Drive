# Google Photo Manager

## M1 and M2

M1 provides offline, link-safe filesystem scanning, streaming SHA-256 hashing,
centralized write-path validation, and a WAL-mode SQLite state store with
compare-and-set stage transitions. M2 adds sequential metadata matching,
Pillow image verification, duplicate cataloguing, and HTML/CSV/JSON reports.

Run the M1 tests with:

```text
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests
python -m mypy src
google-photo-manager INPUT_ROOT --db data/archive.db --reports reports
```

M2 supports `--rehash`, `--retry-failed`, `--max-frames`, and `--max-pixels`.
It does not validate video files. HEIC/HEIF/AVIF support is optional; install
`pip install -e ".[heif]"` when a compatible decoder is needed. Without it,
those formats are recorded as `DECODER_NOT_AVAILABLE`.

Archive copying, video validation, concurrency pipelines, and shutdown work
belong to later milestones and are not included.
