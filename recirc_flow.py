#!/usr/bin/env python3
"""
Flow-triggered recirculation for a Rinnai tankless heater (RUR199iN + RWM module).

Control:  the heater's own local console, TCP port 9798, no auth:
              set recirculation_duration <minutes>
              set set_recirculation_enabled true

WHY THIS IS NOT A NAIVE "FLOW -> PUMP" LOOP
-------------------------------------------
Recirculation pushes water through the heater, so the flow switch also sees the
pump's own flow. A rule as simple as "flow -> start pump" re-triggers forever.
Four independent guards prevent that:

  1. AUTHORITATIVE STATE - every decision reads the heater's own recirculation
     state (`heater` command). We never assume the pump is off.
  2. ONE TRIGGER PER BURST - a "burst" is a continuous run of flow. Once a
     burst has triggered recirculation it cannot trigger again until flow has
     actually stopped.
  3. COOLDOWN - after any recirculation window ends (ours or the unit's own
     schedule), no new trigger is allowed for COOLDOWN_S seconds.
  4. RATE LIMIT - hard cap of MAX_TRIGGERS_PER_HOUR.

Requires: python3 (3.9+). Config: config.yaml (see config.example.yaml); the
          heater is auto-discovered on the local network by default. gpiozero is
          only needed for the experimental GPIO trigger mode.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import os
import re
import select
import signal
import socket
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# Defaults (config.yaml overrides these; see config.example.yaml)
# --------------------------------------------------------------------------- #
HEATER_HOST_DEFAULT = "auto"      # find the module on the LAN, or pin an IP
HEATER_PORT = 9798
FLOW_PIN = 14
RECIRC_MINUTES = 30               # phase length
RENEW_BELOW_S = 120.0             # top up the window
STATE_POLL_S = 2.0
MIN_FLOW_S = 0.0                  # 0 = trigger on first tick
COOLDOWN_S = 180.0                # 3 min no-retrigger
MAX_TRIGGERS_PER_HOUR = 12
FIRE_AND_FORGET = True
LOG_ALL_PUSHES = False
TRIGGER_MODE_DEFAULT = "multi"    # multi | stream | gpio
LOOP_S = 0.02

log = logging.getLogger("recirc")


# --------------------------------------------------------------------------- #
# Heater console client
# --------------------------------------------------------------------------- #
class HeaterUnavailable(RuntimeError):
    pass


class Heater:
    """Line-oriented client for the Rinnai module's console on TCP 9798."""

    RECIRC_LINE = re.compile(r"recirc\s+([YN])\b", re.I)
    WINDOW_LINE = re.compile(r"recirc:\s*(\d+):(\d\d)\s*window", re.I)
    PROP_LINE = re.compile(r"recirculation_enabled:\s*(true|false)", re.I)
    SCHED_LINE = re.compile(r"schedule:.*enabled", re.I)

    def __init__(self, host: str, port: int = 9798, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._fire_sock = None
        self._lock = threading.Lock()
        self._duration_sent = None

    def _ensure(self):
        if self._sock is not None:
            return self._sock
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(1.0)
        self._read_until_idle(s, first=1.0, idle=0.2)   # drain "# monitor" banner
        self._sock = s
        return s

    @staticmethod
    def _read_until_idle(sock, first, idle, cap=16384):
        buf = b""
        sock.settimeout(first)
        try:
            while len(buf) < cap:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                sock.settimeout(idle)
        except (socket.timeout, OSError):
            pass
        return buf.decode("utf-8", "replace")

    def _flush(self, sock, cap=65536):
        """Discard unread reply bytes, bounded so a chatty peer cannot stall us.

        The timeout restore MUST be in finally: the ordinary 'nothing to drain'
        case raises socket.timeout before the loop ends, and leaving the socket
        at the 10 ms probe timeout makes every later write fail on a busy link.
        """
        dropped = 0
        old = sock.gettimeout()
        try:
            sock.settimeout(0.01)
            while dropped < cap:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                dropped += len(chunk)
        except (socket.timeout, OSError, BlockingIOError):
            pass
        finally:
            try:
                sock.settimeout(old)
            except OSError:
                pass

    def command(self, cmd, first=0.5, idle=0.12):
        with self._lock:
            last_error = None
            for _ in range(2):                       # one silent retry
                try:
                    sock = self._ensure()
                    self._flush(sock)
                    sock.sendall((cmd + "\r\n").encode())
                    return self._read_until_idle(sock, first, idle)
                except OSError as exc:
                    last_error = exc
                    self.close()
            raise HeaterUnavailable("%r failed: %s" % (cmd, last_error))

    def start_recirculation_fast(self, minutes):
        """Write the enable command and return without waiting for the echo.

        The pump needs ~0.5 s to report regardless, so blocking ~200 ms on the
        console's acknowledgement only delays putting the bytes on the wire.
        Confirmation comes from the push stream instead.

        These writes go out on their OWN socket: the console's replies would
        otherwise sit unread on the command socket and be mistaken for the
        answer to the next `heater` query (observed: the controller parsing
        "#? set ..." as a state reply and concluding the pump was off).
        """
        with self._lock:
            try:
                if self._fire_sock is None:
                    # cap the dial timeout: this runs under the command lock, so
                    # a blackholed heater must not stall the control step for 5 s
                    self._fire_sock = socket.create_connection(
                        (self.host, self.port), timeout=min(self.timeout, 2.0))
                    self._fire_sock.settimeout(0.5)
                    self._read_until_idle(self._fire_sock, first=0.5, idle=0.1)
                payload = b""
                send_duration = self._duration_sent != minutes
                if send_duration:
                    payload += ("set recirculation_duration %d\r\n" % minutes).encode()
                payload += b"set set_recirculation_enabled true\r\n"
                self._flush(self._fire_sock)
                self._fire_sock.sendall(payload)
            except OSError as exc:
                # Never let a dead fast-path socket escape as a raw OSError: the
                # controller only handles HeaterUnavailable, so anything else
                # skips the cooldown and re-fires on every loop tick.
                self.close()
                raise HeaterUnavailable("fast trigger failed: %s" % exc)
            # cached only after the bytes are actually away
            if send_duration:
                self._duration_sent = minutes
            return True

    def close(self):
        for attr in ("_sock", "_fire_sock"):
            s = getattr(self, attr, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    def state(self):
        """Return (recirc_running_or_None_if_unparseable,
        window_seconds_remaining_or_None, raw_line)."""
        raw = self.command("heater")
        running = None
        window = None

        m = self.RECIRC_LINE.search(raw)
        if m:
            running = m.group(1).upper() == "Y"
        w = self.WINDOW_LINE.search(raw)
        if w:
            window = int(w.group(1)) * 60 + int(w.group(2))

        if running is None:                          # firmware fallback
            prop = self.command("find recirculation_enabled")
            p = self.PROP_LINE.search(prop)
            if p:
                running = p.group(1).lower() == "true"
            # p is None -> running stays None: an unparseable reply is an
            # unknown state, not a "stopped" verdict

        return running, window, raw.strip().splitlines()[0] if raw.strip() else ""

    def schedule_enabled(self):
        try:
            return bool(self.SCHED_LINE.search(self.command("heater")))
        except HeaterUnavailable:
            return False

    def start_recirculation(self, minutes):
        # The duration is latched in the module and only applied at enable time,
        # so it only needs re-sending when it has changed. Skipping it removes a
        # whole command round-trip from the trigger path -- the one place where
        # ~300 ms is directly visible to someone standing at a tap.
        if self._duration_sent != minutes:
            self.command("set recirculation_duration %d" % minutes)
            self._duration_sent = minutes
        reply = self.command("set set_recirculation_enabled true")
        return "set_recirculation_enabled" in reply


# --------------------------------------------------------------------------- #
# Flow switch input
# --------------------------------------------------------------------------- #
class StreamDemand:
    """Trigger source with NO hardware: the module's live push stream.

    `trace -1` makes the module push `#@ prop change '<name>'` / `#@  -> value`
    as state changes. Measured latency: recirculation_enabled -> true in
    ~0.95-1.24 s after the command; domestic_combustion -> true when the burner
    fires on a hot draw. NOTE: flow rate and temperatures are NOT pushed at all.

    .active is True while the burner is firing (i.e. hot water is being drawn).
    """

    def __init__(self, host, port=9798, log_all_pushes=None):
        self.host = host
        self.port = port
        self.sock = None
        self.buf = ""
        self.values = {}
        self.pending = None
        self._last_connect = 0.0
        self.log_all_pushes = LOG_ALL_PUSHES if log_all_pushes is None else log_all_pushes

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=4.0)
        s.settimeout(0.5)
        s.sendall(b"trace -1\r\n")
        self.sock = s
        self.buf = ""
        self._last_connect = time.monotonic()

    def poll(self):
        if self.sock is None:
            if time.monotonic() - self._last_connect < 5.0:
                return
            try:
                self._connect()
            except OSError:
                return
        try:
            self.sock.settimeout(0.01)
            data = self.sock.recv(8192)
            if not data:
                raise OSError("stream closed")
            self.buf += data.decode("utf-8", "replace")
        except socket.timeout:
            pass
        except OSError:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            # Stale cached values survive a reconnect and defeat edge
            # detection: if combustion flipped false while offline, the next
            # true push would compare equal to the cached one.
            self.values = {}
            self.pending = None
            self.buf = ""
            return

        lines = self.buf.split("\n")
        self.buf = lines.pop()
        for line in lines:
            line = line.strip()
            if self.log_all_pushes and line:
                # journald timestamps to the microsecond: this is the evidence
                # for "which signal fires first on a real draw"
                log.info("push: %s", line)
            if line.startswith("#@ prop change '"):
                self.pending = line.split("'")[1]
            elif line.startswith("#@  ->") and self.pending:
                self.values[self.pending] = line.split("->", 1)[1].strip().strip('"')
                self.pending = None

    def poll_blocking(self, timeout=0.2):
        """Event-driven: block until the socket has bytes (or timeout).

        A push is handled the instant it arrives instead of waiting for the next
        loop tick, which is the difference between ~2 ms and ~20 ms of avoidable
        delay on the trigger path. Returns True if bytes were processed.
        """
        if self.sock is None:
            try:
                self._connect()
            except OSError:
                time.sleep(min(timeout, 0.05))
                return False
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.values = {}
            self.pending = None
            self.buf = ""
            return False
        if not ready:
            return False
        self.poll()          # non-blocking read + parse of what is waiting
        return True

    @property
    def active(self):
        """True while the burner is firing (a real hot-water draw)."""
        self.poll()
        return self.values.get("domestic_combustion") == "true"

    @property
    def pump_running(self):
        self.poll()
        return self.values.get("recirculation_enabled") == "true"


class MultiSignalDemand:
    """OR of every signal that can indicate water flowing, with hysteresis.

    Signals watched (any one is enough):
      * domestic_combustion  -> true   (burner lit = hot water being drawn)
      * priority_status      -> true   (a controller took priority)
      * GPIO flow switch     (experimental; untested on hardware)

    Loop safety: recirculation itself lights the burner, so all signals feed ONE
    combined demand flag. Once anything fires, demand is held for HOLD_S and
    cannot re-arm until it has been quiet for QUIET_S. The controller's cooldown
    and pump-state guards sit on top of that.

    `.episode_signal` names whichever signal started the current demand episode,
    so real-world use records what actually fires first.
    """

    HOLD_S = 20.0
    QUIET_S = 60.0
    WATCHED = ("domestic_combustion", "priority_status")

    def __init__(self, host, port=9798, gpio=None, log_all_pushes=None):
        self.stream = StreamDemand(host, port, log_all_pushes=log_all_pushes)
        self.gpio = gpio
        self._last_any = -1e9
        self._prev = {}
        self.episode_signal = None

    def poll(self):
        self.stream.poll()
        now = time.monotonic()
        fired = None

        for name in self.WATCHED:
            cur = self.stream.values.get(name)
            prev = self._prev.get(name)
            if cur is not None and cur != prev and cur == "true":
                fired = fired or name
            self._prev[name] = cur

        if self.gpio is not None and self.gpio.active:
            fired = fired or "gpio"

        if fired:
            quiet_for = now - self._last_any
            if quiet_for >= self.QUIET_S or self.episode_signal is None:
                self.episode_signal = fired
            self._last_any = now

    def poll_blocking(self, timeout=0.2):
        """Delegate the blocking wait, then re-evaluate the combined demand flag."""
        updated = self.stream.poll_blocking(timeout)
        self.poll()
        return updated

    @property
    def active(self):
        self.poll()
        if self.gpio is not None and self.gpio.active:
            return True
        return (time.monotonic() - self._last_any) < self.HOLD_S


class GpioFlowSwitch:
    """Dry contact to GND on GPIO14. Active-low; internal pull-up enabled."""

    def __init__(self, pin=14):
        try:
            from gpiozero import Button       # lazy: not present on a dev box
        except ImportError as exc:
            raise SystemExit("trigger mode gpio needs gpiozero "
                             "(pip install rinnai-recirc[gpio]): %s" % exc)
        # bounce_time is a hard floor on detection latency; 20 ms is enough to
        # swallow contact chatter without eating a fifth of the 100 ms budget.
        self._button = Button(pin, pull_up=True, bounce_time=0.02)

    @property
    def active(self):
        return bool(self._button.is_pressed)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class RecircController:
    """Flow -> recirculation, latched so the pump's own flow cannot re-trigger."""

    def __init__(self, heater, flow, recirc_minutes=5, min_flow_s=1.0,
                 cooldown_s=180.0, state_poll_s=2.0, max_per_hour=12,
                 renew_below_s=120.0, fire_and_forget=None):
        self.heater = heater
        self.flow = flow
        self.recirc_minutes = recirc_minutes
        self.min_flow_s = min_flow_s
        self.cooldown_s = cooldown_s
        self.state_poll_s = state_poll_s
        self.max_per_hour = max_per_hour
        self.renew_below_s = renew_below_s
        self.fire_and_forget = FIRE_AND_FORGET if fire_and_forget is None else fire_and_forget

        self._burst_start = None
        self._last_flow_end = 0.0
        self._triggers_in_burst = 0

        self._device_recirc = False
        self._device_checked_at = -1e9
        self._cooldown_until = 0.0
        self._state_unknown = True   # nothing confirmed yet: hold triggers

        self._trigger_times = []
        self._unavailable_logged = False
        self._optimistic_since = 0.0
        self._rate_warned = False
        self._stopped_streak = 0
        self._phase_until = 0.0
        self._last_renew = 0.0

    def _refresh_device(self, now):
        if now - self._device_checked_at < self.state_poll_s:
            return
        try:
            running, window, line = self.heater.state()
            self._device_checked_at = now
            # Debounce: the console's reply is not clean every time, and a single
            # bad read once made the controller believe the pump was off, re-fire
            # on the next burner cycle, and reset the window forever.
            if running is None:
                # An unparseable reply is NOT a stopped reading: two garbled
                # polls in a row would clear the running guard and let the
                # next draw reset the pump's window. Unknown inhibits instead.
                self._state_unknown = True
                self._stopped_streak = 0
                return
            self._state_unknown = False
            if running:
                self._stopped_streak = 0
                if not self._device_recirc:
                    log.info("heater reports recirculation running (window=%ss)", window)
                self._device_recirc = True
            else:
                self._stopped_streak += 1
                if self._stopped_streak >= 2:
                    self._stopped_streak = 0
                    if self._device_recirc:
                        log.info("heater reports recirculation stopped (2 consecutive reads)")
                    self._device_recirc = False
            if self._device_recirc:
                self._cooldown_until = max(self._cooldown_until, now + self.cooldown_s)
                # safety net: if the unit is running but its window is nearly out
                # while our phase is still active, top it up (re-issuing resets it)
                if (self.renew_below_s > 0
                        and window is not None and window < self.renew_below_s
                        and now < self._phase_until and now - self._last_renew > 60.0):
                    self._last_renew = now
                    try:
                        self.heater.start_recirculation(self.recirc_minutes)
                        log.info("renewed recirculation window (was %ss, phase %d min)",
                                 window, self.recirc_minutes)
                    except HeaterUnavailable as exc:
                        log.warning("renewal failed: %s", exc)
            self._unavailable_logged = False
        except HeaterUnavailable as exc:
            self._device_checked_at = now
            self._state_unknown = True
            # If the console is unreachable we cannot confirm the pump state.
            # Never leave an optimistic _device_recirc latched: that would block
            # every future trigger, silently, forever.
            if self._device_recirc and now - self._optimistic_since > 3.0:
                self._device_recirc = False
            if not self._unavailable_logged:
                log.warning("heater unreachable, holding off: %s", exc)
                self._unavailable_logged = True

    def _rate_ok(self, now):
        if self.max_per_hour <= 0:          # 0 = no cap at all
            return True
        cutoff = now - 3600.0
        self._trigger_times = [t for t in self._trigger_times if t > cutoff]
        return len(self._trigger_times) < self.max_per_hour

    def step(self, now, flow_active):
        """Advance the state machine with an injected clock. Returns event name."""
        self._refresh_device(now)

        if flow_active:
            if self._burst_start is None:
                self._burst_start = now
                self._triggers_in_burst = 0
                self._rate_warned = False
                log.debug("flow burst start")
        else:
            if self._burst_start is not None:
                self._last_flow_end = now
                log.debug("flow burst end")
            self._burst_start = None

        if self._state_unknown:
            # No confirmed pump state: the console may still take the enable
            # command even while state queries fail, which would reset a
            # running window. Hold until an explicit reading arrives.
            return None

        # The heater is the authority on whether recirculation is needed, but
        # re-issuing the enable RESETS its window to full. With the pump-state
        # gate removed, every burner cycle inside an active window looked like a
        # new draw and the window never expired: 428 triggers in ~21 h and the
        # pump effectively ran continuously. So the gate is back -- not to
        # second-guess the heater, but to stop resetting its timer.
        if self._device_recirc:
            return None
        if now < self._cooldown_until:              # 0 by default: no dead time
            return None
        if self._burst_start is None:
            return None
        if self._burst_start < self._cooldown_until:
            # the draw began during a cooldown (e.g. while the unit's own
            # schedule was running) - only a NEW draw may trigger
            return None
        if self._triggers_in_burst > 0:             # this burst already fired
            return None
        if now - self._burst_start < self.min_flow_s:
            return None
        if not self._rate_ok(now):
            if not self._rate_warned:          # once per burst, not per tick
                self._rate_warned = True
                log.warning("rate limit reached (%d/hour), ignoring this draw",
                            self.max_per_hour)
            return None

        try:
            if self.fire_and_forget and hasattr(self.heater, "start_recirculation_fast"):
                ok = self.heater.start_recirculation_fast(self.recirc_minutes)
            else:
                ok = self.heater.start_recirculation(self.recirc_minutes)
        except HeaterUnavailable as exc:
            log.error("failed to start recirculation: %s", exc)
            self._cooldown_until = now + 30.0
            return None

        if not ok:
            log.error("heater rejected the recirculation command")
            self._cooldown_until = now + 30.0
            return None

        self._triggers_in_burst += 1
        self._trigger_times.append(now)
        self._cooldown_until = now + self.cooldown_s
        self._device_recirc = True                  # optimistic until polled
        self._optimistic_since = now                # ...but not forever
        self._device_checked_at = now
        self._phase_until = now + self.recirc_minutes * 60.0
        src = getattr(self.flow, "episode_signal", None)
        log.info("flow detected%s -> recirculation ON for %d minutes",
                 (" via " + src) if src else "", self.recirc_minutes)
        return "recirculation_started"


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def service_once(source, controller, stop, loop_s=LOOP_S):
    """One iteration of the service loop, extracted so the wiring is testable.

    Event-driven when the source can block on its socket: a push is handled the
    instant it lands. The timeout still drives the periodic device-state refresh
    when the stream is silent.
    """
    try:
        if hasattr(source, "poll_blocking"):
            source.poll_blocking(min(loop_s * 10, 0.2))
        else:
            stop.wait(loop_s)
        flow = source.active
    except Exception as exc:
        log.error("flow source read failed: %s", exc)
        flow = False
    try:
        controller.step(time.monotonic(), flow)
    except Exception:
        log.exception("unexpected error in control step")


# --------------------------------------------------------------------------- #
# Discovery: find the module's console on the LAN (TCP 9798, no auth)
# --------------------------------------------------------------------------- #
def _local_net(cidr):
    import ipaddress
    if cidr and cidr != "auto":
        return ipaddress.ip_network(cidr, strict=False)
    # Routing lookup only: a connected UDP socket picks the default-route
    # interface without sending a packet.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        local = probe.getsockname()[0]
    finally:
        probe.close()
    return ipaddress.ip_network("%s/24" % local, strict=False)


def _speaks_console(ip, port, timeout):
    """True if ip:port answers `heater` with a console reply (mentions recirc)."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return False
    try:
        s.settimeout(timeout)
        s.sendall(b"heater\r\n")
        buf = b""
        try:
            while len(buf) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                s.settimeout(0.15)     # reply done after a short idle gap
        except (socket.timeout, OSError):
            pass
        return "recirc" in buf.decode("utf-8", "replace").lower()
    finally:
        try:
            s.close()
        except OSError:
            pass


def find_heaters(cidr="auto", port=HEATER_PORT, timeout=0.5, workers=64,
                 attempts=3, retry_delay_s=2.0):
    """Addresses on the subnet that speak the Rinnai console.

    Retries the whole scan when it comes back empty: the module briefly drops
    or delays new clerk connections right after a connection burst, and a scan
    run while the controller (or a phone app) is churning its own connections
    can legitimately miss a heater that is fine a second later.
    """
    import concurrent.futures
    net = _local_net(cidr)
    hosts = [str(h) for h in net.hosts()]
    for attempt in range(1, attempts + 1):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            hits = [ip for ip in pool.map(
                        lambda ip: ip if _speaks_console(ip, port, timeout) else None, hosts)
                    if ip]
        if hits or attempt == attempts:
            return sorted(hits, key=lambda a: tuple(int(p) for p in a.split(".")))
        log.info("no console found (attempt %d/%d), retrying in %ss",
                 attempt, attempts, retry_delay_s)
        time.sleep(retry_delay_s)
    return []


def _resolve_host(settings):
    """Return the heater address, discovering it when heater.host is 'auto'."""
    if settings.heater_host not in ("", "auto"):
        return settings.heater_host
    log.info("looking for the heater console on port %d (%s)...",
             settings.heater_port, settings.discovery_cidr)
    try:
        hits = find_heaters(settings.discovery_cidr, settings.heater_port,
                            settings.discovery_timeout_s, settings.discovery_workers)
    except OSError as exc:
        log.error("cannot scan the local network (%s); pin the heater's address "
                  "with heater.host in the config", exc)
        return None
    if not hits:
        log.error("no Rinnai console found on the local network. Check that the "
                  "heater's Wi-Fi module is on this network, or pin its address "
                  "with heater.host in the config (the Rinnai app or your router "
                  "can tell you the address).")
        return None
    if len(hits) > 1:
        log.warning("multiple Rinnai consoles found: %s -- using %s "
                    "(pin heater.host in the config to silence this)", hits, hits[0])
    else:
        log.info("heater console found at %s", hits[0])
    return hits[0]


# --------------------------------------------------------------------------- #
# Settings: config.yaml over the defaults above
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Settings:
    heater_host: str = HEATER_HOST_DEFAULT    # "auto" = discover on the LAN
    heater_port: int = HEATER_PORT
    trigger_mode: str = TRIGGER_MODE_DEFAULT  # multi | stream | gpio
    gpio_pin: int = FLOW_PIN
    min_flow_s: float = MIN_FLOW_S
    recirc_minutes: int = RECIRC_MINUTES
    renew_below_s: float = RENEW_BELOW_S
    state_poll_s: float = STATE_POLL_S
    cooldown_s: float = COOLDOWN_S
    max_per_hour: int = MAX_TRIGGERS_PER_HOUR
    fire_and_forget: bool = FIRE_AND_FORGET
    log_all_pushes: bool = LOG_ALL_PUSHES
    loop_s: float = LOOP_S
    log_level: str = "INFO"
    discovery_cidr: str = "auto"              # or e.g. 192.168.1.0/24
    discovery_timeout_s: float = 0.5
    discovery_workers: int = 64


CONFIG_SEARCH = ("config.yaml", os.path.expanduser("~/.config/rinnai-recirc/config.yaml"))


def load_settings(path=None):
    """Build Settings from config.yaml (or the given path).

    With no path given, ./config.yaml then ~/.config/rinnai-recirc/config.yaml
    are used when present; no file means pure defaults. An explicit --config
    that cannot be read is a hard error.
    """
    import yaml

    explicit = path is not None
    if path is None:
        for cand in CONFIG_SEARCH:
            if os.path.exists(cand):
                path = cand
                break
    raw = {}
    if path is not None:
        try:
            with open(path) as fh:
                raw = yaml.safe_load(fh) or {}
        except OSError as exc:
            if explicit:
                raise SystemExit("config error: cannot read %s: %s" % (path, exc))
        except yaml.YAMLError as exc:
            raise SystemExit("config error: %s is not valid YAML: %s" % (path, exc))
        if not isinstance(raw, dict):
            raise SystemExit("config error: %s must be a mapping of sections" % path)

    s = Settings()

    def section(name):
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise SystemExit("config error: '%s:' must be a mapping" % name)
        return value

    def take(sec, key, attr, cast, lo=None, hi=None):
        if key not in sec:
            return
        v = sec[key]
        if cast is bool:
            if not isinstance(v, bool):
                raise SystemExit("config error: %s must be true or false, got %r" % (key, v))
        else:
            if isinstance(v, bool):
                raise SystemExit("config error: %s must be %s, got %r" % (key, cast.__name__, v))
            try:
                v = cast(v)
            except (TypeError, ValueError):
                raise SystemExit("config error: %s must be %s, got %r" % (key, cast.__name__, sec[key]))
            if isinstance(v, float) and not math.isfinite(v):
                raise SystemExit("config error: %s must be a finite number, got %r" % (key, sec[key]))
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise SystemExit("config error: %s must be between %s and %s, got %r" % (key, lo, hi, sec[key]))
        setattr(s, attr, v)

    heater = section("heater")
    if "host" in heater:
        raw_host = heater["host"]
        s.heater_host = ("auto" if raw_host is None else str(raw_host).strip()) or "auto"
    take(heater, "port", "heater_port", int, 1, 65535)

    trigger = section("trigger")
    if "mode" in trigger:
        s.trigger_mode = str(trigger["mode"]).strip().lower()
    if s.trigger_mode not in ("multi", "stream", "gpio"):
        raise SystemExit("config error: trigger.mode must be multi, stream or gpio, got %r" % s.trigger_mode)
    take(trigger, "gpio_pin", "gpio_pin", int, 0, 27)
    take(trigger, "min_flow_s", "min_flow_s", float, 0.0, 60.0)

    recirc = section("recirculation")
    take(recirc, "minutes", "recirc_minutes", int, 5, 120)
    take(recirc, "renew_below_s", "renew_below_s", float, 0.0, 7200.0)

    debounce = section("debounce")
    take(debounce, "cooldown_s", "cooldown_s", float, 0.0, 86400.0)
    take(debounce, "max_per_hour", "max_per_hour", int, 0, 1000)
    take(debounce, "state_poll_s", "state_poll_s", float, 0.5, 60.0)

    advanced = section("advanced")
    take(advanced, "fire_and_forget", "fire_and_forget", bool)
    take(advanced, "log_all_pushes", "log_all_pushes", bool)
    take(advanced, "loop_s", "loop_s", float, 0.005, 1.0)
    if "log_level" in advanced:
        s.log_level = str(advanced["log_level"]).strip().upper()
    if s.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise SystemExit("config error: log_level must be DEBUG, INFO, WARNING or ERROR, got %r" % s.log_level)

    disc = advanced.get("discovery") or {}
    if not isinstance(disc, dict):
        raise SystemExit("config error: 'discovery:' must be a mapping")
    if "cidr" in disc:
        s.discovery_cidr = str(disc["cidr"]).strip() or "auto"
    if s.discovery_cidr != "auto":
        import ipaddress
        try:
            parsed_net = ipaddress.ip_network(s.discovery_cidr, strict=False)
        except ValueError as exc:
            raise SystemExit("config error: discovery.cidr %r: %s" % (s.discovery_cidr, exc))
        if parsed_net.version != 4:
            raise SystemExit("config error: discovery.cidr %r must be IPv4 "
                             "(the scanner only scans IPv4)" % s.discovery_cidr)
    take(disc, "timeout_s", "discovery_timeout_s", float, 0.05, 2.0)
    take(disc, "workers", "discovery_workers", int, 1, 512)

    known_keys = {
        "heater": {"host", "port"},
        "trigger": {"mode", "gpio_pin", "min_flow_s"},
        "recirculation": {"minutes", "renew_below_s"},
        "debounce": {"cooldown_s", "max_per_hour", "state_poll_s"},
        "advanced": {"fire_and_forget", "log_all_pushes", "loop_s", "log_level",
                     "discovery"},
    }
    for name, value in raw.items():
        if name not in known_keys:
            log.warning("unknown config section %r (known: %s) -- check spelling",
                        name, ", ".join(known_keys))
            continue
        if not isinstance(value, dict):
            continue
        for key in value:
            if name == "advanced" and key == "discovery":
                for dkey in (value["discovery"] or {}):
                    if dkey not in ("cidr", "timeout_s", "workers"):
                        log.warning("unknown config key advanced.discovery.%r", dkey)
            elif key not in known_keys[name]:
                log.warning("unknown config key %s.%r (check spelling)", name, key)
    return s


def run(settings=None):
    settings = settings or load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    host = _resolve_host(settings)
    if host is None:
        return 1                  # systemd's Restart=always retries in 10 s

    heater = Heater(host, settings.heater_port)
    if settings.trigger_mode == "multi":
        source = MultiSignalDemand(host, settings.heater_port,
                                   log_all_pushes=settings.log_all_pushes)
        log.info("trigger mode: MULTI (combustion + priority pushes)")
    elif settings.trigger_mode == "stream":
        source = StreamDemand(host, settings.heater_port,
                              log_all_pushes=settings.log_all_pushes)
        log.info("trigger mode: STREAM (domestic_combustion pushes, no hardware)")
    else:
        source = GpioFlowSwitch(settings.gpio_pin)
        log.info("trigger mode: GPIO%d", settings.gpio_pin)

    controller = RecircController(
        heater, source,
        recirc_minutes=settings.recirc_minutes, min_flow_s=settings.min_flow_s,
        cooldown_s=settings.cooldown_s, state_poll_s=settings.state_poll_s,
        max_per_hour=settings.max_per_hour,
        renew_below_s=settings.renew_below_s,
        fire_and_forget=settings.fire_and_forget,
    )

    try:
        running, window, line = heater.state()
        log.info("heater state: recirculation=%s window=%s", running,
                 "unknown" if window is None else "%ds" % window)
        if heater.schedule_enabled():
            log.warning(
                "heater's CACHED schedule flag reads enabled (this value is often "
                "days/weeks stale -- trust the app, not the console). A scheduled "
                "window only suppresses a trigger while the pump is actually "
                "running, which is checked live."
            )
    except HeaterUnavailable as exc:
        log.warning("cannot reach heater at startup (will keep retrying): %s", exc)

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    src_desc = {
        "multi": "push stream (combustion + priority)",
        "stream": "push stream (domestic_combustion)",
        "gpio": "GPIO%d" % settings.gpio_pin,
    }.get(settings.trigger_mode, settings.trigger_mode)
    log.info("watching %s -> %s:%d (recirc %d min, cooldown %.0fs, max %d/h)",
             src_desc, host, settings.heater_port, settings.recirc_minutes,
             settings.cooldown_s, settings.max_per_hour)

    try:
        while not stop.is_set():
            service_once(controller.flow, controller, stop, settings.loop_s)
    finally:
        heater.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rinnai-recirc",
        description="Flow-triggered recirculation for Rinnai RWM modules "
                    "(local console on TCP 9798; no cloud, no account).")
    parser.add_argument("--version", action="version", version="rinnai-recirc 0.1.0")
    sub = parser.add_subparsers(dest="command")
    _run_p = sub.add_parser("run", help="run the controller (the default command)")
    _run_p.add_argument("--config", help="path to config.yaml")
    _disc_p = sub.add_parser("discover", help="list Rinnai consoles found on the local network")
    _disc_p.add_argument("--cidr", default="auto", help="subnet to scan, e.g. 192.168.1.0/24 (default: the local /24)")
    _disc_p.add_argument("--port", type=int, default=HEATER_PORT)
    _disc_p.add_argument("--timeout", type=float, default=0.5)
    state_p = sub.add_parser("state", help="show the heater's current recirculation state")
    state_p.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args(argv)

    if args.command == "discover":
        hits = find_heaters(args.cidr, args.port, args.timeout)
        if not hits:
            print("no Rinnai console found on %s (port %d)" % (args.cidr, args.port))
            return 1
        for hit in hits:
            print(hit)
        return 0

    settings = load_settings(getattr(args, "config", None))
    if args.command == "state":
        host = _resolve_host(settings)
        if host is None:
            return 1
        heater = Heater(host, settings.heater_port)
        try:
            running, window, line = heater.state()
            print("recirculation: %s  window: %s" % (
                "unknown" if running is None else ("RUNNING" if running else "stopped"),
                "unknown" if window is None else "%ds" % window))
        except HeaterUnavailable as exc:
            print("heater unreachable: %s" % exc)
            return 1
        finally:
            heater.close()
        return 0
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
