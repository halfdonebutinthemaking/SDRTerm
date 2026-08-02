> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# HackRF One

Driver for the HackRF One. It uses a direct ctypes binding to `libhackrf`.

**Device name:** `HackRF`
**Tunable range:** 1 MHz – 6 GHz
**Library:** `libhackrf` (loaded directly through ctypes. No Python wrapper is needed.)

The driver did use `pyhackrf` and `pyhackrf2` before. Both packages hard-code
the Linux library filename (`libhackrf.so.0`). They do not load on macOS,
where the file is `libhackrf.dylib`. Loading `libhackrf` directly through
ctypes prevents this problem and works on both platforms without changes to
the environment variables or symlinks.

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
# macOS
brew install hackrf                 # installs libhackrf and hackrf_info tools

# Debian / Ubuntu
sudo apt install libhackrf0 hackrf  # runtime library and tools

# Fedora
sudo dnf install hackrf hackrf-devel
```

You do not need a Python package. The driver loads `libhackrf` directly
through ctypes. Make sure the library is visible with `hackrf_info` from the
same shell before you start SDRTerm. If `hackrf_info` sees the device, the
SDRTerm driver can see it too.
