# レイアウトと複数レイヤー

VDLSのlayout機能は、複数のnodeをtarget解像度に応じて配置します。layoutは単なる見た目用macroではなく、Semantic AST上の意味を持つnodeとして保持できます。

## stack

縦方向：

```racket
(stack vertical
  (gap 16px)
  (padding 24px)
  (text "Title")
  (text "Description")
  (text "Footer"))
```

横方向：

```racket
(stack horizontal
  (gap 20px)
  (text "A")
  (text "B")
  (text "C"))
```

## grid

```racket
(grid
  (columns 3)
  (gap 12px)
  (padding 24px)
  (text "1")
  (text "2")
  (text "3")
  (text "4")
  (text "5")
  (text "6"))
```

## alignment

```racket
(align center)
(align top-left)
(align bottom-right)
```

anchorとlayout alignmentは別概念です。

- layout alignment：割り当てられた矩形内での配置
- anchor：node自身の座標基準

## marginとpadding

```racket
(margin 24px)
(padding 16px)
```

- margin：外側の空間
- padding：container内側の空間

## fit

```racket
(fit contain)
(fit cover)
(fit stretch)
```

- `contain`：全体を収め、余白を許す
- `cover`：領域を埋め、はみ出しを許す
- `stretch`：縦横比を維持せず領域へ合わせる

## safe area

```racket
(safe-area title-safe)
```

字幕、番組タイトル、縦動画UIとの衝突回避などに利用できます。

## Render Graphへのlowering

```text
LayoutGroup
  -> core/layout-stack または core/layout-grid
  -> target解像度を参照
  -> 子nodeごとの明示矩形
  -> transform/composite
```

FFmpeg filter文字列へ直接変換する前に配置が解決されるため、GPU backendやGUIでも同じlayout意味を共有できます。

## componentとの組合せ

```racket
(component profile-grid
  ([people : List<Person>])
  (grid
    (columns 2)
    (gap 20px)
    (for/list ([person people])
      (profile-card person))))
```

## targetごとの解像度

同じlayout sourceを横動画と縦動画へ使う場合、target解像度ごとに矩形を解決します。

```racket
(output
  (id landscape)
  (video (width 1920px) (height 1080px)))

(output
  (id vertical)
  (video (width 1080px) (height 1920px)))
```

layoutの意味は共通ですが、最終矩形は異なります。

## debug

Render Graphを表示します。

```powershell
python vdls.py graph examples/layout-stack-grid.vdsl --format json
```

静止画確認：

```powershell
python vdls.py render-frame examples/layout-stack-grid.vdsl `
  --target main --time 0s --output layout.png
```
