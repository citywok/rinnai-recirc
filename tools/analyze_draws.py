#!/usr/bin/env python3
"""Does anything reliably PRECEDE a draw?

Hypothesis under test: the module emits a particular sequence (the
clerk-connect / local_ip / wifi_ssid 'blip') before every flow event, which
would make it an earlier trigger than domestic_combustion.

Method: for every draw (controller 'flow burst start'), collect the pushed
events in the PRECEDING window and count how many draws had each event kind
before them. A real precursor appears before ~all draws; a coincidence before
one or two.

Usage:
    analyze_draws.py [--since TIME] [--window 10]
"""
import argparse
import collections
import datetime
import os
import re
import subprocess
import sys

UNIT = os.environ.get("RECIRC_UNIT", "rinnai-recirc.service")
TS = re.compile(r"^\w+ \d+ (\d\d:\d\d:\d\d\.\d+)")


def journal(since):
    return subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "--since", since,
         "-o", "short-precise", "--no-pager"],
        capture_output=True, text=True).stdout.splitlines()


def classify(line):
    """Map a raw line to an event kind we can count."""
    if "flow burst start" in line:
        return "DRAW"
    if "#@ prop change 'domestic_combustion'" in line or '#@  -> true' in line and "combustion" in line:
        return "combustion"
    if "prop change 'local_ip'" in line or "prop change 'wifi_ssid'" in line:
        return "wifi_prop"
    if "clerk" in line and ("connect" in line or "dispatch" in line or "on_event" in line):
        return "clerk_batch"
    if "ui wifi-status" in line:
        return "wifi_status_ui"
    if "prop change" in line:
        return "prop_other"
    if "#@ tele" in line:
        return "tele"
    if line.strip().endswith("#@  -> \"\"") or line.strip().endswith('#@  -> ""'):
        return "wifi_empty_value"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="1 hour ago", help="Journal start time (default: 1 hour ago)")
    ap.add_argument("--window", type=float, default=10.0)
    args = ap.parse_args()

    events = []
    for line in journal(args.since):
        m = TS.match(line)
        if not m:
            continue
        t = datetime.datetime.strptime(m.group(1), "%H:%M:%S.%f")
        kind = classify(line)
        if kind:
            events.append((t, kind, line.strip()))
    events.sort()

    draws = [e for e in events if e[1] == "DRAW"]
    print(f"draws found: {len(draws)}   window: {args.window:.0f}s before each\n")
    if not draws:
        print("no draws in range")
        return 1

    precursor = collections.Counter()
    per_draw = []
    for t, _, _ in draws:
        before = [e for e in events if 0 < (t - e[0]).total_seconds() <= args.window]
        kinds = collections.Counter(k for _, k, _ in before)
        per_draw.append((t, [e for e in before], kinds))
        for k in kinds:
            if k != "DRAW":
                precursor[k] += 1

    print(f"{'draw time':<14}{'events before':>14}   kinds")
    for t, before, kinds in per_draw:
        summary = ", ".join(f"{k}x{n}" for k, n in sorted(kinds.items()))
        print(f"{t.strftime('%H:%M:%S.%f')[:-3]:<14}{len(before):>14}   {summary or '(nothing)'}")

    n = len(draws)
    print(f"\n=== precursor rate (appears before N of {n} draws) ===")
    for k, c in precursor.most_common():
        print(f"  {k:<18}{c:>3}/{n}   {100*c/n:5.1f}%")
    print("\nA real precursor should sit near 100%. Anything low is coincidence.")

    # detail for the draws that DID have a wifi event before them
    print("\n=== draws preceded by a wifi_prop event ===")
    any_wifi = False
    for t, before, kinds in per_draw:
        if "wifi_prop" in kinds:
            any_wifi = True
            print(f"  {t.strftime('%H:%M:%S.%f')[:-3]}:")
            for e in before[-6:]:
                print(f"     {e[0].strftime('%H:%M:%S.%f')[:-3]}  {e[1]:<16} {e[2][:90]}")
    if not any_wifi:
        print("  none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
