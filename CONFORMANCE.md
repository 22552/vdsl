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
| 006 §5: shaping and rendering are separate stages | **Partial** | `TextRequest -> TextLayout -> ShapedRun -> surface` is explicit. `ShapedRun` exposes real font cmap glyph IDs, half-open clusters, advances and offsets before rasterization, including TrueType Collections. The reference fallback does not yet perform contextual GSUB/GPOS itself; libass remains authoritative for complex-script substitutions at the surface adapter boundary. |
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

## Project-global timeline extension

The implementation also defines a backwards-compatible `ProjectTimeline`
extension above the Draft 1.0 scene model:

- sources without `(timeline ...)` receive an implicit sequential timeline,
  while outputs retain the legacy selected-scene behavior;
- an explicit timeline places finite scenes at project-global rational times;
- project-scoped video, text, subtitle, watermark, and audio layers share the
  same time domain and may span scene boundaries;
- timeline markers are typed `timeline-metadata` Render Graph outputs wired to
  the muxer and lower to validated MP4 chapters;
- project subtitle cues are normalized to global timestamps before burn-in and
  deterministic SRT/WebVTT sidecar export.

`examples/global-timeline.vdsl` is the executable compatibility and lowering
example. It renders two sequential scenes under one BGM/watermark, carries
subtitles across the scene boundary, and emits two chapters.

## Lisp language status (001 + 011)

| Requirement | Status | Implementation |
|---|---|---|
| 011 §2–8: finite pure compile-time evaluation | Implemented | The independent `vdls_lisp.py` stage evaluates lexical closures, typed/non-typed `lambda`, `define`, `let`, `let*`, `if`, short-circuit `and`/`or`, and `cond` before Semantic AST construction. |
| 011 §3, §9: finite lists and higher-order operations | Implemented | Immutable lists, `cons`/`first`/`rest`/`length`/`append`/`range`, `map`, `list-filter`, folds, `flat-map`, `take`, `drop`, and `enumerate` are bounded and dimension checked. Parallel length mismatches use `VDLS-LISP-021`. |
| 011 §10: compile-time iteration | Implemented | `for/list`, `for*/list`, and `for/fold` expand finite sequences. Step, iteration, depth, and expanded-node limits use `VDLS-LISP-030`. |
| 011 §11: typed components and slots | Implemented | Named/defaulted typed parameters and typed `NodeList` slots expand before AST construction with stable `VDLS-LISP-040..043` diagnostics. |
| 011 §12: modules | Implemented | Explicit exports, namespaced `(as ...)`, `(only ...)`, `(rename ...)`, `include`, root isolation, content digests, private-name rewriting, and cycle detection are supported. Legacy flat imports remain compatible. |
| 011 §14: deterministic random streams | Implemented for scalar properties | `random` and vector-component extraction from `random2/3/4` lower from `(project-seed,node-id,frame-index,stream,salt,component)` without traversal or thread state. `r` remains the stream-zero shorthand. |
| 011 §13, §17: hygiene and identity | **Partial** | Expansion uses lexical environments, private module renaming, deterministic node allocation, explicit resource limits, and records definition/component/module/limit identity. Full call-site-derived local IDs and multi-location expansion traces remain to be added. |

`examples/lisp-core.vdsl` exercises functions, typed closures, higher-order
lists, iteration, typed components, and a namespaced imported module, then
builds the expanded result through the ordinary AST/Graph/backend pipeline.

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
| 008 §7: `cubic-bezier` and `spring` easing | Implemented | Bézier x-coordinates are numerically inverted at compile time into a fixed deterministic LUT; spring lowers to a damped closed-form FFmpeg expression and may overshoot when under-damped. |
| 008 §17: karaoke cues | Implemented | Inline contiguous karaoke segments validate as half-open cue ranges, burn in through deterministic ASS `\k` events, and export plain UTF-8 SRT/WebVTT sidecars. |
| 008 §11–15: core media effects | Partial | Multiple positioned video layers and all 12 required blend modes, transforms, geometry/tone/blur/temperature/tint/4×5 color-matrix filters, chroma key with despill, alpha-from-luma, file/generated asset masks through explicit `scale2ref`/`alphamerge` ports, reverse, exact-frame `freezeframes`, duplicate/blend frame-rate conversion, explicit SDR output color conversion, trim/speed/fades, standard audio filters, and timeline-aware sidechain ducking lower to typed FFmpeg plans. Non-normal blend modes decode sRGB to linear light before blend math and explicitly normalize through premultiplied-alpha surfaces. Masks sourced from arbitrary node outputs and multi-pass loudness analysis remain tracked gaps. |
