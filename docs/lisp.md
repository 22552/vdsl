# Lisp機能とコンパイル時展開

VDLSの `001 + 011` は、合わせて1つのVDLS Lisp言語を構成します。Racketそのものを実行するのではなく、有限・純粋・決定的な独自Lispとして実装されています。

## 何のためのLispか

主な目的は次のとおりです。

- 共通計算を関数へまとめる
- 複数nodeやlayerをcomponentとして再利用する
- `for` で大量のnodeを生成する
- module/importで部品ライブラリを作る
- 同じseedから再現可能な乱数系列を作る

任意のファイルI/O、ネットワーク、プロセス実行、状態変更はできません。

## define

値を定義できます。

```racket
(define margin 24px)
(define title-size 72px)
```

関数も定義できます。

```racket
(define (label value x)
  (text value
    (position x 80px)))
```

これは `lambda` を使った定義の糖衣構文です。

```racket
(define label
  (lambda (value x)
    (text value
      (position x 80px))))
```

## 型注釈

公開関数やcomponentには型を付けられます。

```racket
(define (fade-value
         [time : Time]
         [start : Time]
         [duration : Time])
  : Number
  (clamp (/ (- time start) duration) 0 1))
```

## letとlet*

```racket
(let ([x 100px]
      [y 200px])
  (position x y))
```

`let` の初期化式は外側のscopeで評価されます。

```racket
(let* ([base 24px]
       [double (* base 2)])
  (text "Size"
    (font (size double))))
```

`let*` は上から順に束縛が見えるようになります。

## 条件分岐

```racket
(if (> width 1000px)
    72px
    48px)
```

```racket
(cond
  [(< T 1s) "開始"]
  [(< T 3s) "途中"]
  [else "終了"])
```

`and` と `or` は短絡評価します。

## immutable list

```racket
(list 10 20 30)
(cons 0 (list 10 20 30))
(first values)
(rest values)
(length values)
(append a b)
(range 5)
(range 2 10 2)
```

`range` の終端は含みません。

## 高階関数

```racket
(map square values)
(list-filter positive? values)
(foldl + 0 values)
(foldr combine empty values)
(flat-map make-layers items)
(take values 3)
(drop values 3)
(enumerate values)
```

複数listへ `map` する場合は長さが一致している必要があります。

## for/list

```racket
(for/list ([i (range 5)])
  (* i i))
```

nodeを複数生成する例：

```racket
(for/list ([i (range 5)])
  (text "●"
    (position
      (+ 80px (* i 120px))
      240px)))
```

## for*/list

複数sequenceの直積を生成します。

```racket
(for*/list ([x (range 4)]
            [y (range 3)])
  (text "□"
    (position
      (+ 60px (* x 100px))
      (+ 60px (* y 100px)))))
```

## for/fold

```racket
(for/fold ([total 0])
          ([value values])
  (+ total value))
```

## 展開制限

コンパイル時評価は有限でなければなりません。処理系は少なくとも次を制限します。

- 最大反復回数
- 最大展開node数
- 最大展開深度
- 最大評価step数

制限を超えた場合は `VDLS-LISP-030` になります。

## component

複数nodeやlayerを引数付き部品へまとめられます。

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

componentはSemantic AST構築前に型検査・展開されます。

## slot

componentへ任意の子node列を渡せます。

```racket
(component card
  ([title : String])
  (slot body : NodeList<Visual>)

  (group
    (text title)
    (slot-ref body)))
```

```racket
(card
  (title "Result")
  (body
    (text "42")
    (shape rect ...)))
```

## moduleとexport

```racket
(module titles
  (export lower-third title-card)

  (component lower-third ...)
  (component title-card ...)
  (define private-margin 24px))
```

外部へ公開されるのは `export` した名前だけです。

## import

名前空間付きimport：

```racket
(import "components/titles.vdsl"
  (as titles))

(titles:lower-third
  (name "Alice")
  (role "Engineer"))
```

必要な名前だけ：

```racket
(import "components/titles.vdsl"
  (only lower-third title-card))
```

rename：

```racket
(import "components/titles.vdsl"
  (rename [lower-third person-label]))
```

project root外の参照は禁止され、循環importも検出されます。

## include

`include` は別ファイルのformを現在のmoduleへ取り込みます。名前空間を分けたい場合は `import` を使います。

## 決定的乱数

```racket
(for/list ([i (range 100)])
  (shape circle
    (position
      (* width  (random i 0))
      (* height (random i 1)))
    (radius
      (+ 2px (* 8px (random i 2))))))
```

各粒子で独立したstreamを使えるため、`r` 1個だけを共有する問題がありません。

## 評価段階

```text
reader
  -> module解決
  -> syntax/component展開
  -> compile-time Lisp評価
  -> Semantic AST
  -> frame式評価
  -> pixel/sample式評価
  -> backend lowering
```

コンパイル時にはnode構造を生成できます。フレーム時には既存プロパティ値だけを評価し、新しいnodeやsceneを生成できません。

次は [再利用可能な部品とモジュール](components.md) を参照してください。
