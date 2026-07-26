# CLIリファレンス

VDLSの参照実装は `vdls.py` から実行できます。Python 3.11以上、FFmpeg、FFprobeが必要です。

## check

ソースを読み取り、Lisp展開、型検査、asset解決、Render Graph計画、FFmpeg能力確認まで行います。最終動画は生成しません。

```powershell
python vdls.py check examples/hello.vdsl
```

CIや編集途中の確認では、まず `check` を使います。

JSON診断：

```powershell
python vdls.py --diagnostic-format json check examples/hello.vdsl
```

## graph

backend-neutralなRender Graphを表示します。

```powershell
python vdls.py graph examples/hello.vdsl --format json
```

sourceがどのnode、port、edge、targetへlowerされたか確認できます。

## build

動画やsidecarなどの最終成果物を生成します。

```powershell
python vdls.py build examples/hello.vdsl
```

outputを選ぶ：

```powershell
python vdls.py build examples/hello.vdsl --target main
```

cacheを使わず再実行：

```powershell
python vdls.py build examples/hello.vdsl --target main --no-cache
```

## preview

低遅延のpreview buildを実行します。

```powershell
python vdls.py preview examples/hello.vdsl
```

解像度を一時的に下げる：

```powershell
python vdls.py preview examples/hello.vdsl --resolution 640x360
```

ファイル変更を監視する：

```powershell
python vdls.py preview examples/hello.vdsl --resolution 640x360 --watch
```

preview解像度overrideは最終output仕様を書き換えず、非適合preview成果物として記録されます。

## render-frame

指定時刻の静止画を生成します。

```powershell
python vdls.py render-frame examples/hello.vdsl `
  --target main `
  --time 1s `
  --output frame.png
```

animation、字幕、layout、色処理の確認に便利です。

## inspect

動画・画像・音声のmetadataを調べます。

```powershell
python vdls.py inspect build/hello.mp4
```

画像では正規化されたEXIF情報を取得できます。build manifestへはprivacy-reducedな要約が記録され、GPS座標そのものは複製しません。

## emit

build時に中間成果物を保存できます。

```powershell
python vdls.py build examples/hello.vdsl `
  --emit ast-json `
  --emit graph-json `
  --emit commands
```

代表的な出力：

- Semantic AST JSON
- Render Graph JSON
- 実際のargv
- FFmpeg filter script
- build manifest

## project discovery

ディレクトリを指定した場合、次の優先順位でprojectを探索します。

1. `vdls.toml`
2. `project.vdsl`
3. `main.vdsl`

ソースファイルを直接指定した場合、そのファイル位置がproject rootの基準になります。

## lock

意図的にFFmpegやasset構成を変更した後、lockfileを更新します。

```powershell
python vdls.py lock --project .
```

`--locked` はlockfileとの差異を拒否します。

```powershell
python vdls.py build . --locked
```

`--frozen` はlockedに加えてoffline動作を要求します。

## reproducible build

```powershell
python vdls.py build . --reproducible
```

`--reproducible` は概ね次を要求します。

- offline
- locked
- FFmpeg capability digest一致
- file asset integrity一致
- pinned font
- metadata正規化
- creation time正規化
- deterministic encoder threading
- random seed固定

詳細は [再現可能ビルドとキャッシュ](reproducible-builds.md) を参照してください。

## cache

content-addressed cacheの状態確認、削除、整理に対応します。利用可能なsubcommandは次で確認してください。

```powershell
python vdls.py --help
```

cache hit/missはmachine-readable result envelopeとmanifestにも記録されます。

## 終了コード

主な分類：

- usage error
- source/parse/type error
- asset error
- plugin error
- backend/capability error
- output error
- permission error
- timeout
- interrupt
- internal error

timeoutは通常 `124`、割込みは `130` に対応します。

## 安全な外部実行

FFmpegなどの外部コマンドはargument arrayとして実行され、shell文字列連結を行いません。複雑なfilter graphはUTF-8の `-filter_complex_script` へ書き出されます。

## 困ったとき

```powershell
python vdls.py --help
python vdls.py check path/to/project.vdsl
python vdls.py --diagnostic-format json check path/to/project.vdsl
```

それでも解決しない場合は [トラブルシューティング](troubleshooting.md) を参照してください。
