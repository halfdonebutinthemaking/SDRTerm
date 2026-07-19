# RTL-SDR V3

Driver for the RTL-SDR V3 dongle via `pyrtlsdr` / `librtlsdr`.

**Device name:** `RTL-SDR-V3`  
**Tunable range:** 25 MHz – 1766 MHz  
**Library:** `pyrtlsdr` (wraps `librtlsdr`)

## Supported sample rates

| Rate | Note |
|------|------|
| 250 000 Hz | Minimum — narrowest noise floor |
| 1 024 000 Hz | |
| 1 400 000 Hz | |
| 1 800 000 Hz | |
| 2 048 000 Hz | |
| 2 400 000 Hz | Maximum stable rate on most hardware |

Values outside this set cause librtlsdr to silently round or produce spurious tones on some hardware.

## Controls

| Key | Action |
|-----|--------|
| `b` | Toggle bias-tee on/off (only shown when hardware supports it) |

## Gain

Manual gain range: 0.0 – 49.6 dB in 0.5 dB steps. Hardware AGC (`a`) is available but not recommended for spectrum analysis — it adjusts across the full bandwidth in response to any strong signal, making the noise floor unstable.

## Installation

```bash
brew install librtlsdr   # macOS / Homebrew
uv sync
python fix_venv.py       # patches pyrtlsdr for the osmocom librtlsdr build
```

See the compatibility patches section in the main README for details on why `fix_venv.py` is needed.
