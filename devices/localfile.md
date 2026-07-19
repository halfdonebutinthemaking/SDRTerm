# localfile — IQ File Replay

Replays an IQ recording as if it were live hardware. The file loops continuously. Useful for analysing captures without hardware present, or for testing and developing plugins against known signals.

**Device name:** `localfile`  
**Selected by:** `--file PATH`

## Supported formats

| Format | Extension | IQ layout | Sample rate | Centre frequency |
|--------|-----------|-----------|-------------|-----------------|
| Raw IQ | `.iq` | Raw `complex64` binary | Supply via `--bw` | Supply via `--f`, or parsed from filename |
| WAV IQ | `.wav` | Stereo PCM — left=I, right=Q | Read from WAV header | Supply via `--f`, or parsed from filename |
| SigMF | `.sigmf-data` / `.sigmf` | Raw `cf32_le` + JSON sidecar | Read from `.sigmf-meta` | Read from `.sigmf-meta` |

## Filename frequency parsing

If the filename contains a frequency in the SDR++ convention (`_<freq>Hz_`), the centre frequency is extracted automatically and `--f` is not required:

```
baseband_105800000Hz_12-00-00_01-01-2026 2400k.wav   → 105.8 MHz
recording_433920000Hz.iq                              → 433.92 MHz
```

## Pacing

Playback is paced to match the configured sample rate using a monotonic deadline, so the spectrum updates at the same cadence as real hardware. Raw `.iq` files are memory-mapped (`np.memmap`) so large files do not load into RAM; WAV files are loaded fully on open.

## Usage

```bash
# Raw IQ — must supply sample rate and centre frequency
uv run python main.py --file recording.iq --bw 2.4M --f 105.8M

# WAV — sample rate from header, centre frequency from filename or --f
uv run python main.py --file recording.wav

# SigMF — both read from sidecar automatically
uv run python main.py --file recording.sigmf-data
```

## Generating a test file

```bash
uv run python scripts/gen_doppler_test.py
uv run python main.py --file doppler_test.sigmf-data
```
