# VDLS documentation

VDLS is a declarative S-expression language for describing video projects.
The reference compiler turns a source file into a typed render graph and uses
FFmpeg for the reference render backend.

## Start here

```powershell
python vdls.py check examples/hello.vdsl
python vdls.py build examples/hello.vdsl --no-cache
```

The generated files are written under `build/` by default. `check` validates
the source and FFmpeg capabilities without producing a movie.

- [Language overview](language.md)
- [Timeline and rendering](timeline.md)
- [Lisp layer](lisp.md)
- [CLI reference](cli.md)

Working examples live in [`../examples`](../examples). For the implementation
conformance matrix, see [`../CONFORMANCE.md`](../CONFORMANCE.md).
