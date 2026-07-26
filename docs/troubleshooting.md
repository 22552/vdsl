# トラブルシューティング

## まず実行するコマンド

```powershell
python vdls.py check project.vdsl
```

machine-readable診断：

```powershell
python vdls.py --diagnostic-format json check project.vdsl
```

cacheを無視したbuild：

```powershell
python vdls.py build project.vdsl --no-cache
```

## FFmpegが見つからない

確認：

```powershell
ffmpeg -version
ffprobe -version
```

両方が `PATH` 上に必要です。Scoop、Homebrew、aptなどで導入した場合、terminalを開き直してください。

## 必要なfilterやencoderがない

VDLSはbuild前にFFmpegの次をprobeします。

- version
- filters
- encoders
- decoders
- pixel formats

必要な能力がない場合、最終renderを開始する前にcapability errorになります。別のFFmpeg buildを利用するか、対象機能を変更してください。

## source parse error

括弧の対応、文字列quote、未知のformを確認します。

```racket
(text "Hello")
```

portable VDLSでは、Racketのquote、quasiquote、vector、reader extensionなどは使えません。

## undefined identifier

典型例：

- asset IDの綴り違い
- module namespaceの付け忘れ
- exportされていない名前を参照
- lexical scope外の変数

```racket
(import "components/titles.vdsl" (as titles))
(titles:lower-third ...)
```

## 単位の型エラー

次のような演算はできません。

```racket
(+ 2s 10px)
```

Time、Length、Angle、Gainなどは暗黙変換されません。

## durationを決められない

node、layer、scene、assetのどこからも有限durationを推論できない場合はerrorです。

対策：

```racket
(duration 5s)
```

を適切なscene、layer、generated assetなどへ追加します。

## import cycle

```text
a.vdsl -> b.vdsl -> c.vdsl -> a.vdsl
```

module依存を一方向へ整理し、共通定義を別moduleへ分離します。

## Lisp展開制限

大量の `for/list`、深いcomponent展開、停止しない再帰相当の構造は `VDLS-LISP-030` になる可能性があります。

- iteration数を減らす
- 共通処理をlayoutやGPU-native nodeへ置き換える
- 展開されるnode数を確認する

## parallel mapの長さ不一致

複数listを同時に `map` またはparallel `for/list` する場合、長さを一致させます。

## textが期待どおり表示されない

確認項目：

- font fileが存在するか
- font family名が正しいか
- fallbackに依存していないか
- box幅とoverflow設定
- wrap mode
- language/direction
- stroke/shadowによるbounds拡大

再現可能buildではhost font fallbackが拒否されることがあります。

## 字幕がずれる

- scene-local cueかproject-global cueか確認
- timeline layerのstartを確認
- cueが半開区間 `[start,end)` になっているか確認
- speed/trim後のsource timeとplacement timeを混同していないか確認

## chapterが出ない

- markerがtimelineにあるか
- containerがchapter metadataに対応しているか
- muxerへ `timeline-metadata` が接続されているか
- `--emit graph-json` や `--emit commands` で確認

## outputが0 byteまたは壊れている

VDLSはpublish前にFFprobe検証します。失敗した場合、temporary outputは最終pathへ置換されません。

確認：

```powershell
python vdls.py build project.vdsl --no-cache --emit commands
```

生成されたargvとfilter scriptを確認します。

## cacheが怪しい

```powershell
python vdls.py build project.vdsl --no-cache
```

で再現するか確認します。再現しなければcache identityまたは古いartifactの問題です。

## lockfile mismatch

FFmpeg、plugin、asset、fontを意図的に更新した場合：

```powershell
python vdls.py lock --project .
```

意図しない変更なら、環境をlockfileへ戻してください。

## previewが重い

```powershell
python vdls.py preview project.vdsl --resolution 640x360 --watch
```

- preview解像度を下げる
- asset proxyを使う
- expensive filterを一時的に無効化する
- render-frameで必要時刻だけ確認する

## timeoutまたはinterrupt

外部processはbounded cleanupされます。

- timeout：通常終了コード124
- Ctrl+Cなどのinterrupt：通常終了コード130

未publishのtemporary artifactは削除されます。

## bug reportに含めるもの

- VDLS sourceの最小再現例
- `python --version`
- `ffmpeg -version`
- JSON診断
- `--emit graph-json`
- `--emit commands`
- build manifest
- OS
- expected resultとactual result

秘密情報、個人ファイルpath、非公開assetは必要に応じて置換してください。
