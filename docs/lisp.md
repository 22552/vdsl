# Compile-time Lisp layer

VDLS includes a finite, pure Lisp layer for generating VDLS forms at
compile time. It is not a runtime scripting environment: evaluation is
bounded and has no arbitrary file, network, or process access.

Available building blocks include lexical `define`, `lambda`, `let`, `let*`,
`if`, `cond`, immutable lists, `range`, `map`, filters, folds, and bounded
`for/list`, `for*/list`, and `for/fold` forms.

```racket
(define (label value x)
  (text value (position x 80px)))

(for/list ([i (range 3)])
  (label i (* i 180px)))
```

`component` declarations package typed, defaulted reusable graph fragments.
Modules explicitly export names and can be imported with `as`, `only`, and
`rename`. Import resolution is confined to the project root and detects cycles.

`random`, `random2`, `random3`, and `random4` are deterministic stream
functions. `r` is the stream-zero scalar shorthand.
