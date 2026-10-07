"""Inspect a large timestamp-first tick CSV without loading it into memory."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def inspect(path: Path) -> dict[str, object]:
    months: Counter[str] = Counter()
    rows = malformed = out_of_order = adjacent_equal = 0
    first = last = previous = None
    header = ""

    with path.open("rb") as source:
        header = source.readline().rstrip(b"\r\n").decode("utf-8-sig", errors="replace")
        for line in source:
            rows += 1
            timestamp = line.partition(b",")[0].strip()
            if len(timestamp) < 19 or timestamp[4:5] != b"-" or timestamp[7:8] != b"-":
                malformed += 1
                continue
            month = timestamp[:7].decode("ascii", errors="replace")
            months[month] += 1
            first = timestamp if first is None else first
            last = timestamp
            if previous is not None:
                if timestamp < previous:
                    out_of_order += 1
                elif timestamp == previous:
                    adjacent_equal += 1
            previous = timestamp

    return {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "header": header,
        "data_rows": rows,
        "first_timestamp": first.decode("ascii", errors="replace") if first else None,
        "last_timestamp": last.decode("ascii", errors="replace") if last else None,
        "month_counts": dict(sorted(months.items())),
        "malformed_timestamps": malformed,
        "out_of_order_rows": out_of_order,
        "adjacent_equal_timestamps": adjacent_equal,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error(f"file not found: {args.file}")
    print(json.dumps(inspect(args.file), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
