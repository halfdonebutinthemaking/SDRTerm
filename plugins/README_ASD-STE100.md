> **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see the original filename in the same folder.

# Plugins

| Plugin | Description | Preview | Docs | Docs (STE100) |
|--------|-------------|---------|------|---------------|
| **spectrum** | Always-active FFT display. Shows the averaged dBFS power spectrum as a bar chart or waterfall. | ![](../images/running.gif) ![](../images/waterfall.gif) | [Docs](spectrum/) | [STE100](spectrum/spectrum_ASD-STE100.md) |
| **fm** | FM broadcast audio decoder with real-time playback and a channel-bandwidth highlight. | ![](fm/images/fm.gif) | [Docs](fm/) | [STE100](fm/fm_ASD-STE100.md) |
| **rds** | RDS decoder. Gets PS name, RadioText, PTY, PI code, and TP/TA flags from the FM 57 kHz subcarrier. | ![](rds/images/rds.gif) | [Docs](rds/) | [STE100](rds/rds_ASD-STE100.md) |
| **nrsc5_text** | NRSC-5 HD Radio decoder for digital IBOC sidebands. Pure Python and NumPy. | ![](nrsc5_text/images/nrsc.gif) | [Docs](nrsc5_text/) | [STE100](nrsc5_text/nrsc5_text_ASD-STE100.md) |
| **peak_marker** | Marks the strongest signal peak. Has hold-off, alpha-beta Doppler tracking, and follow mode. | ![](peak_marker/images/peak.gif) | [Docs](peak_marker/) | [STE100](peak_marker/peak_marker_ASD-STE100.md) |
| **modclass** | Live modulation classifier. Finds OOK, AM, FM, BPSK, QPSK, 8PSK, QAM16, and FSK with an on-device neural network. | ![](modclass/images/modclass.gif) | [Docs](modclass/) | [STE100](modclass/modclass_ASD-STE100.md) |
| **constellation** | IQ constellation scatter plot. Tune the symbol rate until the clusters snap into focus. This helps you find the modulation order. | ![](constellation/images/constellation_bpsk.gif) | [Docs](constellation/) | [STE100](constellation/constellation_ASD-STE100.md) |
| **range-scan** | Stepped frequency scan across a configurable range. Gives a signal detection list based on SNR. | ![](range_scan/images/range.gif) | [Docs](range_scan/) | [STE100](range_scan/range_scan_ASD-STE100.md) |
| **record** | Captures IQ or plugin output to a SigMF or WAV file. Start and stop the recording from the record tab. | | [Docs](record/) | [STE100](record/record_ASD-STE100.md) |
| **rtl-tcp-passive** | Streams live IQ over TCP to RTL-TCP-compatible clients. The hardware stays under SDRTerm control. | | [Docs](rtltcp_passive/) | [STE100](rtltcp_passive/rtltcp_passive_ASD-STE100.md) |
| **rtl-tcp-active** | Like passive. It also sends client frequency, gain, and sample-rate commands to the hardware. | | [Docs](rtltcp_active/) | [STE100](rtltcp_active/rtltcp_active_ASD-STE100.md) |
| **vdl2** | VDL Mode 2 decoder. D8PSK 10,500 sym/s, HDLC/AVLC frames, ACARS text. Tune to a VDL2 channel at 250 kHz bandwidth and open the VDL2 tab. | ![](vdl2/images/vdl2.gif) | [Docs](vdl2/) | [STE100](vdl2/vdl2_ASD-STE100.md) |
| **freqhop** | Frequency hopper. Keeps a saved list of frequencies with a dwell time for each slot. It cycles by itself so you can monitor many airband channels. | ![](freqhop/images/freqhop.gif) | [Docs](freqhop/) | [STE100](freqhop/freqhop_ASD-STE100.md) |
| **acars** | Classic ACARS decoder. AM/AFSK 2400 baud, mark=2400 Hz / space=1200 Hz. Decodes the aircraft registration, flight ID, and message text with a BCS integrity check. | | [Docs](acars/) | [STE100](acars/acars_ASD-STE100.md) |
| **pocsag** | POCSAG paging decoder. Direct 2-FSK, finds 512/1200/2400 baud by itself, BCH(31,21) error correction. Decodes numeric and alphanumeric messages with RIC. | | [Docs](pocsag/) | [STE100](pocsag/pocsag_ASD-STE100.md) |

Each plugin folder has a `README.md`. GitHub renders it by itself when
you go into the folder. A simplified-English version rewritten in
[ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English)
is next to each one in the `_ASD-STE100.md` file.

You can also find a [top-level STE100 index](README_ASD-STE100.md) and STE100
versions of the top-level [README](../README_ASD-STE100.md) and
[device documentation](../devices/).
