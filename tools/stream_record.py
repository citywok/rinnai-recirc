#!/usr/bin/env python3
"""Record raw heater push lines to a local JSONL file.

Captures can include network details. Review them before sharing.
"""

import argparse
import json
import math
import os
import socket
import time


def record(sock, output, seconds):
    deadline = time.monotonic() + seconds
    pending = b""
    while (remaining := deadline - time.monotonic()) > 0:
        sock.settimeout(min(0.2, remaining))
        try:
            chunk = sock.recv(16384)
        except socket.timeout:
            continue
        if not chunk:
            return
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            now = time.time()
            output.write(json.dumps({
                "t_ms": int(now * 1000),
                "wall": time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % (int(now * 1000) % 1000),
                "sub_ms": round((now - int(now)) * 1000, 3),
                "line": line,
            }) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Heater module LAN address")
    parser.add_argument("output", help="New JSONL file")
    parser.add_argument("--port", type=int, default=9798)
    parser.add_argument("--seconds", type=float, default=900)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")

    with socket.create_connection((args.host, args.port), timeout=5) as sock:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", buffering=1) as output:
            sock.sendall(b"trace -1\r\n")
            try:
                record(sock, output, args.seconds)
            finally:
                try:
                    sock.settimeout(0.2)
                    sock.sendall(b"trace 0\r\n")
                except OSError:
                    pass
    print("Capture written to %s" % args.output)


if __name__ == "__main__":
    main()
