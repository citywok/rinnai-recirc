# Rinnai RUR199iN recirculation controller — measured notes

Flow-triggered recirculation for a Rinnai RUR199iN + RWM Wi-Fi module (firmware
232). No hardware required: the module's own console on TCP 9798 does the work.

## What the device actually exposes

| Interface | Port | Auth | Use |
|---|---|---|---|
| Text console | 9798 | **none** | `list`, `heater`, `set ...` |
| Push stream | 9798 (`trace -1`) | none | `#@ prop change '<prop>'` / `#@  -> value` |
| Unknown protocol | 9799 | none | banner `{"protocol":[1]}`, not reverse-engineered |

Commands that matter:

```
set recirculation_duration 30          # minutes; 5 is the floor, 30 works
set set_recirculation_enabled true
```

## The measurement trap: pull is stale, push is live

Every value from `list` carries an age stamp, e.g. `{119m}`. The maintenance
snapshot refreshes on the module's own cadence in bursts 40-70 s apart.
`set do_maintenance_retrieval true` does **not** force a refresh: 1040 requests
over 17 minutes produced a median flow-value age of **300 s** (worst 840 s).

The `trace -1` stream is different: it pushes control state as it changes.
Verified across masks 1/2/4/8/15/31/255/-1:

| Signal | Pushed? |
|---|---|
| `recirculation_enabled`, `set_recirculation_enabled` | yes |
| `domestic_combustion`, `domestic_temperature` | yes |
| **`m01_water_flow_rate_raw`, `m02`/`m08`/`m11` temperatures** | **never, at any mask** |

## Measured latencies (ms-stamped stream, 5 cycles)

| Event | Median | Range |
|---|---|---|
| write acknowledged (`set_recirculation_enabled`) | 0.018 s | 0.016-0.081 s |
| pump reported running (`recirculation_enabled = true`) | **0.560 s** | 0.493-0.598 s |
| pump reported stopped | 0.558 s | 0.492-0.619 s |

Earlier figures of 0.95-1.26 s were polling artefacts; timestamp the stream, not
your poll loop.

### Trigger-path latency budget

`detection (combustion push) -> pump commanded`, measured 1.66 s in the first
live test. Decomposed, and then reduced:

| Component | Before | After |
|---|---|---|
| `MIN_FLOW_S` debounce | 1.00 s | **0.30 s** |
| duration + enable commands | 2 x 350 ms | **1 x 211 ms** |
| loop tick | 0.10 s | 0.10 s |
| device reports ON | 0.56 s | 0.56 s |
| **detection -> ON** | **1.66 s** | **~1.31 s** |

Live round-trip measurement: old read timeouts (first 1.5 / idle 0.30) averaged
**350 ms**; new (0.5 / 0.12) averaged **211 ms**. The duration command is now
sent only when it changes, because the module latches it and applies it at enable
time -- so the trigger path is a single write.

On the test setup, tap-to-hot time was about 3 seconds before the trigger-path
changes. The estimated time after those changes was 2.3–2.8 seconds.

## Controller design

The measured LAN setup triggers on `domestic_combustion` or `priority_status`.
The controller applies these guards against self-triggering:

1. authoritative pump state — never trigger while `recirculation_enabled` is true;
2. one trigger per demand episode (episode must go quiet to re-arm);
3. a draw that began during a cooldown does not count;
4. configurable cooldown and hourly trigger cap (defaults of 180 seconds and
   12 triggers per hour).

The recirc pump only ever runs when recirculation is on, so flow seen with recirc
off cannot be the pump. The genuinely ambiguous signal is the burner, which can
light inside a window to heat the loop.

Window renewal: re-issuing the enable resets the window to full, so the controller
tops up if the remaining window falls below 120 s while a phase is active.

## Known unknowns

- **`schedule_enabled` is unreliable from the console**: observed at `{83d}` old
  and contradicting the app. It is used only for a startup log line, never in the
  trigger path.
- The push stream did not report live flow rate or the maintenance temperatures
  listed above.
