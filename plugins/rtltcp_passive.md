# rtl-tcp-passive — RTL-TCP Passive Server

Starts a TCP server that streams the live IQ data to any RTL-TCP-compatible client (SDR#, GQRX, GNU Radio, etc.). Client frequency, gain, and sample-rate commands are silently ignored — hardware is controlled only by SDRTerm.

## Controls

| Key | Action |
|-----|--------|
| `o` | Set listen port (default: 1234) |

## Usage

Enable this plugin, then connect any RTL-TCP client to `localhost:1234`. The client will receive the IQ stream at whatever sample rate and centre frequency SDRTerm is currently using.

Use this mode when you want SDRTerm to remain in control of the hardware while another application observes the same IQ stream (e.g. running a decoder alongside the spectrum view).
