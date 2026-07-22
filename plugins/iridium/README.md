# iridium — Iridium L-band Burst Detector

**Stage 1 detector.** This plugin identifies bursts on the Iridium satellite
downlink band and shows live per-channel activity statistics. It does **not**
demodulate anything — the purpose is to answer the make-or-break diagnostic
question: *does my antenna hear Iridium at all, and on which channels?*

Iridium is a low-earth-orbit constellation of 66 satellites. Signals appear
on the ground at roughly −120 dBm as short (~20 ms) bursts, hopping across
252 channels spaced 41.667 kHz apart in the 1616.0 – 1626.5 MHz L-band
downlink.

## Signal parameters

| Parameter | Value |
|---|---|
| Band | 1616.0 – 1626.5 MHz (10.5 MHz total) |
| Channels | 252 total, 41.667 kHz spacing |
| Modulation | Differential QPSK (DQPSK), 25 kbaud — *not demodulated by this plugin* |
| Burst length | ~20 ms (typical, varies by frame type) |
| Recommended SDR bandwidth | ≥ 2 MHz (RTL-SDR max is 2.4 – 3 MHz stable) |
| Suggested centre frequency | 1621.25 MHz (covers most of the band with 2 MHz BW) |
| Antenna | Patch or helical aimed skyward; miniloops and wire dipoles rarely work |

## Controls

| Key | Action |
|---|---|
| `+` / `=` | Raise detection threshold (+3 dB, up to 30 dB) |
| `-` | Lower detection threshold (−3 dB, down to 3 dB) |
| `r` | Clear counters and rebuild noise floor |

The current threshold in dB is shown on the header line and is saved
with the plugin state, so it survives preset save/load and plugin
restart.

## What you see

The tab shows three regions:

1. **Header line** with total burst count, per-second rate, and how many of
   the 252 Iridium channels fall inside your current tuning window.
2. **Active channel list** — the top 20 channels ranked by burst count in
   the last 10 seconds. Each row shows channel number, centre frequency,
   an activity bar, and the count.
3. **Recent bursts list** — up to 15 most recent detections with wall-clock
   timestamp, channel, frequency, and detection power over the noise floor
   (in dB).

If the status line shows `[IR band?]` you are not tuned inside the Iridium
band — set centre near 1621.25 MHz with bandwidth ≥ 2 MHz.

## How it works

1. **Sliding FFT** — 2048-point FFT with a Hann window on every incoming IQ
   chunk. At 2 MSPS each frame is ~1 ms, and a typical chunk produces
   ~8 frames per call.
2. **Per-bin noise floor** — exponentially smoothed average power per FFT
   bin (α = 0.02, ~50-frame time constant). Slow enough not to be inflated
   by real bursts, fast enough to adapt to gain changes.
3. **Channel map** — at each tuning change the plugin precomputes which
   FFT bin range covers each visible Iridium channel (typically ~40 bins
   per channel at 2 MSPS / 2048 FFT).
4. **Per-channel detection** — for every channel, the mean power in its bin
   range is compared to the local noise floor. A ratio above the current
   threshold (default 12 dB = 16× power) in any frame of the current chunk
   counts as one burst on that channel. Real Iridium bursts typically land
   15–30 dB above noise, so 12 dB catches them while rejecting most
   birdies and spurs. Use `+` / `-` in the tab to raise or lower the
   threshold if you see too many false positives (typical: raise to 15 dB
   in a spur-heavy environment) or want to catch weaker bursts.
5. **Rate stats** — burst rate is updated once per second from a rolling
   accumulator. Per-channel counts use a 10-second sliding window.

## Interpreting the output

| What you see | What it means |
|---|---|
| Rate 0/s over minutes | No signal reaching the receiver. Check antenna, gain, tuning. |
| Rate 1–5/s on a few channels | Marginal reception. Some strong bursts but most missed. |
| Rate 10+/s across many channels | Good reception. A patch antenna during an overhead pass. |
| Rate steady, only 1–2 channels | Probably not real Iridium — most likely a nearby CW carrier or interferer. |
| Rate rises for ~10 minutes then falls | Iridium satellite pass. Normal. |

## Antenna reality check

The most common outcome for RTL-SDR + wire antenna + no LNA is **zero
bursts**. This is not a decoder bug. Iridium is a satellite signal at
~−120 dBm, roughly 60 dB weaker than a broadcast FM station. Successful
receivers use one of:

- Iridium-specific patch antenna (e.g. Sarantel or third-party knockoffs)
- Helical antenna aimed at the sky
- Cheap active GPS antenna (also L-band, 1575 MHz — close enough to work
  as a starter) with a bias-tee to power it
- LNA in-line (~20 dB gain, low noise figure)

If Stage 1 shows real activity, Stage 2 (raw burst dump to file) becomes
worthwhile. Otherwise the antenna is the bottleneck, not the software.

## Limitations

- **Detect-only.** No demodulation, no channel identification beyond the
  frequency-to-index mapping, no message decoding.
- **RTL-SDR sees only 1/4 of the band.** At 2 MSPS you cover ~48 of the
  252 channels. Bursts on other channels are invisible. HackRF at 8 MSPS
  or Airspy R2 at 10 MSPS covers proportionally more.
- **Overcounts by design.** A burst spanning two IQ chunks may be counted
  twice. This is fine for the "is there activity?" question but the count
  should not be interpreted as a precise burst-per-second measurement.
- **False positives from CW.** A steady narrowband carrier inside the
  Iridium band (unlikely but possible) will register as continuous bursts
  on one channel. Sanity check by looking at the spectrum tab.
- **No calibration.** The dB readout is relative to the per-bin noise
  floor at the time of detection, not to an absolute reference.

## Roadmap

- **Stage 2** — write each detected burst as a small SigMF-annotated IQ
  slice for offline demodulation with the iridium-toolkit.
- **Stage 3** — inline DQPSK demodulation with `multiprocessing.Pool`
  workers (Python threading loses to the GIL for CPU-bound demod). Emit
  raw `A:OK <ts> <freq> <bits>` lines matching iridium-toolkit's format so
  the same offline parsers work unchanged.

Stage 3 is only worth building if Stage 1 shows real activity.
