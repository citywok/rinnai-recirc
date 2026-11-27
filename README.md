# rinnai-recirc

On-demand hot-water recirculation for a Rinnai tankless heater with the **RWM
Wi-Fi module** (e.g. RUR199iN). When you open a tap, this starts the
recirculation pump so hot water arrives in seconds instead of a minute-plus.

**No cloud, no account, no app password.** The RWM module exposes an
unauthenticated text console on your LAN (TCP port 9798); this tool talks to
that directly. Your Rinnai credentials never leave your house.

```
tap opens -> burner/priority push from the module -> pump commanded -> hot loop
```

## Requirements

- A Rinnai heater whose RWM module is on your network (the same one the
  Control-R app uses). Python 3.9+ on a Linux host that's always on.
- The LAN-based modes require no additional hardware.

## Quick start

```bash
# 1. install
pipx install .            # or: pip install .

# 2. find your heater
rinnai-recirc discover    # prints the module's LAN address

# 3. configure
mkdir -p ~/.config/rinnai-recirc
cp config.example.yaml ~/.config/rinnai-recirc/config.yaml
$EDITOR ~/.config/rinnai-recirc/config.yaml   # at least: heater.host

# 4. try it (watch the journal while someone opens a tap)
rinnai-recirc state       # shows the pump state right now
rinnai-recirc run         # foreground; ctrl-c to stop
```

Run it as a service that survives reboots:

```bash
mkdir -p ~/.config/systemd/user
cp rinnai-recirc.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rinnai-recirc
loginctl enable-linger "$USER"            # keep running after logout
journalctl --user -u rinnai-recirc -f
```

`pipx` puts the binary in `~/.local/bin`, which is what the unit file expects.
No pipx? `python3 recirc_flow.py` from a checkout works too (it reads the same
config file).

## Configuration

Everything lives in one YAML file (`~/.config/rinnai-recirc/config.yaml`).
Every key is optional — see [`config.example.yaml`](config.example.yaml) for
the annotated full list. The basics:

| Key | Default | Meaning |
|---|---|---|
| `heater.host` | `auto` | Module address, or `auto` to find it on the LAN at startup |
| `trigger.mode` | `multi` | `multi` (burner+priority pushes), `stream` (burner only) |
| `recirculation.minutes` | `30` | How long each recirculation phase runs (module floor: 5) |
| `debounce.cooldown_s` | `180` | Dead time after a window ends before a new draw may trigger (`0` = off) |
| `debounce.max_per_hour` | `12` | Hard cap on triggers per hour (`0` = unlimited) |
| `debounce.state_poll_s` | `2.0` | How often the heater's pump state is re-read |

## Why it isn't a naive "flow -> pump" loop

Recirculation pushes water through the heater, so any flow sensor also sees the
pump's own flow — the naive loop re-triggers forever. Four independent guards:

1. **Authoritative state** — every decision reads the heater's own pump state
   (`recirculation_enabled`), never an assumption; a trigger is only allowed
   after **two consecutive** "stopped" reads, so a single glitchy reply can't
   cause a false start.
2. **One trigger per burst** — a continuous run of flow triggers once; the
   burst must actually end before it can trigger again.
3. **Cooldown** — after any recirculation window (ours or the unit's own
   schedule) ends, no new trigger for `cooldown_s`.
4. **Rate limit** — hard cap of `max_per_hour` triggers.

## Trigger modes

| Mode | Trigger |
|---|---|
| `multi` | Burner and priority pushes (default) |
| `stream` | Burner pushes only |

Measured latencies and protocol notes are in [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

## Troubleshooting

- **`discover` finds nothing** — check that the controller host can reach the
  heater on the LAN. If you know the module address, set `heater.host` directly.
- **Multiple results** — you have more than one Rinnai module; pin the right
  one with `heater.host`.
- **Trigger storms / pump never stops** — raise `debounce.cooldown_s`, lower
  `debounce.max_per_hour`.

## How it was built

The module's console protocol, the push-stream semantics (which properties are
pushed and which are never pushed), and measured trigger latencies are documented in
[`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

## License

MIT — see [LICENSE](LICENSE).
