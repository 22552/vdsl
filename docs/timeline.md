# Timeline and rendering

A scene is a local subtree. A `timeline` adds project-global placement, so
scenes and global media can overlap or span the whole output.

```racket
(timeline
  (scene opening (start 0s))
  (scene main (start 3s))
  (layer (audio music) (start 0s) (duration 8s))
  (layer (text "Demo" (position 20px 20px)) (start 0s) (duration 8s))
  (marker (id chapter-1) (time 3s) (label "Main")))
```

Global text and video layers can act as watermarks. Subtitle layers may be
burned in, exported as SRT/WebVTT sidecars, or both. Markers lower to
container chapters in the FFmpeg backend.

Use `stack` and `grid` nodes when child placement should be calculated from a
layout. The compiler keeps those as semantic layout nodes, then resolves them
to target dimensions during FFmpeg lowering.
