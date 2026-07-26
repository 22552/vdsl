# CLI reference

```powershell
python vdls.py check examples/hello.vdsl
python vdls.py graph examples/hello.vdsl --format json
python vdls.py build examples/hello.vdsl --target main --no-cache
python vdls.py inspect build/hello.mp4
python vdls.py render-frame examples/hello.vdsl --target main --time 1s --output frame.png
python vdls.py preview examples/hello.vdsl
```

Use `--diagnostic-format json` for machine-readable diagnostics and `--emit`
to save AST, graph, command, or manifest artifacts alongside a build.

For reproducible builds, update the lock file after intentional toolchain
changes, then build with `--reproducible`. This enables locked, offline
verification, validates asset/font integrity, and fixes backend capability
identity.
