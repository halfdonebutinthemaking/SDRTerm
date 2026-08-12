# Ideas

Loose ideas and thought experiments — not yet on the roadmap, no commitment.
Graduate to `future_additions.md` when they turn into an actual work item.

---

## Reception sphere / antenna coverage dome

**Status:** thought experiment (2026-08-12)

Turn days of `adsb.csv` fixes into a 3D visualisation of what the antenna
actually hears from where it sits.  Each aircraft position is a ray from
the antenna at (azimuth, elevation, range); accumulated over time this
reveals the real-world reception envelope — blind spots (buildings,
hills, tree line), main-beam direction, elevation ceiling, near-field
obstructions.

### What we can compute per fix

From the existing CSV log + the preset's `location_lat/lon`:

- **Azimuth** — bearing receiver → aircraft (0–360°)
- **Ground distance** — haversine (already implemented, `_haversine_km`)
- **Elevation angle** — `atan2(aircraft_alt − antenna_alt, ground_distance)`
- **Slant range** — √(ground² + Δalt²)

Needs one new preset field: `location_alt_m` (antenna AMSL, metres).
Without it we'd have to assume 0 m and near-field elevation angles
(within ~50 km) would be off.

### Views worth building, ordered by insight-per-effort

1. **2D polar reception plot** — 360° azimuth vs. max range per bearing.
   Classic ADS-B receiver diagnostic; reveals main beam and blocked
   sectors in one glance.
2. **Hemisphere point cloud** — every fix projected onto a sky dome by
   (az, el).  Density heatmap.  Blind spots appear as bald patches.
3. **Full 3D volumetric hull** — outer envelope of all fixes in local
   ENU (east/north/up) space, coloured by hits per cell.  The closest
   thing to a "solid model" of the reception volume.
4. **Range-vs-elevation slices per azimuth** — thin polar wedges,
   stacked.  Separates near-field obstructions (fences, walls) from
   far-field blockages (hills, distant terrain).

### Delivery options

| Option | Effort | Interactive |
|---|---|---|
| Python script → static PNGs (matplotlib polar + mpl3d) | small | no |
| Standalone HTML via Plotly (WebGL rotate/zoom) | small–medium | yes |
| New "Coverage" tab in the ADS-B web plugin (reuses Cesium) | medium | yes, in-app |
| glTF/OBJ export for Blender / three.js | medium–large | external |

### Shortest useful path

Standalone Python script → Plotly HTML with the 2D polar plot + 3D
hemisphere point cloud, reading the existing CSV log, one added
`location_alt_m` preset field.  ~1–2 h of work.  Days of logging is
100 k–1 M rows — comfortable for both Python and WebGL.
