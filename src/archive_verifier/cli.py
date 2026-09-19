"""M2 command-line entry point."""

from __future__ import annotations

import argparse

from .m2 import run_m2


def main() -> int:
    parser = argparse.ArgumentParser(prog="google-photo-manager")
    parser.add_argument("input_root")
    parser.add_argument("--db", required=True)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--rehash", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-frames", type=int, default=10000)
    parser.add_argument("--max-pixels", type=int, default=500000000)
    arguments = parser.parse_args()
    run_m2(
        arguments.input_root,
        arguments.db,
        arguments.reports,
        rehash=arguments.rehash,
        retry_failed=arguments.retry_failed,
        max_frames=arguments.max_frames,
        max_pixels=arguments.max_pixels,
    )
    return 0
