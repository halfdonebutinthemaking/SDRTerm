> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# SDRTerm

![SDRTerm live spectrum](images/running.gif)

Most SDR tools are GUI applications that need a display server. Other SDR tools are narrow command-line programs that do one thing and give no interactive control. SDRTerm fills the gap. It is a live RF spectrum analyser that runs fully in the terminal. It gives you real-time controls and a plugin pipeline for signal decoding.

No display server is needed. No framework is needed. Open an SSH session to a headless computer. Connect a supported device. Then look at the spectrum.

**What it does:**
- Shows the live dBFS spectrum and waterfall in the terminal with curses
- Gives interactive frequency, bandwidth, and gain control
- Has plugin decoders: FM audio, RDS, NRSC-5 HD Radio, peak tracking with Doppler follow, and frequency range scan
- Plays back IQ files (raw `.iq`, WAV, SigMF), so you can look at recordings without hardware
- Uses pluggable hardware drivers, with one file for each device

---

## Requirements

- **macOS** (Apple Silicon, Homebrew). The library path fix is only for Apple Silicon with Homebrew. Linux works without it.
- **RTL-SDR dongle** connected through USB
- **librtlsdr** installed with Homebrew: `brew install librtlsdr`
- **Python 3.12+** managed with `uv`

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
| `--d` | `NAME` | Open a specific device by name (for example, `RTL-SDR-V3`). If you do not give a name, the program finds a device by itself. |
| `--file` | `PATH` | Play back a `.iq` (raw complex64), stereo `.wav`, or SigMF (`.sigmf-data` / `.sigmf`) IQ file in place of opening hardware. This selects the `localfile` device by itself. For WAV and SigMF files, the program reads the sample rate and centre frequency from the file metadata. |
| `--bw` | `BW` | Set the first capture bandwidth or sample rate (for example, `2.4M`, `1024k`, `250000`). For `.iq` files, the value must match the rate used to record the file. For `.wav` files, this value replaces the rate in the file header. |
| `--f` | `FREQ` | Set the first center frequency. You can give `105.8M`, `433.5k`, or a raw Hz value. |
| `--g` | `GAIN` | Set the first gain in dB (for example, `32.8`). The program does not use this value if you also set `--i on`. |
| `--i` | `on\|off` | Turn hardware AGC on (`on`) or off (`off`) at startup. |
| `--preset` | `FILE` | Load a `.sdrterm` preset file at startup. This replaces all other settings. |

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
The header shows the low, center, and high frequencies of the visible window.  
The footer shows the active tab name, the device status (bandwidth, bias-tee state), the IQ correction state, and all shortcuts you can use.

Press `v` to change between the **spectrum** view (bar chart) and the **waterfall** view (scrolling time-frequency). The waterfall fills from the top with the newest frame. Older frames move down. Block characters (`░▒▓█`) show signal strength. Plugin overlays (such as the FM channel highlight) work in both views.

![Waterfall view](images/waterfall.gif)

---

## Keyboard controls

### Always available (all tabs)

| Key | Action |
|-----|--------|
| `f` | Enter a frequency. Type a value (`105.8M`, `433.5k`, `162000000`). Press `ret` to commit. Press `esc` to cancel. |
| `tab` | Move through the core tab and the active plugin tabs |
| `q` | Quit |

### Core tab

| Key | Action |
|-----|--------|
| `←` / `→` | Shift the center frequency by one display column (about bandwidth divided by terminal width) |
| `,` / `.` | Shift the center frequency by one FFT bin (the smallest step) |
| `↑` / `↓` | Increase or decrease the bandwidth (steps through the rates the current device supports) |
| `a` | Turn hardware AGC on or off |
| `g` | Enter gain mode. `↑`/`↓` change the gain by 0.5 dB. Press `g` again to exit. |
| `i` | Turn software IQ correction on or off |
| `v` | Change between the spectrum view (bar chart) and the waterfall view (scrolling time-frequency) |
| `p` | Open the plugin menu. `↑`/`↓` move the cursor. `space` toggles the stage state. `<`/`>` reorders the pipeline. `ret` applies changes. `esc` cancels. |
| `b` | Device-specific toggle. This is bias-tee on RTL-SDR V3 and RF amplifier on HackRF. The footer shows the state. |

### Plugin tabs (all plugins)

Go to a plugin tab with `tab`. Two keys are available on every plugin tab:

| Key | Action |
|-----|--------|
| `x` | Turn off this plugin and go back to the core tab |
| `d` | Open the debug console for this plugin. Scroll with `↑`/`↓`/`PgUp`/`PgDn`. Press `esc` to close. |

---

## Plugins

| Plugin | Description | Docs |
|--------|-------------|------|
| `spectrum` | Always-on FFT display and waterfall | [spectrum.md](plugins/spectrum.md) |
| `fm` | FM broadcast audio decoder with a channel-bandwidth highlight | [fm.md](plugins/fm.md) |
| `rds` | RDS decoder for PS name, RadioText, PTY, PI code, and TP/TA | [rds.md](plugins/rds.md) |
| `nrsc5_text` | NRSC-5 HD Radio decoder (pure Python, CFO correction, Viterbi) | [nrsc5_text.md](plugins/nrsc5_text.md) |
| `peak_marker` | Peak-frequency marker with hold-off and alpha-beta Doppler tracking | [peak_marker.md](plugins/peak_marker.md) |
| `record` | Writes the signal to a file (WAV audio or raw IQ/SigMF) | [record.md](plugins/record.md) |
| `rtl-tcp-passive` | RTL-TCP server. Streams IQ to clients. Ignores commands. | [rtltcp_passive.md](plugins/rtltcp_passive.md) |
| `rtl-tcp-active` | RTL-TCP server. Streams IQ and applies client commands to hardware. | [rtltcp_active.md](plugins/rtltcp_active.md) |
| `range-scan` | Stepped frequency scan with a signal detection list | [range_scan.md](plugins/range_scan.md) |

---

## Bandwidth

Bandwidth is equal to the IQ sample rate that the device gives you. A more narrow bandwidth gives a lower noise floor (fewer noise watts for each bin).

Each device gives the bandwidths it supports in `supported_bandwidths`. The `↑`/`↓` keys step only through that list. They never ask for a rate the device cannot use.

**RTL-SDR V3 supported rates:** `250 000` · `1 024 000` · `1 400 000` · `1 800 000` · `2 048 000` · `2 400 000` Hz  
**HackRF One supported rates:** `2` · `4` · `6` · `8` · `10` · `12.5` · `16` · `20` MHz  
**localfile device:** all of the above. It is a software device and accepts any step.

Plugins give a `min_sample_rate` value. When you turn on a plugin, the bandwidth goes up if it must. It never goes below the current user setting.

---

## Plugin architecture

Plugins are in `plugins/`. Each file that contains a `Decoder` subclass with a `name` value that is not empty is found and loaded by itself at startup. No registration is needed.

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

Active plugins run in the order shown in the plugin menu. The default order is by filename. The `process()` function of each plugin gets the results of all the plugins that ran before it. Earlier plugin output is visible to later plugins through the `results` dict.

**The pipeline order is important.** The `record` plugin captures the output of the plugin **directly before it** in the pipeline. If FM is before record, the audio is saved as WAV. If record is first (or has no plugin before it), raw IQ is written in place of audio.

#### Change the pipeline order

Open the plugin menu with `p`. While the menu is open:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the cursor to a different plugin |
| `space` | Stage or unstage the highlighted plugin (toggle without applying) |
| `<` | Move the highlighted plugin one position earlier in the pipeline |
| `>` | Move the highlighted plugin one position later in the pipeline |
| `ret` | Apply all staged changes and close the menu |
| `esc` | Cancel. Discard the staged changes and go back to the previous order. |

The order shown in the menu is the execution order. The reorder is applied in one step when you press `ret`. This lets you freely rearrange and toggle many plugins before you commit.

An example: to record FM audio, open the menu, turn on FM and record, and make sure FM is **above** (before) record. If record is above FM, it sees raw IQ in place of the decoded audio.

### Write a plugin

Subclass `Decoder` from `core.py` and put the file in `plugins/`:

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

Add `save_state()` and `load_state()` to let the preset system save your plugin's configuration. Both are called by the program when the user saves or loads a preset. No wiring in `main.py` is needed.

```python
class MyDecoder(Decoder):
    def __init__(self):
        self._threshold = 10.0

    def save_state(self) -> dict:
        return {'threshold': self._threshold}

    def load_state(self, d: dict) -> None:
        self._threshold = d.get('threshold', 10.0)
```

The saved dict is kept under `plugin_states.<name>` in the `.sdrterm` JSON file.

#### Text input from the user

To ask the user for a free-form string (path, frequency, port number), set `state.path_input` to the first value. Then register a callback on `state.path_input_cb`. The framework shows the prompt in the footer. It then calls the callback with the string the user typed when the user presses `ret`:

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

The label shown before the input cursor is set by `state.path_input_label` (default is `'Path'`). The framework resets both `path_input` and `path_input_cb` to `None` after the callback runs.

#### Draw overlays on the core view

The framework calls `draw_overlay()` after every frame (spectrum or waterfall). It gives a reference to the live curses window. The plugin can paint anything on top of the body rows. Use `chgat()` to change the color attribute of characters already drawn:

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

`chgat(y, x, n, attr)` recolors `n` characters at `(y, x)` in place. It does not touch the character content. It works the same in both spectrum and waterfall modes. Color pair 1 is cyan. The framework sets it up at startup.

#### Full-view plugins

Set `full_view = True` to take over the full terminal body. When the tab of the plugin is active, the framework skips the spectrum and waterfall rendering fully and calls `draw_full()` in place. The framework still draws the footer row and handles all modals (frequency entry, path input, flash messages).

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
| When called | After every spectrum/waterfall frame | In place of spectrum/waterfall, on the active plugin tab only |
| What it renders | Decorations on top of the existing body | The full screen body |
| Spectrum still drawn | Yes | No |
| Footer | Drawn by framework | Drawn by framework |
| `realtime` setting | Usually `True` | Usually `False` (background worker) |

See `range_scan.py` for a full example.

### Make a plugin recordable

Add the recording hooks so the `record` plugin can capture the output of this plugin:

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

Hardware drivers are in `devices/`. Each file that contains a `Device` subclass with a `name` value that is not empty is found by itself.

| Device | Description | Docs |
|--------|-------------|------|
| `RTL-SDR-V3` | RTL-SDR V3 dongle. 25 MHz to 1766 MHz, up to 2.4 MSPS. | [rtlsdr_v3.md](devices/rtlsdr_v3.md) |
| `HackRF` | HackRF One. 1 MHz to 6 GHz, up to 20 MSPS. | [hackrf.md](devices/hackrf.md) |
| `localfile` | IQ file replay. Raw `.iq`, WAV, SigMF. | [localfile.md](devices/localfile.md) |

The application tries each device found in filename order. It opens the first one that works. `--d NAME` selects a specific driver by name (case is not important). `--file PATH` selects the `localfile` device directly.

See [localfile.md](devices/localfile.md) for the supported formats, filename frequency parsing, and usage examples.

### Write a device driver

Subclass `Device` from `core.py` and put the file in `devices/`:

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

`supported_bandwidths` is the only list the application uses for the `↑`/`↓` BW step. It does not need to match the global `BW_STEPS` constant. It can hold any values the hardware supports.

---

## Gain

The gain starts at **0.0 dB manual** gain. Use `↑`/`↓` in gain mode (`g`) to step in 0.5 dB steps up to 49.6 dB.  
`a` turns on hardware AGC (`[Gain: auto]`). Press `a` again to go back to the last manual value.

Auto gain is not a good choice for spectrum analysis in most cases. The hardware AGC changes the gain across the full bandwidth in response to any strong signal. This makes the noise floor unstable and hides weak signals.

---

## IQ correction

The correction is software-only. It is applied for each frame before the FFT:

1. **DC offset removal** removes the mean of the IQ samples. This removes the centre-frequency spike caused by DC leakage in the ADC.
2. **Amplitude balance** scales the Q channel to match the I channel power. This cuts down mirror images.
3. **Phase balance** makes I and Q orthogonal by removing the cross-correlation component. This cuts down mirror images more.

---

## Implementation

### curses rendering

- `nodelay(True)` is a non-blocking key read. Sampling is not blocked by input.
- `erase()` runs before each frame in place of `clear()`. This stops flicker.
- The C-level stderr (fd 2) is sent to `/dev/null` during the curses session. `librtlsdr` prints messages to stderr on every sample-rate change. These messages would corrupt the display.

---

## Compatibility patches

### Background

`pyrtlsdr 0.5.x` was written for an extended `librtlsdr` fork. That fork adds GPIO and PLL dithering functions that are not in the official osmocom build shipped by Homebrew (`librtlsdr 2.0.x`).

Without patches, the import of `pyrtlsdr` fails at module load:

```
AttributeError: dlsym(…, rtlsdr_set_dithering): symbol not found
```

### What gets patched

`fix_venv.py` patches two files inside the project `.venv`:

**`.venv/…/rtlsdr/librtlsdr.py`** has seven missing ctypes symbol bindings wrapped in `try/except AttributeError`:

```python
try:
    f = librtlsdr.rtlsdr_set_dithering
    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]
except AttributeError:
    pass
```

Functions patched: `rtlsdr_set_dithering`, `rtlsdr_set_gpio_input`, `rtlsdr_set_gpio_bit`, `rtlsdr_get_gpio_bit`, `rtlsdr_set_gpio_byte`, `rtlsdr_get_gpio_byte`, `rtlsdr_set_gpio_status`.

**`.venv/…/rtlsdr/rtlsdr.py`** has two runtime call sites inside `RtlSdr.open()` guarded with `hasattr`:

```python
if hasattr(librtlsdr, 'rtlsdr_set_dithering'):
    result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))
```

### Apply or re-apply the patches

```bash
python fix_venv.py
```

`fix_venv.py` is idempotent. It finds each patch that is already applied and skips it. Run it again after `uv sync --reinstall`.

### Homebrew library path (Apple Silicon)

Homebrew on Apple Silicon installs to `/opt/homebrew/lib`. This path is not in the default `dyld` search path.  
`main.py` sets the environment variable before `pyrtlsdr` triggers `dlopen()`:

```python
os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib')
```

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
