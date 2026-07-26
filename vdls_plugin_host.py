"""Out-of-process VDLS plugin transport.

Protocol transport is isolated from the compiler frontend. The host application
injects its diagnostic type, manifest validator and canonical serializer.
"""
from __future__ import annotations

import concurrent.futures, json, os, shutil, struct, subprocess, sys
from pathlib import Path
from typing import Any, Callable


class PluginProcessHostBase:
    MAX_MESSAGE_BYTES=4*1024*1024

    def __init__(
        self, manifest: dict[str,Any], project_root: Path, cache_root: Path,
        granted_permissions: set[str], timeout: float=10.0, *,
        diagnostic: type[Exception], validate_manifest: Callable[...,Any],
        canonical_json: Callable[[Any],str], host_version: str,
    ):
        self.manifest=manifest
        self.Diagnostic=diagnostic
        self.canonical_json=canonical_json
        self.host_version=host_version
        validate_manifest(
            json.loads(Path(manifest["manifestPath"]).read_text(encoding="utf-8")),
            Path(manifest["manifestPath"]),granted_permissions)
        self.project_root=project_root.resolve()
        self.cache_root=cache_root.resolve()
        self.timeout=timeout; self.process=None; self.next_id=1
        self.initialized=False; self.closed=False

    def _error(self, code: str, message: str, **kwargs: Any) -> Exception:
        return self.Diagnostic(code,message,**kwargs)

    def _command(self) -> list[str]:
        entry=Path(self.manifest["entry"])
        if not entry.exists():
            raise self._error("VDLS-PLUGIN-001",f"plugin entry not found: {entry}")
        if entry.suffix.lower()==".py": return [sys.executable,"-I",str(entry)]
        if entry.suffix.lower()==".rkt":
            racket=shutil.which("racket")
            if not racket:
                raise self._error("VDLS-PLUGIN-007","Racket runtime not found")
            return [racket,str(entry)]
        return [str(entry)]

    def start(self) -> None:
        if self.process is not None: return
        environment={name:os.environ[name] for name in
                     ("SYSTEMROOT","WINDIR","PATH","PATHEXT","TEMP","TMP")
                     if name in os.environ}
        environment["PYTHONIOENCODING"]="utf-8"
        self.process=subprocess.Popen(
            self._command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,cwd=str(Path(self.manifest["entry"]).parent),
            env=environment,shell=False)

    @staticmethod
    def _read_exact(stream: Any, count: int) -> bytes:
        chunks=[]; remaining=count
        while remaining:
            chunk=stream.read(remaining)
            if not chunk: raise EOFError("plugin stream closed")
            chunks.append(chunk); remaining-=len(chunk)
        return b"".join(chunks)

    def _read_response(self) -> dict[str,Any]:
        assert self.process and self.process.stdout
        length=struct.unpack(">I",self._read_exact(self.process.stdout,4))[0]
        if length>self.MAX_MESSAGE_BYTES:
            raise self._error("VDLS-PLUGIN-010","plugin response exceeds size limit")
        payload=self._read_exact(self.process.stdout,length)
        try: result=json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError,json.JSONDecodeError):
            raise self._error("VDLS-PLUGIN-010","plugin returned malformed JSON-RPC")
        if not isinstance(result,dict) or result.get("jsonrpc")!="2.0":
            raise self._error("VDLS-PLUGIN-010","plugin returned invalid JSON-RPC envelope")
        return result

    def request(self, method: str, params: dict[str,Any]) -> Any:
        self.start()
        assert self.process and self.process.stdin
        if self.process.poll() is not None:
            raise self._error("VDLS-PLUGIN-009","plugin process terminated unexpectedly")
        request_id=self.next_id; self.next_id+=1
        message=self.canonical_json({
            "jsonrpc":"2.0","id":request_id,"method":method,"params":params
        }).encode("utf-8")
        if len(message)>self.MAX_MESSAGE_BYTES:
            raise self._error("VDLS-PLUGIN-010","plugin request exceeds size limit")
        try:
            self.process.stdin.write(struct.pack(">I",len(message))+message)
            self.process.stdin.flush()
        except (BrokenPipeError,OSError):
            raise self._error("VDLS-PLUGIN-009","plugin process terminated unexpectedly")
        executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future=executor.submit(self._read_response)
        try: response=future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            self.process.kill()
            raise self._error("VDLS-PLUGIN-008",f"plugin request timed out: {method}")
        except EOFError:
            stderr=self.process.stderr.read(4096) if self.process.stderr else b""
            raise self._error(
                "VDLS-PLUGIN-009","plugin process terminated unexpectedly",
                notes=(stderr.decode("utf-8","replace"),))
        finally:
            executor.shutdown(wait=False,cancel_futures=True)
        if response.get("id")!=request_id:
            raise self._error("VDLS-PLUGIN-010","plugin response id mismatch")
        if "error" in response:
            raise self._error("VDLS-PLUGIN-010",f"plugin error: {response['error']}")
        return response.get("result")

    def initialize(self) -> Any:
        if self.initialized: return None
        result=self.request("vdls.initialize",{
            "hostVersion":self.host_version,"abi":"vdls.plugin/1",
            "projectRoot":str(self.project_root),"cacheRoot":str(self.cache_root),
            "deterministic":True,
        })
        self.initialized=True
        return result

    def capabilities(self) -> Any:
        if not self.initialized: self.initialize()
        return self.request("vdls.capabilities",{})

    def invoke(self, capability: str, request: dict[str,Any]) -> Any:
        if capability not in self.manifest["capabilities"]:
            raise self._error(
                "VDLS-PLUGIN-005",
                f"required capability not declared `{capability}`")
        if not self.initialized: self.initialize()
        return self.request("vdls.invoke",{
            "capability":capability,"request":request})

    def cancel(self, request_id: str) -> Any:
        return self.request("vdls.cancel",{"requestId":request_id})

    def shutdown(self) -> None:
        if self.closed: return
        self.closed=True
        if self.process is None: return
        try:
            if self.process.poll() is None and self.initialized:
                self.request("vdls.shutdown",{})
        except self.Diagnostic:
            self.process.kill()
            raise
        finally:
            if self.process.poll() is None:
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired: self.process.kill()
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
                if stream: stream.close()

    def __enter__(self) -> "PluginProcessHostBase":
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.shutdown()
