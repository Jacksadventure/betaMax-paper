#!/usr/bin/env python3
"""Print report.py summaries for databases produced by Docker runs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List

import report


DEFAULT_RESULTS_DIR = Path("docker-results")
DEFAULT_PREFIX = "betamax"
DEFAULT_STEMS = ("single", "double", "triple")


def _host_prefix(results_dir: Path, prefix: str) -> Path:
    prefix_path = Path(prefix)
    if not prefix_path.is_absolute():
        return results_dir / prefix_path

    try:
        relative_prefix = prefix_path.relative_to("/results")
    except ValueError as exc:
        raise ValueError("absolute --prefix values must be under /results; use --db for arbitrary DB paths") from exc
    return results_dir / relative_prefix


def _default_db_paths(results_dir: Path, prefix: str) -> List[Path]:
    host_prefix = _host_prefix(results_dir, prefix)
    return [Path(f"{host_prefix}_{stem}.db") for stem in DEFAULT_STEMS]


def _existing_paths(paths: Iterable[Path]) -> tuple[List[Path], List[Path]]:
    existing: List[Path] = []
    missing: List[Path] = []
    for path in paths:
        if path.is_file():
            existing.append(path)
        else:
            missing.append(path)
    return existing, missing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print betaMax reports for DBs written by the Docker quickstart/results volume."
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="host directory mounted as /results in Docker (default: docker-results)",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="DB_PREFIX used inside Docker, without the mutation suffix (default: betamax)",
    )
    parser.add_argument(
        "--db",
        action="append",
        type=Path,
        help="explicit Docker result DB path; may be repeated and overrides --results-dir/--prefix",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail if any expected DB is missing instead of reporting the DBs that exist",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        candidate_paths = args.db if args.db else _default_db_paths(args.results_dir, args.prefix)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    existing, missing = _existing_paths(candidate_paths)
    for path in missing:
        print(f"[WARN] Missing Docker result DB {path}", file=sys.stderr)

    if missing and args.strict:
        return 1
    if not existing:
        print(
            "[ERROR] No Docker result DBs found. Run the Docker benchmark first or pass --db docker-results/<name>.db.",
            file=sys.stderr,
        )
        return 1

    report.DATABASES = [str(path) for path in existing]
    print("Docker report DBs:")
    for db_path in report.DATABASES:
        print(f"  {db_path}")
    report.main(include_optional=False, include_plots=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())