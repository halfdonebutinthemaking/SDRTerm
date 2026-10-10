# Documentation Alignment

Rules that keep SDRTerm's docs in sync with the code. If something is
documented one way and the repo looks another way, the repo wins — but
the drift must be closed in the next doc PR, not left standing.

This file is the single source of truth for *how* we document. Content
rules for individual plugins/devices live inside their own READMEs.

---

## 1. File-layout conventions

### 1.1 Plugins (`plugins/<name>/`)

The current layout is **subdirectory, one plugin per folder**. Flat
`plugins/<name>.py` is still accepted by `plugins/__init__.py` for
backwards compatibility but **no new plugin may use it**.

```
plugins/<name>/
  __init__.py            — required, usually empty
  <name>.py              — Decoder subclass; filename must equal <name>
  README.md              — plain-English docs (see §5)
  <name>_ASD-STE100.md   — STE-100 sibling (see §3)
  images/                — screenshots and GIFs referenced from README.md
  docs/                  — optional LaTeX pipeline walkthroughs
  web/                   — optional static assets served by the webserver plugin
```

Supporting Python modules alongside `<name>.py` (e.g. `plugins/vdl2/protocol.py`,
`plugins/pocsag/bch.py`) are fine and need no doc of their own — reference
them from `README.md` when they matter for understanding the pipeline.

### 1.2 Devices (`devices/`)

Devices stay flat — one file per driver, no subdirectory. The discovery
scan in `devices/__init__.py` skips any module whose name starts with
`_`, so internal/fallback drivers (e.g. `devices/_null.py`) can live in
the same directory without being auto-opened.

```
devices/
  __init__.py            — auto-discovery loader
  <name>.py              — Device subclass
  <name>.md              — plain-English docs
  <name>_ASD-STE100.md   — STE-100 sibling
  _<name>.py             — internal; no public docs required (document inline)
```

### 1.3 Top-level assets

```
images/                  — top-level GIFs/screenshots referenced from README.md
samples/                 — recorded IQ / SigMF (auto-created; gitignored except preset-driven fixtures)
presets/                 — versioned .sdrterm presets
scripts/                 — one-shot CLI utilities; document in README.md inline
```

### 1.4 Where images live

- Images referenced from the **top-level** README → `images/`.
- Images referenced from a **plugin** README → `plugins/<name>/images/`.
- Images referenced from the **plugins/README.md** index → link to each
  plugin's own `images/` directory via relative paths
  (`plugins/<name>/images/foo.gif`). No shared pool.

---

## 2. README hierarchy

Three tiers, each with a specific job:

| Tier | File | Job |
|------|------|-----|
| Project | `README.md` | Elevator pitch, install, hardware, controls, link to plugins/README.md for the plugin catalogue |
| Plugin index | `plugins/README.md` | Table of **every** plugin with one-line description + links to per-plugin docs |
| Device index | `devices/` (no README; linked from project README) | One `.md` per device |
| Per-plugin | `plugins/<name>/README.md` | Full plugin docs (see §5) |

**Rule:** `README.md` must not duplicate the plugin table from
`plugins/README.md`. It links to the plugin index and summarises only
*categories* of plugins (audio decoders, data decoders, utilities,
infrastructure). This keeps the project README stable as plugins come
and go.

---

## 3. STE-100 pairing

Every end-user `.md` has an [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English)
sibling. The naming is mechanical:

| Original | STE-100 sibling |
|----------|-----------------|
| `README.md` | `README_ASD-STE100.md` |
| `plugins/README.md` | `plugins/README_ASD-STE100.md` |
| `plugins/<name>/README.md` | `plugins/<name>/<name>_ASD-STE100.md` |
| `devices/<name>.md` | `devices/<name>_ASD-STE100.md` |

**Content rules:**

- The STE-100 version must open with the banner line:
  > **This document is written in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English).** For the full-English version, see [`README.md`](README.md) (or the original filename in the same folder).
- Same section hierarchy as the original — headings in the same order,
  same count. If one gains or loses a section, the other must follow in
  the same commit.
- Same images, same tables, same code blocks. Only prose changes.
- Links use the same relative paths (STE-100 doc links to STE-100 doc).

**Exempt** (not user-facing, no STE-100 pairing required):

- `CLAUDE.md`
- `documentation_alignment.md` (this file)
- `future_additions.md` / `future_additions_ASD-STE100.md` are a legacy
  pair; keep the pairing if the file exists, don't require it for new
  policy/planning docs.
- Build/CI configuration (`pyproject.toml` comments, etc.)

---

## 4. Per-plugin README content rules

Every `plugins/<name>/README.md` must have, in this order:

1. `# <name> — Short Title` — one short title line.
2. One-sentence summary of what the plugin does.
3. Hero image or GIF (if the plugin has one in `images/`).
4. `## Controls` — table of key bindings when the plugin tab is active.
5. `## Pipeline / Signal flow` — short description of the DSP chain,
   including `min_sample_rate` and any plugin-ordering requirements.
6. `## Preset keys` — any `plugin_states.<name>.*` fields the user can
   set, with types and defaults.
7. `## Web view` — only if the plugin exposes `web_json`. Describe the
   endpoint, query params, and the HUD/widget surface.
8. `## Limitations / known issues` — what doesn't work and why.

Device READMEs follow the same shape minus plugin-specific sections,
plus a `## Supported bandwidths` table.

---

## 5. `.gitignore` conventions

Each plugin that produces build artefacts (LaTeX, model files, user
captures) must add its own `.gitignore` entries at the **project-root**
`.gitignore`, grouped by plugin and preceded by a one-line comment
explaining what the pattern covers. Example:

```gitignore
# LaTeX build artefacts under plugins/<name>/docs/
plugins/<name>/docs/*.aux
plugins/<name>/docs/*.log
plugins/<name>/docs/*.out
plugins/<name>/docs/*.toc
plugins/<name>/docs/*.pdf
```

If a plugin ships a `docs/pipeline.tex`, the five companion build files
(`*.aux`, `*.log`, `*.out`, `*.toc`, `*.pdf`) MUST be gitignored. The
compiled PDF is a build artefact, not a source — regenerate it locally.

---

## 6. Change checklist

When you add, rename, or delete a plugin or device, the same PR must
touch every item that applies:

- [ ] Code: `plugins/<name>/<name>.py` or `devices/<name>.py` added/renamed/removed.
- [ ] Plugin index: `plugins/README.md` table row added/updated/removed.
- [ ] STE-100 index: `plugins/README_ASD-STE100.md` same change.
- [ ] Per-plugin README: `plugins/<name>/README.md` created with the
      section layout from §4.
- [ ] STE-100 sibling: `plugins/<name>/<name>_ASD-STE100.md` created
      with the banner and matching section layout from §3.
- [ ] Top-level README: `README.md` category paragraph updated only if
      the new plugin introduces a new *category* of capability.
- [ ] `.gitignore`: build-artefact entries added if the plugin ships
      a `docs/` directory or user-capture directory.
- [ ] Tests: the discovery test in `tests/test_plugins.py` picks up the
      new plugin automatically; no doc test is required but
      `plugins/README.md` row count should match `len(load_plugins())`.

Renames also require redirect comments in the old location for one
release, or a hard rename with a note in the PR description — never
leave dangling links.

---

## 7. Review heuristic

Before merging any PR that changes `plugins/` or `devices/`, run this
in a terminal and compare the two counts:

```bash
# Number of real plugins (subdir layout)
find plugins -mindepth 2 -maxdepth 2 -name '*.py' -not -name '__init__.py' \
  | grep -v '/web/' | grep -v '/docs/' | sort -u \
  | awk -F/ '{print $2}' | sort -u | wc -l

# Number of rows in the plugin index
grep -cE '^\| \*\*' plugins/README.md
```

If they diverge, §6's checklist was skipped.

Same for devices:

```bash
ls devices/*.py | grep -v __init__ | grep -v '/_' | wc -l   # drivers
ls devices/*.md | grep -v ASD-STE100 | wc -l                 # English docs
ls devices/*_ASD-STE100.md | wc -l                           # STE-100 siblings
```

All three numbers must match.
