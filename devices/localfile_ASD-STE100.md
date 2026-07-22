> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# localfile — IQ File Replay

Plays back an IQ recording as if it were live hardware. The file loops without stop. Use it to examine captures when no hardware is present. You can also use it to test and build plugins against known signals.

**Device name:** `localfile`  
**Selected by:** `--file PATH`

## Supported formats

| Format | Extension | IQ layout | Sample rate | Centre frequency |
|--------|-----------|-----------|-------------|-----------------|
| Raw IQ | `.iq` | Raw `complex64` binary | Give with `--bw` | Give with `--f`, or read from the filename |
| WAV IQ | `.wav` | Stereo PCM — left=I, right=Q | Read from the WAV header | Give with `--f`, or read from the filename |
| SigMF | `.sigmf-data` / `.sigmf` | Raw `cf32_le` + JSON sidecar | Read from `.sigmf-meta` | Read from `.sigmf-meta` |

## Filename frequency parsing

If the filename contains a frequency in the SDR++ form (`_<freq>Hz_`), the driver reads the centre frequency by itself. You do not need to give `--f`:

```
baseband_105800000Hz_12-00-00_01-01-2026 2400k.wav   → 105.8 MHz
recording_433920000Hz.iq                              → 433.92 MHz
```

## Pacing

The driver paces playback to match the set sample rate. It uses a monotonic deadline, so the spectrum updates at the same rate as real hardware. Raw `.iq` files are memory-mapped (`np.memmap`), so large files do not load into RAM. WAV files load fully when you open them.

## Usage

```bash
# Raw IQ — you must give the sample rate and the centre frequency
uv run python main.py --file recording.iq --bw 2.4M --f 105.8M

# WAV — sample rate from the header, centre frequency from the filename or --f
uv run python main.py --file recording.wav

# SigMF — the driver reads both from the sidecar by itself
uv run python main.py --file recording.sigmf-data
```

## Generating a test file

```bash
uv run python scripts/gen_doppler_test.py
uv run python main.py --file doppler_test.sigmf-data
```
