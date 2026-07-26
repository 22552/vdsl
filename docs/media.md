# 映像・音声・字幕

このページでは、VDLSで扱える主要なmedia機能をまとめます。

## 映像source

```racket
(video main-video
  (trim 2s 12s)
  (speed 1.25)
  (position 0px 0px)
  (scale 1 1)
  (opacity 1))
```

主な操作：

- trim
- speed
- position / anchor / scale / rotation / skew
- crop / pad / flip
- opacity
- reverse
- freeze-frame
- frame-rate変換

## 複数layer

```racket
(scene
  (id composite)
  (duration 6s)

  (layer
    (video background))

  (layer
    (video foreground
      (position 960px 540px)
      (anchor center)
      (scale 0.5 0.5)
      (opacity 0.85)
      (blend screen))))
```

layerはz-orderとsource orderに従って合成されます。

## blend mode

主なmode：

```text
normal multiply screen overlay darken lighten
color-dodge color-burn hard-light soft-light
difference exclusion
```

合成は原則としてlinear-light、premultiplied alphaで行われます。

## filter

```racket
(video main-video
  (filters
    (scale 1280px 720px)
    (gaussian-blur 4px)
    (contrast 1.1)
    (saturation 0.9)))
```

主なvisual filter：

- brightness
- contrast
- saturation
- exposure
- gamma
- hue
- temperature
- tint
- invert
- grayscale
- color-matrix
- gaussian-blur
- box-blur
- unsharp
- chroma-key
- alpha-from-luma
- reverse
- freeze-frame
- frame-rate duplicate/blend

filterは左から右へ適用されます。

## mask

```racket
(video foreground
  (mask mask-image))
```

file assetやgenerated assetをmaskとして使えます。alpha-from-lumaと組み合わせることもできます。

## chroma key

```racket
(video green-screen
  (filters
    (chroma-key "#00ff00"
      (similarity 0.1)
      (smoothness 0.08)
      (spill 0.05))))
```

## 色空間

VDLSは色変換を明示的に扱います。

- working color space
- transfer function
- primaries
- matrix/range
- alpha mode

backendは、意味が同じと証明できる場合だけ変換を省略できます。

## 音声

```racket
(audio music
  (trim 0s 30s)
  (gain -8dB)
  (pan 0)
  (filters
    (high-pass 80Hz)
    (compressor
      (threshold -18dB)
      (ratio 4)
      (attack 10ms)
      (release 100ms))))
```

主なaudio機能：

- gain / volume
- pan
- fade-in / fade-out
- high-pass / low-pass
- EQ
- compressor
- limiter
- resample
- mix
- loudness normalization
- sidechain ducking
- tone生成
- silence生成

## mix

```racket
(mix
  (track voice (gain 0dB))
  (track bgm   (gain -12dB)))
```

## ducking

```racket
(duck bgm
  (sidechain voice)
  (amount 9dB)
  (attack 50ms)
  (release 300ms))
```

project-global timelineへvoiceとBGMを配置すると、scene境界をまたいだduckingも可能です。

## text

```racket
(text "Hello"
  (font
    (family "Noto Sans")
    (size 72px)
    (weight 700))
  (fill "#ffffffff")
  (stroke "#000000ff" 4px)
  (shadow 0px 4px 12px "#00000080")
  (box
    (width 900px)
    (wrap word)
    (overflow shrink)))
```

Text Engineは次を扱います。

- Unicode normalization
- bidi
- complex shaping
- font file pinning
- glyph ID / cluster / advance / offset
- word / grapheme / balanced wrapping
- clip / ellipsis / shrink / visible overflow
- stroke
- Gaussian blur shadow

## subtitle

```racket
(subtitles captions
  (style youtube-caption)
  (burn-in true)
  (sidecar webvtt))
```

対応形式：

- SRT
- WebVTT
- burn-in
- sidecar export
- project-global timestamp正規化

## karaoke

```racket
(cue 0s 2s
  (karaoke
    (segment 0s 500ms "こ")
    (segment 500ms 1s "ん")
    (segment 1s 2s "にちは")))
```

karaoke segmentは順序付き・非重複の半開区間として検証されます。burn-inではASS karaoke eventへlowerでき、sidecarでは通常textとして出力されます。

## output検証

render後はFFprobeで次を確認します。

- fileが存在する
- sizeが0ではない
- 必要なstreamがある
- width / height
- frame rate
- duration

検証後にatomic publishされます。
