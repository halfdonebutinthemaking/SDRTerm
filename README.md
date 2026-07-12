# RTL-SDR Spectrum Analyzer

A terminal-based RF spectrum analyzer for RTL-SDR dongles, written in Python.  
Displays a live dBFS spectrum in the terminal using curses, with interactive controls for frequency, bandwidth, and gain.

---

## Intention

The goal is a lightweight, dependency-minimal spectrum viewer that runs entirely in the terminal — no GUI, no browser, no heavy SDR framework.  
It reads raw IQ samples directly from the RTL-SDR hardware via `pyrtlsdr`, computes an averaged FFT, and renders a scrolling spectrum with a dB-scaled vertical axis, similar to the waterfall-less view in GQRX or SDR#.

---

## Requirements

- **macOS** (Apple Silicon, Homebrew) — the library path fix is Apple Silicon / Homebrew specific; Linux works without it
- **RTL-SDR dongle** connected via USB
- **librtlsdr** installed via Homebrew: `brew install librtlsdr`
- **Python 3.12+** managed via `uv`

---

## Installation

```bash
brew install librtlsdr
uv sync
python fix_venv.py      # apply compatibility patches (see below)
```

---

## Usage

```bash
uv run python main.py
```

### Keyboard controls

| Key | Action |
|-----|--------|
| `←` / `→` | Shift center frequency left / right by one FFT bin |
| `↑` / `↓` | Increase / decrease bandwidth (cycles through preset sample rates) |
| `F` | Enter frequency — type a value (`105.8M`, `433.5k`, `162000000`), `RET` to commit, `ESC` to cancel |
| `G` | Enter gain mode — `↑`/`↓` adjust gain ±0.5 dB, `G` again to exit |
| `A` | Toggle auto gain (hardware AGC) on/off |
| `I` | Toggle software IQ correction on/off |
| `Q` | Quit |

### Display layout

```
[Gain: 0.0 dB] 104.600 MHz       105.800 MHz       107.000 MHz
     |·········································································
   0 |████████████████████████████████████████████████████████████████
     |·····████████████████████████████████████████████████████████████
 -10 |···············████████████████████████████████
     |
 -20 |
     ...
-110 |·········································································
[IQ:off]                    BW 2.400 MHz  A=auto  G=gain  I=IQ  F=freq  Q=quit
```

- **Header**: current gain setting on the left, then the low / center / high frequencies of the visible window
- **Spectrum**: filled bar chart, one column per terminal column, peak-hold per column when multiple FFT bins map to the same column
- **dB axis**: tick marks every 10 dB; when in gain mode a `>` marker shows the current gain position on the axis
- **Footer**: IQ correction state on the left, controls on the right

### Bandwidth presets (Hz)

`250 000` · `1 024 000` · `1 400 000` · `1 800 000` · `2 048 000` · `2 400 000`

Starts at 2.4 MHz. Bandwidth = RTL-SDR sample rate, so it also determines the noise floor: narrower bandwidth → lower noise floor (fewer noise watts per bin).

### Gain

Starts at **0.0 dB manual** gain. Use `↑`/`↓` in gain mode to increase in 0.5 dB steps up to 49.6 dB.  
`A` enables hardware AGC (`[Gain: auto]`); pressing `A` again returns to the last manual value.

Auto gain is generally not recommended for spectrum analysis — the hardware AGC adjusts gain across the entire bandwidth in response to any strong signal, making the noise floor unstable and suppressing weak signals.

### IQ correction

Software-only, applied per frame before the FFT:

1. **DC offset removal** — subtracts the mean of the IQ samples, eliminating the centre-frequency spike caused by DC leakage in the ADC
2. **Amplitude balance** — scales the Q channel to match the I channel power, reducing mirror images
3. **Phase balance** — orthogonalises I and Q by removing the cross-correlation component, further reducing mirror images

---

## Implementation

### Signal processing

```
RTL-SDR IQ samples
  → reshape into N_AVG frames of FFT_BINS samples each
  → (optional) IQ correction per frame
  → Hann window × frame
  → FFT (FFT_BINS points) → fftshift
  → |FFT|² accumulated across N_AVG frames
  → 10·log10(mean power / FFT_BINS²)   [dBFS]
```

Constants:

| Name | Value | Purpose |
|------|-------|---------|
| `FFT_BINS` | 4096 | Bin count; larger = lower mean noise floor |
| `N_AVG` | 8 | Frames averaged per display update; reduces variance, not mean floor |
| `REFRESH_S` | 0.15 s | Target frame period (~7 fps) |
| `DB_MAX` / `DB_MIN` | 0 / −110 dBFS | Vertical axis range |

### Noise floor

The theoretical noise floor is approximately `10·log10(kTB)` referred to full scale, but in practice it scales with FFT bin width:

```
bin_width = sample_rate / FFT_BINS
```

Increasing `FFT_BINS` from 512 → 4096 lowers the mean floor by ~9 dB (`10·log10(4096/512)`).  
Reducing bandwidth (sample rate) lowers the floor further — this is correct behaviour, identical to reducing the RBW on a bench spectrum analyser.

### curses rendering

- `nodelay(True)` — non-blocking key reads so sampling is not blocked by waiting for input
- `erase()` before each frame instead of `clear()` — avoids flicker
- C-level stderr (fd 2) is redirected to `/dev/null` for the duration of the curses session; `librtlsdr` prints "Exact sample rate is: X Hz" to stderr on every sample-rate change, which would corrupt the terminal display

---

## Compatibility patches

### Background

`pyrtlsdr 0.5.x` was written against an **extended** `librtlsdr` fork that adds GPIO control and PLL dithering functions not present in the **official osmocom** build shipped by Homebrew (`librtlsdr 2.0.x`).

Without patches, importing `pyrtlsdr` fails at module load with:

```
AttributeError: dlsym(…, rtlsdr_set_dithering): symbol not found
```

And even after fixing the import, `RtlSdr.open()` calls `rtlsdr_set_dithering` at runtime, raising:

```
AttributeError: dlsym(…, rtlsdr_set_dithering): symbol not found
```

### What gets patched

`fix_venv.py` patches two files inside the project `.venv`:

**`.venv/…/rtlsdr/librtlsdr.py`** — module-level ctypes symbol bindings for seven missing functions are wrapped in `try/except AttributeError`:

```python
# before
f = librtlsdr.rtlsdr_set_dithering
f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]

# after
try:
    f = librtlsdr.rtlsdr_set_dithering
    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]
except AttributeError:
    pass
```

Functions patched: `rtlsdr_set_dithering`, `rtlsdr_set_gpio_input`, `rtlsdr_set_gpio_bit`, `rtlsdr_get_gpio_bit`, `rtlsdr_set_gpio_byte`, `rtlsdr_get_gpio_byte`, `rtlsdr_set_gpio_status`.

**`.venv/…/rtlsdr/rtlsdr.py`** — two runtime call sites inside `RtlSdr.open()` and `RtlSdr.set_dithering()` are guarded with `hasattr`:

```python
# before
result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))

# after
if hasattr(librtlsdr, 'rtlsdr_set_dithering'):
    result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))
```

### Applying / re-applying patches

```bash
python fix_venv.py
```

`fix_venv.py` is idempotent — it detects whether each patch is already applied and skips it. Re-run it any time after `uv sync --reinstall` overwrites the venv.

### Homebrew library path (Apple Silicon)

Homebrew on Apple Silicon installs to `/opt/homebrew/lib`, which is not in the default `dyld` search path.  
`main.py` sets the environment variable before `pyrtlsdr` triggers `dlopen()`:

```python
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')
```

This has no effect if the variable is already set in the shell environment.

---

## Project structure

```
main.py          — spectrum analyzer application
fix_venv.py      — re-applies venv compatibility patches after uv sync --reinstall
pyproject.toml   — project metadata and dependencies (pyrtlsdr, numpy)
uv.lock          — locked dependency versions
.venv/           — project-local virtual environment (managed by uv)
```
