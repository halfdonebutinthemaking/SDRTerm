# Plugins

| Plugin | Description | Preview | Docs | Docs (STE100) |
|--------|-------------|---------|------|---------------|
| **spectrum** | Always-active FFT display — averaged dBFS power spectrum rendered as bar chart or waterfall | ![](../images/running.gif) ![](../images/waterfall.gif) | [Docs](spectrum/) | [STE100](spectrum/spectrum_ASD-STE100.md) |
| **fm** | FM broadcast audio decoder with real-time playback and channel-bandwidth highlight | ![](fm/images/fm.gif) | [Docs](fm/) | [STE100](fm/fm_ASD-STE100.md) |
| **rds** | RDS decoder — PS name, RadioText, PTY, PI code, TP/TA flags from the FM 57 kHz subcarrier | ![](rds/images/rds.gif) | [Docs](rds/) | [STE100](rds/rds_ASD-STE100.md) |
| **nrsc5_text** | NRSC-5 HD Radio decoder for digital IBOC sidebands, pure Python/NumPy | ![](nrsc5_text/images/nrsc.gif) | [Docs](nrsc5_text/) | [STE100](nrsc5_text/nrsc5_text_ASD-STE100.md) |
| **peak_marker** | Marks the strongest signal peak; hold-off, alpha-beta Doppler tracking, and follow mode | ![](peak_marker/images/peak.gif) | [Docs](peak_marker/) | [STE100](peak_marker/peak_marker_ASD-STE100.md) |
| **modclass** | Live modulation classifier — identifies OOK, AM, FM, BPSK, QPSK, 8PSK, QAM16, FSK using an on-device neural network | ![](modclass/images/modclass.gif) | [Docs](modclass/) | [STE100](modclass/modclass_ASD-STE100.md) |
| **constellation** | IQ constellation scatter plot — tune symbol rate until clusters snap into focus to identify modulation order | ![](constellation/images/constellation_bpsk.gif) | [Docs](constellation/) | [STE100](constellation/constellation_ASD-STE100.md) |
| **range-scan** | Stepped frequency scan across a configurable range with SNR-based signal detection list | ![](range_scan/images/range.gif) | [Docs](range_scan/) | [STE100](range_scan/range_scan_ASD-STE100.md) |
| **record** | Captures IQ or plugin output to SigMF / WAV file; start and stop recording from the record tab | | [Docs](record/) | [STE100](record/record_ASD-STE100.md) |
| **rtl-tcp-passive** | Streams live IQ over TCP to RTL-TCP-compatible clients; hardware stays under SDRTerm control | | [Docs](rtltcp_passive/) | [STE100](rtltcp_passive/rtltcp_passive_ASD-STE100.md) |
| **rtl-tcp-active** | Like passive, but also forwards client frequency, gain, and sample-rate commands to hardware | | [Docs](rtltcp_active/) | [STE100](rtltcp_active/rtltcp_active_ASD-STE100.md) |
| **vdl2** | VDL Mode 2 decoder — D8PSK 10,500 sym/s, HDLC/AVLC frames, ACARS text; tune to a VDL2 channel at 250 kHz bandwidth and open the VDL2 tab | ![](vdl2/images/vdl2.gif) | [Docs](vdl2/) | [STE100](vdl2/vdl2_ASD-STE100.md) |
| **freqhop** | Frequency hopper — maintain a saved list of frequencies with per-slot dwell times; cycles automatically so you can monitor multiple airband channels | ![](freqhop/images/freqhop.gif) | [Docs](freqhop/) | [STE100](freqhop/freqhop_ASD-STE100.md) |
| **acars** | Classic ACARS decoder — AM/AFSK 2400 baud, mark=2400 Hz / space=1200 Hz; decodes aircraft registration, flight ID, and message text with BCS integrity check | | [Docs](acars/) | [STE100](acars/acars_ASD-STE100.md) |
| **pocsag** | POCSAG paging decoder — direct 2-FSK, auto-detects 512/1200/2400 baud, BCH(31,21) error correction, decodes numeric and alphanumeric messages with RIC | | [Docs](pocsag/) | [STE100](pocsag/pocsag_ASD-STE100.md) |

Each plugin folder has a `README.md` that GitHub renders automatically when
you navigate into the folder. A simplified-English version rewritten in
[ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English)
lives alongside each in the `_ASD-STE100.md` file.

There is also a [top-level STE100 index](README_ASD-STE100.md) and STE100
versions of the top-level [README](../README_ASD-STE100.md) and
[device documentation](../devices/).
