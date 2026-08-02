# HackRF One

Driver for the HackRF One via a direct ctypes binding to `libhackrf`.

**Device name:** `HackRF`
**Tunable range:** 1 MHz – 6 GHz
**Library:** `libhackrf` (loaded directly via ctypes; no Python wrapper dependency)

The driver used to import `pyhackrf` / `pyhackrf2`, but both packages hard-code the Linux
library filename (`libhackrf.so.0`) and fail to load on macOS where the file is
`libhackrf.dylib`.  Loading `libhackrf` directly via ctypes avoids the wrapper entirely and
works uniformly on both platforms with no environment variables or symlink workarounds.

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
| `b` | Toggle bias-tee (3.3 V antenna port power) on/off — shown as `[bias-tee:on/off]` in footer |
| `B` | Toggle ~14 dB RF pre-amplifier on/off — shown as `[amp:on/off]` in footer |

The bias-tee (antenna port power) is separate from the RF amp. Bias-tee feeds
3.3 V DC up the coax to power an external LNA or active antenna; the RF amp
is HackRF's own internal ~14 dB pre-amp between the RF frontend and the mixer.
Both survive bandwidth changes.

## Gain

HackRF has no hardware AGC. The `gain` property maps a single dB value onto two independent stages:

- **LNA** (RF front-end): 0–40 dB in 8 dB steps
- **VGA** (baseband): 0–62 dB in 2 dB steps

When set to `auto` (the `a` key), a mid-range preset of LNA 24 dB + VGA 30 dB is applied.

## Installation

```bash
# macOS
brew install hackrf                 # installs libhackrf + hackrf_info tools

# Debian / Ubuntu
sudo apt install libhackrf0 hackrf  # runtime library + tools

# Fedora
sudo dnf install hackrf hackrf-devel
```

No Python package is required — the driver loads `libhackrf` directly via ctypes.
Verify the library is discoverable with `hackrf_info` from the same shell before
running SDRTerm; if `hackrf_info` sees the device, the SDRTerm driver will too.
