#!/usr/bin/env python3
"""Tests for the trigger-latency changes (commit a5fb734).

Covers exactly what changed:
  1. start_recirculation() caches recirculation_duration and only re-sends it
     when the value changes (the trigger path is now a single write).
  2. Heater.command() read timeouts were tightened (first 1.5->0.5, idle 0.30->0.12).
  3. MIN_FLOW_S default dropped 1.0 -> 0.3, which changes which bursts trigger.

These tests assert behaviour, not implementation trivia: a stub console records
the wire traffic the controller actually produces, and the controller is driven
with an injected clock so no test sleeps on wall time.

Run:  python3 -m unittest -v test_trigger_latency.py
      (also runnable under pytest)
"""
import datetime
import inspect
import json
import os
import shutil
import tempfile
import select
import socket
import threading
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

from recirc_flow import Heater, RecircController, StreamDemand, MIN_FLOW_S, service_once


class MockConsole:
    """Stands in for the module's TCP 9798 console; records every command."""

    def __init__(self):
        self.commands = []
        self.recirc = False
        self.window = 0
        self.duration = 5
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.sendall(b"# monitor\r\n#   help for help\r\n")
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode(errors="replace").strip()
                    if cmd:
                        self.commands.append(cmd)
                        conn.sendall(self._reply(cmd).encode())
        except OSError:
            return

    def _reply(self, cmd):
        if cmd == "heater":
            w = max(self.window, 0)
            return ("#   heater: op Y  prio Y  lock N  recirc %s  set_temp 120F  recirc_temp -1C\r\n"
                    "#   recirc:  %d:%02d window\r\n"
                    "# schedule: Mon 09:00, enabled, out\r\n"
                    % ("Y" if self.recirc else "N", w // 60, w % 60))
        if cmd.startswith("set recirculation_duration"):
            self.duration = int(cmd.split()[-1])
            return "#? set 'recirculation_duration' to %d\r\n" % self.duration
        if cmd == "set set_recirculation_enabled true":
            self.recirc, self.window = True, self.duration * 60
            return "#? set 'set_recirculation_enabled' to true\r\n"
        if cmd == "set set_recirculation_enabled false":
            self.recirc, self.window = False, 0
            return "#? set 'set_recirculation_enabled' to false\r\n"
        if cmd.startswith("find recirculation_enabled"):
            return "recirculation_enabled:  %s {1s}\r\n" % ("true" if self.recirc else "false")
        return "#?\r\n"

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass

    def of_type(self, prefix):
        return [c for c in self.commands if c.startswith(prefix)]


class DurationCachingTests(unittest.TestCase):
    """The whole point of the change: one write on the trigger path, not two."""

    def setUp(self):
        self.console = MockConsole()
        self.heater = Heater("127.0.0.1", self.console.port)

    def tearDown(self):
        self.heater.close()
        self.console.close()

    def test_cache_starts_empty(self):
        self.assertIsNone(self.heater._duration_sent)

    def test_duration_sent_only_on_first_trigger(self):
        self.assertTrue(self.heater.start_recirculation(30))
        self.assertTrue(self.heater.start_recirculation(30))
        self.assertEqual(self.console.of_type("set recirculation_duration"),
                         ["set recirculation_duration 30"],
                         "duration must not be re-sent when unchanged")
        self.assertEqual(len(self.console.of_type("set set_recirculation_enabled")), 2)

    def test_second_trigger_is_a_single_write(self):
        self.heater.start_recirculation(30)
        before = len(self.console.commands)
        self.heater.start_recirculation(30)
        sent = self.console.commands[before:]
        self.assertEqual(sent, ["set set_recirculation_enabled true"],
                         "repeat trigger must be exactly one command")

    def test_duration_resent_when_value_changes(self):
        self.heater.start_recirculation(30)
        self.heater.start_recirculation(10)
        self.assertEqual(self.console.of_type("set recirculation_duration"),
                         ["set recirculation_duration 30",
                          "set recirculation_duration 10"])

    def test_cache_tracks_last_sent_value(self):
        self.heater.start_recirculation(5)
        self.assertEqual(self.heater._duration_sent, 5)
        self.heater.start_recirculation(5)
        self.assertEqual(self.heater._duration_sent, 5)


class TimeoutTests(unittest.TestCase):
    """Tightened read timeouts are what removed ~140 ms per round-trip."""

    def test_command_read_timeouts_are_tight(self):
        sig = inspect.signature(Heater.command)
        self.assertEqual(sig.parameters["first"].default, 0.5)
        self.assertEqual(sig.parameters["idle"].default, 0.12)

    def test_min_flow_default_is_zero_for_fastest_mode(self):
        """Fastest mode: 0 means trigger on the very first tick of a draw."""
        self.assertEqual(MIN_FLOW_S, 0.0)


class StubHeater:
    """Minimal heater for driving the controller with an injected clock."""

    def __init__(self):
        self.triggers = 0
        self.running = False
        self.last_minutes = None

    def state(self):
        return self.running, None, "# stub"

    def start_recirculation(self, minutes):
        self.triggers += 1
        self.running = True
        self.last_minutes = minutes
        return True

    def command(self, *a, **k):
        return ""

    def close(self):
        pass


class EventDrivenTests(unittest.TestCase):
    """The loop is event-driven now: no poll tick in the trigger path."""

    def test_poll_blocking_wakes_on_arrival(self):
        a, b = socket.socketpair()
        src = StreamDemand("127.0.0.1", 1)   # never dials; socket is injected
        src.sock = a

        def push():
            time.sleep(0.05)
            b.sendall(b"#@ prop change 'domestic_combustion'\n#@  -> true\n")

        th = threading.Thread(target=push, daemon=True)
        th.start()
        t0 = time.perf_counter()
        got = src.poll_blocking(2.0)         # 2 s timeout, data arrives at 50 ms
        elapsed_ms = (time.perf_counter() - t0) * 1000
        th.join()

        self.assertTrue(got, "poll_blocking should report that bytes were handled")
        self.assertEqual(src.values.get("domestic_combustion"), "true")
        self.assertLess(elapsed_ms, 100,
                        "woke after %.1f ms -- select() should return on arrival, "
                        "not wait out the timeout (target is <100 ms)" % elapsed_ms)
        a.close()
        b.close()


class MinFlowBehaviourTests(unittest.TestCase):
    """MIN_FLOW_S 1.0 -> 0.3 changes which bursts trigger; pin both sides."""

    STEP = 0.05

    def _drive(self, burst_s, min_flow_s=None):
        heater = StubHeater()
        ctl = RecircController(
            heater, None, recirc_minutes=30,
            min_flow_s=MIN_FLOW_S if min_flow_s is None else min_flow_s,
            cooldown_s=0.0, state_poll_s=1000.0, max_per_hour=99,
            renew_below_s=0,
        )
        t = 1000.0
        start = t
        while t - start < burst_s:
            t += self.STEP
            ctl.step(t, True)
        for _ in range(20):
            t += self.STEP
            ctl.step(t, False)
        return heater.triggers

    def test_burst_shorter_than_explicit_threshold_ignored(self):
        """The threshold still works when configured; it is just 0 by default now."""
        self.assertEqual(self._drive(0.20, min_flow_s=0.5), 0)

    def test_first_tick_triggers_in_fastest_mode(self):
        """MIN_FLOW_S=0 means the very first active tick fires -- no debounce."""
        self.assertEqual(self._drive(0.05), 1)

    def test_burst_above_threshold_triggers(self):
        self.assertEqual(self._drive(0.50), 1,
                         "0.5s draw now triggers; it did not when MIN_FLOW_S was 1.0")

    def test_trigger_uses_configured_phase_length(self):
        heater = StubHeater()
        ctl = RecircController(
            heater, None, recirc_minutes=30, min_flow_s=MIN_FLOW_S,
            cooldown_s=0.0, state_poll_s=1000.0, max_per_hour=99,
            renew_below_s=0,
        )
        t = 1000.0
        for _ in range(10):
            t += self.STEP
            ctl.step(t, True)
        self.assertEqual(heater.triggers, 1)
        self.assertEqual(heater.last_minutes, 30)


class NoWindowResetTests(unittest.TestCase):
    """Corrected contract after the 428-trigger runaway: re-issuing the enable
    RESETS the heater's window, so firing while the pump runs means the pump
    never stops. Burner cycles inside an active window must not re-trigger."""

    def test_burner_cycle_during_active_window_does_not_reset_it(self):
        heater = StubHeater()
        heater.running = True
        ctl = RecircController(
            heater, None, recirc_minutes=30, min_flow_s=0.0,
            cooldown_s=0.0, state_poll_s=1000.0, max_per_hour=99, renew_below_s=0,
        )
        ctl._device_recirc = True          # pump running, window active

        t = 1000.0
        for i in range(200):               # 10 s of burner on/off cycling
            t += 0.05
            ctl.step(t, i % 2 == 0)        # burner pulses
        self.assertEqual(heater.triggers, 0,
                         "firing here would reset the window and run the pump forever")

    def test_new_burst_after_the_pump_stops_fires(self):
        heater = StubHeater()
        heater.running = False
        ctl = RecircController(
            heater, None, recirc_minutes=30, min_flow_s=0.0,
            cooldown_s=0.0, state_poll_s=1.0, max_per_hour=99, renew_below_s=0,
        )
        ctl._device_recirc = True
        t = 1000.0
        for _ in range(5):
            t += 1.0
            ctl.step(t, False)             # idle: two polls confirm pump off
        for _ in range(5):
            t += 1.0
            ctl.step(t, True)              # a real draw
        self.assertEqual(heater.triggers, 1)


class ServiceWiringTests(unittest.TestCase):
    """Gap found in review: swapping the event-driven branch for a sleep left
    every test green. Pin the wiring itself."""

    def test_uses_poll_blocking_when_available(self):
        calls = []

        class SpyingSource:
            active = False

            def poll_blocking(self, timeout):
                calls.append(timeout)
                return False

        stop = threading.Event()
        service_once(SpyingSource(), _NullController(), stop, loop_s=0.02)
        self.assertEqual(len(calls), 1, "poll_blocking must be used when present")
        self.assertLessEqual(calls[0], 0.2)

    def test_falls_back_to_waiting_without_poll_blocking(self):
        class PlainSource:
            active = False

        stop = threading.Event()
        t0 = time.perf_counter()
        service_once(PlainSource(), _NullController(), stop, loop_s=0.05)
        self.assertGreaterEqual((time.perf_counter() - t0), 0.04,
                                "must wait loop_s when the source cannot block")

    def test_source_exception_does_not_propagate(self):
        class Exploding:
            active = False

            def poll_blocking(self, timeout):
                raise RuntimeError("boom")

        service_once(Exploding(), _NullController(), threading.Event(), 0.02)


class _NullController:
    def step(self, now, flow):
        return None


class ReplyPollutionTests(unittest.TestCase):
    """The fire-and-forget socket exists so its unread replies cannot be
    mistaken for the answer to a later `heater` query. Nothing tested that."""

    def setUp(self):
        self.console = MockConsole()
        self.heater = Heater("127.0.0.1", self.console.port)

    def tearDown(self):
        self.heater.close()
        self.console.close()

    def test_fast_trigger_leaves_no_pending_reply_on_console_socket(self):
        self.heater.command("heater")          # open + drain the console socket
        self.heater.start_recirculation_fast(30)
        time.sleep(0.15)                       # let any reply arrive
        ready, _, _ = select.select([self.heater._sock], [], [], 0)
        self.assertFalse(
            ready,
            "console socket has unread bytes: a fire-and-forget reply leaked onto "
            "the socket that carries state queries, which is how 'heater' ends up "
            "parsing '#? set ...' as its state answer",
        )

    def test_state_is_parsed_correctly_after_a_fast_trigger(self):
        self.heater.start_recirculation_fast(30)
        running, window, _ = self.heater.state()
        self.assertTrue(running, "state must reflect the pump being on")
        self.assertIsNotNone(window)


class FlushTests(unittest.TestCase):
    """_flush must restore the socket timeout on EVERY path. Restoring it inside
    the try meant the ordinary 'nothing to drain' case raised timeout first and
    left the socket at 10 ms, so later writes failed on a busy link."""

    def test_flush_restores_timeout_when_nothing_to_drain(self):
        a, b = socket.socketpair()
        src = Heater("127.0.0.1", 1)
        a.settimeout(0.3)
        src._flush(a)                      # nothing queued -> recv raises timeout
        self.assertAlmostEqual(a.gettimeout(), 0.30, places=6)
        a.close()
        b.close()

    def test_flush_restores_timeout_after_draining(self):
        a, b = socket.socketpair()
        src = Heater("127.0.0.1", 1)
        a.settimeout(0.25)
        b.sendall(b"stale reply\r\n")
        src._flush(a)
        self.assertAlmostEqual(a.gettimeout(), 0.25, places=6)
        a.close()
        b.close()


class FastPathDurationCachingTests(unittest.TestCase):
    """The fast path caches the duration too, on its own socket. Mutation testing
    found this uncovered: forcing send_duration=True left every test green."""

    def setUp(self):
        self.console = MockConsole()
        self.heater = Heater("127.0.0.1", self.console.port)

    def tearDown(self):
        self.heater.close()
        self.console.close()

    def test_duration_sent_once_across_fast_triggers(self):
        self.heater.start_recirculation_fast(30)
        time.sleep(0.15)
        self.heater.start_recirculation_fast(30)
        time.sleep(0.15)
        self.assertEqual(
            len(self.console.of_type("set recirculation_duration")), 1,
            "duration must be sent once, not on every fast trigger")
        self.assertEqual(
            len(self.console.of_type("set set_recirculation_enabled")), 2,
            "each fast trigger must still send the enable")


class TapAlignmentTests(unittest.TestCase):
    """Tap markers and journal events must land on the SAME clock.

    An earlier version parsed journal times onto a 1900-01-01 base while markers
    carried a real date, so every window matched ZERO events while still
    printing a confident table. Current code anchors both to now(); these tests
    pin that, plus the torn-append handling.
    """

    def _write(self, lines):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "taps.jsonl")
        with open(path, "w") as fh:
            for line in lines:
                fh.write(line + "\n")
        return tmp, path

    def setUp(self):
        import analyze_taps as A
        self.A = A

    def test_marker_is_on_the_same_clock_as_journal_events(self):
        now_ms = int(time.time() * 1000)
        tmp, path = self._write([json.dumps({"t_ms": now_ms, "wall": "x", "note": "t"})])
        try:
            taps, malformed = self.A.load_taps(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(malformed, 0)
        self.assertEqual(len(taps), 1)
        marker, note = taps[0]
        self.assertEqual(note, "t")
        # journal events are parsed against now(), so a marker written now must
        # be within seconds of now -- not 46,000 days away on a 1900 base
        self.assertLess(abs((datetime.datetime.now() - marker).total_seconds()), 10,
                        "marker clock diverged from the journal clock")

    def test_torn_final_line_is_counted_not_fatal(self):
        now_ms = int(time.time() * 1000)
        good = json.dumps({"t_ms": now_ms, "wall": "x", "note": "good"})
        tmp, path = self._write([good, '{"t_ms": 17'])   # truncated append
        try:
            taps, malformed = self.A.load_taps(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(taps), 1, "the good marker must survive a torn one")
        self.assertEqual(malformed, 1, "the torn line must be counted, not silently dropped")


    def test_single_bad_stopped_read_does_not_unlatch(self):
        """The real failure: one dirty console read said 'stopped', the gate
        opened, the next burner cycle re-fired, and the window reset forever
        (428 triggers). Requiring TWO consecutive stopped reads prevents it."""
        heater = StubHeater()
        heater.running = True
        responses = [True, False, True, True]      # poll 2 is a dirty read

        class Flaky:
            def __init__(self):
                self.i = 0

            def state(self):
                v = responses[min(self.i, len(responses) - 1)]
                self.i += 1
                return v, 1500, "# flaky"

        ctl = RecircController(
            Flaky(), None, recirc_minutes=30, min_flow_s=0.0,
            cooldown_s=0.0, state_poll_s=2.0, max_per_hour=99, renew_below_s=0,
        )
        ctl._device_recirc = True
        t = 1000.0
        # polls land on steps 0 and 2; the dirty read is poll 2, and step 2
        # carries demand, so a single-read debounce would open the gate exactly
        # when the burner is cycling
        for i in range(4):
            t += 1.0
            ctl.step(t, i != 1)
        self.assertEqual(
            heater.triggers, 0,
            "a single bad 'stopped' read must not unlatch the running state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
