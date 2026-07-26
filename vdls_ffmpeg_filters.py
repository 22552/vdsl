"""Typed standard-library filter lowering for the FFmpeg backend."""
from __future__ import annotations

from fractions import Fraction
import json, math
from typing import Any, Callable

BLEND_MODES={
    "multiply":"multiply","screen":"screen","overlay":"overlay",
    "darken":"darken","lighten":"lighten","color-dodge":"dodge",
    "color-burn":"burn","hard-light":"hardlight","soft-light":"softlight",
    "difference":"difference","exclusion":"exclusion",
}


def ffmpeg_blend_mode(name: str) -> str:
    return BLEND_MODES.get(name,"")


def compile_extended_visual_effect(
    name: str,
    args: list[Any],
    diagnostic: Callable[...,Exception],
    unit_scalar: Callable[[Any,str | None],str],
    validate_rgba: Callable[[str],tuple[int,int,int,int]],
    frame_rate: Fraction | None = None,
) -> list[str] | None:
    if name=="temperature" and len(args)==1:
        shift=Fraction(unit_scalar(args[0],None))
        kelvin=Fraction(6500)+shift
        if not 1000<=kelvin<=40000:
            raise diagnostic(
                "VDLS-TYPE-009",
                "temperature result must be within 1000..40000 K")
        return [f"colortemperature=temperature={kelvin}:mix=1:pl=1"]
    if name=="tint" and len(args)==1:
        amount=Fraction(unit_scalar(args[0],None))
        if not -1<=amount<=1:
            raise diagnostic("VDLS-TYPE-009","tint must be within [-1,1]")
        return [f"colorbalance=gm={amount}"]
    if name=="color-matrix":
        values=[]
        for item in args:
            values.extend(item if isinstance(item,list) else [item])
        if len(values)!=20:
            raise diagnostic(
                "VDLS-PARSE-003","color-matrix requires a 4x5 matrix")
        coefficients=[Fraction(unit_scalar(item,None)) for item in values]
        channels=("r","g","b","a")
        sources=("r","g","b","alpha")
        expressions=[]
        for row,channel in enumerate(channels):
            offset=row*5
            terms=[
                f"{coefficients[offset+i]}*{source}(X,Y)"
                for i,source in enumerate(sources)
                if coefficients[offset+i]
            ]
            if coefficients[offset+4]:
                terms.append(f"{coefficients[offset+4]}*255")
            expression="+".join(terms) or "0"
            expressions.append(f"{channel}='clip({expression},0,255)'")
        return ["format=rgba","geq="+":".join(expressions)]
    if name=="chroma-key" and args:
        color=str(args[0]).lstrip("#")
        red,green,blue,_=validate_rgba(color)
        options={str(item[0]):item[1:] for item in args[1:]
                 if isinstance(item,list) and item}
        similarity=Fraction(str(options.get("similarity",["0.1"])[0]))
        smoothness=Fraction(str(options.get("smoothness",["0.08"])[0]))
        spill=Fraction(str(options.get("spill",["0.05"])[0]))
        if not 0<similarity<=1 or not 0<=smoothness<=1 or not 0<=spill<=1:
            raise diagnostic("VDLS-TYPE-009","invalid chroma-key settings")
        result=[
            f"chromakey=color=0x{color[:6]}:similarity={similarity}:"
            f"blend={smoothness}",
        ]
        if spill:
            if green>=red and green>=blue: screen="green"
            elif blue>=red and blue>=green: screen="blue"
            else:
                raise diagnostic(
                    "VDLS-BACKEND-003",
                    "spill suppression requires a green or blue key")
            result.append(f"despill=type={screen}:mix={spill}")
        return result
    if name=="alpha-from-luma" and not args:
        return [
            "format=rgba",
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            "a='clip(0.2126*r(X,Y)+0.7152*g(X,Y)+0.0722*b(X,Y),0,255)'",
        ]
    if name=="mask" and len(args)==1:
        value=args[0]
        if isinstance(value,list) and len(value)==2 and value[0]=="asset-ref":
            value=value[1]
        if not isinstance(value,str):
            raise diagnostic(
                "VDLS-PARSE-003","mask requires an asset reference")
        return [f"__vdls_mask__={value}"]
    if name=="reverse" and not args:
        return ["reverse","setpts=PTS-STARTPTS"]
    if name=="freeze-frame" and len(args)==2:
        if frame_rate is None:
            raise diagnostic(
                "VDLS-TIME-003",
                "freeze-frame lowering requires an output frame rate")
        at=Fraction(unit_scalar(args[0],None))
        duration=Fraction(unit_scalar(args[1],None))
        if at<0 or duration<=0:
            raise diagnostic(
                "VDLS-TYPE-009",
                "freeze-frame time must be non-negative and duration positive")
        frames=round(duration*frame_rate)
        if frames<1:
            raise diagnostic(
                "VDLS-TIME-006",
                "freeze-frame duration is shorter than one output frame")
        first=round(at*frame_rate)
        last=first+frames-1
        return [f"__vdls_freeze__={first}:{last}:{first}"]
    if name=="frame-rate" and args:
        fps=Fraction(unit_scalar(args[0],None))
        if fps<=0:
            raise diagnostic("VDLS-TYPE-009","frame-rate must be positive")
        options={str(item[0]):item[1:] for item in args[1:]
                 if isinstance(item,list) and item}
        mode=str(options.get("mode",["duplicate"])[0])
        if mode=="duplicate": return [f"fps=fps={fps}"]
        if mode=="blend": return [f"framerate=fps={fps}"]
        raise diagnostic("VDLS-PARSE-007",f"invalid frame-rate mode `{mode}`")
    return None


def compile_audio_effects(
    effects: list[Any],
    diagnostic: Callable[...,Exception],
    unit_scalar: Callable[[Any,str | None],str],
    parse_ratio: Callable[[str],dict[str,Any]],
) -> list[str]:
    result=[]
    for effect in effects:
        if not isinstance(effect,list) or not effect:
            raise diagnostic("VDLS-PARSE-003","invalid audio filter")
        name=str(effect[0]); args=effect[1:]
        if name=="duck" and args:
            target=str(args[0])
            options={str(item[0]):item[1:] for item in args[1:]
                     if isinstance(item,list) and item}
            required={"sidechain","amount","attack","release"}
            if not required<=options.keys():
                raise diagnostic(
                    "VDLS-PARSE-003",
                    "duck requires sidechain, amount, attack, and release")
            sidechain=str(options["sidechain"][0])
            amount=parse_ratio(str(options["amount"][0]))
            attack=parse_ratio(str(options["attack"][0]))
            release=parse_ratio(str(options["release"][0]))
            if amount.get("unit")!="dB":
                raise diagnostic("VDLS-TYPE-004","duck amount requires dB")
            amount_db=Fraction(amount["num"],amount["den"])
            attack_s=Fraction(attack["num"],attack["den"])
            release_s=Fraction(release["num"],release["den"])
            if not 0<amount_db<=60 or attack_s<=0 or release_s<=0:
                raise diagnostic("VDLS-TYPE-009","invalid duck settings")
            descriptor={
                "target":target,"sidechain":sidechain,
                "threshold":max(
                    0.000976563,
                    0.125*math.pow(
                        10,-float(amount_db)/(20*(1-1/20)))),
                "attackMs":float(attack_s*1000),
                "releaseMs":float(release_s*1000),
            }
            result.append(
                "__vdls_duck__="+json.dumps(
                    descriptor,sort_keys=True,separators=(",",":")))
        elif name in {"high-pass","low-pass"} and len(args)==1:
            frequency=Fraction(unit_scalar(args[0],"Hz"))
            if frequency<=0:
                raise diagnostic("VDLS-TYPE-009",f"{name} must be positive")
            result.append(
                f"{'highpass' if name=='high-pass' else 'lowpass'}=f={frequency}")
        elif name=="equalizer" and args:
            for band in args:
                if (not isinstance(band,list) or len(band)!=4
                        or band[0]!="band"):
                    raise diagnostic(
                        "VDLS-PARSE-003",
                        "equalizer requires (band frequency gain-db q)")
                frequency=Fraction(unit_scalar(band[1],"Hz"))
                gain=parse_ratio(str(band[2]))
                q=Fraction(str(band[3]))
                if (frequency<=0 or gain.get("unit")!="dB" or q<=0):
                    raise diagnostic("VDLS-TYPE-009","invalid equalizer band")
                gain_db=Fraction(gain["num"],gain["den"])
                result.append(
                    f"equalizer=f={frequency}:t=q:w={q}:g={gain_db}")
        elif name=="compressor":
            options={str(item[0]):item[1:] for item in args
                     if isinstance(item,list) and item}
            required={"threshold","ratio","attack","release"}
            if not required<=options.keys():
                raise diagnostic(
                    "VDLS-PARSE-003",
                    "compressor requires threshold, ratio, attack, and release")
            threshold=parse_ratio(str(options["threshold"][0]))
            ratio_value=Fraction(str(options["ratio"][0]))
            attack=parse_ratio(str(options["attack"][0]))
            release=parse_ratio(str(options["release"][0]))
            if threshold.get("unit")!="dB" or ratio_value<1:
                raise diagnostic("VDLS-TYPE-009","invalid compressor settings")
            threshold_db=float(Fraction(threshold["num"],threshold["den"]))
            threshold_linear=math.pow(10,threshold_db/20)
            attack_ms=float(Fraction(attack["num"],attack["den"])*1000)
            release_ms=float(Fraction(release["num"],release["den"])*1000)
            result.append(
                f"acompressor=threshold={threshold_linear:.12g}:"
                f"ratio={ratio_value}:attack={attack_ms:.12g}:"
                f"release={release_ms:.12g}")
        elif name=="limiter":
            options={str(item[0]):item[1:] for item in args
                     if isinstance(item,list) and item}
            if not options.get("ceiling"):
                raise diagnostic("VDLS-PARSE-003","limiter requires ceiling")
            ceiling=parse_ratio(str(options["ceiling"][0]))
            if ceiling.get("unit")!="dB":
                raise diagnostic("VDLS-TYPE-004","limiter ceiling requires dB")
            ceiling_db=float(Fraction(ceiling["num"],ceiling["den"]))
            result.append(
                f"alimiter=limit={math.pow(10,ceiling_db/20):.12g}")
        elif name=="normalize-loudness" and len(args)==1:
            loudness=parse_ratio(str(args[0]))
            if loudness.get("unit")!="LUFS":
                raise diagnostic(
                    "VDLS-TYPE-004","normalize-loudness requires LUFS")
            target=Fraction(loudness["num"],loudness["den"])
            if not -70<=target<=-5:
                raise diagnostic(
                    "VDLS-TYPE-009",
                    "loudness target must be within -70..-5 LUFS")
            result.append(f"loudnorm=I={target}:LRA=11:TP=-1.5")
        else:
            raise diagnostic(
                "VDLS-BACKEND-003",f"audio filter `{name}` is unsupported")
    return result
