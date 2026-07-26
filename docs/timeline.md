# タイムラインとレンダリング

VDLSでは、scene-localな編集とproject-globalなtimelineを分けて扱います。

- `scene`：1つの局所的な編集単位
- `timeline`：複数sceneや全編素材をproject全体の時刻へ配置する構造

## scene-local時間

scene内では時刻0から始まります。

```racket
(scene
  (id opening)
  (duration 3s)
  (layer
    (text "Opening"
      (position 640px 360px))
    (start 500ms)
    (duration 2s)))
```

このtextの `t` は、layerが始まった時点を基準とするローカル時間です。

## project-global timeline

```racket
(timeline
  (scene opening (start 0s))
  (scene main    (start 3s))

  (layer
    (audio music)
    (start 0s)
    (duration 12s))

  (layer
    (text "VDLS Demo"
      (position 24px 24px))
    (start 0s)
    (duration 12s))

  (marker
    (id chapter-main)
    (time 3s)
    (label "Main")))
```

この例では、

- `opening` が0秒から
- `main` が3秒から
- BGMとwatermarkが全編
- 3秒地点にchapter marker

として配置されます。

## `t` と `T`

- `t`：現在のnodeまたはlayerのローカル時刻
- `T`：output全体のproject-global時刻

```racket
(position
  (+ 100px (* 50px (sin t)))
  (+ 100px (* 20px (sin T))))
```

局所animationには `t`、全編同期させたい処理には `T` が向いています。

## sceneを順番に並べる

開始時刻を省略できる構文が実装で許可されている場合、前のscene終了時刻から順番に配置されます。明示する場合は、すべて有理数時刻として扱われます。

```racket
(timeline
  (scene intro (start 0s))
  (scene body  (start 4s))
  (scene outro (start 14s)))
```

scene同士を重ねることもできます。

```racket
(timeline
  (scene background (start 0s))
  (scene overlay    (start 2s)))
```

## 全編BGMとsidechain ducking

```racket
(timeline
  (scene main (start 0s))
  (layer
    (audio music
      (gain -10dB))
    (start 0s)
    (duration 30s)))
```

voiceとBGMを同じtimelineへ配置すれば、timeline-awareなduckingへlowerできます。

## 全編watermark

```racket
(layer
  (text "SAMPLE"
    (position 1850px 40px)
    (anchor top-right)
    (opacity 0.5))
  (start 0s)
  (duration 30s))
```

textだけでなく、imageやvideoもglobal layerとして配置できます。

## 字幕

subtitle cueはglobal timestampへ正規化されます。

```racket
(layer
  (subtitles captions
    (burn-in true)
    (sidecar webvtt))
  (start 0s)
  (duration 30s))
```

字幕は次のいずれか、または両方を生成できます。

- 動画へのburn-in
- UTF-8 SRT sidecar
- WebVTT sidecar

## markerとchapter

```racket
(marker
  (id opening)
  (time 0s)
  (label "Opening"))

(marker
  (id result)
  (time 12s)
  (label "Result"))
```

FFmpeg backendでは、検証済みmetadataとしてmuxerへ渡され、対応containerではchapterになります。

## Render Graph上の扱い

明示的timelineは `core/project-timeline` にlowerされます。

```text
scene placement ----+
global video -------+--> project timeline --> composite --> encode
watermark -----------+
global audio -----------------------------> mix --> encode
markers ----------------------------------> timeline metadata --> mux
```

字幕sidecarは動画本体とは別のartifact targetです。

## layoutとの組合せ

`stack` や `grid` はtarget解像度をもとに配置を解決します。

```racket
(grid
  (columns 2)
  (gap 20px)
  (padding 24px)
  (text "A")
  (text "B")
  (text "C")
  (text "D"))
```

layout nodeはbackend依存文字列ではなくSemantic ASTに残り、FFmpeg lowering前に明示的な矩形へ解決されます。

## 従来projectとの互換性

`timeline` のない既存ソースは壊れません。処理系は暗黙のtimelineを作成し、従来のoutput scene選択を保持します。

次は [アニメーションとイージング](animation.md) または [映像・音声・字幕](media.md) を参照してください。
