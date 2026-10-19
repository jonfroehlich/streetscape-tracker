#!/usr/bin/env python3
"""
Rebuild a crawl's parsed history and queries files from its RAW store (#442).

The raw file (`<stem>.pass<N>.raw.jsonl.gz`) holds every response byte for
byte, with the request beside it; the history and queries files are a VIEW
of it under whichever parser was current when the crawl ran. When the parser
learns to read more of a response, or the endpoint's shape moves, this
re-derives the view from the raw copy -- zero requests, any host.

Writes `<out-dir>/<stem>.pass<N>.history.csv.gz` and `.queries.csv.gz` from
`rebuild_from_raw`, the same row builders the crawl itself uses, so a rebuild
over an unchanged parser reproduces the crawl's files. Refuses to overwrite
unless --force. Needs the work list the crawl ran from (city, anchors).

Usage:
    python scripts/reparse_gsv_history_raw.py --raw results/X.pass1.raw.jsonl.gz --worklist X.worklist.csv.gz --out-dir rebuilt/
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.download_gsv_history import (  # noqa: E402
    load_worklist,
    output_paths,
    rebuild_from_raw,
)

_RAW_RE = re.compile(r"^(?P<stem>.+)\.pass(?P<pass>[12])\.raw\.jsonl\.gz$")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--raw", required=True, help="<stem>.pass<N>.raw.jsonl.gz")
    p.add_argument("--worklist", required=True, help="the work list the crawl ran from")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--force", action="store_true", help="overwrite existing outputs")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    m = _RAW_RE.match(os.path.basename(args.raw))
    if not m:
        print(f"{args.raw}: not a <stem>.pass<N>.raw.jsonl.gz name", file=sys.stderr)
        return 2
    stem, pass_no = m.group("stem"), int(m.group("pass"))
    paths = output_paths(args.out_dir, stem, pass_no)
    existing = [p for k, p in paths.items() if k != "raw" and os.path.exists(p)]
    if existing and not args.force:
        print(f"refusing to overwrite {existing[0]} (use --force)", file=sys.stderr)
        return 1
    worklist = load_worklist(args.worklist)
    history, queries = rebuild_from_raw(args.raw, worklist, pass_no)
    os.makedirs(args.out_dir, exist_ok=True)
    history.to_csv(paths["history"], index=False, compression="gzip")
    queries.to_csv(paths["queries"], index=False, compression="gzip")
    print(
        f"{stem} pass {pass_no}: {len(queries)} queries, {len(history)} history rows "
        f"-> {paths['history']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
