"""FFmpeg capability discovery and rendered-artifact validation."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from fractions import Fraction
import hashlib, json, re
from pathlib import Path
from typing import Any, Callable
from vdls_process import run_external


@dataclass(frozen=True)
class FFmpegCapabilities:
    executable: str
    version: str
    filters: tuple[str,...]
    encoders: tuple[str,...]
    decoders: tuple[str,...]
    pixel_formats: tuple[str,...]
    digest: str

    def manifest(self) -> dict[str,Any]:
        value=asdict(self)
        for key in ("filters","encoders","decoders","pixel_formats"):
            value[key]=list(value[key])
        return value


def _run(executable: str, arguments: list[str],
         diagnostic: Callable[...,Exception]) -> str:
    completed=run_external([executable,*arguments],timeout=30)
    if completed.returncode:
        raise diagnostic(
            "VDLS-FFMPEG-003",
            f"FFmpeg capability probe failed: {' '.join(arguments)}",
            notes=((completed.stderr or completed.stdout)[-4000:],))
    return completed.stdout+completed.stderr


def _listed_names(output: str, kind: str) -> tuple[str,...]:
    names=set()
    if kind in {"filters","encoders","decoders"}:
        # Capability lines start with flag columns, followed by the name.
        pattern=re.compile(r"^\s*[.A-Z|]{2,8}\s+([A-Za-z0-9_]+)\s",re.MULTILINE)
        names.update(pattern.findall(output))
    else:
        # -pix_fmts: FLAGS NAME NB_COMPONENTS BITS_PER_PIXEL ...
        pattern=re.compile(r"^\s*[IOHPB.]{5}\s+([A-Za-z0-9_]+)\s",re.MULTILINE)
        names.update(pattern.findall(output))
    return tuple(sorted(names))


def probe_ffmpeg(executable: str, diagnostic: Callable[...,Exception]
                 ) -> FFmpegCapabilities:
    version_output=_run(executable,["-version"],diagnostic)
    version=version_output.splitlines()[0] if version_output.splitlines() else ""
    raw={
        "filters":_run(executable,["-hide_banner","-filters"],diagnostic),
        "encoders":_run(executable,["-hide_banner","-encoders"],diagnostic),
        "decoders":_run(executable,["-hide_banner","-decoders"],diagnostic),
        "pixel_formats":_run(executable,["-hide_banner","-pix_fmts"],diagnostic),
    }
    parsed={key:_listed_names(value,key) for key,value in raw.items()}
    identity={
        "version":version,
        **{key:list(value) for key,value in parsed.items()},
    }
    digest="sha256:"+hashlib.sha256(json.dumps(
        identity,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    return FFmpegCapabilities(
        str(Path(executable).resolve()),version,parsed["filters"],
        parsed["encoders"],parsed["decoders"],parsed["pixel_formats"],digest)


def require_capabilities(
    capabilities: FFmpegCapabilities, requirements: dict[str,list[str]],
    diagnostic: Callable[...,Exception],
) -> None:
    available={
        "filters":set(capabilities.filters),
        "encoders":set(capabilities.encoders),
        "decoders":set(capabilities.decoders),
        "pixel_formats":set(capabilities.pixel_formats),
    }
    for kind,names in requirements.items():
        missing=sorted(set(names)-available.get(kind,set()))
        if missing:
            raise diagnostic(
                "VDLS-FFMPEG-004",
                f"required FFmpeg {kind[:-1]} is unavailable: `{missing[0]}`")


def _fraction(value: str) -> Fraction:
    if not value or value=="0/0": return Fraction(0)
    return Fraction(value)


def validate_artifact(
    path: Path, probe_data: dict[str,Any], expected: dict[str,Any],
    diagnostic: Callable[...,Exception],
) -> None:
    if not path.is_file() or path.stat().st_size==0:
        raise diagnostic("VDLS-FFMPEG-010",f"expected output is missing or empty: {path}")
    streams=probe_data.get("streams",[])
    video=next((stream for stream in streams
                if "width" in stream and "height" in stream),None)
    audio=next((stream for stream in streams
                if stream.get("codec_type")=="audio"),None)
    if expected.get("video"):
        wanted=expected["video"]
        if video is None:
            raise diagnostic("VDLS-FFMPEG-012","expected video stream is missing")
        if (video.get("width"),video.get("height")) != (
                wanted["width"],wanted["height"]):
            raise diagnostic(
                "VDLS-FFMPEG-012",
                "rendered video dimensions do not match the output target")
        actual_rate=_fraction(str(video.get("r_frame_rate","0/0")))
        wanted_rate=Fraction(wanted["frameRate"]["num"],wanted["frameRate"]["den"])
        if actual_rate != wanted_rate:
            raise diagnostic(
                "VDLS-FFMPEG-012",
                f"rendered frame rate {actual_rate} does not match {wanted_rate}")
        expected_color=expected.get("color")
        if expected_color:
            names={
                "color_primaries":{
                    "bt709":"bt709","bt2020":"bt2020",
                    "display-p3":"smpte432"},
                "color_transfer":{
                    "srgb":"iec61966-2-1","bt1886":"bt709","bt709":"bt709",
                    "pq":"smpte2084","hlg":"arib-std-b67","linear":"linear"},
                "color_space":{
                    "rgb":"gbr","bt709":"bt709","bt2020-ncl":"bt2020nc"},
                "color_range":{"full":"pc","limited":"tv"},
            }
            fields={
                "color_primaries":"primaries","color_transfer":"transfer",
                "color_space":"matrix","color_range":"range",
            }
            for probe_key,descriptor_key in fields.items():
                wanted_value=names[probe_key].get(
                    expected_color[descriptor_key])
                if wanted_value and video.get(probe_key)!=wanted_value:
                    raise diagnostic(
                        "VDLS-FFMPEG-012",
                        f"rendered {descriptor_key} `{video.get(probe_key)}` "
                        f"does not match `{wanted_value}`")
    elif video is not None:
        raise diagnostic("VDLS-FFMPEG-012","unexpected video stream was rendered")
    if expected.get("audio") and audio is None:
        raise diagnostic("VDLS-FFMPEG-012","expected audio stream is missing")
    if not expected.get("audio") and audio is not None:
        raise diagnostic("VDLS-FFMPEG-012","unexpected audio stream was rendered")
    actual_duration=Fraction(str(probe_data.get("format",{}).get("duration","0")))
    wanted_duration=Fraction(expected["duration"]["num"],expected["duration"]["den"])
    # Container duration quantization may differ by one millisecond.
    if abs(actual_duration-wanted_duration)>Fraction(1,1000):
        raise diagnostic(
            "VDLS-FFMPEG-012",
            f"rendered duration {actual_duration} does not match {wanted_duration}")
    expected_markers=expected.get("markers",[])
    chapters=probe_data.get("chapters",[])
    if len(chapters)!=len(expected_markers):
        raise diagnostic(
            "VDLS-FFMPEG-012",
            f"rendered chapter count {len(chapters)} does not match "
            f"{len(expected_markers)}")
    for chapter,marker in zip(chapters,expected_markers):
        actual_start=Fraction(str(chapter.get("start_time","0")))
        wanted_start=Fraction(marker["time"]["num"],marker["time"]["den"])
        if abs(actual_start-wanted_start)>Fraction(1,1000):
            raise diagnostic(
                "VDLS-FFMPEG-012",
                f"rendered chapter start {actual_start} does not match "
                f"{wanted_start}")
        actual_title=str(chapter.get("tags",{}).get("title",""))
        wanted_title=str(marker.get("label") or marker["markerId"])
        if actual_title!=wanted_title:
            raise diagnostic(
                "VDLS-FFMPEG-012",
                f"rendered chapter title `{actual_title}` does not match "
                f"`{wanted_title}`")
