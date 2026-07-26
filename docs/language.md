# Language overview

Every source starts with `#lang vdls` and contains one `project` form. A
project declares assets, scenes, outputs, and optionally a global `timeline`.

```racket
#lang vdls
(project
  (id "intro")
  (build-options (seed 2026))
  (output (id main) (video (width 1280px) (height 720px) (frame-rate 30fps)))
  (asset (id background) (generated (solid-color "#182033")))
  (scene (id opening) (duration 3s)
    (layer (video background))
    (layer (text "Hello, VDLS" (position 640px 360px) (anchor center)))))
```

Time and dimensions are exact rational values. Use units such as `3s`, `12f`,
`640px`, `30fps`, `-3dB`, and `90deg` where the property requires one.

Layers accept timing, opacity, transforms, effects, animations, masks, and
blend modes. Text uses the independent Text Engine; it does not lower through
FFmpeg `drawtext`.

`r` is a deterministic pseudo-random scalar for the current frame. It is
derived from the project seed, node identity and frame index, so rebuilding
the same source gives the same motion.
