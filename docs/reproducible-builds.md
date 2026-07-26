# 再現可能ビルドとキャッシュ

VDLSは、同じsource・asset・toolchain・設定から同じ意味の成果物を再生成できるように設計されています。

## 通常build

```powershell
python vdls.py build project.vdsl
```

通常buildでも、source、asset、Render Graph、backend能力などからcache identityを構築します。

## lockfile

FFmpegやasset構成を意図的に変更した後、lockfileを更新します。

```powershell
python vdls.py lock --project .
```

lockfileにはbackend identity、capability digest、asset integrity、plugin情報などが記録されます。

## locked build

```powershell
python vdls.py build . --locked
```

lockfileと実環境が一致しない場合はbuild前に失敗します。

## frozen build

```powershell
python vdls.py build . --frozen
```

`--frozen` はlockedに加えてoffline動作を要求します。mutableなremote sourceへアクセスしません。

## reproducible build

```powershell
python vdls.py build . --reproducible
```

このmodeでは概ね次を行います。

- `--offline` と `--locked` を有効化
- FFmpeg capability digestを検証
- file assetのintegrityを検証
- host font fallbackを拒否
- pinned font assetを要求
- metadataを正規化または除去
- creation timeを正規化
- encoder threadingを固定
- random seedを固定
- manifestへ再現性の根拠を記録

## fontを固定する理由

同じfamily nameでも、OSやinstall状況によって別font fileが選ばれる可能性があります。再現可能buildではfont assetを明示し、そのdigestをmanifestに含めます。

## 決定的乱数

```racket
(build-options (seed 2026))
```

```racket
(random stream salt)
```

乱数はmutable generator stateを持ちません。並列実行順が変わってもsequenceは変化しません。

## cache identity

pure nodeのcache keyには概ね次が含まれます。

```text
spec major
node kind
canonical parameters
ordered input cache keys
plugin ID/version
backend semantic version
relevant environment fingerprint
```

source locationやtemporary pathはsemantic cache keyへ含めません。

## cacheを使わない

```powershell
python vdls.py build project.vdsl --no-cache
```

backendやcacheの問題を切り分けるときに使います。

## manifest

成功したbuildはmanifestを生成します。

主な記録内容：

- source digest
- asset digest
- output digest
- lock hash
- font digest
- plugin identity
- backend version
- FFmpeg capability digest
- selected target
- cache hit/miss
- reproducible modeの証拠

## GPUと再現性

GPU backendではvendorやdriverによる浮動小数点差があり得ます。仕様上、意味的に等価なpixel結果を要求しますが、backendが `gpu-deterministic-bitexact` を宣言しない限りvendor間のbit exact一致は要求されません。

## 再現できないとき

1. `--no-cache` で再build
2. lockfileを確認
3. FFmpeg versionとcapability digestを確認
4. font assetが固定されているか確認
5. remote assetがmutableでないか確認
6. seedが明示されているか確認
7. manifest同士を比較
