> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# freqhop — Frequency Hopper

The plugin keeps a saved list of frequencies with per-slot dwell times.
It cycles through them automatically. Use it to monitor many airband
channels, VDL2 sub-bands, or any set of discrete frequencies. You do not
need to retune manually.

![Frequency hopper cycling through airband channels](images/freqhop.gif)

## Controls

| Key | Action |
|-----|--------|
| `h` | Toggle hopping on or off |
| `a` | Add the current tuned frequency as a new slot |
| `A` | Type a new frequency (for example `136.9M`, `129.125M`) |
| `r` | Remove the selected slot |
| `[` | Lower dwell time of the selected slot (−0.5 s) |
| `]` | Raise dwell time of the selected slot (+0.5 s) |
| `↑` / `k` | Move selection cursor up |
| `↓` / `j` | Move selection cursor down |
| `Enter` | Manually tune to the selected slot (does not start hopping) |

## Slot list

Each row shows the slot number, frequency, and dwell time. When hopping
is active:

- The **current tuned slot** shows in **green** with a `▶` mark.
- The **selection cursor** shows in reverse video.
- If the cursor is on the active slot, both effects combine (green +
  reverse).

The header shows the hop state:

| Status | Meaning |
|---|---|
| `[HOPPING  N/M  next in Xs]` | Actively cycling; countdown to next hop |
| `[idle — h to start]` | Slots defined but hopper stopped |
| `[no slots — a=add current freq  A=type a freq]` | Empty list |

## How it works

1. When hopping starts, the plugin saves `state.center_hz`. It then
   tunes the SDR to slot 0.
2. After `dwell_s + 0.20 s` (settle window), the plugin sets
   `state.pending_freq` to the next slot. The main loop retunes the
   device on the next iteration.
3. When hopping stops (or the plugin is deactivated), the plugin
   restores the saved centre frequency.

The 0.20 s settle window flushes the IQ buffer after each retune. So
the first samples at the new frequency do not reach decoders. These
samples may still contain data from the previous tuning.

## Persistence

Slots survive save and load through `save_state` and `load_state`. In
`.sdrterm` presets they show as:

```json
{
  "plugin_states": {
    "freqhop": {
      "slots": [
        {"freq_hz": 136900000.0, "dwell_s": 60.0},
        {"freq_hz": 136925000.0, "dwell_s": 60.0},
        {"freq_hz": 136875000.0, "dwell_s": 60.0}
      ]
    }
  }
}
```

See `presets/florida_vdl2.sdrterm` for a working example (three VDL2
channels with 60 s dwell each).

## Limitations

- **No frequency-specific gain**: all slots share the current gain
  setting. A slot list that spans many bands (for example HF and VHF)
  may work well on some frequencies and poorly on others.
- **Uniform settle time**: 0.20 s works for RTL-SDR and HackRF. But a
  slow synthesiser may need longer. It is not configurable at present.
- **No dwell scheduling**: the plugin cycles slots strictly in order.
  If you want one frequency listened to twice as often, add it twice.
