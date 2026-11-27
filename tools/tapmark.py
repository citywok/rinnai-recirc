#!/usr/bin/env python3
"""Append a timestamped tap marker to a local JSONL file."""

import argparse
import json
import os
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="JSONL file to append to")
    parser.add_argument("note", nargs="*", help="Optional marker label")
    args = parser.parse_args()

    now = time.time()
    marker = {
        "t_ms": int(now * 1000),
        "wall": time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % (int(now * 1000) % 1000),
        "note": " ".join(args.note),
    }
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        output.write(json.dumps(marker) + "\n")
    print("Tap marked at %s" % marker["wall"])


if __name__ == "__main__":
    main()
