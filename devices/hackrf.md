# HackRF One

Driver for the HackRF One via `pyhackrf` / `libhackrf`.

**Device name:** `HackRF`  
**Tunable range:** 1 MHz – 6 GHz  
**Library:** `pyhackrf` (wraps `libhackrf`)

## Supported sample rates

| Rate | Baseband filter set to |
|------|----------------------|
| 2 MHz | 1.75 MHz |
| 4 MHz | 3.5 MHz |
| 6 MHz | 6 MHz |
| 8 MHz | 7 MHz |
| 10 MHz | 9 MHz |
| 12.5 MHz | 12 MHz |
| 16 MHz | 15 MHz |
| 20 MHz | 20 MHz |

The baseband filter bandwidth is set automatically when the sample rate changes.

## Controls

| Key | Action |
|-----|--------|
| `b` | Toggle RF amplifier on/off (shown as `[amp:on/off]` in footer) |

## Gain

HackRF has no hardware AGC. The `gain` property maps a single dB value onto two independent stages:

- **LNA** (RF front-end): 0–40 dB in 8 dB steps
- **VGA** (baseband): 0–62 dB in 2 dB steps

When set to `auto` (the `a` key), a mid-range preset of LNA 24 dB + VGA 30 dB is applied.

## Installation

```bash
brew install hackrf      # macOS / Homebrew — installs libhackrf
pip install pyhackrf
```
