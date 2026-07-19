# rtl-tcp-active — RTL-TCP Active Server

Like the passive server but also applies client-sent frequency, gain, and sample-rate commands to the hardware. Use this when the connected client needs full hardware control.

## Controls

| Key | Action |
|-----|--------|
| `o` | Set listen port (default: 1234) |

## Usage

Enable this plugin, then connect an RTL-TCP-compatible client to `localhost:1234`. Frequency/gain/rate commands sent by the client are forwarded to the SDR hardware — the SDRTerm display updates to reflect the client-driven settings.

Use this mode for wideband scanning software or other tools that need to retune the hardware autonomously (e.g. dump1090, rtl_433 in network mode).

## Difference from passive server

| | Passive | Active |
|-|---------|--------|
| IQ stream to client | yes | yes |
| Client can retune hardware | no | yes |
| SDRTerm retains control | yes | no (client drives) |
