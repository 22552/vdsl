# 言語の基本

VDLSソースはUTF-8で記述し、通常は先頭に `#lang vdls` を置きます。展開後のファイルには、ちょうど1つの `project` が存在します。

```racket
#lang vdls

(project
  (id "hello")
  (build-options (seed 2026))

  (output
    (id main)
    (file "build/hello.mp4")
    (video
      (width 1280px)
      (height 720px)
      (frame-rate 30fps)))

  (asset
    (id background)
    (generated
      (solid-color "#182033")))

  (scene
    (id opening)
    (duration 3s)
    (layer
      (video background))
    (layer
      (text "Hello, VDLS"
        (position 640px 360px)
        (anchor center)))))
```

## project

`project` はビルド全体のルートです。主に次を含みます。

- `id`：プロジェクト識別子
- `build-options`：seed、再現性などのビルド設定
- `output`：生成するファイル
- `asset`：動画、音声、画像、フォント、字幕、生成素材
- `scene`：scene-localな動画構造
- `timeline`：project-globalな配置

## output

1つのprojectに複数のoutputを定義できます。

```racket
(output
  (id landscape)
  (file "build/landscape.mp4")
  (video
    (width 1920px)
    (height 1080px)
    (frame-rate 30fps)))

(output
  (id vertical)
  (file "build/vertical.mp4")
  (video
    (width 1080px)
    (height 1920px)
    (frame-rate 60fps)))
```

純粋な共通処理はRender Graph上で共有でき、output固有の変換やencodeだけを分岐できます。

## asset

assetは再利用可能な素材です。

```racket
(asset
  (id main-video)
  (file "assets/main.mp4"))

(asset
  (id music)
  (file "assets/bgm.wav"))

(asset
  (id bg)
  (generated
    (solid-color "#10141f")))
```

参照時はasset IDを使います。backendへ落とすまで、ソース内の構造と実ファイル処理は分離されます。

## sceneとlayer

sceneはローカル時間を持つ編集単位です。layerは重なり順と時間範囲を持ちます。

```racket
(scene
  (id intro)
  (duration 5s)

  (layer
    (video main-video
      (trim 0s 5s)))

  (layer
    (text "Introduction"
      (position 80px 80px))
    (start 500ms)
    (duration 4s)))
```

layerの主な設定は次のとおりです。

- `start`
- `duration`
- z-order
- opacity
- blend mode
- transform
- filter/effect
- animation
- mask

## 単位

時間・長さ・角度・音量などは型付き単位として扱われます。

```text
3s        3秒
250ms     0.25秒
12f       12フレーム
640px     640ピクセル
30fps     30フレーム毎秒
90deg     90度
-6dB      -6デシベル
440Hz     440ヘルツ
```

VDLSは可能な限り有理数を保持します。フレーム時刻やdurationを浮動小数点の累積で計算しないため、長いタイムラインでもずれにくくなっています。

## 式

位置、opacity、filter値、animationなどには純粋な式を使用できます。

```racket
(position
  (* width 0.5)
  (+ 120px (* 40px (sin t))))
```

標準変数：

- `t`：現在ノードのローカル時刻
- `T`：project/output全体の時刻
- `u`：animation進捗 `[0,1]`
- `frame`：0始まりの出力フレーム番号
- `fps`：出力フレームレート
- `width` / `height`：現在surfaceの大きさ
- `input-width` / `input-height`：入力素材の大きさ
- `r`：現在ノード・現在フレーム用の決定的乱数

式は純粋です。任意のファイルI/O、ネットワーク、プロセス実行、時計参照、状態変更はできません。

## 決定的乱数

```racket
(position
  (* width (random 0 0))
  (* height (random 1 0)))
```

利用可能な関数：

```racket
(random stream salt)
(random2 stream salt)
(random3 stream salt)
(random4 stream salt)
```

乱数はproject seed、node ID、frame、stream、salt、vector componentから導出されます。スレッド順やグラフ走査順に依存しません。

`r` は互換用の省略形で、stream 0のscalar乱数に相当します。

## テキスト

```racket
(text "こんにちは"
  (position 640px 360px)
  (anchor center)
  (font
    (family "Noto Sans CJK JP")
    (size 72px)
    (weight 700))
  (fill "#ffffffff")
  (stroke "#000000ff" 4px)
  (shadow 0px 6px 14px "#00000080"))
```

テキストは独立したText Engineを通ります。FFmpeg `drawtext` を規範経路として使わず、字形整形、bidi、折返し、stroke、shadowなどを独立surfaceとして扱います。

## 従来形式との互換性

明示的な `timeline` を持たないprojectも有効です。その場合、sceneは従来どおり選択または連結され、内部では暗黙の連続timelineとして正規化されます。

次は [タイムラインとレンダリング](timeline.md) を参照してください。
