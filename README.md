# VDLS Reference Implementation

This repository contains an executable reference implementation of the
[VDLS Draft 1.0 specification](https://github.com/22552/docssss).

The current implementation provides a backend-neutral semantic frontend,
typed Render Graph lowering, and an FFmpeg reference backend. It is being
developed toward full Draft 1.0 conformance; unsupported constructs fail with
stable `VDLS-*` diagnostics rather than being silently approximated.
Implemented requirements and known gaps are tracked in
[CONFORMANCE.md](CONFORMANCE.md).

## Requirements

- Python 3.11 or newer
- FFmpeg and FFprobe on `PATH`

No third-party Python packages are required.

## Quick start

```powershell
python vdls.py check examples/hello.vdsl
python vdls.py graph examples/hello.vdsl --format json
python vdls.py build examples/hello.vdsl --emit ast-json --emit graph-json
python vdls.py preview examples/hello.vdsl
python vdls.py preview --resolution 640x360 --watch examples/hello.vdsl
python vdls.py render-frame examples/hello.vdsl `
  --target main --time 1s --output frame.png
```

Machine-readable output:

```powershell
python vdls.py --diagnostic-format json check examples/hello.vdsl
```

Project discovery follows `vdls.toml`, `project.vdsl`, then `main.vdsl`.
`--locked` requires a valid `vdls.lock`; `--frozen` also enables offline mode.

## Implemented language profile

- `#lang vdls` portable reader and prohibited reader-extension checks
- exact rational time, frame, length, ratio, angle, gain, and frequency units
- project, output, asset, scene, layer, video, audio, text, shape, and group
- file, URL, generated, and plugin asset-source AST forms
- trim, timing, transforms, opacity, blend mode, filters, and animations in AST
- typed and defaulted template expansion before Semantic AST construction
- explicit relative imports with cycle detection and project-root isolation
- JSON locale catalogs, fallback chains, and `(tr "...")` expansion before AST
- SRT/WebVTT parsing into exact half-open cues and subtitle burn-in
- deterministic SRT/WebVTT sidecars and privacy-reduced EXIF provenance
- explicit output color descriptors and validated BT.709 SDR conversion
- pure expression validation and FFmpeg expression lowering
- `r`, a deterministic per-frame pseudo-random scalar in `[0,1)` derived from
  `frame` and `(build-options (seed ...))`
- stable JSON diagnostics and canonical JSON serialization
- content-addressed render cache with digest verification, status, prune, and clean
- Plugin ABI 1 manifest, capability, permission, entry-path, and lock validation
- isolated plugin processes using bounded length-prefixed JSON-RPC 2.0 with
  lifecycle, timeout, cancellation, and crash diagnostics

## Render pipeline

The compiler emits a backend-neutral typed DAG with explicit ports, edges,
time domains, purity, resources, cache identities, and output targets. The
graph validator checks node identity, port compatibility, required inputs,
cycles, and target producers.

The FFmpeg backend currently renders:

- file or deterministic solid-color video backgrounds
- independent `TextRequest -> TextLayout -> RGBA surface` pipeline, with
  complex HarfBuzz-compatible shaping and bidi rasterization through libass
- deterministic tone and silence audio generators
- audio gain, EQ, high/low-pass, dynamics, loudness normalization,
  resampling, mixing, H.264/AAC encoding, and muxing
- timeline-aware sidechain ducking with explicit attack/release
- sharp foreground text with independently blurred two-layer shadows
- deterministic word/grapheme/balanced wrapping and
  visible/clip/ellipsis/shrink text-box overflow
- common geometry, color, flip, rotation, grayscale, invert, and blur filters
- temperature/tint, 4×5 color matrices, chroma key/despill,
  alpha-from-luma, reverse, and frame-rate conversion
- file/generated asset masks lowered through explicit two-input graph ports
- all required normal/multiply/screen/overlay/light/difference blend modes,
  including positioned layers with explicit alpha masks
- `from`/`to` and keyframe animation for text position and opacity
- SRT/WebVTT subtitle rendering through the FFmpeg/libass adapter
- deterministic SRT/WebVTT sidecar export with independent manifest artifacts
- normalized EXIF inspection and privacy-reduced EXIF provenance summaries
- grapheme-safe typewriter, line reveal, word highlight, and text fade helpers
- text-box word wrapping and clipping on independent RGBA surfaces
- multi-video overlay, transform, opacity, multiply/screen blend, and timed placement
- video/audio speed and fade helpers plus ordered crop/pad/tone filters
- safe output metadata muxing and normalized metadata inspection
- atomic output publication followed by FFprobe validation and SHA-256 hashing

External commands are always passed as argument arrays with `shell=False`.

Strict reproducible builds use `--reproducible`. This implies
`--offline --locked`, verifies the locked FFmpeg capability digest and all file-asset
integrity declarations, requires pinned font assets, normalizes metadata, and
uses deterministic encoder threading. Run `vdls lock --project PATH` after a
toolchain change to refresh the locked backend identity.
Non-trivial filter graphs are written to UTF-8 filter-script files.

Per-frame random motion:

```racket
(project
  (build-options (seed 2026))
  ...
  (text "R"
    (position (* r 580) 140)))
```

For a fixed seed, `r` is identical everywhere within one frame and changes on
the next frame. Rebuilding the same source with the same seed reproduces the
same sequence.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The test suite covers reader behavior, units, namespaces, expressions,
templates, configuration, lockfiles, Semantic AST, typed graph validation,
audio pipelines, and CLI diagnostics.

## Conformance status

The implementation does not yet claim complete Draft 1.0 conformance.
  Remaining work includes the complete animation evaluator, every
  standard-library filter and generator, native glyph-run export beyond the
  current libass raster adapter, stronger OS-level plugin sandboxing, full plugin dependency
resolution, GPU lowering, and the remaining CLI lifecycle commands.
