#!/usr/bin/env python3
"""What happens after EVERY real tap?

Aligns every pushed event against the human's tap markers (written by tapmark.py) and reports:

  * the ordered sequence seen after each tap, with millisecond offsets
  * which event kinds appear after ALL taps -- the invariant to key on
  * which are noise, by comparing against no-tap windows

This is the difference between "something happened after I turned it on" and
"this happens after every tap", which is the only thing safe to trigger on.

Three things this file is careful about, because each one silently manufactures
a false 100% row:

  * DATES.  journalctl short-precise prints no year, so the year is anchored
    near now and the real calendar date is kept.  Reducing both sides to a bare
    time-of-day aliases every day onto one window.
  * DEAD AIR.  The controller is only running/streaming part of the day.  A
    uniform draw over the whole span lands in silence ~38% of the time and a
    silent control window scores 0%, which makes the 18s heartbeat look like a
    discriminating trigger.  Control windows are therefore drawn only from live
    segments (splits at gaps longer than --window).
  * INDEPENDENCE.  Two taps 3s apart share 27s of a 30s window; counting both
    turns one observation into "2/2".  Overlapping tap windows are reported but
    counted once.

Usage:  analyze_taps.py TAPS_JSONL [--window 30] [--since TIME]
"""
import argparse
import collections
import datetime
import json
import os
import random
import re
import statistics
import subprocess
import sys

UNIT = os.environ.get("RECIRC_UNIT", "rinnai-recirc.service")
TS = re.compile(r"^(\w{3}) +(\d+) (\d\d):(\d\d):(\d\d)\.(\d+)")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
BATCH_KIND = "clerk_batch"


def parse_ts(match, ref):
    """journalctl short-precise prints no year; anchor it near `ref`.

    The year is the one that puts the line closest to now, which handles both a
    Dec 31 line read on Jan 1 and a Jan 1 line read on Dec 31.  Reducing both
    sides to a bare time-of-day aliases every day onto one window.
    """
    mon, day, hh, mm, ss, frac = match.groups()
    try:
        month = MONTHS[mon]
        cands = []
        for y in (ref.year - 1, ref.year, ref.year + 1):
            try:
                cands.append(datetime.datetime(
                    y, month, int(day), int(hh), int(mm), int(ss),
                    int(frac.ljust(6, "0")[:6])))
            except ValueError:
                continue
        if not cands:
            return None
        return min(cands, key=lambda t: abs((t - ref).total_seconds()))
    except KeyError:
        return None


def classify(line):
    if "flow burst start" in line:
        return "CONTROLLER_DRAW"
    if "flow detected" in line:
        return "CONTROLLER_TRIGGER"
    if "prop change 'domestic_combustion'" in line:
        return "combustion_change"
    if "prop change 'local_ip'" in line or "prop change 'wifi_ssid'" in line:
        return "wifi_identity"
    if "#@ tele" in line:
        return "tele"
    if "clerk" in line:
        return "clerk_batch"
    if "ui wifi-status" in line or "ui: wifi" in line or "#@ ui " in line:
        return "wifi_ui"
    if "prop change" in line:
        return "prop_other"
    if line.strip().endswith('#@  -> ""'):
        return "empty_value"
    if "#@  -> " in line:
        return "prop_value"
    return None


def load_events(since):
    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "--since", since,
         "-o", "short-precise", "--no-pager"],
        capture_output=True, text=True).stdout
    ref = datetime.datetime.now()
    events = []
    for line in out.splitlines():
        m = TS.match(line)
        if not m:
            continue
        t = parse_ts(m, ref)
        if t is None:
            continue
        kind = classify(line)
        if kind:
            events.append((t, kind, line))
    events.sort()
    return events


def load_taps(path):
    """Returns (taps, malformed_line_count).

    A torn final line from an interrupted tapmark.py append must not destroy
    every result -- it is counted and skipped.
    """
    taps = []
    malformed = 0
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return taps, malformed
    with fh:
        for raw in fh:
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:      # torn multi-byte append
                malformed += 1
                continue
            if not line:
                continue
            try:
                r = json.loads(line)
                dt = datetime.datetime.fromtimestamp(r["t_ms"] / 1000.0)
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                malformed += 1
                continue
            taps.append((dt, r.get("note", "")))
    return sorted(taps), malformed


def live_segments(events, window):
    """Contiguous stretches of the journal with no gap longer than `window`.

    A window inside one segment can never straddle a period when the controller
    was down, which is what makes the control sample honest.
    """
    if not events:
        return []
    segs = []
    start = prev = events[0][0]
    for t, _, _ in events[1:]:
        if (t - prev).total_seconds() > window:
            segs.append((start, prev))
            start = t
        prev = t
    segs.append((start, prev))
    return [(a, b) for a, b in segs if (b - a).total_seconds() >= window]


def subtract_intervals(intervals, banned):
    out = []
    for a, b in intervals:
        cur = [(a, b)]
        for ba, bb in banned:
            nxt = []
            for x, y in cur:
                if bb <= x or ba >= y:
                    nxt.append((x, y))
                    continue
                if x < ba:
                    nxt.append((x, min(ba, y)))
                if y > bb:
                    nxt.append((max(bb, x), y))
            cur = nxt
        out.extend(cur)
    return [(a, b) for a, b in out if b > a]


def sample_windows(intervals, count, rng):
    """Sample window START times uniformly across the union of intervals."""
    total = sum((b - a).total_seconds() for a, b in intervals)
    picks = []
    if total <= 0:
        return picks
    for _ in range(count):
        r = rng.uniform(0.0, total)
        for a, b in intervals:
            w = (b - a).total_seconds()
            if r <= w:
                picks.append(a + datetime.timedelta(seconds=r))
                break
            r -= w
    return picks


def measure_batch_period(events, cluster=2.0):
    """Median gap between status batches, measured from the journal itself.

    A batch is identified by its own 'clerk connect' line, so a batch whose
    lines are spread over more than `cluster` seconds cannot be split in two
    and inflate the observed batch rate.  Falls back to a gap-based clustering
    only if the journal has no connect lines at all.
    """
    starts = [t for t, k, l in events
              if k == BATCH_KIND and "clerk" in l and "connect" in l]
    if not starts:
        for t, k, _ in events:
            if k != BATCH_KIND:
                continue
            if not starts or (t - starts[-1]).total_seconds() > cluster:
                starts.append(t)
    starts.sort()
    gaps = [(starts[i + 1] - starts[i]).total_seconds()
            for i in range(len(starts) - 1)]
    return (statistics.median(gaps) if gaps else None), len(starts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=30.0)
    ap.add_argument("--since", default="1 hour ago", help="Journal start time (default: 1 hour ago)")
    ap.add_argument("--controls", type=int, default=25,
                    help="random no-tap windows to compare against")
    ap.add_argument("--quiet-zone", type=float, default=60.0,
                    help="keep control windows this far from any tap")
    ap.add_argument("taps", help="JSONL file written by tapmark.py")
    args = ap.parse_args()

    events = load_events(args.since)
    taps, malformed = load_taps(args.taps)
    if not taps:
        print("no taps recorded. run:  python3 tapmark.py TAPS_JSONL at the moment you open the tap")
        return 1

    if events:
        print(f"{len(taps)} taps, {len(events)} events, window {args.window:.0f}s "
              f"(journal {events[0][0]:%H:%M:%S}..{events[-1][0]:%H:%M:%S})")
    else:
        print(f"{len(taps)} taps, 0 events, window {args.window:.0f}s")
    if malformed:
        print(f"WARNING: skipped {malformed} unparseable tap-marker line(s) "
              f"(torn append?) in {args.taps}")
    if not events:
        print("no events in range -- nothing can be measured")
        return 1

    lo, hi = events[0][0], events[-1][0]

    # ---- which taps can we actually measure, and which are independent?
    usable, out_of_range = [], []
    for tap, note in taps:
        if tap < lo or tap + datetime.timedelta(seconds=args.window) > hi:
            out_of_range.append((tap, note))
        else:
            usable.append((tap, note))
    independent, overlaps = [], []
    for tap, note in usable:
        if independent and (tap - independent[-1][0]).total_seconds() < args.window:
            overlaps.append((tap, note, independent[-1][0]))
        else:
            independent.append((tap, note))

    for tap, note in out_of_range:
        print(f"SKIP tap at {tap:%Y-%m-%d %H:%M:%S} ({note or '-'}): "
              f"its window falls outside the loaded journal "
              f"[{lo:%H:%M:%S}..{hi:%H:%M:%S}] -- not measurable with --since {args.since}")
    for tap, note, first in overlaps:
        print(f"NOTE tap at {tap:%H:%M:%S} ({note or '-'}) is "
              f"{(tap - first).total_seconds():.0f}s after the tap at {first:%H:%M:%S}; "
              f"its window overlaps that one and is counted once")
    if (out_of_range or overlaps) and independent:
        print()
    if not independent:
        print("no measurable independent taps -- nothing to report")
        return 1

    appeared = collections.Counter()
    offsets = collections.defaultdict(list)
    for i, (tap, note) in enumerate(independent, 1):
        window = [(t, k, l) for t, k, l in events
                  if 0 <= (t - tap).total_seconds() <= args.window]
        label = f" ({note})" if note else ""
        print(f"--- tap {i} at {tap:%H:%M:%S.%f}{label}: {len(window)} events")
        seen = set()
        for t, k, l in window:
            off = (t - tap).total_seconds()
            print(f"     +{off:6.3f}s  {k}")
            if k not in seen:
                seen.add(k)
                offsets[k].append(off)
        for k in seen:
            appeared[k] += 1
        if not window:
            print("     (nothing at all in the window)")
        print()

    n = len(independent)
    print(f"=== appears after N of {n} independent tap(s) ===")
    for k, c in appeared.most_common():
        offs = offsets[k]
        med = statistics.median(offs) if offs else float("nan")
        flag = "  <== INVARIANT" if c == n else ""
        print(f"  {k:<20}{c:>3}/{n}  {100*c/n:5.0f}%   first-seen median +{med:.3f}s{flag}")
    if n < 3:
        print(f"\nCAVEAT: only {n} independent tap(s). 'INVARIANT' above means "
              f"'consistent with this sample', not 'happens every time'. Record at "
              f"least 3 taps spaced more than {args.window:.0f}s apart before "
              f"trusting a 100% row.")

    # ---- chance baseline, measured rather than assumed
    period, nbatches = measure_batch_period(events)
    if period:
        print(f"\nChance baseline: {nbatches} status batches in range, median gap "
              f"{period:.2f}s.")
        print(f"At that cadence any {args.window:.0f}s window catches one with "
              f"probability {min(100.0, args.window / period * 100.0):.0f}%.")
    else:
        print("\nChance baseline: no status batches in range -- cannot compute.")
    print("Only kinds at 100% AND low in control deserve to be a trigger.")

    # ---- control windows: same analysis where no water was running.
    # Sampled from LIVE time only.  A uniform draw over the whole span lands in
    # dead air a third of the time and scores 0%, which is how the heartbeat
    # gets reported as a discriminating trigger.
    random.seed(1)
    segs = live_segments(events, args.window)
    live_seconds = sum((b - a).total_seconds() for a, b in segs)
    span_seconds = (hi - lo).total_seconds()
    # A control window [s, s+window] must neither overlap the tap window
    # [tap, tap+window] nor come within quiet_zone of the tap.  Both need the
    # same bound, so the banner is window+quiet on EACH side -- using only
    # quiet_zone on the right leaves overlap whenever window > quiet_zone.
    banned = [(tap - datetime.timedelta(seconds=args.window + args.quiet_zone),
               tap + datetime.timedelta(seconds=args.window + args.quiet_zone))
              for tap, _ in taps]
    allowed = subtract_intervals([(a, b - datetime.timedelta(seconds=args.window))
                                  for a, b in segs], banned)
    picks = sample_windows(allowed, args.controls, random) if args.controls else []

    print(f"\nlive time {live_seconds:.0f}s of {span_seconds:.0f}s span "
          f"({100 * live_seconds / max(span_seconds, 1):.0f}%), {len(segs)} segment(s); "
          f"{len(picks)} control window(s) from {len(allowed)} allowed interval(s)")
    if not args.controls:
        print("control comparison disabled (--controls 0): cannot tell signal from heartbeat")
    elif not picks:
        print("NO control windows available (journal too short, or every live "
              "second is within the quiet zone of a tap).")
        print("Refusing to print a tap-vs-control table -- it would show every kind "
              "as 100% vs 0%.")
        return 0
    elif len(picks) < args.controls:
        print(f"WARNING: only {len(picks)} of {args.controls} control windows fit; "
              f"rates below are noisy")

    if picks:
        ctrl = collections.Counter()
        silent = 0
        for t in picks:
            seen = {k for tt, k, _ in events
                    if 0 <= (tt - t).total_seconds() <= args.window}
            if not seen:
                silent += 1
            for k in seen:
                ctrl[k] += 1
        print(f"\n=== tap windows vs {len(picks)} control windows (no water) ===")
        print(f"  {'kind':<20}{'taps':>8}{'control':>10}{'delta':>9}")
        for k in sorted(set(appeared) | set(ctrl),
                        key=lambda x: -(appeared.get(x, 0) - ctrl.get(x, 0))):
            tp = 100 * appeared.get(k, 0) / n
            cp = 100 * ctrl.get(k, 0) / len(picks)
            mark = "  <== discriminates" if tp - cp > 30 else ""
            print(f"  {k:<20}{tp:7.0f}%{cp:9.0f}%{tp-cp:+8.0f}{mark}")
        if silent:
            print(f"\nWARNING: {silent} control window(s) contained no events at all; "
                  f"the live-segment sampler should make this impossible.")
        if appeared.get(BATCH_KIND) and period:
            bp = 100 * ctrl.get(BATCH_KIND, 0) / len(picks)
            print(f"\nEmpirical heartbeat check: {bp:.0f}% of no-tap windows contain "
                  f"{BATCH_KIND} (predicted "
                  f"{min(100.0, args.window / period * 100.0):.0f}% from the "
                  f"measured {period:.1f}s cadence).")
        print("\nA usable trigger is HIGH in taps and LOW in control.")
        print("Anything high in both is the heartbeat, not your water.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
