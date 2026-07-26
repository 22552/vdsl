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
    def plain_text(cue: dict[str,Any]) -> str:
        payload=cue["payload"]
        if payload["kind"]=="Karaoke":
            return "".join(segment["text"] for segment in payload["segments"])
        return payload["text"]
    if format_name=="srt":
        blocks=[]
        for index,cue in enumerate(cues,1):
            text=plain_text(cue)
            blocks.append(
                f"{index}\n"
                f"{_timestamp(cue['start'],',')} --> "
                f"{_timestamp(cue['end'],',')}\n{text}")
        return "\n\n".join(blocks)+"\n"
    if format_name=="vtt":
        blocks=["WEBVTT"]
        for cue in cues:
            text=plain_text(cue)
            blocks.append(
                f"{_timestamp(cue['start'],'.')} --> "
                f"{_timestamp(cue['end'],'.')}\n{text}")
        return "\n\n".join(blocks)+"\n"
    raise ValueError(f"unsupported subtitle sidecar format: {format_name}")


def _ass_timestamp(value: dict[str,int]) -> str:
    centiseconds=round(Fraction(value["num"],value["den"])*100)
    hours,remainder=divmod(centiseconds,360_000)
    minutes,remainder=divmod(remainder,6_000)
    seconds,centiseconds=divmod(remainder,100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def serialize_karaoke_ass(cues: list[dict[str,Any]]) -> str:
    """Minimal deterministic ASS track for timed karaoke segments."""
    lines=[
        "[Script Info]","ScriptType: v4.00+","PlayResX: 1920","PlayResY: 1080","",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: VDLS,KaiTi,54,&H00FFFFFF,&H0000FFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,48,1",
        "","[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for cue in cues:
        payload=cue["payload"]
        if payload["kind"]=="Karaoke":
            def karaoke_part(segment: dict[str,Any]) -> str:
                duration=Fraction(segment["end"]["num"],segment["end"]["den"])-Fraction(
                    segment["start"]["num"],segment["start"]["den"])
                escaped=(segment["text"].replace("\\","\\\\")
                         .replace("{","\\{").replace("}","\\}"))
                return "{\\\\k"+str(round(duration*100))+"}"+escaped
            text="".join(karaoke_part(segment)
                         for segment in payload["segments"])
        else:
            text=payload["text"].replace("\\","\\\\").replace("{","\\{").replace("}","\\}")
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(cue['start'])},{_ass_timestamp(cue['end'])},"
            f"VDLS,,0,0,0,,{text}")
    return "\n".join(lines)+"\n"
