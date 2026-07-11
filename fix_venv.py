#!/usr/bin/env python3
"""
Re-apply compatibility patches to the pyrtlsdr venv after `uv sync --reinstall`.

pyrtlsdr 0.5.x targets an extended librtlsdr fork that adds GPIO and dithering
functions not present in the official osmocom build shipped by Homebrew.
Patches two files:
  - rtlsdr/librtlsdr.py  : module-level ctypes symbol bindings
  - rtlsdr/rtlsdr.py     : runtime calls inside RtlSdr.open() and set_dithering()
"""

import sys
from pathlib import Path

SITE = Path(__file__).parent / ".venv" / "lib" / "python3.12" / "site-packages" / "rtlsdr"

# (file, old_text, new_text)
PATCHES = [
    # ── librtlsdr.py: module-level symbol bindings ────────────────────────
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_set_dithering\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_set_dithering\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_set_gpio_input\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_set_gpio_input\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_set_gpio_bit\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8, c_int]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_set_gpio_bit\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8, c_int]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_get_gpio_bit\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8, POINTER(c_int)]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_get_gpio_bit\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_uint8, POINTER(c_int)]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_set_gpio_byte\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_set_gpio_byte\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, c_int]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_get_gpio_byte\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, POINTER(c_int)]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_get_gpio_byte\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, POINTER(c_int)]\n"
        "except AttributeError:\n"
        "    pass",
    ),
    (
        "librtlsdr.py",
        "f = librtlsdr.rtlsdr_set_gpio_status\n"
        "f.restype, f.argtypes = c_int, [p_rtlsdr_dev, POINTER(c_int)]",
        "try:\n"
        "    f = librtlsdr.rtlsdr_set_gpio_status\n"
        "    f.restype, f.argtypes = c_int, [p_rtlsdr_dev, POINTER(c_int)]\n"
        "except AttributeError:\n"
        "    pass",
    ),

    # ── rtlsdr.py: runtime call in RtlSdr.open() ─────────────────────────
    (
        "rtlsdr.py",
        "        result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))\n"
        "        if result < 0:\n"
        "            raise IOError('Error code %d when setting PLL dithering mode'\\\n"
        "                           % (result))",
        "        if hasattr(librtlsdr, 'rtlsdr_set_dithering'):\n"
        "            result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))\n"
        "            if result < 0:\n"
        "                raise IOError('Error code %d when setting PLL dithering mode'\\\n"
        "                               % (result))",
    ),

    # ── rtlsdr.py: runtime call in RtlSdr.set_dithering() ────────────────
    (
        "rtlsdr.py",
        "        result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(enabled))\n"
        "        if result < 0:\n"
        "            raise IOError('Error code %d when setting PLL dither mode'\\",
        "        if not hasattr(librtlsdr, 'rtlsdr_set_dithering'):\n"
        "            return\n"
        "        result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(enabled))\n"
        "        if result < 0:\n"
        "            raise IOError('Error code %d when setting PLL dither mode'\\",
    ),
]


def patch_file(path: Path, patches: list) -> tuple[int, int]:
    if not path.exists():
        print(f"ERROR: {path} not found — run `uv sync` first.", file=sys.stderr)
        sys.exit(1)
    text = path.read_text()
    applied = skipped = 0
    for old, new in patches:
        if old in text:
            text = text.replace(old, new)
            applied += 1
        elif new in text:
            skipped += 1
        else:
            print(f"WARNING: patch target not found in {path.name}:\n  {old[:70]}...",
                  file=sys.stderr)
    path.write_text(text)
    return applied, skipped


def main():
    if not SITE.exists():
        print(f"ERROR: {SITE} not found — run `uv sync` first.", file=sys.stderr)
        sys.exit(1)

    by_file: dict[str, list] = {}
    for fname, old, new in PATCHES:
        by_file.setdefault(fname, []).append((old, new))

    total_applied = total_skipped = 0
    for fname, file_patches in by_file.items():
        a, s = patch_file(SITE / fname, file_patches)
        print(f"  {fname}: {a} patched, {s} already applied")
        total_applied += a
        total_skipped += s

    print(f"Done — {total_applied} patch(es) applied, {total_skipped} already in place.")


if __name__ == "__main__":
    main()
