> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# RTL-SDR V3

Driver for the RTL-SDR V3 dongle through `pyrtlsdr` / `librtlsdr`.

**Device name:** `RTL-SDR-V3`  
**Tunable range:** 25 MHz – 1766 MHz  
**Library:** `pyrtlsdr` (wraps `librtlsdr`)

## Supported sample rates

| Rate | Note |
|------|------|
| 250 000 Hz | Minimum — the most narrow noise floor |
| 1 024 000 Hz | |
| 1 400 000 Hz | |
| 1 800 000 Hz | |
| 2 048 000 Hz | |
| 2 400 000 Hz | The maximum stable rate on most hardware |

Values outside this set make librtlsdr round the value without a message. On some hardware they can also cause false tones.

## Controls

| Key | Action |
|-----|--------|
| `b` | Turn the bias-tee on or off (shown only when the hardware supports it) |

## Gain

Manual gain range: 0.0 – 49.6 dB in 0.5 dB steps. The hardware AGC (`a`) is available, but we do not recommend it for spectrum analysis. It changes across the full bandwidth when it sees a strong signal. This makes the noise floor unstable.

## Installation

```bash
brew install librtlsdr   # macOS / Homebrew
uv sync
python fix_venv.py       # patches pyrtlsdr for the osmocom librtlsdr build
```

See the compatibility patches section in the main README for the reason why `fix_venv.py` is needed.
