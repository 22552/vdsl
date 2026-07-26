# 再利用可能な部品とモジュール

VDLSでは、複数のnodeやlayerを `component` としてまとめ、別ファイルからimportできます。

## componentを作る

```racket
(component lower-third
  ([name : String]
   [role : String]
   [accent : Color "#4fa3ffff"])

  (group
    (shape rect
      (size 760px 120px)
      (fill "#101010dd"))

    (text name
      (position 40px 28px)
      (font (size 38px)))

    (text role
      (position 42px 78px)
      (font (size 22px))
      (fill accent))))
```

使用例：

```racket
(lower-third
  (name "Hachinohe")
  (role "VDLS developer"))
```

## default引数

```racket
(component badge
  ([label : String]
   [color : Color "#ff3366ff"])
  ...)
```

`color` を省略するとdefault値が使われます。

## slot

```racket
(component panel
  ([title : String])
  (slot body : NodeList<Visual>)

  (group
    (text title)
    (slot-ref body)))
```

```racket
(panel
  (title "Result")
  (body
    (text "42")
    (shape rect ...)))
```

slotには型を付けられます。

## module

`components/titles.vdsl`：

```racket
#lang vdls

(module titles
  (export lower-third title-card)

  (define margin 24px)

  (component lower-third ...)
  (component title-card ...)

  (component internal-helper ...))
```

`internal-helper` はexportされていないため外部から参照できません。

## 名前空間付きimport

```racket
(import "components/titles.vdsl"
  (as titles))

(titles:lower-third
  (name "Alice")
  (role "Engineer"))
```

名前衝突を避けやすいため、共有ライブラリでは名前空間付きimportが推奨です。

## only

```racket
(import "components/titles.vdsl"
  (only lower-third))
```

## rename

```racket
(import "components/titles.vdsl"
  (rename [lower-third person-label]))
```

## include

```racket
(include "shared/constants.vdsl")
```

`include` は現在のmoduleへformを取り込みます。公開APIを明確にしたい場合はmodule/importを使います。

## 循環import

```text
a.vdsl -> b.vdsl -> c.vdsl -> a.vdsl
```

循環はcompile errorです。

## project root隔離

import pathはproject rootから外へ出られません。

```text
../../secret.vdsl
```

のような参照は拒否されます。

## 部品の設計指針

良いcomponent：

- 役割が1つ
- parameterが型付き
- default値が妥当
- 内部IDを外部へ漏らさない
- backend固有文字列を含めない
- output解像度を固定しすぎない

例：

- lower third
- title card
- subtitle panel
- profile card
- transition
- watermark
- chart block
- reusable audio chain

## Gitで管理する利点

componentは通常のVDLS sourceなので、

- diffが読める
- reviewできる
- versionを固定できる
- Web版とdesktop版で共有できる
- GUIから生成して手書き修正できる

という利点があります。
