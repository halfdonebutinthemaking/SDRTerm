> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# rds — RDS Decoder

This plugin decodes RDS (Radio Data System) data. The data is on the 57 kHz subcarrier of FM broadcasts.

![RDS decoder showing station name and RadioText](images/rds.gif)

The plugin shows the PI code, PS name (station name), and RadioText (song or artist). It also shows the PTY (programme type), TP (traffic programme) and TA (traffic announcement) flags.

The FM plugin must be on. It must come before the RDS plugin in the pipeline.

## Output

The data fills in step by step as the plugin gets more groups. The full PS name (8 characters) usually comes in a few seconds after you tune. RadioText can take 10 to 30 seconds. The time depends on the broadcast cycle of the station.

| Field | Description |
|-------|-------------|
| PI | Programme Identifier — unique station code |
| PS | Programme Service name — 8-character station name |
| RT | RadioText — up to 64 characters (song title, artist, etc.) |
| PTY | Programme Type — genre code (e.g. 4 = Rock, 10 = Pop) |
| TP | Traffic Programme flag |
| TA | Traffic Announcement flag — set during live traffic bulletins |

This plugin has no tab-specific keys.
