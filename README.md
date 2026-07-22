# SDRTerm

![SDRTerm live spectrum](images/running.gif)

Most SDR tools are GUI applications that require a display server, or narrow command-line tools that do one thing without interactivity. SDRTerm fills the gap: a live RF spectrum analyser that runs entirely in the terminal, with real-time controls and a plugin pipeline for signal decoding.

No display server. No framework. SSH into a headless box, connect a supported device, and see the spectrum.

**What it does:**
- Live dBFS spectrum and waterfall, rendered in the terminal with curses
- Interactive frequency, bandwidth, and gain control
- Plugin decoders: FM audio, RDS, NRSC-5 HD Radio, peak tracking with Doppler follow, frequency range scan
- IQ file replay (raw `.iq`, WAV, SigMF) — analyse recordings without hardware
- Pluggable hardware drivers — one file per device

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

### Command-line parameters

| Flag | Argument | Description |
|------|----------|-------------|
| `--d` | `NAME` | Open a specific device by name (e.g. `RTL-SDR-V3`). Falls back to auto-detect if omitted. |
| `--file` | `PATH` | Replay a `.iq` (raw complex64), stereo `.wav`, or SigMF (`.sigmf-data` / `.sigmf`) IQ file instead of opening hardware. Selects the `localfile` device automatically. WAV and SigMF sample rates and centre frequency are read from the file metadata. |
| `--bw` | `BW` | Set the initial capture bandwidth / sample rate (e.g. `2.4M`, `1024k`, `250000`). For `.iq` files: must match the rate the file was recorded at. For `.wav` files: overrides the rate from the file header. |
| `--f` | `FREQ` | Set the initial center frequency. Accepts `105.8M`, `433.5k`, or a raw Hz value. |
| `--g` | `GAIN` | Set the initial gain in dB (e.g. `32.8`). Ignored if `--i on` is also set. |
| `--i` | `on\|off` | Enable (`on`) or disable (`off`) hardware AGC at startup. |
| `--preset` | `FILE` | Load a `.sdrterm` preset file at startup (overrides all other settings). |

Examples:

```bash
# Live hardware
uv run python main.py --d RTL-SDR-V3 --f 105.8M --g 28.0

# Replay a recorded IQ file
uv run python main.py --file recording.iq --bw 2.4M --f 105.8M
```

---

## Display

![SDRTerm spectrum](images/running.gif)

The core tab shows the full-bandwidth spectrum.  
The header displays the low / center / high frequencies of the visible window.  
The footer shows the active tab name, device status (bandwidth, bias-tee state), IQ correction state, and all available shortcuts.

Press `v` to switch between **spectrum** (bar chart) and **waterfall** (scrolling time-frequency) views. The waterfall fills from the top with the newest frame; older frames scroll downward. Signal strength is encoded in block characters (`░▒▓█`). Plugin overlays (such as the FM channel highlight) apply in both views.

![Waterfall view](images/waterfall.gif)

---

## Keyboard controls

### Always available (all tabs)

| Key | Action |
|-----|--------|
| `f` | Enter frequency — type a value (`105.8M`, `433.5k`, `162000000`), `ret` to commit, `esc` to cancel |
| `tab` | Cycle through core tab and active plugin tabs |
| `q` | Quit |

### Core tab

| Key | Action |
|-----|--------|
| `←` / `→` | Shift center frequency by one display column (≈ bandwidth ÷ terminal width) |
| `,` / `.` | Shift center frequency by one FFT bin (finest step) |
| `↑` / `↓` | Increase / decrease bandwidth (steps through the current device's supported rates) |
| `a` | Toggle hardware AGC on/off |
| `g` | Enter gain mode — `↑`/`↓` adjust gain ±0.5 dB, `g` again to exit |
| `i` | Toggle software IQ correction on/off |
| `v` | Toggle between spectrum (bar chart) and waterfall (scrolling time-frequency) views |
| `p` | Open plugin menu — `↑`/`↓` navigate, `space` stage toggle, `<`/`>` reorder pipeline, `ret` apply, `esc` cancel |
| `b` | Device-specific toggle — bias-tee on RTL-SDR V3, RF amplifier on HackRF (shown in footer) |

### Plugin tabs (all plugins)

Navigate to a plugin tab with `tab`. Two keys are available on every plugin tab:

| Key | Action |
|-----|--------|
| `x` | Disable this plugin and return to the core tab |
| `d` | Open the debug console for this plugin (scroll with `↑`/`↓`/`PgUp`/`PgDn`, `esc` to close) |

---

## Plugins

| Plugin | Description | Docs |
|--------|-------------|------|
| `spectrum` | Always-on FFT display and waterfall | [spectrum.md](plugins/spectrum.md) |
| `fm` | FM broadcast audio decoder with channel-bandwidth highlight | [fm.md](plugins/fm.md) |
| `rds` | RDS decoder — PS name, RadioText, PTY, PI code, TP/TA | [rds.md](plugins/rds.md) |
| `nrsc5_text` | NRSC-5 HD Radio decoder (pure Python, CFO correction, Viterbi) | [nrsc5_text.md](plugins/nrsc5_text.md) |
| `peak_marker` | Peak-frequency marker with hold-off and alpha-beta Doppler tracking | [peak_marker.md](plugins/peak_marker.md) |
| `record` | Write signal to file (WAV audio or raw IQ/SigMF) | [record.md](plugins/record.md) |
| `rtl-tcp-passive` | RTL-TCP server — stream IQ to clients, ignore commands | [rtltcp_passive.md](plugins/rtltcp_passive.md) |
| `rtl-tcp-active` | RTL-TCP server — stream IQ and apply client commands to hardware | [rtltcp_active.md](plugins/rtltcp_active.md) |
| `range-scan` | Stepped frequency scan with signal detection list | [range_scan.md](plugins/range_scan.md) |

---

## Bandwidth

Bandwidth equals the IQ sample rate delivered by the device. Narrower bandwidth → lower noise floor (fewer noise watts per bin).

Each device declares the bandwidths it supports via `supported_bandwidths`. The `↑`/`↓` keys step through that list only — they never request a rate the device cannot handle.

**RTL-SDR V3 supported rates:** `250 000` · `1 024 000` · `1 400 000` · `1 800 000` · `2 048 000` · `2 400 000` Hz  
**HackRF One supported rates:** `2` · `4` · `6` · `8` · `10` · `12.5` · `16` · `20` MHz  
**localfile device:** all of the above (software device, accepts any step).

Plugins declare `min_sample_rate`; enabling a plugin raises the bandwidth if necessary, but never lowers it below the current user setting.

---

## Plugin architecture

Plugins live in `plugins/`. Each file that contains a `Decoder` subclass with a non-empty `name` is discovered and loaded automatically at startup — no registration required.

```
plugins/
  spectrum.py        — always-on FFT display (built-in, key-less)
  fm.py              — FM broadcast audio decoder
  rds.py             — RDS (Radio Data System) decoder
  nrsc5_text.py      — NRSC-5 HD Radio decoder (digital sideband, pure Python)
  peak_marker.py     — peak-frequency marker with hold-off and Doppler tracking
  record.py          — write signal to file (WAV or raw IQ)
  rtltcp_passive.py  — RTL-TCP server, streams IQ to clients (read-only)
  rtltcp_active.py   — RTL-TCP server, applies client frequency/gain/rate commands
  range_scan.py      — stepped frequency scan with signal detection list
  __init__.py        — auto-discovery loader
```

### Plugin pipeline

Active plugins run in the order shown in the plugin menu (filename order by default). Each plugin's `process()` receives the accumulated results of all plugins that ran before it — earlier plugins' output is visible to later ones via the `results` dict.

**Pipeline order matters.** The `record` plugin captures the output of its **immediate predecessor** in the pipeline: if FM precedes record, audio is saved as WAV; if record is first (or has no predecessor), raw IQ is written instead.

#### Reordering the pipeline

Open the plugin menu with `p`. While the menu is open:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the cursor to a different plugin |
| `space` | Stage or unstage the highlighted plugin (toggle without applying) |
| `<` | Move the highlighted plugin one position earlier in the pipeline |
| `>` | Move the highlighted plugin one position later in the pipeline |
| `ret` | Apply all staged changes and close the menu |
| `esc` | Cancel — discard staged changes and restore the previous order |

The order shown in the menu is the execution order. Reordering is applied atomically when you press `ret`, so you can freely rearrange and toggle multiple plugins before committing.

A practical example: to record FM audio, open the menu, enable FM and record, and ensure FM appears **above** (before) record. If record is above FM, it sees raw IQ instead of decoded audio.

### Writing a plugin

Subclass `Decoder` from `core.py` and place the file in `plugins/`:

```python
from core import Decoder, AppState

class MyDecoder(Decoder):
    name            = 'mymode'          # unique ID
    key             = 'y'               # any non-empty string; presence includes the plugin in the plugin menu
    key_help        = 'o=path'          # tab-specific shortcut hints shown in footer
    min_sample_rate = 250_000           # minimum BW this decoder needs

    def start(self, state: AppState) -> None:   ...
    def process(self, samples, state, results=None, sdr=None): return {}
    def stop(self) -> None:                     ...

    # optional hooks
    def handle_key(self, key, state, sdr) -> bool: ...
    def status_text(self, state, result) -> str:   ...
    def draw_overlay(self, screen_obj, state, result,
                     freq_min, freq_range, plot_w, height): ...

    # preset persistence — implement to survive save/load cycles
    def save_state(self) -> dict:        return {}
    def load_state(self, d: dict) -> None: ...
```

#### Preset persistence

Implement `save_state()` and `load_state()` to let the preset system serialise your plugin's configuration. Both are called automatically when the user saves or loads a preset — no wiring in `main.py` required.

```python
class MyDecoder(Decoder):
    def __init__(self):
        self._threshold = 10.0

    def save_state(self) -> dict:
        return {'threshold': self._threshold}

    def load_state(self, d: dict) -> None:
        self._threshold = d.get('threshold', 10.0)
```

The saved dict is stored under `plugin_states.<name>` in the `.sdrterm` JSON file.

#### Text input from the user

To prompt the user for a free-form string (path, frequency, port number), set `state.path_input` to the initial value and register a callback on `state.path_input_cb`. The framework displays the prompt in the footer and calls the callback with the entered string when the user presses `ret`:

```python
def handle_key(self, key, state, sdr) -> bool:
    if key == ord('o'):
        state.path_input       = self._path or ''
        state.path_input_label = 'Output path'
        plugin = self
        state.path_input_cb    = lambda val: plugin._set_path(val or None)
        return True
    return False

def _set_path(self, path):
    self._path = path
```

The label shown before the input cursor is controlled by `state.path_input_label` (default `'Path'`). The framework resets both `path_input` and `path_input_cb` to `None` after the callback runs.

#### Drawing overlays on the core view

`draw_overlay()` is called after every frame (spectrum or waterfall) with a reference to the live curses window. The plugin can paint anything on top of the body rows using `chgat()` to change the color attribute of already-drawn characters:

```python
import curses
from core import LABEL_W

class MyDecoder(Decoder):
    def draw_overlay(self, screen_obj, state, result,
                     freq_min, freq_range, plot_w, height):
        span_l = state.center_hz - 50_000
        span_r = state.center_hz + 50_000
        col_l  = int(max(0,      (span_l - freq_min) / freq_range * plot_w))
        col_r  = int(min(plot_w, (span_r - freq_min) / freq_range * plot_w))
        if col_r <= col_l or not curses.has_colors():
            return
        n = col_r - col_l
        for r in range(height):
            try:
                screen_obj.chgat(r + 1, LABEL_W + col_l, n, curses.color_pair(1))
            except curses.error:
                pass
```

`chgat(y, x, n, attr)` recolors `n` characters at `(y, x)` in place without touching character content, so it works identically in both spectrum and waterfall modes. Color pair 1 is cyan (initialized by the framework at startup).

#### Full-view plugins

Set `full_view = True` to take over the entire terminal body. When the plugin's tab is active, the framework skips spectrum/waterfall rendering entirely and calls `draw_full()` instead. The framework still draws the footer row and handles all modals (frequency entry, path input, flash messages).

```python
import curses

class MyDecoder(Decoder):
    full_view = True

    def draw_full(self, screen_obj, state: AppState, result: dict,
                  rows: int, cols: int) -> None:
        # rows and cols are the full terminal dimensions.
        # The footer occupies row rows-1 — draw content into rows 0..rows-2.
        # The framework calls screen_obj.erase() before this method.
        try:
            screen_obj.addstr(0, 0, 'My plugin view', curses.A_BOLD)
            for i, item in enumerate(result.get('items', [])):
                if 1 + i >= rows - 1:
                    break
                screen_obj.addstr(1 + i, 0, str(item)[:cols - 1])
        except curses.error:
            pass
```

Key differences from overlay plugins:

| | Overlay (`draw_overlay`) | Full-view (`draw_full`) |
|-|--------------------------|------------------------|
| When called | After every spectrum/waterfall frame | Instead of spectrum/waterfall, on the active plugin tab only |
| What it renders | Decorations on top of the existing body | The entire screen body |
| Spectrum still drawn | Yes | No |
| Footer | Drawn by framework | Drawn by framework |
| `realtime` setting | Usually `True` | Usually `False` (background worker) |

See `range_scan.py` for a complete example.

### Making a plugin recordable

Implement the recording hooks so the `record` plugin can capture this plugin's output:

```python
class MyDecoder(Decoder):
    record_ext = 'myext'     # file extension; None = not recordable (default)

    def record_open(self, path: str):
        return open(path, 'wb')

    def record_write(self, handle, result: dict) -> int:
        data = result.get('mydata')
        handle.write(data)
        return len(data)

    def record_close(self, handle) -> None:
        handle.close()
```

---

## Device architecture

Hardware drivers live in `devices/`. Each file that contains a `Device` subclass with a non-empty `name` is discovered automatically.

| Device | Description | Docs |
|--------|-------------|------|
| `RTL-SDR-V3` | RTL-SDR V3 dongle — 25 MHz–1766 MHz, up to 2.4 MSPS | [rtlsdr_v3.md](devices/rtlsdr_v3.md) |
| `HackRF` | HackRF One — 1 MHz–6 GHz, up to 20 MSPS | [hackrf.md](devices/hackrf.md) |
| `localfile` | IQ file replay — raw `.iq`, WAV, SigMF | [localfile.md](devices/localfile.md) |

The application tries each discovered device in filename order and opens the first one that succeeds. `--d NAME` selects a specific driver by name (case-insensitive). `--file PATH` selects the `localfile` device directly.

See [localfile.md](devices/localfile.md) for supported formats, filename frequency parsing, and usage examples.

### Writing a device driver

Subclass `Device` from `core.py` and place the file in `devices/`:

```python
from core import Device, AppState

class MyDevice(Device):
    name                 = 'MY-DEVICE'              # unique ID matched by --d
    key_help             = 'x=feature'              # shortcut hint in core footer
    supported_bandwidths = [1_000_000, 2_000_000]   # Hz, ascending order

    def open(self) -> bool:   ...   # return False if hardware unavailable
    def close(self) -> None:  ...

    @property
    def sample_rate(self): ...
    @sample_rate.setter
    def sample_rate(self, v): ...

    @property
    def center_freq(self): ...
    @center_freq.setter
    def center_freq(self, v): ...

    @property
    def gain(self): ...
    @gain.setter
    def gain(self, v): ...

    def read_samples_async(self, callback, num_samples): ...
    def cancel_read_async(self): ...

    # optional UI hooks (core tab only)
    def handle_key(self, key, state: AppState) -> bool: ...
    def status_text(self, state: AppState) -> str: ...
```

`supported_bandwidths` is the only list the application consults for `↑`/`↓` BW stepping — it is not required to match the global `BW_STEPS` constant and can contain any values the hardware supports.

---

## Gain

Starts at **0.0 dB manual** gain. Use `↑`/`↓` in gain mode (`g`) to step in 0.5 dB increments up to 49.6 dB.  
`a` enables hardware AGC (`[Gain: auto]`); pressing `a` again returns to the last manual value.

Auto gain is generally not recommended for spectrum analysis — the hardware AGC adjusts across the entire bandwidth in response to any strong signal, making the noise floor unstable and suppressing weak signals.

---

## IQ correction

Software-only, applied per frame before the FFT:

1. **DC offset removal** — subtracts the mean of the IQ samples, eliminating the centre-frequency spike caused by DC leakage in the ADC
2. **Amplitude balance** — scales the Q channel to match the I channel power, reducing mirror images
3. **Phase balance** — orthogonalises I and Q by removing the cross-correlation component, further reducing mirror images

---

## Implementation

### curses rendering

- `nodelay(True)` — non-blocking key reads so sampling is not blocked by input
- `erase()` before each frame instead of `clear()` — avoids flicker
- C-level stderr (fd 2) is redirected to `/dev/null` during the curses session; `librtlsdr` prints messages to stderr on every sample-rate change, which would corrupt the display

---

## Compatibility patches

### Background

`pyrtlsdr 0.5.x` was written against an extended `librtlsdr` fork that adds GPIO and PLL dithering functions not present in the official osmocom build shipped by Homebrew (`librtlsdr 2.0.x`).

Without patches, importing `pyrtlsdr` fails at module load:

```
AttributeError: dlsym(…, rtlsdr_set_dithering): symbol not found
```

### What gets patched

`fix_venv.py` patches two files inside the project `.venv`:

**`.venv/…/rtlsdr/librtlsdr.py`** — seven missing ctypes symbol bindings wrapped in `try/except AttributeError`:

```python
try:
    f = librtlsdr.rtlsdr_set_dithering
    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]
except AttributeError:
    pass
```

Functions patched: `rtlsdr_set_dithering`, `rtlsdr_set_gpio_input`, `rtlsdr_set_gpio_bit`, `rtlsdr_get_gpio_bit`, `rtlsdr_set_gpio_byte`, `rtlsdr_get_gpio_byte`, `rtlsdr_set_gpio_status`.

**`.venv/…/rtlsdr/rtlsdr.py`** — two runtime call sites inside `RtlSdr.open()` guarded with `hasattr`:

```python
if hasattr(librtlsdr, 'rtlsdr_set_dithering'):
    result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))
```

### Applying / re-applying patches

```bash
python fix_venv.py
```

`fix_venv.py` is idempotent — it detects whether each patch is already applied and skips it. Re-run after `uv sync --reinstall`.

### Homebrew library path (Apple Silicon)

Homebrew on Apple Silicon installs to `/opt/homebrew/lib`, which is not in the default `dyld` search path.  
`main.py` sets the environment variable before `pyrtlsdr` triggers `dlopen()`:

```python
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')
```

---

## Troubleshooting

### Terminal shows stair-step output after quitting SDRTerm

If after quitting the app your shell shows `ls` or other commands with each
new line stepping to the right instead of returning to column 0 — for example:

```
LICENSE             __pycache__         data                fix_venv.py
                                                                                    README.md           core.py
                                                                                                                    devices/
```

then the terminal's `ONLCR` output flag has been left off. This means `\n`
moves the cursor down without also returning to column 0. curses turns
`ONLCR` off during normal operation and restores it on exit, but the
restore can fail if a plugin's `stop()` writes to the terminal while
curses is still active, or if a daemon worker thread interferes with
`endwin()` at shutdown.

**Fix in the current shell**: run `stty sane`. This restores the terminal
to POSIX defaults (idempotent — safe to run any time). Alternatively,
open a new shell.

**Prevention**: SDRTerm registers an `atexit` handler in `main.py` that
runs `stty sane` on every exit path (normal quit, uncaught exception,
Ctrl+C, `sys.exit`). If you still see the stair-step after quitting the
app, either that handler did not run — which happens with `os._exit()`
or a hard interpreter crash — or another program left the terminal in
that state. `stty sane` always fixes it manually.

### Signal is inaudible or shows nothing

Common causes, in order of likelihood:
- **Gain too low**: press `g` for the gain modal and increase manually.
  With a small antenna and no LNA, VHF/UHF signals often need 40 dB+.
- **Wrong tune frequency**: use `range-scan` across the expected band to
  find the real carrier before you tune manually.
- **Bandwidth too small**: wideband signals (VDL2, POCSAG) need at least
  250 kHz. Check the status bar.
- **Antenna mismatch**: a small VHF antenna does not receive UHF well
  and vice versa.

### `librtlsdr` errors on start (macOS)

If pyrtlsdr fails with "librtlsdr not found":
- Make sure Homebrew's `librtlsdr` is installed: `brew install librtlsdr`
- On Apple Silicon, `main.py` sets `DYLD_LIBRARY_PATH=/opt/homebrew/lib`
  before the import. If you get the error anyway, check that `librtlsdr`
  is installed at that path with `ls /opt/homebrew/lib/librtlsdr*`.

---

## Project structure

```
main.py                — CLI argument parsing and curses main loop
core.py                — shared constants, AppState, Decoder/Device base classes
keys.py                — keyboard handler (handle_keys state machine)
presets.py             — preset save / load / migrate logic
render.py              — curses rendering (draw, overlays, menus)
fix_venv.py            — re-applies venv compatibility patches after uv sync --reinstall
pyproject.toml         — project metadata and dependencies
uv.lock                — locked dependency versions

presets/               — saved .sdrterm preset files (auto-created on first save)
samples/               — recorded IQ / SigMF files (auto-created on first recording)

scripts/
  gen_doppler_test.py  — generates a synthetic LEO Doppler SigMF test file (±20 kHz, 10 s)
  diag_nrsc5.py        — standalone NRSC-5 diagnostic script (CFO, sync, Viterbi pipeline)

plugins/
  __init__.py          — auto-discovery loader
  spectrum.py          — always-on FFT spectrum decoder
  spectrum.md          — spectrum plugin documentation
  fm.py                — FM broadcast audio decoder (with WAV recording hooks)
  fm.md                — FM plugin documentation
  rds.py               — RDS decoder: PS name, RadioText, PTY, PI code, TP/TA flags
  rds.md               — RDS plugin documentation
  nrsc5_text.py        — NRSC-5 HD Radio decoder (pure Python, CFO correction, Viterbi)
  nrsc5_text.md        — NRSC-5 plugin documentation
  peak_marker.py       — peak-frequency marker with hold-off and Doppler tracking
  peak_marker.md       — peak marker plugin documentation
  record.py            — write signal to file via predecessor plugin's recording hooks
  record.md            — record plugin documentation
  rtltcp_passive.py    — RTL-TCP server: stream IQ to clients, ignore commands
  rtltcp_passive.md    — RTL-TCP passive server documentation
  rtltcp_active.py     — RTL-TCP server: stream IQ and apply client commands to hardware
  rtltcp_active.md     — RTL-TCP active server documentation
  range_scan.py        — stepped frequency scan with signal detection list
  range_scan.md        — range-scan plugin documentation
  images/
    02_plugin_fm.png   — FM plugin tab screenshot
    05_range-scan.png  — range-scan plugin view
    06_rds.png         — RDS plugin tab screenshot
    07_peak-marker.png — peak marker plugin tab screenshot
    08_nrsc.png        — NRSC-5 plugin tab screenshot
    range.gif          — range-scan in action
    peak.gif           — peak marker in action

devices/
  __init__.py          — auto-discovery loader
  rtlsdr_v3.py         — RTL-SDR V3 driver (pyrtlsdr)
  rtlsdr_v3.md         — RTL-SDR V3 device documentation
  hackrf.py            — HackRF One driver (pyhackrf / libhackrf)
  hackrf.md            — HackRF One device documentation
  localfile.py         — IQ file replay device (raw complex64, memory-mapped)
  localfile.md         — localfile device documentation

images/
  running.gif          — live spectrum animation
  waterfall.gif        — waterfall view animation
  01_main.png          — core tab screenshot (static)
  03_waterfall.png     — waterfall view screenshot (static)
```
