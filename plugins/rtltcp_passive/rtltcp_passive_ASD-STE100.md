> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# rtl-tcp-passive — RTL-TCP Passive Server

This plugin starts a TCP server. The server sends the live IQ data to any RTL-TCP client (SDR#, GQRX, GNU Radio, etc.). The plugin ignores client commands for frequency, gain, and sample rate. Only SDRTerm controls the hardware.

## Controls

| Key | Action |
|-----|--------|
| `o` | Set the listen port (default: 1234) |

## Usage

Turn on this plugin. Then connect any RTL-TCP client to `localhost:1234`. The client gets the IQ stream at the sample rate and centre frequency that SDRTerm uses.

Use this mode when you want SDRTerm to keep control of the hardware. At the same time, another application can look at the same IQ stream. For example, you can run a decoder next to the spectrum view.
