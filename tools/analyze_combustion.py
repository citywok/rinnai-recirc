#!/usr/bin/env python3
"""What appears before EVERY definitely-combustion event?

For each `domestic_combustion -> true` push: take every event in the preceding
window, classify it, and then take the INTERSECTION across all combustion events
-- the entries that overlap on every single one.

The discriminator that matters: events arriving milliseconds before combustion
are the SAME push batch (the module sends '#@ tele', "clerk watch'd", then the
prop change, all in one TCP write). They are not earlier signals -- they are the
same delivery. So the report separates:

    same-batch   :  offset < 5 ms   (travels with the combustion push itself)
    earlier      :  offset >= 100 ms (a genuinely earlier event)

Nothing usable as an early trigger can be same-batch.

Usage: analyze_combustion.py [--window 10]
"""
import argparse
import collections
import datetime
import os
import re
import statistics
import subprocess
import sys

UNIT = os.environ.get("RECIRC_UNIT", "rinnai-recirc.service")
TS = re.compile(r"^\w+ \d+ (\d\d:\d\d:\d\d\.\d+)")
SAME_BATCH_MS = 5.0
EARLIER_MS = 100.0


def classify(line):
    if "prop change 'domestic_combustion'" in line:
        return "COMBUSTION_PROP"
    if line.strip().endswith("#@  -> true"):
        return "VALUE_TRUE"
    if line.strip().endswith("#@  -> false"):
        return "VALUE_FALSE"
    if "#@ tele" in line:
        return "tele"
    if "watch'd 'domestic_combustion'" in line:
        return "clerk_watch_combustion"
    if "clerk" in line:
        return "clerk_batch"
    if "prop change 'local_ip'" in line or "prop change 'wifi_ssid'" in line:
        return "wifi_identity"
    if "ui wifi-status" in line or "ui: wifi" in line or "#@ ui " in line:
        return "wifi_ui"
    if "prop change" in line:
        return "prop_other"
    if "#@  -> " in line:
        return "prop_value"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--since", default="1 hour ago", help="Journal start time (default: 1 hour ago)")
    args = ap.parse_args()

    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "--since", args.since,
         "-o", "short-precise", "--no-pager"],
        capture_output=True, text=True).stdout

    events = []
    for line in out.splitlines():
        m = TS.match(line)
        if not m:
            continue
        t = datetime.datetime.strptime(m.group(1), "%H:%M:%S.%f")
        events.append((t, classify(line), line))
    events.sort()

    combustion = [(t, l) for t, k, l in events if k == "COMBUSTION_PROP"]
    print(f"combustion events: {len(combustion)}   lookback window: {args.window:.0f}s\n")
    if not combustion:
        print("none found")
        return 1

    per = []
    for t, _ in combustion:
        before = [((t - et).total_seconds() * 1000.0, k)
                  for et, k, _ in events
                  if 0 < (t - et).total_seconds() <= args.window]
        per.append((t, before))

    # ---- the intersection: kinds present before EVERY combustion event
    sets = []
    for _, before in per:
        sets.append({k for off, k in before})
    intersection = set.intersection(*sets) if sets else set()

    # split intersection into same-batch-only vs genuinely earlier
    print("=== per-event, the last 6 entries before combustion ===")
    for t, before in per[-12:]:
        print(f"\n  combustion at {t.strftime('%H:%M:%S.%f')[:-3]}")
        for off_ms, k in before[-6:]:
            tag = "same-batch" if off_ms < SAME_BATCH_MS else ("EARLIER" if off_ms >= EARLIER_MS else "")
            print(f"     -{off_ms:9.3f} ms  {k:<24}{tag}")

    print(f"\n=== INTERSECTION: kinds present before ALL {len(combustion)} combustion events ===")
    for k in sorted(intersection):
        offs = []
        same = 0
        for _, before in per:
            for off, kk in before:
                if kk == k:
                    offs.append(off)
                    if off < SAME_BATCH_MS:
                        same += 1
                    break
        med = statistics.median(offs) if offs else float("nan")
        kind = ("SAME-BATCH ONLY (not an earlier signal)" if same == len(per)
                else "has earlier occurrences")
        print(f"  {k:<26} median offset {med:8.3f} ms   {kind}")

    print(f"\n=== presence rate across all combustion events ===")
    cnt = collections.Counter()
    for _, before in per:
        for k in {k for _, k in before}:
            cnt[k] += 1
    n = len(combustion)
    for k, c in cnt.most_common():
        flag = "  <== IN EVERY ONE" if c == n else ""
        print(f"  {k:<26}{c:>4}/{n}  {100*c/n:5.1f}%{flag}")

    print("\nInterpretation: a kind marked SAME-BATCH ONLY arrives inside the same")
    print("TCP write as the combustion push, micro/ms ahead of it. It cannot be used")
    print("as an early trigger. Anything at 100% with a large offset would be one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
