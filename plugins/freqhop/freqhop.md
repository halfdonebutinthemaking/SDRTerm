# freqhop — Frequency Hopper

Maintains a saved list of frequencies with per-slot dwell times and cycles
through them automatically. Useful for monitoring multiple airband channels,
VDL2 sub-bands, or any set of discrete frequencies without needing to retune
manually.

![Frequency hopper cycling through airband channels](images/freqhop.gif)

## Controls

| Key | Action |
|-----|--------|
| `h` | Toggle hopping on/off |
| `a` | Add the current tuned frequency as a new slot |
| `A` | Type a new frequency (e.g. `136.9M`, `129.125M`) |
| `r` | Remove the selected slot |
| `[` | Decrease dwell time of the selected slot (−0.5 s) |
| `]` | Increase dwell time of the selected slot (+0.5 s) |
| `↑` / `k` | Move selection cursor up |
| `↓` / `j` | Move selection cursor down |
| `Enter` | Manually tune to the selected slot (does not start hopping) |

## Slot list

Each row shows the slot number, frequency, and dwell time. When hopping is
active:

- The **currently tuned slot** is highlighted in **green** and marked with `▶`
- The **selection cursor** is shown in reverse video
- If the cursor is on the active slot, both effects combine (green + reverse)

The header shows the hop state:

| Status | Meaning |
|---|---|
| `[HOPPING  N/M  next in Xs]` | Actively cycling; countdown to next hop |
| `[idle — h to start]` | Slots defined but hopper stopped |
| `[no slots — a=add current freq  A=type a freq]` | Empty list |

## How it works

1. When hopping starts, `state.center_hz` is saved and the SDR is tuned to
   slot 0.
2. After `dwell_s + 0.20 s` (settle window), `state.pending_freq` is set to
   the next slot. The main loop retunes the device on the next iteration.
3. When hopping stops (or the plugin is deactivated), the saved centre
   frequency is restored.

The 0.20 s settle window flushes the IQ buffer after each retune so the first
samples at the new frequency (which may still contain data from the previous
tuning) don't reach decoders.

## Persistence

Slots survive save/load via `save_state` / `load_state`. In `.sdrterm`
presets they appear as:

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

See `presets/florida_vdl2.sdrterm` for a working example (three VDL2 channels
with 60 s dwell each).

## Limitations

- **No frequency-specific gain**: all slots share the current gain setting.
  A slot list spanning multiple bands (e.g. HF and VHF) may work well on some
  frequencies and poorly on others.
- **Uniform settle time**: 0.20 s works for RTL-SDR and HackRF but a slow
  synthesiser might need longer. Not currently configurable.
- **No dwell scheduling**: slots are cycled strictly in order. If you want
  one frequency listened to twice as often, add it twice.
