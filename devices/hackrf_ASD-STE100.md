> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# HackRF One

Driver for the HackRF One through `pyhackrf` / `libhackrf`.

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

The baseband filter bandwidth changes by itself when the sample rate changes.

## Controls

| Key | Action |
|-----|--------|
| `b` | Turn the RF amplifier on or off (shown as `[amp:on/off]` in the footer) |

## Gain

The HackRF has no hardware AGC. The `gain` property maps one dB value onto two separate stages:

- **LNA** (RF front-end): 0–40 dB in 8 dB steps
- **VGA** (baseband): 0–62 dB in 2 dB steps

When you set the gain to `auto` (the `a` key), the driver uses a mid-range preset of LNA 24 dB and VGA 30 dB.

## Installation

```bash
brew install hackrf      # macOS / Homebrew — installs libhackrf
pip install pyhackrf
```
