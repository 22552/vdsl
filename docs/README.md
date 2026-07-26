# VDLS 日本語ドキュメント

VDLS（Video Description Lisp）は、動画・音声・字幕・文字・アニメーションをS式で記述し、型付きSemantic ASTとRender Graphを経由してFFmpegなどのバックエンドへ変換するメディア向けLispです。

このディレクトリは、実際にVDLSを使う人向けの日本語ガイドです。厳密な規範仕様は [`22552/docssss`](https://github.com/22552/docssss) を参照してください。

## 最初に読むページ

1. [クイックスタート](getting-started.md) — インストールから最初の動画生成まで
2. [言語の基本](language.md) — project、asset、scene、layer、単位、式
3. [タイムラインとレンダリング](timeline.md) — 複数scene、全編BGM、字幕、chapter
4. [Lisp機能と部品化](lisp.md) — 関数、for、component、module/import
5. [CLIリファレンス](cli.md) — check、build、preview、graph、inspect

## 機能別ガイド

- [映像・音声・字幕](media.md)
- [アニメーションとイージング](animation.md)
- [レイアウトと複数レイヤー](layout.md)
- [再利用可能な部品とモジュール](components.md)
- [再現可能ビルドとキャッシュ](reproducible-builds.md)
- [トラブルシューティング](troubleshooting.md)

## 最小実行例

```powershell
python vdls.py check examples/hello.vdsl
python vdls.py build examples/hello.vdsl --no-cache
```

`check` はソース、アセット、FFmpegの能力を確認します。動画は生成しません。`build` は指定された出力をレンダリングし、通常は `build/` 以下へ成果物を配置します。

## VDLSの処理モデル

```text
VDLS source
  -> Lisp展開・型検査
  -> Semantic AST
  -> Render Graph
  -> FFmpeg / GPU / plugin backend
  -> 動画・音声・字幕・manifest
```

## 実行可能な例

[`../examples`](../examples) には、基本動画、音声処理、字幕、複数動画レイヤー、global timeline、Lisp機能、レイアウト、再現可能ビルドなどの例があります。

実装済み機能と未完成部分は [`../CONFORMANCE.md`](../CONFORMANCE.md) にまとまっています。
