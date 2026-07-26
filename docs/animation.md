# アニメーションとイージング

VDLSでは、時間に依存する値を有理数時刻と純粋式で扱います。

## from / to

```racket
(animate opacity
  (from 0)
  (to 1)
  (duration 1s)
  (easing smoothstep))
```

開始前は `from`、終了後は `to` になります。

## keyframe

```racket
(animate position.x
  (keyframes
    (0s   100px)
    (1s   900px (easing ease-out-cubic))
    (2.5s 500px (easing smoothstep))))
```

keyframe時刻は昇順である必要があり、同一時刻の重複は原則エラーです。

## 標準イージング

- `linear`
- `smoothstep`
- `smootherstep`
- `ease-in-quad`
- `ease-out-quad`
- `ease-in-out-quad`
- `ease-in-cubic`
- `ease-out-cubic`
- `ease-in-out-cubic`

## cubic-bezier

```racket
(easing
  (cubic-bezier 0.42 0 0.58 1))
```

x座標を数値的に反転して時間から進捗を求めます。y値をそのまま時間として扱いません。

参照実装では、決定的な固定LUTへ変換できます。

## spring

```racket
(easing
  (spring
    (mass 1)
    (stiffness 170)
    (damping 20)
    (initial-velocity 0)))
```

減衰が小さい場合はovershootできます。参照backendでは閉形式または決定的samplingへlowerします。

## 時間変数

```racket
(position
  (+ 640px (* 120px (sin t)))
  (+ 360px (* 40px (cos T))))
```

- `t`：node-local time
- `T`：project-global time
- `u`：animation progress
- `frame`：output frame index
- `fps`：frame rate

## 色の補間

color interpolationは既定でlinear RGB上で行います。encoded sRGBで補間する場合は明示的な指定が必要です。

## angle

angleは最短経路補間を利用できます。

```racket
(animate rotation
  (from 350deg)
  (to 10deg)
  (duration 1s))
```

## text helper

```racket
(typewriter 2s)
(reveal-lines 3s)
(highlight-words timings)
(text-fade-in 500ms)
```

`typewriter` はUnicode code pointではなくgrapheme cluster単位で進みます。

## 決定的な動き

```racket
(position
  (* width  (random 10 0))
  (* height (random 10 1)))
```

同じproject seed、node、frame、stream、saltなら同じ値になります。

## preview時の確認

```powershell
python vdls.py render-frame project.vdsl --target main --time 1.5s --output frame.png
python vdls.py preview project.vdsl --resolution 640x360 --watch
```
