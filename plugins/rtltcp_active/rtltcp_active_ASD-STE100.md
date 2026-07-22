> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).

# rtl-tcp-active — RTL-TCP Active Server

This plugin is like the passive server. But it also sends the client commands to the hardware. The commands are for frequency, gain and sample rate. Use this plugin when the client needs full hardware control.

## Controls

| Key | Action |
|-----|--------|
| `o` | Set the listen port (default: 1234) |

## Usage

Turn on this plugin. Then connect an RTL-TCP client to `localhost:1234`. The plugin sends the client commands for frequency, gain and sample rate to the SDR hardware. The SDRTerm display updates to show the settings from the client.

Use this mode for wideband scanning software or other tools. These tools need to retune the hardware on their own. Examples are dump1090 and rtl_433 in network mode.

## Difference from passive server

| | Passive | Active |
|-|---------|--------|
| IQ stream to client | yes | yes |
| Client can retune hardware | no | yes |
| SDRTerm keeps control | yes | no (client drives) |
