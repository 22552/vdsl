"""Deterministic subtitle sidecar serialization."""
from __future__ import annotations

from fractions import Fraction
from typing import Any


def _timestamp(value: dict[str,int], separator: str) -> str:
    milliseconds=round(
        Fraction(value["num"],value["den"])*1000)
    hours,remainder=divmod(milliseconds,3_600_000)
    minutes,remainder=divmod(remainder,60_000)
    seconds,millis=divmod(remainder,1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def serialize_sidecar(cues: list[dict[str,Any]], format_name: str) -> str:
    if format_name=="srt":
        blocks=[]
        for index,cue in enumerate(cues,1):
            text=cue["payload"]["text"]
            blocks.append(
                f"{index}\n"
                f"{_timestamp(cue['start'],',')} --> "
                f"{_timestamp(cue['end'],',')}\n{text}")
        return "\n\n".join(blocks)+"\n"
    if format_name=="vtt":
        blocks=["WEBVTT"]
        for cue in cues:
            text=cue["payload"]["text"]
            blocks.append(
                f"{_timestamp(cue['start'],'.')} --> "
                f"{_timestamp(cue['end'],'.')}\n{text}")
        return "\n\n".join(blocks)+"\n"
    raise ValueError(f"unsupported subtitle sidecar format: {format_name}")
