> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md).

# iridium — Iridium L-band Burst Detector

**Stage 1 detector.** This plugin finds bursts on the Iridium satellite
downlink band. It shows live activity for each channel. It does **not**
decode anything. The goal is to answer a simple question: does the antenna
hear Iridium at all, and on which channels?

Iridium is a low-earth-orbit group of 66 satellites. Signals reach the
ground at about −120 dBm as short bursts of about 20 ms. They hop across
252 channels. The channels are 41.667 kHz apart. The band is 1616.0 – 1626.5
MHz.

## Signal parameters

| Parameter | Value |
|---|---|
| Band | 1616.0 – 1626.5 MHz (10.5 MHz total) |
| Channels | 252 total, 41.667 kHz spacing |
| Modulation | Differential QPSK (DQPSK), 25 kbaud — *this plugin does not decode* |
| Burst length | About 20 ms (varies by frame type) |
| Recommended SDR bandwidth | ≥ 2 MHz (RTL-SDR max is 2.4 – 3 MHz stable) |
| Suggested centre frequency | 1621.25 MHz (covers most of the band with 2 MHz BW) |
| Antenna | Patch or helical, aimed up. Miniloops and wire dipoles rarely work. |

## Controls

| Key | Action |
|---|---|
| `+` / `=` | Raise the detection threshold (+3 dB, up to 30 dB) |
| `-` | Lower the detection threshold (−3 dB, down to 3 dB) |
| `r` | Clear the counters and start the noise floor again |

The header line shows the current threshold in dB. The value is saved
with the plugin state. It survives preset save and load, and plugin
restart.

## What you see

The tab has three parts:

1. **Header line** shows the total burst count, the rate per second, and
   how many of the 252 channels are inside the current tuning window.
2. **Active channel list** shows the top 20 channels ranked by burst count
   in the last 10 seconds. Each row shows the channel number, the centre
   frequency, an activity bar, and the count.
3. **Recent bursts list** shows up to 15 detections. Each row has a wall
   clock time, the channel, the frequency, and the power over the noise
   floor in dB.

If the status line shows `[IR band?]`, the tuning is not inside the Iridium
band. Set the centre near 1621.25 MHz with bandwidth of 2 MHz or more.

## How it works

1. **Sliding FFT** — a 2048-point FFT with a Hann window runs on every IQ
   chunk. At 2 MSPS each frame is about 1 ms. A typical chunk gives about
   8 frames per call.
2. **Per-bin noise floor** — the plugin keeps a smooth average of the
   power per FFT bin (α = 0.02, about 50-frame time constant). This is
   slow enough that real bursts do not raise the noise floor. It is fast
   enough to adapt to gain changes.
3. **Channel map** — when the tuning changes, the plugin lists the FFT
   bin range for each visible Iridium channel. At 2 MSPS with a 2048-point
   FFT, each channel covers about 40 bins.
4. **Per-channel detection** — for each channel, the plugin gets the mean
   power in its bin range. It compares this to the local noise floor. If
   the ratio is above the current threshold (default 12 dB = 16× power)
   in any frame of the chunk, the plugin counts one burst on that channel.
   Real Iridium bursts are 15–30 dB above the noise floor, so 12 dB
   catches them and rejects most birdies. Use `+` / `-` in the tab to
   change the threshold. Raise it to 15 dB in a place with many spurs.
   Lower it to 9 dB if you want to catch weaker bursts.
5. **Rate stats** — the plugin updates the burst rate once per second from
   a rolling counter. The per-channel counts use a 10-second sliding window.

## How to read the output

| What you see | What it means |
|---|---|
| Rate 0/s for minutes | No signal reaches the receiver. Check the antenna, gain, tuning. |
| Rate 1–5/s on a few channels | Marginal reception. Some strong bursts but many are missed. |
| Rate 10+/s across many channels | Good reception. A patch antenna during an overhead pass. |
| Rate steady, only 1–2 channels | Not real Iridium. Most likely a nearby CW carrier or interferer. |
| Rate rises for about 10 minutes, then falls | An Iridium satellite pass. Normal. |

## Antenna reality check

The most common result for an RTL-SDR with a wire antenna and no LNA is
**zero bursts**. This is not a bug in the decoder. Iridium is a satellite
signal at about −120 dBm. It is about 60 dB weaker than a broadcast FM
station. To hear Iridium, use one of these:

- An Iridium patch antenna (for example, a Sarantel or a copy).
- A helical antenna aimed at the sky.
- A cheap active GPS antenna (GPS is at 1575 MHz, close enough to work as
  a starter). You need a bias-tee to power it.
- An LNA in line, with about 20 dB gain and a low noise figure.

If Stage 1 shows real activity, Stage 2 (save each raw burst to a file) is
worth building. If not, the antenna is the limit, not the software.

## Limitations

- **Detect only.** No decoding, no channel ID beyond the frequency-to-index
  map, no message parsing.
- **RTL-SDR sees only 1/4 of the band.** At 2 MSPS you cover about 48 of
  the 252 channels. Bursts on other channels are invisible. A HackRF at
  8 MSPS or an Airspy R2 at 10 MSPS sees more.
- **The counter can double-count by design.** A burst that spans two IQ
  chunks may count twice. This is fine for "is there activity?" but the
  count is not an exact bursts-per-second value.
- **False positives from CW.** A steady narrow carrier inside the Iridium
  band shows up as continuous bursts on one channel. Check the spectrum
  tab to confirm.
- **No calibration.** The dB value is relative to the per-bin noise floor
  at the time of detection. It is not an absolute reference.

## Roadmap

- **Stage 2** — save each detected burst as a small SigMF IQ slice for
  offline decoding with the iridium-toolkit.
- **Stage 3** — inline DQPSK decode with `multiprocessing.Pool` workers
  (Python threading loses to the GIL for CPU-bound work). Emit raw
  `A:OK <ts> <freq> <bits>` lines in the same format as iridium-toolkit.
  The offline parsers then work without changes.

Stage 3 is only worth building if Stage 1 shows real activity.
