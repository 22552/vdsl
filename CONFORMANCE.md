# VDLS Draft 1.0 conformance

This implementation tracks the normative documents in
[`22552/docssss`](https://github.com/22552/docssss).

## Text and FFmpeg status

| Requirement | Status | Implementation |
|---|---|---|
| 004 §1–2: capability analysis before rendering | Implemented | The backend runs `-version`, `-filters`, `-encoders`, `-decoders`, and `-pix_fmts`, then rejects missing plan requirements before FFmpeg render execution. |
| 004 §1, §3, §5: safe argv, filter script, labels, explicit maps | Implemented | Commands use `shell=False`, non-trivial graphs use `-filter_complex_script`, generated streams are deterministically labeled, and every intended stream is explicitly mapped. |
| 004 §1, §18: output artifact validation | Implemented | Output existence, non-zero size, stream presence, dimensions, frame rate, and duration are checked with FFprobe before atomic publication. |
| 004 §1, §2: debug command and toolchain record | Implemented | `--emit commands` records exact argv plus filter scripts; manifests record the FFmpeg version and capability digest. |
| 004 §8: `drawtext` is not the normative text path | Implemented | The backend consumes a Text Engine surface and composites it with `overlay`; generated graphs contain no `drawtext`. |
| 006 §5: normalization policy is explicit | Implemented | `preserve`, `nfc`, and `nfkc` are part of `TextRequest` and the layout digest. The default is `preserve`. |
| 006 §5: bidi and complex shaping | Implemented by adapter | The libass adapter requests `shaping=complex` and uses its HarfBuzz-compatible shaper and bidi implementation. |
| 006 §5: deterministic pinned fonts | Implemented | A font asset can be pinned; every selected font file is included in the build manifest digest set. |
| 006 §5: shaping and rendering are separate stages | **Partial** | `TextRequest -> TextLayout` is independent from the renderer, but the current libass adapter performs glyph shaping and rasterization internally. A native shaped-run result (`glyph-id`, cluster, advance, offset) is still required for strict Text Engine conformance. |
| 006 §5.7: text box/wrapping | Implemented by adapter | Independent RGBA surfaces implement `none`, `word`, Unicode-grapheme, and dynamic-programming balanced wrapping plus `clip`, `ellipsis`, quarter-pixel binary-search `shrink`, and true `visible` overflow. Visible surfaces expand from measured glyph, stroke, blur, and shadow bounds while preserving the original box anchor; all constraints participate in deterministic layout digests. |
| 006 §5.8: Gaussian blurred text shadows | Implemented by adapter | The independent Text Engine emits a separate back-layer shadow event with explicit X/Y offset and Gaussian blur, preserving a sharp foreground glyph layer. |
| 006 §1, §9: image metadata and provenance | Implemented | `inspect` exposes normalized EXIF tags and GPS data; manifests record a privacy-reduced EXIF digest/summary without copying GPS coordinates. |
| 006 §6.6: subtitle burn-in and sidecar output | Implemented | Subtitle nodes may simultaneously burn in and atomically export deterministic UTF-8 SRT or WebVTT artifacts; sidecars are independent manifest targets. |

The current boundary intentionally permits replacing the libass adapter with a
native HarfBuzz/FreeType implementation without changing FFmpeg lowering.

## Module boundaries

- `vdls.py`: language frontend, Semantic AST, Render Graph, CLI and backend
  orchestration.
- `vdls_text_engine.py`: text request/layout contract and raster adapter.
- `vdls_plugin_host.py`: isolated length-prefixed JSON-RPC plugin transport.
- `vdls_ffmpeg_filters.py`: typed standard audio-filter lowering.

Further splitting of the frontend, Render Graph, and FFmpeg backend remains
desirable; new subsystem implementations should not be added to `vdls.py`.

## CLI status

| Requirement | Status | Implementation |
|---|---|---|
| 009 §3: project discovery precedence | Implemented | Upward discovery prefers `vdls.toml`, then `project.vdsl`, then `main.vdsl`; direct source paths anchor the project root. |
| 009 §5.1: atomic final publication | Implemented | Render output and emitted text artifacts are validated/written as temporary siblings and atomically replaced. |
| 009 §5.2: backend planning during `check` | Implemented | `check` lowers selected targets to backend plans and verifies detected capabilities without rendering final media. |
| 009 §7–8: machine-readable result envelope | Implemented | JSON success and failure paths emit one `vdls.cli-result/1` document with absolute project path, diagnostics, artifacts, targets, cache counts, and elapsed time. |
| 009 §9: manifest for every successful build | Implemented | `build` always atomically publishes `vdls.build-manifest/1`, recording source/asset/output digests, lock hash, fonts, plugins, backend version, and capability digest. |
| 009 §13: exit status mapping | Implemented for diagnostic families | Usage, source, asset, plugin, backend, output, config, cache, internal, capability, permission, timeout diagnostics map to the normative status table. |
| 009 §5.4: preview watch and resolution profile | Implemented | `--resolution WIDTHxHEIGHT` overrides selected preview targets while preserving timeline/layer semantics and tags outputs as non-conformant. `--watch` monitors the project tree (including modules, config, locale catalogs, local assets, and plugin manifests), ignores generated roots, debounces changes, and cancels superseded child builds with bounded interrupt/kill behavior. |
| 009 §12: bounded interrupt cleanup | Implemented for external tools | Shell-free child processes are supervised, terminate then kill after a bounded grace period, remove unpublished render/sidecar temporaries, and map interrupt/timeout to 130/124. |
| 009 §14: `--reproducible` mode | Implemented | The mode implies offline+locked, pins and verifies FFmpeg capabilities and file assets, rejects host-font fallback and mutable sources, fixes encoder threading and random seed, strips metadata, normalizes creation time, and records the evidence in the manifest. |

## Standard text helpers

| Requirement | Status | Implementation |
|---|---|---|
| 008 §16: grapheme-safe `typewriter` | Implemented | Unicode extended grapheme clusters are segmented with UAX #29 `\X`; timed prefixes are shaped independently and rendered through the Text Engine surface path. |
| 008 §16: `reveal-lines` | Implemented | Logical lines are revealed over an exact rational duration without entering `drawtext`. |
| 008 §16: `highlight-words` | Implemented | Ordered, non-overlapping rational word timings are validated and lowered to timed shaped-text highlight events. |
| 008 §16: `text-fade-in` | Implemented | Text alpha fades in over the declared rational duration in the raster adapter. |
| 008 §11–15: core media effects | Partial | Multiple positioned video layers and all 12 required blend modes, transforms, geometry/tone/blur/temperature/tint/4×5 color-matrix filters, chroma key with despill, alpha-from-luma, file/generated asset masks through explicit `scale2ref`/`alphamerge` ports, reverse, exact-frame `freezeframes`, duplicate/blend frame-rate conversion, explicit SDR output color conversion, trim/speed/fades, standard audio filters, and timeline-aware sidechain ducking lower to typed FFmpeg plans. Linear-light premultiplied compositing, masks sourced from arbitrary node outputs, and multi-pass loudness analysis remain tracked gaps. |
