> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# record — Signal Recorder

This plugin records the output of the plugin that comes just before it in the pipeline. It writes the output to a file.

## Controls

| Key | Action |
|-----|--------|
| `e` | Start the recording (press again to stop) |
| `f` | Change the raw IQ format between SigMF (`.sigmf-data` + `.sigmf-meta`) and plain `.iq` |
| `o` | Set the output path prefix (default: auto-made timestamp name) |

The recording is off when you first turn the plugin on. The status line shows `[REC ready]` until you press `e`. When you press `e` a second time, the plugin closes and finalises the file.

## Output format

The format depends on the plugin that comes before `record` in the pipeline:

| Predecessor | Output |
|-------------|--------|
| `fm` | `.wav` — PCM audio at 48 kHz |
| any other recordable plugin | plugin-specific format |
| none / non-recordable | `.sigmf-data` + `.sigmf-meta` — raw IQ |

## Pipeline order

**The pipeline order is important.** The `record` plugin records the plugin that comes **just before it**. To record FM audio, make sure that `fm` is above (before) `record` in the plugin menu. If `record` is first, it gets raw IQ instead.

To change the order, open the plugin menu with `p`. Then use `<` and `>` to move the plugins.
