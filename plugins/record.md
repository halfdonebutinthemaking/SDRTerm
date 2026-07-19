# record — Signal Recorder

Captures the output of the immediately preceding plugin in the pipeline to a file.

## Controls

| Key | Action |
|-----|--------|
| `o` | Set output path prefix (default: auto-generated timestamp name) |

## Output format

The format depends on what plugin precedes `record` in the pipeline:

| Predecessor | Output |
|-------------|--------|
| `fm` | `.wav` — PCM audio at 48 kHz |
| any other recordable plugin | plugin-specific format |
| none / non-recordable | `.sigmf-data` + `.sigmf-meta` — raw IQ |

## Pipeline order

**Pipeline order matters.** The `record` plugin captures its **immediate predecessor**. To record FM audio, ensure `fm` appears above (before) `record` in the plugin menu. If `record` is first, it sees raw IQ instead.

To reorder: open the plugin menu with `p`, then use `<`/`>` to move plugins.
