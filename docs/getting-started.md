# クイックスタート

このページでは、VDLSの参照実装を動かして最初の動画を生成します。

## 必要なもの

- Python 3.11以上
- FFmpeg
- FFprobe
- VDLSリポジトリ

PythonとFFmpegが使えることを確認します。

```powershell
python --version
ffmpeg -version
ffprobe -version
```

## リポジトリを取得

```powershell
git clone https://github.com/22552/vdsl.git
cd vdsl
```

必要なPython依存関係は `pyproject.toml` に従って導入します。

```powershell
python -m pip install -e .
```

直接 `vdls.py` を実行するだけでも利用できます。

## 付属exampleを確認

```powershell
python vdls.py check examples/hello.vdsl
```

成功すれば、sourceの読取り、型検査、Render Graph計画、FFmpeg capability確認まで完了しています。

## 最初のbuild

```powershell
python vdls.py build examples/hello.vdsl --no-cache
```

成果物はsourceやoutput設定に応じて `build/` などへ生成されます。

## 自分のファイルを作る

`hello-ja.vdsl` を作成します。

```racket
#lang vdls

(project
  (id "hello-ja")
  (build-options (seed 2026))

  (output
    (id main)
    (file "build/hello-ja.mp4")
    (video
      (width 1280px)
      (height 720px)
      (frame-rate 30fps)))

  (asset
    (id background)
    (generated
      (solid-color "#182033")))

  (scene
    (id main-scene)
    (duration 4s)

    (layer
      (video background))

    (layer
      (text "こんにちは、VDLS"
        (position 640px 360px)
        (anchor center)
        (font
          (family "Noto Sans CJK JP")
          (size 72px))
        (fill "#ffffffff")
        (stroke "#000000ff" 3px)))))
```

確認：

```powershell
python vdls.py check hello-ja.vdsl
```

build：

```powershell
python vdls.py build hello-ja.vdsl --no-cache
```

## 1フレームだけ確認

```powershell
python vdls.py render-frame hello-ja.vdsl `
  --target main `
  --time 2s `
  --output build/hello-ja-frame.png
```

全体を毎回encodeせず、位置やtext styleを確認できます。

## animationを加える

textへopacity animationを追加します。

```racket
(text "こんにちは、VDLS"
  (position 640px 360px)
  (anchor center)
  (animate opacity
    (from 0)
    (to 1)
    (duration 1s)
    (easing smoothstep)))
```

VDLSでは時間を有理数として扱うため、frame境界が安定します。

## 次に試すexample

- `examples/audio.vdsl`：音声
- `examples/animation.vdsl`：animation
- `examples/subtitles.vdsl`：字幕
- `examples/video-layers.vdsl`：複数映像layer
- `examples/global-timeline.vdsl`：project-global timeline
- `examples/layout-stack-grid.vdsl`：stack/grid
- `examples/lisp-core.vdsl`：Lisp、for、component、module
- `examples/reproducible.vdsl`：再現可能build

## 開発中の基本ループ

```text
sourceを編集
  -> check
  -> render-frame または preview
  -> build
```

```powershell
python vdls.py check project.vdsl
python vdls.py preview project.vdsl --resolution 640x360 --watch
python vdls.py build project.vdsl --target main
```

次は [言語の基本](language.md) を参照してください。
