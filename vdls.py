"""Small, dependency-free VDLS Draft 1.0 reference core.

It deliberately keeps the semantic frontend independent of FFmpeg.  `build`
emits inspectable artifacts; rendering is only attempted when ffmpeg is found.
"""
from __future__ import annotations

import argparse, hashlib, json, math, os, re, shutil, signal, subprocess
import struct, sys, time, tomllib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any
from vdls_text_engine import (
    FontRequest, Paint, TextEngineError, TextRequest, ffmpeg_ass_filter,
    layout_text, render_ass_surface,
)
from vdls_plugin_host import PluginProcessHostBase
from vdls_ffmpeg_backend import (
    probe_ffmpeg, require_capabilities, validate_artifact,
)
from vdls_process import ProcessInterrupted, ProcessTimedOut, run_external
from vdls_media_metadata import exif_manifest_summary, read_exif
from vdls_subtitles import serialize_sidecar
from vdls_ffmpeg_filters import (
    compile_audio_effects as lower_audio_effects,
    compile_extended_visual_effect, ffmpeg_blend_mode,
)

VERSION = "0.1.0"
UNITS = {
    "s", "ms", "us", "ns", "f", "px", "%", "pct", "deg", "rad", "turn",
    "dB", "db", "Hz", "hz", "kHz", "khz", "lufs", "LUFS",
}
TOKEN = re.compile(r'\s*(?:;[^\n]*|("(?:\\.|[^"\\])*")|([()]|[^\s()]+))')
NUMBER = re.compile(
    r"^([+-]?(?:\d+(?:\.\d+)?|\d+/\d+))"
    r"(s|ms|us|ns|f|px|%|pct|deg|rad|turn|dB|db|Hz|hz|kHz|khz|lufs|LUFS)$"
)
PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\d+/\d+)$")
PURE_OPERATORS = {
    "+","-","*","/","min","max","clamp","abs","floor","ceil","round",
    "=","!=","<","<=",">",">=","and","or","not","sin","cos","tan",
    "asin","acos","atan","exp","log","sqrt","pow","mix","smoothstep",
    "step","mod","vec2","vec3","vec4","component","if","tr",
}
EXPRESSION_VARIABLES = {
    "t","T","u","frame","fps","width","height","input-width","input-height",
    "r","true","false","#t","#f",
}
OUTPUT_PRESETS={
    "youtube-1080p":{"video":{"width":1920,"height":1080,
                             "frameRate":{"num":30,"den":1}}},
    "youtube-short":{"video":{"width":1080,"height":1920,
                             "frameRate":{"num":30,"den":1}}},
    "tiktok-vertical":{"video":{"width":1080,"height":1920,
                               "frameRate":{"num":30,"den":1}}},
    "instagram-reel":{"video":{"width":1080,"height":1920,
                              "frameRate":{"num":30,"den":1}}},
    "preview-low":{"video":{"width":640,"height":360,
                           "frameRate":{"num":24,"den":1}}},
    "archive-lossless":{"video":{"width":1920,"height":1080,
                                "frameRate":{"num":30,"den":1}}},
}

class Symbol(str):
    """Reader-level identifier, distinct from a quoted source string."""

@dataclass
class Diagnostic(Exception):
    code: str
    message: str
    offset: int = 0
    notes: tuple[str, ...] = ()
    help: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self, source: Path | None = None) -> dict[str, Any]:
        primary = None
        if source:
            primary = {"uri": source.resolve().as_uri(), "start": {"line": 1, "column": self.offset + 1, "offset": self.offset}, "end": {"line": 1, "column": self.offset + 1, "offset": self.offset}}
        return {"schema":"vdls.diagnostic/1", "code":self.code, "severity":"error", "message":self.message, "primary":primary, "secondary":[], "notes":list(self.notes), "help":list(self.help), "causes":[], "data":{}}

def tokenize(text: str) -> list[tuple[str, int]]:
    out=[]; pos=0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m=TOKEN.match(text,pos)
        if not m:
            raise Diagnostic("VDLS-READ-005", "invalid token or unmatched delimiter", pos)
        pos=m.end()
        value=m.group(1) or m.group(2)
        if value:
            if value[0] in {"'", "`", ","} or value.startswith(("#(", "#&", "#reader", "#;")):
                raise Diagnostic(
                    "VDLS-READ-006",
                    f"prohibited reader extension `{value}`",
                    m.start(),
                )
            out.append((value, m.start()))
    return out

def parse(text: str) -> list[Any]:
    tokens=tokenize(text); i=0
    def form() -> Any:
        nonlocal i
        if i >= len(tokens): raise Diagnostic("VDLS-READ-005", "unmatched opening delimiter")
        atom, off=tokens[i]; i+=1
        if atom == "(":
            items=[]
            while i < len(tokens) and tokens[i][0] != ")": items.append(form())
            if i == len(tokens): raise Diagnostic("VDLS-READ-005", "unmatched opening delimiter", off)
            i+=1; return items
        if atom == ")": raise Diagnostic("VDLS-READ-005", "unmatched closing delimiter", off)
        if atom.startswith('"'):
            try: return json.loads(atom)
            except json.JSONDecodeError: raise Diagnostic("VDLS-READ-002", "invalid string literal", off)
        return Symbol(atom)
    result=[]
    while i < len(tokens): result.append(form())
    return result

def ratio(value: str, fps: Fraction | None = None) -> dict[str, int]:
    m=NUMBER.match(value)
    if not m: raise ValueError(value)
    raw, unit=m.groups(); v=Fraction(raw)
    if unit == "s": pass
    elif unit == "ms": v /= 1000
    elif unit == "us": v /= 1_000_000
    elif unit == "ns": v /= 1_000_000_000
    elif unit == "f":
        if not fps: raise Diagnostic("VDLS-TIME-003", "frame literal lacks frame-rate context")
        v /= fps
    elif unit == "deg": v *= Fraction(str(math.pi)) / 180
    elif unit == "turn": v *= Fraction(str(math.tau))
    elif unit == "rad": pass
    elif unit in {"dB", "db"}: return {"unit":"dB", "num":v.numerator, "den":v.denominator}
    elif unit in {"kHz", "khz"}:
        v *= 1000
        return {"unit":"Hz", "num":v.numerator, "den":v.denominator}
    elif unit in {"Hz", "hz"}:
        return {"unit":"Hz", "num":v.numerator, "den":v.denominator}
    elif unit in {"lufs","LUFS"}:
        return {"unit":"LUFS","num":v.numerator,"den":v.denominator}
    elif unit == "px":
        return {"unit":"px", "num":v.numerator, "den":v.denominator}
    elif unit in {"%", "pct"}:
        v /= 100
        return {"unit":"ratio", "num":v.numerator, "den":v.denominator}
    return {"num":v.numerator, "den":v.denominator}

def plain_ratio(value: str) -> dict[str, int]:
    """Encode a unitless rational, used by frame-rate declarations."""
    value = Fraction(value)
    return {"num": value.numerator, "den": value.denominator}

def normalize_expression(value: Any, variables: set[str] | None=None) -> dict[str,Any]:
    """Compile the portable pure expression subset to a serializable AST."""
    variables=EXPRESSION_VARIABLES if variables is None else variables
    if isinstance(value,(int,float,bool)):
        return {"kind":"Literal","value":value}
    if isinstance(value,str):
        if NUMBER.match(value):
            return {"kind":"Literal","value":ratio(value)}
        if PLAIN_NUMBER.match(value):
            number=Fraction(value)
            return {"kind":"Literal","value":{"num":number.numerator,"den":number.denominator}}
        if value in {"true","#t"}: return {"kind":"Literal","value":True}
        if value in {"false","#f"}: return {"kind":"Literal","value":False}
        if value in variables: return {"kind":"Variable","name":value}
        if isinstance(value,Symbol):
            raise Diagnostic("VDLS-NAME-001",f"undefined identifier `{value}`")
        return {"kind":"Literal","value":value}
    if not isinstance(value,list) or not value or not isinstance(value[0],str):
        raise Diagnostic("VDLS-TYPE-001","invalid expression")
    operator=value[0]
    if operator not in PURE_OPERATORS:
        raise Diagnostic("VDLS-NAME-001",f"undefined expression function `{operator}`")
    arguments=[normalize_expression(item,variables) for item in value[1:]]
    arity={
        "/":(2,2),"clamp":(3,3),"not":(1,1),"if":(3,3),
        "pow":(2,2),"sqrt":(1,1),"tr":(1,1),
    }.get(operator,(1,None))
    minimum,maximum=arity
    if len(arguments)<minimum or (maximum is not None and len(arguments)>maximum):
        raise Diagnostic("VDLS-PARSE-003",
                         f"`{operator}` received {len(arguments)} operands")
    return {"kind":"Call","operator":operator,"arguments":arguments}

def normalize_animation(form: list[Any]) -> dict[str,Any]:
    if len(form)<3 or form[0]!="animate" or not isinstance(form[1],str):
        raise Diagnostic("VDLS-PARSE-003","animate requires a property and body")
    property_path=str(form[1]); body=form[2:]
    if len(body)==1 and isinstance(body[0],list) and body[0] and body[0][0]=="keyframes":
        frames=[]
        previous=None
        for item in body[0][1:]:
            if not isinstance(item,list) or len(item)<2:
                raise Diagnostic("VDLS-PARSE-003","invalid keyframe")
            timestamp=ratio(item[0])
            if timestamp.get("unit"):
                raise Diagnostic("VDLS-TYPE-004","keyframe time requires duration")
            exact=Fraction(timestamp["num"],timestamp["den"])
            if previous is not None and exact==previous:
                raise Diagnostic("VDLS-TIME-004","duplicate keyframe time")
            if previous is not None and exact<previous:
                raise Diagnostic("VDLS-TIME-005","keyframes are not ordered")
            easing="linear"
            easing_clause=next((clause for clause in item[2:]
                                if isinstance(clause,list) and len(clause)==2
                                and clause[0]=="easing"),None)
            if easing_clause: easing=str(easing_clause[1])
            frames.append({"time":timestamp,"value":item[1],"easing":easing})
            previous=exact
        if not frames: raise Diagnostic("VDLS-PARSE-003","keyframes requires entries")
        return {"kind":"Keyframes","property":property_path,"keyframes":frames}
    clauses={}
    for item in body:
        if isinstance(item,list) and item:
            if item[0] in clauses:
                raise Diagnostic("VDLS-PARSE-004",
                                 f"duplicate animation clause `{item[0]}`")
            clauses[str(item[0])]=item[1:]
    for required in ("from","to","duration"):
        if required not in clauses or len(clauses[required])!=1:
            raise Diagnostic("VDLS-PARSE-003",
                             f"animation requires `{required}`")
    duration_value=ratio(clauses["duration"][0])
    if duration_value.get("unit"):
        raise Diagnostic("VDLS-TYPE-004","animation duration requires time")
    if duration_value["num"]<0:
        raise Diagnostic("VDLS-TIME-001","negative animation duration")
    easing=str(clauses.get("easing",["linear"])[0])
    allowed={
        "linear","smoothstep","ease-in-quad","ease-out-quad",
        "ease-in-out-quad","ease-in-cubic","ease-out-cubic",
        "ease-in-out-cubic",
    }
    if easing not in allowed:
        raise Diagnostic("VDLS-NAME-010",f"easing `{easing}` does not resolve")
    return {"kind":"FromTo","property":property_path,
            "from":clauses["from"][0],"to":clauses["to"][0],
            "duration":duration_value,"easing":easing}

def _subtitle_timestamp(value: str) -> dict[str,int]:
    match=re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})",value.strip())
    if not match:
        match=re.fullmatch(r"(\d{1,2}):(\d{2})[,.](\d{3})",value.strip())
        if not match: raise Diagnostic("VDLS-SUB-001",f"invalid subtitle timestamp `{value}`")
        hours=0; minutes,seconds,millis=map(int,match.groups())
    else:
        hours,minutes,seconds,millis=map(int,match.groups())
    total=Fraction(hours*3600+minutes*60+seconds)+Fraction(millis,1000)
    return {"num":total.numerator,"den":total.denominator}

def parse_subtitles(text: str, suffix: str=".srt") -> list[dict[str,Any]]:
    normalized=text.replace("\r\n","\n").replace("\r","\n").lstrip("\ufeff")
    if suffix.lower()==".vtt":
        normalized=re.sub(r"^WEBVTT[^\n]*\n+","",normalized)
    blocks=re.split(r"\n\s*\n",normalized.strip())
    cues=[]
    for index,block in enumerate(blocks):
        lines=block.splitlines()
        if not lines: continue
        timing_index=next((i for i,line in enumerate(lines) if "-->" in line),None)
        if timing_index is None:
            raise Diagnostic("VDLS-SUB-001","subtitle cue lacks timing line")
        timing=lines[timing_index].split("-->",1)
        start=_subtitle_timestamp(timing[0])
        end_text=timing[1].strip().split()[0]
        end=_subtitle_timestamp(end_text)
        start_q=Fraction(start["num"],start["den"]); end_q=Fraction(end["num"],end["den"])
        if end_q<=start_q:
            raise Diagnostic("VDLS-SUB-002","subtitle cue end must be after start")
        payload="\n".join(lines[timing_index+1:])
        cues.append({"id":f"cue:{index+1}","start":start,"end":end,
                     "payload":{"kind":"Text","text":payload},
                     "region":None,"settings":{},"span":None})
    return cues

def node_id(kind: str, state: dict[str, int]) -> str:
    state[kind]=state.get(kind,0)+1; return f"n:{kind.lower()}:{state[kind]}"

def expand_templates(forms: list[Any], limit: int=100) -> list[Any]:
    definitions: dict[str,dict[str,Any]]={}
    retained=[]
    for form in forms:
        if not (isinstance(form,list) and form and form[0]=="define-template"):
            retained.append(form); continue
        if len(form)<3:
            raise Diagnostic("VDLS-MACRO-004","malformed template definition")
        if isinstance(form[1],list):
            if not form[1]: raise Diagnostic("VDLS-MACRO-004","empty template signature")
            name=str(form[1][0])
            parameters=[{"name":str(item),"default":None} for item in form[1][1:]]
            bodies=form[2:]
        else:
            name=str(form[1])
            if len(form)<4 or not isinstance(form[2],list):
                raise Diagnostic("VDLS-MACRO-004","typed template requires parameters and body")
            parameters=[]
            for spec in form[2]:
                if not isinstance(spec,list) or len(spec)<2:
                    raise Diagnostic("VDLS-MACRO-004","invalid typed template parameter")
                parameters.append({"name":str(spec[0]),
                                   "default":spec[2] if len(spec)>=3 else None})
            bodies=form[3:]
        if name in definitions:
            raise Diagnostic("VDLS-NAME-002",f"duplicate template `{name}`")
        definitions[name]={"parameters":parameters,"bodies":bodies}

    def substitute(value: Any, bindings: dict[str,Any]) -> Any:
        if isinstance(value,Symbol) and value in bindings: return bindings[str(value)]
        if isinstance(value,list): return [substitute(item,bindings) for item in value]
        return value

    def invocation(form: list[Any], depth: int) -> list[Any] | None:
        if depth>limit:
            raise Diagnostic("VDLS-MACRO-003","template expansion depth exceeded")
        if not form: return None
        if form[0]=="instantiate" and len(form)>=2:
            name=str(form[1]); arguments=form[2:]; named=True
        elif str(form[0]) in definitions:
            name=str(form[0]); arguments=form[1:]; named=False
        else: return None
        if name not in definitions:
            raise Diagnostic("VDLS-MACRO-001",f"template not found `{name}`")
        definition=definitions[name]; parameters=definition["parameters"]
        bindings={}
        if named:
            for argument in arguments:
                if not isinstance(argument,list) or len(argument)!=2:
                    raise Diagnostic("VDLS-MACRO-004","invalid template argument")
                key=str(argument[0])
                if key not in {item["name"] for item in parameters}:
                    raise Diagnostic("VDLS-MACRO-007",
                                     f"unknown template parameter `{key}`")
                bindings[key]=argument[1]
        else:
            if len(arguments)>len(parameters):
                raise Diagnostic("VDLS-MACRO-007","too many template arguments")
            bindings.update({parameter["name"]:value
                             for parameter,value in zip(parameters,arguments)})
        for parameter in parameters:
            if parameter["name"] not in bindings:
                if parameter["default"] is None:
                    raise Diagnostic("VDLS-MACRO-006",
                                     f"template parameter missing `{parameter['name']}`")
                bindings[parameter["name"]]=parameter["default"]
        expanded=[substitute(body,bindings) for body in definition["bodies"]]
        result=[]
        for body in expanded:
            nested=invocation(body,depth+1) if isinstance(body,list) else None
            result.extend(nested if nested is not None else [body])
        return result

    output=[]
    for form in retained:
        if isinstance(form,list) and form and form[0]=="project":
            clauses=[]
            for clause in form[1:]:
                expanded=invocation(clause,1) if isinstance(clause,list) else None
                clauses.extend(expanded if expanded is not None else [clause])
            output.append([form[0],*clauses])
        else:
            output.append(form)
    return output

def resolve_imports(forms: list[Any], source: Path,
                    project_root: Path | None=None,
                    stack: tuple[Path,...]=()) -> tuple[list[Any],list[dict[str,Any]]]:
    project_root=(project_root or source.parent).resolve()
    source=source.resolve()
    if source in stack:
        chain=" -> ".join(item.name for item in (*stack,source))
        raise Diagnostic("VDLS-NAME-005",f"cyclic module import: {chain}")
    output=[]; imports=[]
    for form in forms:
        if not (isinstance(form,list) and form and form[0]=="import"):
            output.append(form); continue
        if len(form)<2 or not isinstance(form[1],str):
            raise Diagnostic("VDLS-PARSE-003","import requires a module")
        module=str(form[1])
        if module.startswith("vdls.std."):
            imports.append({"module":module,"path":None,"standard":True})
            continue
        module_path=(source.parent/module).resolve()
        try: module_path.relative_to(project_root)
        except ValueError:
            raise Diagnostic("VDLS-SECURITY-010",
                             f"module import escapes project root: {module}")
        if not module_path.exists():
            raise Diagnostic("VDLS-CONFIG-001",f"module not found: {module}")
        try: module_text=module_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise Diagnostic("VDLS-READ-001","invalid UTF-8 input",error.start)
        module_text=re.sub(r"^\s*#lang\s+vdls[^\n]*(?:\n|$)","",
                           module_text.lstrip("\ufeff"),count=1)
        module_forms=parse(module_text)
        if any(isinstance(item,list) and item and item[0]=="project"
               for item in module_forms):
            raise Diagnostic("VDLS-MACRO-004",
                             "imported module must not contain a project")
        expanded,children=resolve_imports(
            module_forms,module_path,project_root,(*stack,source))
        output.extend(expanded); imports.extend(children)
        imports.append({"module":module,"path":str(module_path),"standard":False})
    return output,imports

def compile_source(text: str, source: Path) -> dict[str, Any]:
    normalized=text.lstrip('\ufeff')
    lang_match=re.match(r"\s*#lang\s+([^\s]+)", normalized)
    if lang_match:
        if lang_match.group(1) != "vdls":
            raise Diagnostic("VDLS-READ-006", "portable source requires `#lang vdls`")
        normalized=normalized[lang_match.end():]
    raw_forms,import_nodes=resolve_imports(parse(normalized),source)
    forms=expand_templates(raw_forms)
    allowed_top={"project","requires-vdls","import","define","define-easing"}
    for form in forms:
        if not isinstance(form,list) or not form or form[0] not in allowed_top:
            name=form[0] if isinstance(form,list) and form else form
            raise Diagnostic("VDLS-PARSE-002",f"unknown top-level form `{name}`")
    projects=[x for x in forms if isinstance(x,list) and x and x[0]=="project"]
    if len(projects)!=1: raise Diagnostic("VDLS-PARSE-001", "expected exactly one top-level `project` form")
    p=projects[0]; state={}; assets={}; scenes=[]; outputs=[]; project_id=None
    settings=None; project_annotations={}
    color_management={
        "workingSpace":{
            "primaries":"bt709","transfer":"linear",
            "matrix":"rgb","range":"full",
        },
        "output":{
            "primaries":"bt709","transfer":"bt709",
            "matrix":"bt709","range":"limited",
        },
        "unknownMetadataPolicy":"warn-and-default",
        "toneMap":None,
        "inherited":True,
    }
    for top in forms:
        if isinstance(top,list) and top and top[0]=="requires-vdls":
            if len(top)!=2 or not isinstance(top[1],str):
                raise Diagnostic("VDLS-PARSE-003",
                                 "requires-vdls requires a version constraint")
            constraint=top[1]
            if re.search(r"(?:>=|>|=)\s*2(?:\.|$)",constraint):
                raise Diagnostic("VDLS-VERSION-001",
                                 f"unsupported VDLS version constraint `{constraint}`")
            project_annotations["vdls.requires"]=constraint

    locale_catalogs: dict[str,dict[str,Any]]={}
    locale_fallbacks: dict[str,str]={}
    default_locale=None
    for locale_form in [item for item in p[1:] if isinstance(item,list)
                        and item and item[0]=="locale"]:
        if len(locale_form)<2:
            raise Diagnostic("VDLS-PARSE-003","locale requires an identifier")
        if isinstance(locale_form[1],str) and not isinstance(locale_form[1],list):
            locale_id=str(locale_form[1]); locale_clauses=locale_form[2:]
        else:
            locale_clauses=locale_form[1:]
            id_clause=next((item for item in locale_clauses if isinstance(item,list)
                            and len(item)==2 and item[0]=="id"),None)
            if not id_clause:
                raise Diagnostic("VDLS-PARSE-003","locale requires id")
            locale_id=str(id_clause[1])
        file_clause=next((item for item in locale_clauses if isinstance(item,list)
                          and len(item)==2 and item[0]=="file"),None)
        if not file_clause:
            raise Diagnostic("VDLS-PARSE-003","locale requires a JSON file")
        locale_path=(source.parent/str(file_clause[1])).resolve()
        if not locale_path.exists():
            raise Diagnostic("VDLS-ASSET-001",f"locale file not found: {file_clause[1]}")
        try: catalog=json.loads(locale_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError,UnicodeDecodeError):
            raise Diagnostic("VDLS-MACRO-008",f"invalid locale file `{file_clause[1]}`")
        if not isinstance(catalog,dict):
            raise Diagnostic("VDLS-MACRO-008","locale root must be a JSON object")
        locale_catalogs[locale_id]=catalog
        fallback=next((item for item in locale_clauses if isinstance(item,list)
                       and len(item)==2 and item[0]=="fallback"),None)
        if fallback: locale_fallbacks[locale_id]=str(fallback[1])
        default_clause=next((item for item in locale_clauses if isinstance(item,list)
                             and len(item)==2 and item[0]=="default"),None)
        if default_locale is None or (default_clause and str(default_clause[1]) in {"true","#t"}):
            default_locale=locale_id

    def translate(key: str) -> str:
        candidates=[]; current=default_locale; seen=set()
        while current:
            if current in seen:
                raise Diagnostic("VDLS-MACRO-009","locale fallback cycle")
            seen.add(current); candidates.append(current)
            language=current.split("-",1)[0]
            if language!=current and language in locale_catalogs and language not in candidates:
                candidates.append(language)
            current=locale_fallbacks.get(current)
        for locale_id in candidates:
            value: Any=locale_catalogs.get(locale_id,{})
            for part in key.split("."):
                if not isinstance(value,dict) or part not in value:
                    value=None; break
                value=value[part]
            if isinstance(value,str): return value
        raise Diagnostic("VDLS-MACRO-008",f"unresolved locale value `{key}`")
    def duration(clause: list[Any]) -> dict[str,int] | None:
        if len(clause)==2 and isinstance(clause[1],str) and NUMBER.match(clause[1]):
            r=ratio(clause[1]); return r if "unit" not in r else None
        return None

    def clauses_by_name(items: list[Any]) -> dict[str,list[list[Any]]]:
        result: dict[str,list[list[Any]]]={}
        for item in items:
            if isinstance(item,list) and item and isinstance(item[0],str):
                result.setdefault(item[0],[]).append(item)
        return result

    def parse_color_descriptor(
        clauses: list[Any], defaults: dict[str,str]
    ) -> dict[str,str]:
        values=dict(defaults)
        allowed={
            "primaries":{"bt709","bt2020","display-p3","unknown"},
            "transfer":{"srgb","bt1886","bt709","pq","hlg","linear","unknown"},
            "matrix":{"rgb","bt709","bt2020-ncl","unknown"},
            "range":{"full","limited","unknown"},
        }
        for item in clauses:
            if not isinstance(item,list) or len(item)!=2:
                raise Diagnostic("VDLS-PARSE-003","invalid color descriptor")
            key=str(item[0]); value=str(item[1])
            if key not in allowed or value not in allowed[key]:
                raise Diagnostic(
                    "VDLS-PARSE-007",f"invalid color {key} `{value}`")
            values[key]=value
        return values

    def compile_node(form: list[Any], inherited_duration: dict[str,int] | None) -> dict[str,Any]:
        if not form or not isinstance(form[0],str):
            raise Diagnostic("VDLS-PARSE-002","invalid media node")
        kind=form[0]; by_name=clauses_by_name(form[1:])
        animations=[]
        for animation in by_name.get("animate",[]):
            animations.append(normalize_animation(animation))
        common={
            "span":None,"annotations":{},
            "start":duration(by_name["start"][0]) if by_name.get("start") else {"num":0,"den":1},
            "duration":duration(by_name["duration"][0]) if by_name.get("duration") else inherited_duration,
            "opacity":by_name.get("opacity",[[None,"1"]])[0][1],
            "blendMode":by_name.get("blend",[[None,"normal"]])[0][1],
            "transform":by_name.get("transform",[None])[0],
            "effects":[effect for item in
                       (by_name.get("filter",[])+by_name.get("filters",[]))
                       for effect in item[1:]],
            "animations":animations,
        }
        if kind in {"video","audio"}:
            refs=[item for item in form[1:] if isinstance(item,list) and item and item[0]=="asset-ref"]
            if len(refs)!=1 or len(refs[0])!=2:
                raise Diagnostic("VDLS-PARSE-003",f"{kind} requires exactly one asset-ref")
            ref=refs[0][1]
            if ref not in assets:
                raise Diagnostic("VDLS-NAME-007",f"asset reference `{ref}` does not resolve")
            result={"kind":kind.title(),"id":node_id(kind,state),"assetRef":ref,**common}
            if by_name.get("trim"):
                trim=by_name["trim"][0]
                if len(trim)!=3:
                    raise Diagnostic("VDLS-PARSE-003","trim requires start and end")
                result["sourceRange"]={"start":ratio(trim[1]),"end":ratio(trim[2])}
            if kind=="audio":
                result["gain"]=by_name.get("gain",[[None,"0dB"]])[0][1]
                result["pan"]=by_name.get("pan",[[None,"0"]])[0][1]
            if by_name.get("speed"):
                speed_clause=by_name["speed"][0]
                if len(speed_clause)!=2 or not PLAIN_NUMBER.match(
                        str(speed_clause[1])):
                    raise Diagnostic("VDLS-TYPE-004","speed requires a ratio")
                speed_value=Fraction(str(speed_clause[1]))
                if speed_value<=0:
                    raise Diagnostic("VDLS-TYPE-004","speed must be positive")
                result["speed"]={
                    "num":speed_value.numerator,"den":speed_value.denominator}
            for fade_name,key in (("fade-in","fadeIn"),("fade-out","fadeOut")):
                if by_name.get(fade_name):
                    fade_clause=by_name[fade_name][0]
                    if (len(fade_clause)!=2
                            or not isinstance(fade_clause[1],str)
                            or not NUMBER.match(fade_clause[1])):
                        raise Diagnostic(
                            "VDLS-PARSE-003",f"{fade_name} requires a duration")
                    fade_duration=ratio(fade_clause[1])
                    if fade_duration.get("unit") or Fraction(
                            fade_duration["num"],fade_duration["den"])<=0:
                        raise Diagnostic(
                            "VDLS-TYPE-004",f"{fade_name} requires positive time")
                    result[key]=fade_duration
            return result
        if kind=="text":
            if len(form)<2:
                raise Diagnostic("VDLS-PARSE-003","text requires content")
            content_value=form[1]
            if (isinstance(content_value,list) and len(content_value)==2
                    and content_value[0]=="tr" and isinstance(content_value[1],str)):
                content_value=translate(content_value[1])
            text_effects=[]
            for helper in ("typewriter","reveal-lines","text-fade-in"):
                if len(by_name.get(helper,[]))>1:
                    raise Diagnostic(
                        "VDLS-PARSE-004",f"duplicate text effect `{helper}`")
                for helper_clause in by_name.get(helper,[]):
                    if (len(helper_clause)!=2
                            or not isinstance(helper_clause[1],str)
                            or not NUMBER.match(helper_clause[1])):
                        raise Diagnostic(
                            "VDLS-PARSE-003",f"{helper} requires a duration")
                    helper_duration=ratio(helper_clause[1])
                    if helper_duration.get("unit"):
                        raise Diagnostic(
                            "VDLS-TYPE-004",f"{helper} requires a time duration")
                    if Fraction(
                            helper_duration["num"],helper_duration["den"])<=0:
                        raise Diagnostic(
                            "VDLS-TYPE-004",f"{helper} duration must be positive")
                    text_effects.append({
                        "kind":helper,"duration":helper_duration})
            effect_kinds={effect["kind"] for effect in text_effects}
            if {"typewriter","reveal-lines"}<=effect_kinds:
                raise Diagnostic(
                    "VDLS-TYPE-001",
                    "typewriter and reveal-lines cannot be combined")
            if len(by_name.get("highlight-words",[]))>1:
                raise Diagnostic(
                    "VDLS-PARSE-004","duplicate text effect `highlight-words`")
            if by_name.get("highlight-words"):
                highlight_clause=by_name["highlight-words"][0]
                timings=[]
                previous_end=Fraction(0)
                for timing in highlight_clause[1:]:
                    if (not isinstance(timing,list)
                            or len(timing) not in {2,3}):
                        raise Diagnostic(
                            "VDLS-PARSE-003",
                            "highlight-words requires timing pairs")
                    values=timing[-2:]
                    if not all(isinstance(value,str) and NUMBER.match(value)
                               for value in values):
                        raise Diagnostic(
                            "VDLS-TYPE-004",
                            "highlight word timing requires durations")
                    start_value,end_value=ratio(values[0]),ratio(values[1])
                    if start_value.get("unit") or end_value.get("unit"):
                        raise Diagnostic(
                            "VDLS-TYPE-004","invalid highlight word timing")
                    start_q=Fraction(start_value["num"],start_value["den"])
                    end_q=Fraction(end_value["num"],end_value["den"])
                    if start_q<previous_end or end_q<=start_q:
                        raise Diagnostic(
                            "VDLS-SUB-003",
                            "highlight word timings must be ordered and non-overlapping")
                    previous_end=end_q
                    timings.append({"start":start_value,"end":end_value})
                if not timings:
                    raise Diagnostic(
                        "VDLS-PARSE-003","highlight-words requires timings")
                text_effects.append({
                    "kind":"highlight-words","timings":timings})
            reveal_kinds={
                effect["kind"] for effect in text_effects
                if effect["kind"] in {
                    "typewriter","reveal-lines","highlight-words"}}
            if len(reveal_kinds)>1:
                raise Diagnostic(
                    "VDLS-TYPE-001",
                    "typewriter, reveal-lines, and highlight-words "
                    "are mutually exclusive")
            return {"kind":"Text","id":node_id("text",state),
                    "content":normalize_expression(content_value),
                    "layout":{k:v[0][1:] for k,v in by_name.items()
                              if k in {"position","anchor","align","box"}},
                    "style":{k:v[0][1:] for k,v in by_name.items()
                             if k in {"font","fill","stroke","shadow"}},
                    "textEffects":text_effects,
                    **common}
        if kind=="shape":
            if len(form)<2 or not isinstance(form[1],str):
                raise Diagnostic("VDLS-PARSE-003","shape requires a shape kind")
            return {"kind":"Shape","id":node_id("shape",state),"shapeKind":form[1],
                    "geometry":form[2:],**common}
        if kind=="group":
            children=[compile_node(item,inherited_duration) for item in form[1:]
                      if isinstance(item,list) and item and item[0] in
                      {"video","audio","text","shape","group","subtitles"}]
            if not children:
                raise Diagnostic("VDLS-PARSE-003","group requires at least one child node")
            return {"kind":"Group","id":node_id("group",state),"children":children,**common}
        if kind=="subtitles":
            refs=[item for item in form[1:] if isinstance(item,list) and item
                  and item[0]=="asset-ref"]
            if len(refs)!=1 or len(refs[0])!=2:
                raise Diagnostic("VDLS-PARSE-003","subtitles requires one asset-ref")
            ref=refs[0][1]
            if ref not in assets:
                raise Diagnostic("VDLS-NAME-007",
                                 f"asset reference `{ref}` does not resolve")
            language=by_name.get("language",[[None,None]])[0][1]
            style=by_name.get("style",[[None,None]])[0][1]
            burn_in=by_name.get("burn-in",[[None,Symbol("true")]])[0][1]
            sidecar=None
            if by_name.get("sidecar"):
                clause=by_name["sidecar"][0]
                if len(clause)==2 and isinstance(clause[1],str):
                    sidecar_path=str(clause[1])
                    extension=Path(sidecar_path).suffix.lower()
                    if extension not in {".srt",".vtt"}:
                        raise Diagnostic(
                            "VDLS-PARSE-007",
                            "subtitle sidecar extension must be .srt or .vtt")
                    sidecar={"path":sidecar_path,
                             "format":extension.removeprefix(".")}
                else:
                    raise Diagnostic(
                        "VDLS-PARSE-003",
                        "sidecar requires an explicit .srt or .vtt path")
            cues=[]
            asset_source=assets[ref]["source"]
            if asset_source["kind"]=="File":
                subtitle_path=(source.parent/asset_source["path"]).resolve()
                if subtitle_path.exists() and subtitle_path.suffix.lower() in {".srt",".vtt"}:
                    cues=parse_subtitles(
                        subtitle_path.read_text(encoding="utf-8"),subtitle_path.suffix)
            return {"kind":"Subtitles","id":node_id("subtitles",state),
                    "assetRef":ref,"language":language,"style":style,
                    "burnIn":str(burn_in) in {"true","#t"},
                    "sidecar":sidecar,
                    "track":{"id":str(ref),"language":language,"kind":"subtitles",
                             "cues":cues,"style":style,"metadata":{},"span":None},
                    **common}
        raise Diagnostic("VDLS-PARSE-002",f"unsupported node `{kind}`")
    for clause in p[1:]:
        if not isinstance(clause,list) or not clause: raise Diagnostic("VDLS-PARSE-002", "malformed project clause")
        tag=clause[0]
        if tag=="id" and len(clause)==2 and isinstance(clause[1],str):
            if project_id: raise Diagnostic("VDLS-PARSE-004", "duplicate project id")
            project_id=clause[1]
        elif tag=="asset":
            if len(clause)<3 or not isinstance(clause[1],str): raise Diagnostic("VDLS-PARSE-003", "asset requires identifier and source")
            aid=clause[1]
            if aid in assets: raise Diagnostic("VDLS-NAME-002", f"duplicate asset `{aid}`")
            src=clause[2]
            if not isinstance(src,list) or len(src)<2 or src[0] not in {"file","url","generated","plugin-source"}:
                raise Diagnostic("VDLS-PARSE-002", "unsupported asset source")
            if src[0]=="file":
                source_value={"kind":"File","path":src[1]}
            elif src[0]=="url":
                checksum=next((x[1] for x in src[2:] if isinstance(x,list)
                               and len(x)==2 and x[0]=="checksum"),None)
                if checksum is None:
                    raise Diagnostic("VDLS-PARSE-003","url source requires checksum")
                source_value={"kind":"Url","url":src[1],"checksum":checksum}
            elif src[0]=="generated":
                source_value={"kind":"Generated","generator":src[1:]}
            else:
                source_value={"kind":"Plugin","plugin":src[1],"arguments":src[2:]}
            integrity_clause=next((item for item in clause[3:]
                                   if isinstance(item,list) and item
                                   and item[0]=="integrity"),None)
            integrity=None
            if integrity_clause:
                if (len(integrity_clause)!=2
                        or not isinstance(integrity_clause[1],list)
                        or len(integrity_clause[1])!=2
                        or integrity_clause[1][0]!="sha256"
                        or not re.fullmatch(
                            r"(?:sha256:)?[0-9a-fA-F]{64}",
                            str(integrity_clause[1][1]))):
                    raise Diagnostic("VDLS-PARSE-003","invalid asset integrity")
                digest=str(integrity_clause[1][1]).lower()
                integrity="sha256:"+digest.removeprefix("sha256:")
            color_clause=next((item for item in clause[3:]
                               if isinstance(item,list) and item
                               and item[0]=="color"),None)
            asset_color=(parse_color_descriptor(
                color_clause[1:],{
                    "primaries":"unknown","transfer":"unknown",
                    "matrix":"unknown","range":"unknown",
                }) if color_clause else None)
            assets[aid]={"kind":"Asset","id":node_id("asset",state),"assetId":aid,
                         "source":source_value,"mediaKind":None,"metadata":None,
                         "integrity":integrity,
                         "color":asset_color,
                         "proxyPolicy":None,"cachePolicy":None,
                         "span":None,"annotations":{}}
        elif tag=="scene":
            if len(clause)<3: raise Diagnostic("VDLS-PARSE-003", "scene requires identifier and clauses")
            sid=clause[1]; layers=[]
            duration_clauses=[c for c in clause[2:] if isinstance(c,list) and c and c[0]=="duration"]
            if len(duration_clauses)>1:
                raise Diagnostic("VDLS-PARSE-004","duplicate scene duration")
            scene_duration=duration(duration_clauses[0]) if duration_clauses else None
            for c in clause[2:]:
                if not isinstance(c,list) or not c: continue
                if c[0]=="layer":
                    if len(c)<3 or not isinstance(c[1],str) or not c[1].lstrip("+-").isdigit(): raise Diagnostic("VDLS-PARSE-003", "layer requires integer z-index and node")
                    content=c[2]
                    if not isinstance(content,list) or not content: raise Diagnostic("VDLS-PARSE-002", "invalid layer node")
                    media=compile_node(content,scene_duration)
                    timing=clauses_by_name(c[3:])
                    layer_start=duration(timing["start"][0]) if timing.get("start") else {"num":0,"den":1}
                    layer_duration=duration(timing["duration"][0]) if timing.get("duration") else scene_duration
                    layers.append({"kind":"Layer","id":node_id("layer",state),
                                   "zIndex":int(c[1]),"start":layer_start,
                                   "duration":layer_duration,"enabled":True,
                                   "blendMode":media["blendMode"],"opacity":media["opacity"],
                                   "content":media,"masks":[],"effects":media["effects"],
                                   "span":None,"annotations":{}})
            scenes.append({"kind":"Scene","id":node_id("scene",state),"sceneId":sid,
                           "start":{"num":0,"den":1},"duration":scene_duration,
                           "background":None,
                           "layers":sorted(enumerate(layers),key=lambda x:(x[1]["zIndex"],x[0])),
                           "markers":[],"metadata":{},
                           "span":None,"annotations":{}})
            scenes[-1]["layers"]=[item[1] for item in scenes[-1]["layers"]]
        elif tag=="output":
            out_id=path=video=audio=None; scene_refs=[]; metadata={}
            for c in clause[1:]:
                if isinstance(c,list) and c:
                    if c[0]=="id" and len(c)==2: out_id=c[1]
                    if c[0]=="file" and len(c)==2: path=c[1]
                    if c[0]=="video" and len(c)>=3 and c[1][0]=="size" and c[2][0]=="fps": video={"width":int(c[1][1]),"height":int(c[1][2]),"frameRate":plain_ratio(c[2][1])}
                    if c[0]=="preset" and len(c)==2:
                        preset=str(c[1])
                        if preset not in OUTPUT_PRESETS:
                            raise Diagnostic("VDLS-PARSE-007",
                                             f"unknown output preset `{preset}`")
                        preset_value=OUTPUT_PRESETS[preset]
                        if video is None: video=dict(preset_value["video"])
                    if c[0]=="audio":
                        sample=next((x[1] for x in c[1:] if isinstance(x,list)
                                     and len(x)==2 and x[0]=="sample-rate"),None)
                        channels=next((x[1] for x in c[1:] if isinstance(x,list)
                                       and len(x)==2 and x[0]=="channels"),"stereo")
                        if sample is None: raise Diagnostic("VDLS-PARSE-003","audio output requires sample-rate")
                        audio={"sampleRate":int(sample),"channelLayout":channels,
                               "sampleFormat":None,"codec":None,"codecOptions":{}}
                    if c[0] in {"scene","scene-ref","use-scene"} and len(c)==2:
                        scene_refs.append(c[1])
                    if c[0]=="metadata" and len(c)==3:
                        metadata[str(c[1])]=c[2]
            if not out_id or not path or (not video and not audio):
                raise Diagnostic("VDLS-PARSE-003", "output requires id, file, and video or audio specification")
            outputs.append({"kind":"Output","id":node_id("output",state),"outputId":out_id,
                            "path":path,"container":Path(path).suffix.lstrip("."),
                            "video":video,"audio":audio,"sceneRefs":scene_refs,
                            "metadata":metadata,"span":None,"annotations":{}})
        elif tag=="requires-vdls":
            if len(clause)!=2 or not isinstance(clause[1],str):
                raise Diagnostic("VDLS-PARSE-003","requires-vdls requires a version constraint")
            constraint=clause[1]
            if re.search(r"(?:>=|>|=)\s*2(?:\.|$)",constraint) or re.search(
                    r"<\s*1(?:\.0)?(?:\s|$)",constraint):
                raise Diagnostic("VDLS-VERSION-001",
                                 f"unsupported VDLS version constraint `{constraint}`")
            project_annotations["vdls.requires"]=constraint
        elif tag in {"settings","project-settings","build-options"}:
            if settings is not None:
                raise Diagnostic("VDLS-PARSE-004","duplicate project settings")
            settings={"clauses":clause[1:]}
        elif tag=="color-management":
            if not color_management["inherited"]:
                raise Diagnostic(
                    "VDLS-PARSE-004","duplicate color-management")
            by_color=clauses_by_name(clause[1:])
            if len(by_color.get("working-space",[]))>1 or len(
                    by_color.get("output",[]))>1:
                raise Diagnostic(
                    "VDLS-PARSE-004","duplicate color descriptor")
            if by_color.get("working-space"):
                color_management["workingSpace"]=parse_color_descriptor(
                    by_color["working-space"][0][1:],
                    color_management["workingSpace"])
            if by_color.get("output"):
                color_management["output"]=parse_color_descriptor(
                    by_color["output"][0][1:],
                    color_management["output"])
            policy=by_color.get("unknown-metadata",[])
            if policy:
                if len(policy)!=1 or len(policy[0])!=2 or str(
                        policy[0][1]) not in {
                            "strict","warn-and-default","preserve"}:
                    raise Diagnostic(
                        "VDLS-PARSE-007","invalid unknown metadata policy")
                color_management["unknownMetadataPolicy"]=str(policy[0][1])
            tone=by_color.get("tone-map",[])
            if tone:
                if len(tone)!=1:
                    raise Diagnostic("VDLS-PARSE-004","duplicate tone-map")
                tone_values=clauses_by_name(tone[0][1:])
                operator=str(tone_values.get(
                    "operator",[[None,"bt2390"]])[0][1])
                if operator not in {"bt2390","hable","reinhard","mobius","clip"}:
                    raise Diagnostic(
                        "VDLS-PARSE-007",f"invalid tone-map operator `{operator}`")
                nits=str(tone_values.get(
                    "target-nits",[[None,"100"]])[0][1])
                if not PLAIN_NUMBER.match(nits) or Fraction(nits)<=0:
                    raise Diagnostic(
                        "VDLS-TYPE-004","target-nits must be positive")
                color_management["toneMap"]={
                    "operator":operator,"targetNits":str(Fraction(nits))}
            color_management["inherited"]=False
        elif tag=="locale":
            project_annotations.setdefault("vdls.locales",[]).append(clause[1:])
        else:
            raise Diagnostic("VDLS-PARSE-002",f"unknown project form `{tag}`")
    if not project_id: raise Diagnostic("VDLS-PARSE-003", "project requires id")
    scene_ids=[scene["sceneId"] for scene in scenes]
    output_ids=[output["outputId"] for output in outputs]
    if len(scene_ids) != len(set(scene_ids)):
        raise Diagnostic("VDLS-NAME-002", "duplicate scene identifier")
    if len(output_ids) != len(set(output_ids)):
        raise Diagnostic("VDLS-NAME-002", "duplicate output identifier")
    known_scenes=set(scene_ids)
    for output in outputs:
        for scene_ref in output["sceneRefs"]:
            if scene_ref not in known_scenes:
                raise Diagnostic("VDLS-NAME-008",f"scene reference `{scene_ref}` does not resolve")
    if color_management["unknownMetadataPolicy"]=="strict":
        unknown_asset=next((
            asset for asset in assets.values()
            if asset["source"]["kind"]=="File"
            and (not asset.get("color") or "unknown" in asset["color"].values())
        ),None)
        if unknown_asset:
            raise Diagnostic(
                "VDLS-COLOR-001",
                f"asset `{unknown_asset['assetId']}` lacks required color metadata")
    hdr_asset=next((
        asset for asset in assets.values()
        if asset.get("color")
        and asset["color"].get("transfer") in {"pq","hlg"}
    ),None)
    if (hdr_asset
            and color_management["output"]["transfer"] not in {"pq","hlg"}
            and not color_management.get("toneMap")):
        raise Diagnostic(
            "VDLS-COLOR-003",
            "HDR-to-SDR output requires an explicit tone-map policy")
    root={"kind":"Project","id":node_id("project",state),"projectId":project_id,
          "astVersion":"1.0.0","settings":settings,"imports":import_nodes,
          "colorManagement":color_management,
          "assets":list(assets.values()),"templates":[],"scenes":scenes,
          "outputs":outputs,"span":None,"annotations":project_annotations}
    return {"astVersion":"1.0.0","node":root}

def graph(ast: dict[str,Any]) -> dict[str,Any]:
    nodes=[]; edges=[]; terminal_by_scene={}
    assets={asset["assetId"]:asset for asset in ast["node"]["assets"]}

    def port(name: str, media_type: str, optional: bool=False) -> dict[str,Any]:
        return {"name":name,"mediaType":media_type,"format":None,"optional":optional}

    def add_node(node_id_: str, kind: str, inputs: list[dict[str,Any]],
                 outputs: list[dict[str,Any]], params: dict[str,Any],
                 time_domain: dict[str,Any] | None=None) -> None:
        semantic=canonical_json({"kind":kind,"params":params}).encode("utf-8")
        nodes.append({
            "id":node_id_,"kind":kind,"inputs":inputs,"outputs":outputs,
            "params":params,"timeDomain":time_domain,
            "capabilities":[],"cachePolicy":{"mode":"content","keySalt":"",
            "reusableAcrossTargets":True},"purity":"pure",
            "resources":{"cpuThreads":1,"memoryBytes":0,"gpuRequired":False,
            "gpuMemoryBytes":0,"temporaryBytes":0},
            "sourceLocation":None,
            "cacheKey":"sha256:"+hashlib.sha256(semantic).hexdigest(),
        })

    def lower_asset(content: dict[str,Any], media: str) -> tuple[str,str]:
        asset=assets[content["assetRef"]]; source=asset["source"]
        if source["kind"]=="Generated":
            generator=source["generator"][0] if source["generator"] else []
            generator_name=generator[0] if isinstance(generator,list) and generator else "unknown"
            generated=f"rg:generate:{content['id']}"
            if media=="audio":
                kind="core/generate-audio"; output=port("audio","audio-stream")
            else:
                kind="core/generate-solid" if generator_name=="solid-color" else "core/generate-visual"
                output=port("surface","frame-surface")
            add_node(generated,kind,[],[output],
                     {"assetId":content["assetRef"],"generator":source["generator"]})
            return generated,output["name"]
        resolve=f"rg:resolve:{content['id']}"
        probe=f"rg:probe:{content['id']}"
        decode=f"rg:decode-{media}:{content['id']}"
        add_node(resolve,"core/resolve-asset",[],[port("file","file")],
                 {"assetId":content["assetRef"],"source":source})
        add_node(probe,"core/probe-media",[port("file","file")],[port("metadata","metadata")],{})
        if media=="audio":
            output=port("audio","audio-stream")
        else:
            output=port("surface","frame-surface")
        add_node(decode,f"core/decode-{media}",[port("file","file")],[output],{})
        edges.extend([
            {"fromNode":resolve,"fromPort":"file","toNode":probe,"toPort":"file"},
            {"fromNode":resolve,"fromPort":"file","toNode":decode,"toPort":"file"},
        ])
        return decode,output["name"]

    def lower_visual(content: dict[str,Any], time_domain: dict[str,Any]) -> tuple[str,str]:
        if content["kind"]=="Video":
            produced,output_port=lower_asset(content,"video")
        elif content["kind"]=="Text":
            produced=f"rg:render-text:{content['id']}"; output_port="surface"
            add_node(produced,"core/render-text",[],[port(output_port,"frame-surface")],
                     {"content":content["content"],"layout":content.get("layout",{}),
                      "style":content.get("style",{}),
                      "textEffects":content.get("textEffects",[])},time_domain)
        elif content["kind"]=="Shape":
            produced=f"rg:shape:{content['id']}"; output_port="surface"
            add_node(produced,"core/generate-shape",[],[port(output_port,"frame-surface")],
                     {"shapeKind":content["shapeKind"],"geometry":content["geometry"]},time_domain)
        elif content["kind"]=="Subtitles":
            resolve=f"rg:resolve:{content['id']}"
            produced=f"rg:render-subtitles:{content['id']}"; output_port="surface"
            add_node(resolve,"core/resolve-asset",[],[port("file","file")],
                     {"assetId":content["assetRef"],
                      "source":assets[content["assetRef"]]["source"]})
            add_node(produced,"core/render-subtitles",[port("file","file")],
                     [port(output_port,"frame-surface")],
                     {"language":content["language"],"style":content["style"],
                      "track":content["track"]},time_domain)
            edges.append({"fromNode":resolve,"fromPort":"file",
                          "toNode":produced,"toPort":"file"})
        elif content["kind"]=="Group":
            lowered=[lower_visual(child,time_domain) for child in content["children"]
                     if child["kind"]!="Audio"]
            if not lowered: raise Diagnostic("VDLS-GRAPH-005","visual group has no visual producer")
            produced,output_port=lowered[0]
            for index,(child_node,child_port) in enumerate(lowered[1:],1):
                composite=f"rg:group-composite:{content['id']}:{index}"
                add_node(composite,"core/composite",
                         [port("background","frame-surface"),port("foreground","frame-surface")],
                         [port("surface","frame-surface")],{"blendMode":"normal"},time_domain)
                edges.extend([
                    {"fromNode":produced,"fromPort":output_port,"toNode":composite,"toPort":"background"},
                    {"fromNode":child_node,"fromPort":child_port,"toNode":composite,"toPort":"foreground"},
                ])
                produced,output_port=composite,"surface"
        else:
            raise Diagnostic("VDLS-GRAPH-006",f"unsupported visual node `{content['kind']}`")
        if content.get("transform"):
            transformed=f"rg:transform:{content['id']}"
            add_node(transformed,"core/transform",[port("surface","frame-surface")],
                     [port("surface","frame-surface")],{"transform":content["transform"]},time_domain)
            edges.append({"fromNode":produced,"fromPort":output_port,
                          "toNode":transformed,"toPort":"surface"})
            produced,output_port=transformed,"surface"
        if content.get("effects"):
            filtered=f"rg:filters:{content['id']}"
            add_node(filtered,"core/filter-chain",[port("surface","frame-surface")],
                     [port("surface","frame-surface")],{"effects":content["effects"]},time_domain)
            edges.append({"fromNode":produced,"fromPort":output_port,
                          "toNode":filtered,"toPort":"surface"})
            produced,output_port=filtered,"surface"
        return produced,output_port

    for scene in ast["node"]["scenes"]:
        previous=None; previous_port=None; audio_nodes=[]; sidecar_nodes=[]
        for index, layer in enumerate(scene["layers"]):
            content=layer["content"]
            time_domain={"start":layer["start"],"duration":layer["duration"],"rate":None}
            if content["kind"]=="Audio":
                audio_nodes.append(lower_asset(content,"audio"))
                continue
            if content["kind"]=="Subtitles" and content.get("sidecar"):
                export=f"rg:export-subtitles:{content['id']}"
                add_node(
                    export,"core/export-subtitles",[],
                    [port("file","file")],{
                        "track":content["track"],
                        "path":content["sidecar"]["path"],
                        "format":content["sidecar"]["format"],
                    },time_domain)
                sidecar_nodes.append((export,"file",content["sidecar"]))
                if not content.get("burnIn"): continue
            produced,produced_port=lower_visual(content,time_domain)
            if previous is None:
                previous,previous_port=produced,produced_port
            else:
                composite=f"rg:composite:{scene['id']}:{index}"
                add_node(composite,"core/composite",
                         [port("background","frame-surface"),port("foreground","frame-surface")],
                         [port("surface","frame-surface")],
                         {"blendMode":layer["blendMode"],"opacity":layer["opacity"],
                          "zIndex":layer["zIndex"]},
                         {"start":{"num":0,"den":1},"duration":scene["duration"],"rate":None})
                edges.extend([
                    {"fromNode":previous,"fromPort":previous_port,"toNode":composite,"toPort":"background"},
                    {"fromNode":produced,"fromPort":produced_port,"toNode":composite,"toPort":"foreground"},
                ])
                previous,previous_port=composite,"surface"
        audio_terminal=None
        if audio_nodes:
            if len(audio_nodes)==1:
                audio_terminal=audio_nodes[0]
            else:
                mixer=f"rg:mix-audio:{scene['id']}"
                inputs=[port(f"track{i}","audio-stream") for i in range(len(audio_nodes))]
                add_node(mixer,"core/mix-audio",inputs,[port("audio","audio-stream")],{},
                         {"start":{"num":0,"den":1},"duration":scene["duration"],"rate":None})
                for i,(audio_node,audio_port) in enumerate(audio_nodes):
                    edges.append({"fromNode":audio_node,"fromPort":audio_port,
                                  "toNode":mixer,"toPort":f"track{i}"})
                audio_terminal=(mixer,"audio")
        terminal_by_scene[scene["sceneId"]]={"visual":(previous,previous_port) if previous else None,
                                             "audio":audio_terminal,
                                             "sidecars":sidecar_nodes}

    targets=[]
    default_scene=ast["node"]["scenes"][0]["sceneId"] if ast["node"]["scenes"] else None
    for output in ast["node"]["outputs"]:
        chosen=output["sceneRefs"][0] if output["sceneRefs"] else default_scene
        terminals=terminal_by_scene.get(chosen,{})
        source_terminal=terminals.get("visual")
        audio_terminal=terminals.get("audio")
        encode=f"rg:encode-video:{output['id']}"
        write=f"rg:write-file:{output['id']}"
        mux_inputs=[]; encoded_outputs=[]
        if output["video"]:
            color_node=f"rg:color-convert:{output['id']}"
            add_node(
                color_node,"core/color-convert",
                [port("surface","frame-surface")],
                [port("surface","frame-surface")],{
                    "workingSpace":ast["node"]["colorManagement"]["workingSpace"],
                    "output":ast["node"]["colorManagement"]["output"],
                    "toneMap":ast["node"]["colorManagement"]["toneMap"],
                    "unknownMetadataPolicy":
                        ast["node"]["colorManagement"]["unknownMetadataPolicy"],
                })
            add_node(encode,"core/encode-video",[port("video","frame-surface")],[port("video","file")],
                     {"video":output["video"],"targetId":output["outputId"]})
            mux_inputs.append(port("video","file")); encoded_outputs.append((encode,"video","video"))
            if source_terminal:
                edges.append({"fromNode":source_terminal[0],"fromPort":source_terminal[1],
                              "toNode":color_node,"toPort":"surface"})
                edges.append({"fromNode":color_node,"fromPort":"surface",
                              "toNode":encode,"toPort":"video"})
        if output["audio"]:
            resample=f"rg:resample-audio:{output['id']}"
            encode_audio=f"rg:encode-audio:{output['id']}"
            add_node(resample,"core/resample-audio",[port("audio","audio-stream")],
                     [port("audio","audio-stream")],output["audio"])
            add_node(encode_audio,"core/encode-audio",[port("audio","audio-stream")],
                     [port("audio","file")],{"audio":output["audio"],"targetId":output["outputId"]})
            if audio_terminal:
                edges.append({"fromNode":audio_terminal[0],"fromPort":audio_terminal[1],
                              "toNode":resample,"toPort":"audio"})
            edges.append({"fromNode":resample,"fromPort":"audio",
                          "toNode":encode_audio,"toPort":"audio"})
            mux_inputs.append(port("audio","file")); encoded_outputs.append((encode_audio,"audio","audio"))
        mux=f"rg:mux:{output['id']}"
        add_node(mux,"core/mux",mux_inputs,[port("file","file")],
                 {"container":output["container"],"targetId":output["outputId"]})
        for encoded_node,encoded_port,mux_port in encoded_outputs:
            edges.append({"fromNode":encoded_node,"fromPort":encoded_port,
                          "toNode":mux,"toPort":mux_port})
        add_node(write,"core/write-file",[port("file","file")],[port("file","file")],
                 {"path":output["path"],"targetId":output["outputId"]})
        edges.append({"fromNode":mux,"fromPort":"file","toNode":write,"toPort":"file"})
        targets.append({"id":output["outputId"],"terminalNode":write,
                        "terminalPort":"file","outputSpec":output})
        for sidecar_index,(sidecar_node,sidecar_port,sidecar) in enumerate(
                terminals.get("sidecars",[]),1):
            targets.append({
                "id":f"{output['outputId']}:sidecar:{sidecar_index}",
                "terminalNode":sidecar_node,
                "terminalPort":sidecar_port,
                "outputSpec":{
                    "kind":"subtitle-sidecar",
                    "path":sidecar["path"],
                    "format":sidecar["format"],
                },
            })
    result={"graphVersion":"1.0.0","nodes":sorted(nodes,key=lambda n:n["id"]),
            "edges":sorted(edges,key=lambda e:(e["fromNode"],e["fromPort"],e["toNode"],e["toPort"])),
            "targets":targets,"metadata":{}}
    validate_graph(result)
    return result

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"

def validate_graph(value: dict[str,Any]) -> None:
    node_map={node["id"]:node for node in value.get("nodes",[])}
    if len(node_map) != len(value.get("nodes",[])):
        raise Diagnostic("VDLS-GRAPH-004","duplicate graph node ID")
    adjacency={node_id_:[] for node_id_ in node_map}
    connected_inputs: set[tuple[str,str]]=set()
    for edge in value.get("edges",[]):
        if edge["fromNode"] not in node_map or edge["toNode"] not in node_map:
            raise Diagnostic("VDLS-GRAPH-002","graph edge references missing node")
        source_ports={p["name"]:p["mediaType"] for p in node_map[edge["fromNode"]]["outputs"]}
        target_ports={p["name"]:p["mediaType"] for p in node_map[edge["toNode"]]["inputs"]}
        if edge["fromPort"] not in source_ports or edge["toPort"] not in target_ports:
            raise Diagnostic("VDLS-GRAPH-002","graph edge references missing port")
        if source_ports[edge["fromPort"]] != target_ports[edge["toPort"]]:
            raise Diagnostic("VDLS-GRAPH-003","incompatible graph port types")
        adjacency[edge["fromNode"]].append(edge["toNode"])
        connected_inputs.add((edge["toNode"],edge["toPort"]))
    for node in value.get("nodes",[]):
        for input_port in node["inputs"]:
            if not input_port["optional"] and (node["id"],input_port["name"]) not in connected_inputs:
                raise Diagnostic("VDLS-GRAPH-002",
                                 f"required input `{node['id']}:{input_port['name']}` is unconnected")
    visiting=set(); visited=set()
    def visit(node_id_:str) -> None:
        if node_id_ in visiting: raise Diagnostic("VDLS-GRAPH-001","graph contains a cycle")
        if node_id_ in visited: return
        visiting.add(node_id_)
        for child in adjacency[node_id_]: visit(child)
        visiting.remove(node_id_); visited.add(node_id_)
    for node_id_ in node_map: visit(node_id_)
    for target in value.get("targets",[]):
        if target["terminalNode"] not in node_map:
            raise Diagnostic("VDLS-GRAPH-005","output target has no reachable producer")

def discover(path: str | None) -> Path:
    if path:
        candidate=Path(path).resolve()
        if candidate.is_dir():
            config=candidate/"vdls.toml"
            if config.exists():
                data=tomllib.loads(config.read_text(encoding="utf-8"))
                return (candidate/data.get("entry","main.vdsl")).resolve()
            for name in ("project.vdsl","main.vdsl"):
                if (candidate/name).exists(): return (candidate/name).resolve()
        return candidate
    cur=Path.cwd()
    while True:
        if (cur/"vdls.toml").exists():
            data=tomllib.loads((cur/"vdls.toml").read_text(encoding="utf-8"))
            return (cur/data.get("entry","main.vdsl")).resolve()
        for name in ("project.vdsl","main.vdsl"):
            if (cur/name).exists(): return cur/name
        if cur.parent==cur: break
        cur=cur.parent
    raise Diagnostic("VDLS-CLI-005", "project not found")

def _merge_config(base: dict[str,Any], overlay: dict[str,Any]) -> dict[str,Any]:
    result=dict(base)
    for key,value in overlay.items():
        if isinstance(value,dict) and isinstance(result.get(key),dict):
            result[key]=_merge_config(result[key],value)
        else:
            result[key]=value
    return result

def load_config(source: Path, explicit: str | None=None,
                profile: str | None=None) -> dict[str,Any]:
    config: dict[str,Any]={
        "spec":"1.0","entry":source.name,"output_dir":"dist",
        "cache_dir":".vdls/cache",
        "build":{"profile":"release","jobs":os.cpu_count() or 1,
                 "warnings_as_errors":False},
        "backend":{"ffmpeg":{"executable":"ffmpeg","probe_executable":"ffprobe"}},
        "plugins":{"lockfile":"vdls.lock","allow_network":False},
    }
    config_path=Path(explicit).resolve() if explicit else source.parent/"vdls.toml"
    project_data={}
    if config_path.exists():
        try: project_data=tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise Diagnostic("VDLS-CONFIG-002",f"invalid TOML: {error}")
        known={"spec","entry","output_dir","cache_dir","build","backend",
               "plugins","profile"}
        unknown=sorted(set(project_data)-known)
        if unknown:
            raise Diagnostic("VDLS-CONFIG-003",
                             f"unknown configuration key `{unknown[0]}`")
        profiles=project_data.pop("profile",{})
        config=_merge_config(config,project_data)
        selected=profile or config.get("build",{}).get("profile")
        if selected and profiles:
            if selected not in profiles:
                raise Diagnostic("VDLS-CONFIG-005",f"profile not found: {selected}")
            config=_merge_config(config,profiles[selected])
    for name,raw in os.environ.items():
        if not name.startswith("VDLS_"): continue
        keys=[key.lower() for key in name[5:].split("__")]
        try: value=tomllib.loads("value = "+raw)["value"]
        except tomllib.TOMLDecodeError: value=raw
        cursor=config
        for key in keys[:-1]:
            cursor=cursor.setdefault(key,{})
            if not isinstance(cursor,dict):
                raise Diagnostic("VDLS-CONFIG-007",
                                 f"environment key `{name}` conflicts with scalar")
        cursor[keys[-1]]=value
    spec=str(config.get("spec",""))
    if not spec.startswith("1."):
        raise Diagnostic("VDLS-CONFIG-009",f"unsupported specification version `{spec}`")
    return config

def validate_lockfile(project_root: Path, config: dict[str,Any]) -> tuple[Path,str]:
    lock_name=config.get("plugins",{}).get("lockfile","vdls.lock")
    lock_path=(project_root/lock_name).resolve()
    if not lock_path.exists():
        raise Diagnostic("VDLS-CONFIG-010","lockfile required but missing")
    try: lock=json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError,UnicodeDecodeError):
        raise Diagnostic("VDLS-CONFIG-012","lockfile format unsupported")
    if lock.get("schema")!="vdls.lock/1":
        raise Diagnostic("VDLS-CONFIG-012","lockfile format unsupported")
    for entry in lock.get("plugins",[]):
        manifest_name=entry.get("manifest")
        if not manifest_name:
            raise Diagnostic("VDLS-PLUGIN-014",
                             f"plugin lock entry missing manifest: {entry.get('id')}")
        manifest_path=(project_root/manifest_name).resolve()
        if not manifest_path.exists():
            raise Diagnostic("VDLS-PLUGIN-001",
                             f"plugin manifest not found: {manifest_name}")
        try: manifest_data=json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError,UnicodeDecodeError):
            raise Diagnostic("VDLS-PLUGIN-002","plugin manifest invalid")
        normalized=validate_plugin_manifest(manifest_data,manifest_path)
        if normalized["id"]!=entry.get("id") or normalized["version"]!=entry.get("version"):
            raise Diagnostic("VDLS-PLUGIN-014","plugin lock identity mismatch")
        digest=plugin_package_digest(manifest_data,manifest_path)
        if digest!=entry.get("sha256"):
            raise Diagnostic("VDLS-PLUGIN-014","plugin lock digest mismatch")
    digest=hashlib.sha256(canonical_json(lock).encode("utf-8")).hexdigest()
    return lock_path,"sha256:"+digest

PLUGIN_CAPABILITIES={
    "syntax.reader","node.source","node.effect","node.generator",
    "asset.provider","analyzer.media","compiler.ast","compiler.graph",
    "backend.render","exporter.sidecar","cli.command",
}
PLUGIN_PERMISSION=re.compile(
    r"^(?:fs:(?:project-read|project-write|cache)|network:(?:none|localhost|https:[^:]+)"
    r"|process:[^:]+|env:[A-Za-z_][A-Za-z0-9_]*|gpu|camera|microphone)$"
)

def plugin_package_digest(data: dict[str,Any], manifest_path: Path) -> str:
    digest=hashlib.sha256()
    digest.update(canonical_json(data).encode("utf-8"))
    root=manifest_path.parent
    for path in sorted((item for item in root.rglob("*") if item.is_file()
                        and item.resolve()!=manifest_path.resolve()),
                       key=lambda item:item.relative_to(root).as_posix()):
        relative=path.relative_to(root).as_posix().encode("utf-8")
        digest.update(struct.pack(">I",len(relative))); digest.update(relative)
        content=path.read_bytes()
        digest.update(struct.pack(">Q",len(content))); digest.update(content)
    return "sha256:"+digest.hexdigest()

def validate_plugin_manifest(data: dict[str,Any], path: Path,
                             granted: set[str] | None=None) -> dict[str,Any]:
    required={"id","name","version","abi","entry","capabilities","permissions"}
    missing=sorted(required-set(data))
    if missing:
        raise Diagnostic("VDLS-PLUGIN-002",
                         f"plugin manifest field missing `{missing[0]}`")
    for key in data:
        if key.startswith("required:"):
            raise Diagnostic("VDLS-PLUGIN-002",
                             f"unsupported required manifest field `{key}`")
    plugin_id=str(data["id"])
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+){2,}",plugin_id):
        raise Diagnostic("VDLS-PLUGIN-002","plugin id must be reverse-DNS-like")
    if not re.fullmatch(r"0|[1-9]\d*\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                        r"(?:-[0-9A-Za-z.-]+)?",str(data["version"])):
        raise Diagnostic("VDLS-PLUGIN-002","plugin version is not SemVer")
    if data["abi"]!="vdls.plugin/1":
        raise Diagnostic("VDLS-PLUGIN-003",
                         f"unsupported plugin ABI `{data['abi']}`")
    capabilities=list(data["capabilities"])
    unknown=sorted(set(capabilities)-PLUGIN_CAPABILITIES)
    if unknown:
        raise Diagnostic("VDLS-PLUGIN-005",
                         f"unknown plugin capability `{unknown[0]}`")
    permissions=list(data["permissions"])
    invalid=next((item for item in permissions
                  if not isinstance(item,str) or not PLUGIN_PERMISSION.fullmatch(item)),None)
    if invalid is not None:
        raise Diagnostic("VDLS-PLUGIN-002",
                         f"invalid plugin permission `{invalid}`")
    if granted is not None:
        denied=sorted(set(permissions)-granted)
        if denied:
            raise Diagnostic("VDLS-PLUGIN-006",
                             f"plugin permission denied `{denied[0]}`")
    entry=(path.parent/str(data["entry"])).resolve()
    try: entry.relative_to(path.parent.resolve())
    except ValueError:
        raise Diagnostic("VDLS-SECURITY-010","plugin entry escapes package root")
    return {"id":plugin_id,"name":str(data["name"]),"version":str(data["version"]),
            "abi":"vdls.plugin/1","entry":str(entry),
            "capabilities":capabilities,"permissions":permissions,
            "dependencies":dict(data.get("dependencies",{})),
            "manifestPath":str(path.resolve())}

def load_locked_plugins(project_root: Path,
                        config: dict[str,Any]) -> list[dict[str,Any]]:
    lock_path,_=validate_lockfile(project_root,config)
    lock=json.loads(lock_path.read_text(encoding="utf-8"))
    result=[]
    for entry in lock.get("plugins",[]):
        path=(project_root/entry["manifest"]).resolve()
        data=json.loads(path.read_text(encoding="utf-8"))
        result.append(validate_plugin_manifest(data,path))
    return result

class PluginProcessHost(PluginProcessHostBase):
    """Compiler-configured facade for the isolated plugin transport."""
    def __init__(self, manifest: dict[str,Any], project_root: Path,
                 cache_root: Path, granted_permissions: set[str],
                 timeout: float=10.0):
        super().__init__(
            manifest,project_root,cache_root,granted_permissions,timeout,
            diagnostic=Diagnostic,validate_manifest=validate_plugin_manifest,
            canonical_json=canonical_json,host_version=VERSION,
        )

class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        code="VDLS-CLI-001" if "unrecognized arguments" in message else "VDLS-CLI-002"
        raise Diagnostic(code,message)

def graph_dot(value: dict[str,Any]) -> str:
    lines=["digraph vdls {","  rankdir=LR;"]
    for node in value["nodes"]:
        label=node["kind"].replace('"','\\"')
        lines.append(f'  "{node["id"]}" [label="{label}"];')
    for edge in value["edges"]:
        lines.append(f'  "{edge["fromNode"]}" -> "{edge["toNode"]}";')
    lines.append("}")
    return "\n".join(lines)+"\n"

def _ratio_text(value: dict[str,int] | None) -> str:
    if not value: raise Diagnostic("VDLS-TIME-010","output duration is unbounded")
    return str(Fraction(value["num"],value["den"]))

def _decimal_text(value: Fraction) -> str:
    return f"{float(value):.12g}"

def _ffmpeg_escape_text(value: str) -> str:
    return value.replace("\\","\\\\").replace("'","\\'").replace(":","\\:")

def _default_font_file(text: str="") -> Path:
    windows_fonts=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"
    non_ascii=any(ord(char)>127 for char in text)
    candidates=([
        windows_fonts/"YuGothR.ttc",windows_fonts/"meiryo.ttc",
        windows_fonts/"msgothic.ttc",
    ] if non_ascii else [])+[
        windows_fonts/"arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists(): return candidate.resolve()
    raise Diagnostic("VDLS-TEXT-001","no deterministic default font file found")

def _ffmpeg_escape_filter_path(path: Path) -> str:
    value=path.as_posix().replace("\\","/")
    return value.replace(":","\\:")

def _rgba_components(value: Any) -> tuple[int,int,int,int]:
    text=str(value).lstrip("#")
    if len(text)==6: text+="ff"
    if not re.fullmatch(r"[0-9A-Fa-f]{8}",text):
        raise Diagnostic("VDLS-COLOR-001",f"invalid RGBA color `{value}`")
    return tuple(int(text[index:index+2],16) for index in range(0,8,2))  # type: ignore

def generated_video_source(generator: list[Any], width: int, height: int,
                           fps: Fraction, scene_duration: str,
                           seed: int) -> str:
    if not generator: raise Diagnostic("VDLS-PARSE-003","empty visual generator")
    name=str(generator[0])
    if name=="solid-color":
        if len(generator)<2: raise Diagnostic("VDLS-PARSE-003","solid-color requires color")
        color=str(generator[1]).lstrip("#")
        return f"color=c=0x{color}:s={width}x{height}:r={fps}:d={scene_duration}"
    if name in {"linear-gradient","radial-gradient"}:
        if len(generator)<3:
            raise Diagnostic("VDLS-PARSE-003",f"{name} requires two colors")
        color0=str(generator[1]).lstrip("#"); color1=str(generator[2]).lstrip("#")
        gradient_type="linear" if name=="linear-gradient" else "radial"
        options=[
            f"gradients=s={width}x{height}",f"r={fps}",f"c0=0x{color0}",
            f"c1=0x{color1}",f"type={gradient_type}","speed=0",
            f"seed={seed}",f"d={scene_duration}",
        ]
        direction=str(generator[3]) if len(generator)>3 else "horizontal"
        if gradient_type=="linear":
            if direction=="vertical":
                options.extend([f"x0={width//2}","y0=0",
                                f"x1={width//2}",f"y1={height}"])
            else:
                options.extend(["x0=0",f"y0={height//2}",
                                f"x1={width}",f"y1={height//2}"])
        else:
            options.extend([f"x0={width//2}",f"y0={height//2}",
                            f"x1={width}",f"y1={height//2}"])
        return ":".join(options)
    if name=="checkerboard":
        if len(generator)<4:
            raise Diagnostic("VDLS-PARSE-003",
                             "checkerboard requires two colors and cell size")
        first=_rgba_components(generator[1]); second=_rgba_components(generator[2])
        cell=Fraction(_unit_scalar(generator[3],"px"))
        if cell<=0: raise Diagnostic("VDLS-TYPE-009","checkerboard cell size must be positive")
        select=f"eq(mod(floor(X/{cell})+floor(Y/{cell}),2),0)"
        channels=[]
        for option,a,b in zip(("r","g","b","a"),first,second):
            channels.append(f"{option}='if({select},{a},{b})'")
        return (
            f"nullsrc=s={width}x{height}:r={fps}:d={scene_duration},"
            f"format=rgba,geq={':'.join(channels)}"
        )
    raise Diagnostic("VDLS-BACKEND-003",
                     f"visual generator `{name}` is unsupported")

def frame_random_ffexpr(seed: int=0) -> str:
    """Stable [0,1) pseudo-random value keyed by output frame and seed."""
    return f"abs(mod(sin((n+{seed})*12.9898)*43758.5453,1))"

def compile_ffexpr(value: Any, variables: dict[str,str] | None=None) -> str:
    """Compile a portable VDLS expression into FFmpeg expression syntax."""
    variables={"t":"t","frame":"n","r":frame_random_ffexpr(),
               "pi":"PI","e":"E",**(variables or {})}
    if isinstance(value,str):
        if PLAIN_NUMBER.match(value):
            return str(Fraction(value))
        if value in variables: return variables[value]
        raise Diagnostic("VDLS-FFMPEG-005",f"unsupported expression variable `{value}`")
    if not isinstance(value,list) or not value:
        raise Diagnostic("VDLS-FFMPEG-005","invalid FFmpeg expression")
    op=str(value[0]); args=value[1:]
    compiled=[compile_ffexpr(item,variables) for item in args]
    if op in {"+","*"} and compiled:
        return "("+op.join(compiled)+")"
    if op=="-" and len(compiled)==1: return f"(-{compiled[0]})"
    if op=="-" and len(compiled)>=2: return "("+"-".join(compiled)+")"
    if op=="/" and len(compiled)==2: return f"({compiled[0]}/{compiled[1]})"
    functions={
        "min":"min","max":"max","clamp":"clip","abs":"abs","floor":"floor",
        "ceil":"ceil","round":"round","sqrt":"sqrt","pow":"pow","exp":"exp",
        "log":"log","sin":"sin","cos":"cos","tan":"tan","<":"lt","<=":"lte",
        "=":"eq",">=":"gte",">":"gt","if":"if","and":"and","or":"or","not":"not",
    }
    if op=="!=" and len(compiled)==2:
        return f"not(eq({compiled[0]},{compiled[1]}))"
    if op in functions:
        return f"{functions[op]}({','.join(compiled)})"
    raise Diagnostic("VDLS-FFMPEG-005",f"expression operator `{op}` cannot be lowered")

def _unit_scalar(value: Any, unit: str | None=None,
                 variables: dict[str,str] | None=None) -> str:
    text=str(value)
    if PLAIN_NUMBER.match(text): return str(Fraction(text))
    if NUMBER.match(text):
        parsed=ratio(text)
        if unit is not None and parsed.get("unit")!=unit:
            raise Diagnostic("VDLS-TYPE-004",f"`{value}` requires {unit}")
        return str(Fraction(parsed["num"],parsed["den"]))
    if isinstance(value,list): return compile_ffexpr(value,variables)
    raise Diagnostic("VDLS-FFMPEG-005",f"value `{value}` cannot be lowered")

def compile_visual_effects(
    effects: list[Any], frame_rate: Fraction | None=None
) -> list[str]:
    result=[]
    for effect in effects:
        if not isinstance(effect,list) or not effect: continue
        name=str(effect[0]); args=effect[1:]
        if name=="scale" and len(args)==2:
            result.append(f"scale=w={_unit_scalar(args[0],'px')}:h={_unit_scalar(args[1],'px')}")
        elif name=="crop" and len(args)==4:
            values=[_unit_scalar(item,"px") for item in args]
            result.append(f"crop=w={values[0]}:h={values[1]}:x={values[2]}:y={values[3]}")
        elif name=="crop-center" and len(args)==2:
            width=_unit_scalar(args[0],"px"); height=_unit_scalar(args[1],"px")
            result.append(
                f"crop=w={width}:h={height}:x=(iw-ow)/2:y=(ih-oh)/2")
        elif name=="pad" and len(args)>=2:
            width=_unit_scalar(args[0],"px"); height=_unit_scalar(args[1],"px")
            options={str(item[0]):item[1:] for item in args[2:]
                     if isinstance(item,list) and item}
            x=(_unit_scalar(options["x"][0],"px") if options.get("x")
               else "(ow-iw)/2")
            y=(_unit_scalar(options["y"][0],"px") if options.get("y")
               else "(oh-ih)/2")
            color=str(options.get("color",["#00000000"])[0]).lstrip("#")
            _rgba_components(color)
            result.append(
                f"pad=w={width}:h={height}:x={x}:y={y}:color=0x{color}")
        elif name=="flip" and len(args)==1:
            result.append("hflip" if args[0]=="horizontal" else "vflip")
        elif name=="rotate" and len(args)==1:
            result.append(f"rotate=angle={_unit_scalar(args[0])}")
        elif name in {"brightness","contrast","saturation","gamma"} and len(args)==1:
            scalar=Fraction(_unit_scalar(args[0]))
            if name=="brightness" and not -1<=scalar<=1:
                raise Diagnostic("VDLS-TYPE-009","brightness must be within [-1,1]")
            if name in {"contrast","saturation"} and scalar<0:
                raise Diagnostic("VDLS-TYPE-009",f"{name} must be non-negative")
            if name=="gamma" and scalar<=0:
                raise Diagnostic("VDLS-TYPE-009","gamma must be positive")
            option={"saturation":"saturation"}.get(name,name)
            result.append(f"eq={option}={scalar}")
        elif name=="exposure" and len(args)==1:
            stops=Fraction(_unit_scalar(args[0]))
            result.append(f"exposure=exposure={stops}")
        elif name=="hue" and len(args)==1:
            result.append(f"hue=h={_unit_scalar(args[0])}")
        elif name=="invert" and not args:
            result.append("negate")
        elif name=="grayscale" and not args:
            result.append("hue=s=0")
        elif name=="gaussian-blur" and len(args)==1:
            result.append(f"gblur=sigma={_unit_scalar(args[0],'px')}")
        elif name=="box-blur" and len(args)==1:
            result.append(f"boxblur=luma_radius={_unit_scalar(args[0],'px')}")
        elif name=="unsharp":
            options={str(item[0]):item[1:] for item in args
                     if isinstance(item,list) and item}
            radius=Fraction(_unit_scalar(
                options.get("radius",["3px"])[0],"px"))
            amount=Fraction(_unit_scalar(
                options.get("amount",["0.5"])[0]))
            matrix=max(3,min(23,2*round(float(radius))+1))
            result.append(
                f"unsharp=luma_msize_x={matrix}:luma_msize_y={matrix}:"
                f"luma_amount={amount}")
        elif name=="opacity" and len(args)==1:
            alpha=Fraction(_unit_scalar(args[0]))
            if not 0<=alpha<=1:
                raise Diagnostic("VDLS-TYPE-009","opacity must be within [0,1]")
            result.extend(["format=rgba",f"colorchannelmixer=aa={alpha}"])
        else:
            extended=compile_extended_visual_effect(
                name,args,Diagnostic,_unit_scalar,_rgba_components,
                frame_rate)
            if extended is None:
                raise Diagnostic(
                    "VDLS-BACKEND-003",
                    f"visual filter `{name}` is unsupported")
            result.extend(extended)
    return result

def compile_audio_effects(effects: list[Any]) -> list[str]:
    return lower_audio_effects(effects,Diagnostic,_unit_scalar,ratio)

def emit_video_filter_chain(
    input_label: str, chain: list[str], filters: list[str],
    label_index: int,
) -> tuple[str,int]:
    current=input_label
    pending=[]
    for item in chain:
        special=(item.startswith("__vdls_freeze__=")
                 or item.startswith("__vdls_mask_input__="))
        if not special:
            pending.append(item)
            continue
        if pending:
            staged=f"[v{label_index:04d}]"; label_index+=1
            filters.append(f"{current}{','.join(pending)}{staged}")
            current=staged; pending=[]
        if item.startswith("__vdls_mask_input__="):
            mask_input=item.split("=",1)[1]
            scaled_mask=f"[k{label_index:04d}]"; label_index+=1
            reference=f"[v{label_index:04d}]"; label_index+=1
            rgba_base=f"[v{label_index:04d}]"; label_index+=1
            gray_mask=f"[k{label_index:04d}]"; label_index+=1
            masked=f"[v{label_index:04d}]"; label_index+=1
            filters.append(
                f"[{mask_input}:v]{current}"
                f"scale2ref{scaled_mask}{reference}")
            filters.append(f"{reference}format=rgba{rgba_base}")
            filters.append(f"{scaled_mask}format=gray{gray_mask}")
            filters.append(
                f"{rgba_base}{gray_mask}alphamerge{masked}")
            current=masked
            continue
        first,last,replace_frame=(
            int(value) for value in item.split("=",1)[1].split(":"))
        source_label=f"[f{label_index:04d}]"; label_index+=1
        replace_label=f"[f{label_index:04d}]"; label_index+=1
        frozen_label=f"[v{label_index:04d}]"; label_index+=1
        filters.append(
            f"{current}split=2{source_label}{replace_label}")
        filters.append(
            f"{source_label}{replace_label}freezeframes="
            f"first={first}:last={last}:replace={replace_frame}"
            f"{frozen_label}")
        current=frozen_label
    if pending or current==input_label:
        output=f"[v{label_index:04d}]"; label_index+=1
        filters.append(
            f"{current}{','.join(pending) if pending else 'null'}{output}")
        current=output
    return current,label_index

def emit_audio_filter_chain(
    input_label: str, chain: list[str], filters: list[str],
    label_index: int, sample_rate: int,
) -> tuple[str,int]:
    current=input_label; pending=[]
    for item in chain:
        if not item.startswith("__vdls_duck_input__="):
            pending.append(item); continue
        if pending:
            staged=f"[a{label_index:04d}]"; label_index+=1
            filters.append(f"{current}{','.join(pending)}{staged}")
            current=staged; pending=[]
        header,payload=item.split("|",1)
        sidechain_index=int(header.split("=",1)[1])
        descriptor=json.loads(payload)
        sidechain=f"[d{label_index:04d}]"; label_index+=1
        ducked=f"[a{label_index:04d}]"; label_index+=1
        sidechain_filters=[
            f"aresample={sample_rate}","asetpts=PTS-STARTPTS"]
        if descriptor.get("sidechainStartMs",0):
            sidechain_filters.append(
                f"adelay={descriptor['sidechainStartMs']}:all=1")
        if descriptor.get("timelineDuration"):
            sidechain_filters.append(
                f"apad=whole_dur={descriptor['timelineDuration']}")
        filters.append(
            f"[{sidechain_index}:a]"
            f"{','.join(sidechain_filters)}{sidechain}")
        filters.append(
            f"{current}{sidechain}sidechaincompress="
            f"threshold={descriptor['threshold']:.12g}:ratio=20:"
            f"attack={descriptor['attackMs']:.12g}:"
            f"release={descriptor['releaseMs']:.12g}:"
            f"knee=1:link=maximum:detection=peak{ducked}")
        current=ducked
    if pending or current==input_label:
        output=f"[a{label_index:04d}]"; label_index+=1
        filters.append(
            f"{current}{','.join(pending) if pending else 'anull'}{output}")
        current=output
    return current,label_index

def compile_output_color_filters(
    color_management: dict[str,Any], assets: dict[str,dict[str,Any]]
) -> list[str]:
    output=color_management["output"]
    maps={
        "primaries":{"bt709":"bt709","bt2020":"bt2020",
                     "display-p3":"smpte432"},
        "transfer":{"srgb":"srgb","bt1886":"bt709","bt709":"bt709",
                    "pq":"smpte2084","hlg":"arib-std-b67","linear":"linear"},
        "matrix":{"rgb":"gbr","bt709":"bt709","bt2020-ncl":"bt2020nc"},
        "range":{"full":"pc","limited":"tv"},
    }
    for key,mapping in maps.items():
        if output[key] not in mapping:
            if color_management["unknownMetadataPolicy"]=="preserve":
                return []
            raise Diagnostic(
                "VDLS-COLOR-002",
                f"output color {key} `{output[key]}` cannot be converted")
    hdr=next((asset["color"] for asset in assets.values()
              if asset.get("color")
              and asset["color"]["transfer"] in {"pq","hlg"}),None)
    if hdr and output["transfer"] not in {"pq","hlg"}:
        tone=color_management.get("toneMap")
        if not tone:
            raise Diagnostic(
                "VDLS-COLOR-003","HDR-to-SDR conversion requires tone-map")
        operator=tone["operator"]
        if operator=="bt2390":
            raise Diagnostic(
                "VDLS-COLOR-002",
                "detected FFmpeg tonemap filter does not provide BT.2390")
        input_transfer=maps["transfer"][hdr["transfer"]]
        input_primaries=maps["primaries"].get(hdr["primaries"])
        input_matrix=maps["matrix"].get(hdr["matrix"])
        if not input_primaries or not input_matrix:
            raise Diagnostic(
                "VDLS-COLOR-001",
                "HDR input requires primaries and matrix metadata")
        return [
            f"zscale=pin={input_primaries}:tin={input_transfer}:"
            f"min={input_matrix}:rin={maps['range'].get(hdr['range'],'tv')}:"
            "p=bt709:t=linear:m=gbr:r=full:npl=100",
            "format=gbrpf32le",
            f"tonemap=tonemap={operator}:peak="
            f"{_decimal_text(Fraction(tone['targetNits']))}",
            f"zscale=p={maps['primaries'][output['primaries']]}:"
            f"t={maps['transfer'][output['transfer']]}:"
            f"m={maps['matrix'][output['matrix']]}:"
            f"r={maps['range'][output['range']]}",
        ]
    return [
        "format=yuv420p,colorspace=iall=bt709:"
        f"space={maps['matrix'][output['matrix']]}:"
        f"trc={maps['transfer'][output['transfer']]}:"
        f"primaries={maps['primaries'][output['primaries']]}:"
        f"range={maps['range'][output['range']]}:format=yuv420p"
    ]

def output_color_ffmpeg_names(color_management: dict[str,Any]) -> dict[str,str]:
    output=color_management["output"]
    maps={
        "primaries":{"bt709":"bt709","bt2020":"bt2020",
                     "display-p3":"smpte432"},
        "transfer":{"srgb":"iec61966-2-1","bt1886":"bt709","bt709":"bt709",
                    "pq":"smpte2084","hlg":"arib-std-b67","linear":"linear"},
        "matrix":{"rgb":"gbr","bt709":"bt709","bt2020-ncl":"bt2020nc"},
        "range":{"full":"pc","limited":"tv"},
    }
    return {key:maps[key].get(value,"unknown")
            for key,value in output.items() if key in maps}

def _easing_ffexpr(name: str, value: str) -> str:
    if name=="linear": return value
    if name=="smoothstep": return f"({value}*{value}*(3-2*{value}))"
    if name=="ease-in-quad": return f"({value}*{value})"
    if name=="ease-out-quad": return f"(1-(1-{value})*(1-{value}))"
    if name=="ease-in-out-quad":
        return f"if(lt({value},0.5),2*{value}*{value},1-pow(-2*{value}+2,2)/2)"
    if name=="ease-in-cubic": return f"({value}*{value}*{value})"
    if name=="ease-out-cubic": return f"(1-pow(1-{value},3))"
    if name=="ease-in-out-cubic":
        return f"if(lt({value},0.5),4*{value}*{value}*{value},1-pow(-2*{value}+2,3)/2)"
    raise Diagnostic("VDLS-FFMPEG-005",f"easing `{name}` cannot be lowered")

def compile_animation_ffexpr(animation: dict[str,Any],
                             start: Fraction=Fraction(0),
                             variables: dict[str,str] | None=None) -> str:
    if animation["kind"]=="FromTo":
        duration=Fraction(animation["duration"]["num"],animation["duration"]["den"])
        before=_unit_scalar(animation["from"],variables=variables)
        after=_unit_scalar(animation["to"],variables=variables)
        if duration==0:
            return f"if(lt(t,{start}),{before},{after})"
        progress=f"clip((t-{start})/{duration},0,1)"
        eased=_easing_ffexpr(animation["easing"],progress)
        return f"({before}+({after}-{before})*{eased})"
    frames=animation["keyframes"]
    if len(frames)==1: return _unit_scalar(frames[0]["value"],variables=variables)
    expression=_unit_scalar(frames[-1]["value"],variables=variables)
    for index in range(len(frames)-2,-1,-1):
        left=frames[index]; right=frames[index+1]
        t0=start+Fraction(left["time"]["num"],left["time"]["den"])
        t1=start+Fraction(right["time"]["num"],right["time"]["den"])
        v0=_unit_scalar(left["value"],variables=variables)
        v1=_unit_scalar(right["value"],variables=variables)
        progress=f"clip((t-{t0})/({t1-t0}),0,1)"
        eased=_easing_ffexpr(right.get("easing","linear"),progress)
        interpolated=f"({v0}+({v1}-{v0})*{eased})"
        expression=f"if(lt(t,{t1}),{interpolated},{expression})"
    first_time=start+Fraction(frames[0]["time"]["num"],frames[0]["time"]["den"])
    first_value=_unit_scalar(frames[0]["value"],variables=variables)
    return f"if(lt(t,{first_time}),{first_value},{expression})"

def ffmpeg_plans(ast: dict[str,Any], source: Path,
                 output_dir: str | None=None,
                 selected: list[str] | None=None,
                 reproducible: bool=False) -> list[dict[str,Any]]:
    """Lower the reference video/audio profile to deterministic FFmpeg argv plans."""
    executable=shutil.which("ffmpeg")
    if not executable: raise Diagnostic("VDLS-FFMPEG-001","FFmpeg executable not found")
    scenes={scene["sceneId"]:scene for scene in ast["node"]["scenes"]}
    assets={asset["assetId"]:asset for asset in ast["node"]["assets"]}
    for asset in assets.values():
        asset_source=asset["source"]
        if asset_source["kind"]!="File": continue
        asset_path=(source.parent/asset_source["path"]).resolve()
        if asset.get("integrity") and asset_path.exists():
            actual="sha256:"+hashlib.sha256(asset_path.read_bytes()).hexdigest()
            if actual!=asset["integrity"]:
                raise Diagnostic(
                    "VDLS-ASSET-003",
                    f"asset integrity mismatch: {asset['assetId']}")
    seed=0
    settings=ast["node"].get("settings")
    if settings:
        seed_clause=next((clause for clause in settings.get("clauses",[])
                          if isinstance(clause,list) and len(clause)==2
                          and clause[0]=="seed"),None)
        if seed_clause:
            try: seed=int(seed_clause[1])
            except ValueError:
                raise Diagnostic("VDLS-CONFIG-004","build seed must be an integer")
    expression_variables={"r":frame_random_ffexpr(seed)}
    plans=[]
    for output in ast["node"]["outputs"]:
        if selected and output["outputId"] not in selected: continue
        scene_id=output["sceneRefs"][0] if output["sceneRefs"] else (
            next(iter(scenes)) if scenes else None)
        if scene_id is None: raise Diagnostic("VDLS-GRAPH-005","output has no scene")
        scene=scenes[scene_id]; video=output["video"]; audio=output["audio"]
        duration=_ratio_text(scene["duration"])
        layers=scene["layers"]
        argv=[executable,"-hide_banner","-nostdin","-y"]
        if reproducible:
            argv.extend(["-fflags","+bitexact"])
        filters=[]; input_index=0; current=None; label_index=1; font_files=[]
        visual_layers=[
            layer for layer in layers
            if layer["content"]["kind"]!="Audio"
            and not (layer["content"]["kind"]=="Subtitles"
                     and not layer["content"].get("burnIn"))
        ]
        audio_layers=[layer for layer in layers if layer["content"]["kind"]=="Audio"]
        sidecars=[]
        for sidecar_index,layer in enumerate(layers,1):
            content=layer["content"]
            descriptor=content.get("sidecar") if content["kind"]=="Subtitles" else None
            if not descriptor: continue
            sidecar_path=(Path(output_dir).resolve()/Path(descriptor["path"]).name
                          if output_dir else
                          (source.parent/descriptor["path"]).resolve())
            sidecars.append({
                "id":f"{output['outputId']}:sidecar:{sidecar_index}",
                "path":str(sidecar_path),
                "format":descriptor["format"],
                "cues":content["track"]["cues"],
                "language":content.get("language"),
            })

        if video:
            width=video["width"]; height=video["height"]
            fps=Fraction(video["frameRate"]["num"],video["frameRate"]["den"])
            def attach_mask_inputs(chain: list[str]) -> list[str]:
                nonlocal input_index
                lowered=[]
                for item in chain:
                    if not item.startswith("__vdls_mask__="):
                        lowered.append(item); continue
                    asset_id=item.split("=",1)[1]
                    if asset_id not in assets:
                        raise Diagnostic(
                            "VDLS-NAME-007",
                            f"mask asset `{asset_id}` does not resolve")
                    mask_source=assets[asset_id]["source"]
                    if mask_source["kind"]=="Generated":
                        generator=mask_source["generator"][0]
                        if not isinstance(generator,list):
                            raise Diagnostic(
                                "VDLS-PARSE-003","invalid mask generator")
                        argv.extend([
                            "-f","lavfi","-i",
                            generated_video_source(
                                generator,width,height,fps,duration,seed),
                        ])
                    elif mask_source["kind"]=="File":
                        mask_path=(
                            source.parent/mask_source["path"]).resolve()
                        if not mask_path.exists():
                            raise Diagnostic(
                                "VDLS-ASSET-001",
                                f"mask asset not found: {mask_source['path']}")
                        if mask_path.suffix.lower() in {
                                ".png",".jpg",".jpeg",".webp",".bmp",
                                ".tif",".tiff"}:
                            argv.extend(["-loop","1","-framerate",str(fps)])
                        argv.extend(["-i",str(mask_path)])
                    else:
                        raise Diagnostic(
                            "VDLS-BACKEND-003",
                            f"mask source `{mask_source['kind']}` is unsupported")
                    lowered.append(f"__vdls_mask_input__={input_index}")
                    input_index+=1
                return lowered
            if not visual_layers:
                raise Diagnostic("VDLS-GRAPH-005","video output has no visual layers")
            first=visual_layers[0]["content"]
            if first["kind"]!="Video":
                raise Diagnostic("VDLS-BACKEND-003",
                                 "reference FFmpeg profile requires a video background")
            asset=assets[first["assetRef"]]; asset_source=asset["source"]
            if asset_source["kind"]=="Generated":
                generator=asset_source["generator"][0]
                if not isinstance(generator,list):
                    raise Diagnostic("VDLS-PARSE-003","invalid visual generator")
                argv.extend(["-f","lavfi","-i",
                             generated_video_source(
                                 generator,width,height,fps,duration,seed)])
            elif asset_source["kind"]=="File":
                asset_path=(source.parent/asset_source["path"]).resolve()
                if not asset_path.exists():
                    raise Diagnostic("VDLS-ASSET-001",
                                     f"local asset not found: {asset_source['path']}")
                if asset_path.suffix.lower() in {
                        ".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"}:
                    argv.extend(["-loop","1","-framerate",str(fps)])
                argv.extend(["-i",str(asset_path)])
            else:
                raise Diagnostic("VDLS-BACKEND-003",
                                 f"asset source `{asset_source['kind']}` is not supported")
            current=f"[{input_index}:v]"; input_index+=1
            base_filters=[]
            if first.get("sourceRange"):
                start=_ratio_text(first["sourceRange"]["start"])
                end=_ratio_text(first["sourceRange"]["end"])
                base_filters.extend([f"trim=start={start}:end={end}","setpts=PTS-STARTPTS"])
            base_filters.append(f"scale=w={width}:h={height}")
            base_filters.extend(attach_mask_inputs(
                compile_visual_effects(first.get("effects",[]),fps)))
            if first.get("speed"):
                speed=Fraction(first["speed"]["num"],first["speed"]["den"])
                base_filters.append(f"setpts=PTS/{speed}")
            opacity=Fraction(str(first.get("opacity","1")))
            if opacity!=1:
                if not 0<=opacity<=1:
                    raise Diagnostic("VDLS-TYPE-009","opacity must be within [0,1]")
                base_filters.extend(
                    ["format=rgba",f"colorchannelmixer=aa={opacity}"])
            if first.get("fadeIn"):
                fade=_decimal_text(Fraction(
                    first["fadeIn"]["num"],first["fadeIn"]["den"]))
                base_filters.append(f"fade=t=in:st=0:d={fade}")
            if first.get("fadeOut"):
                fade_q=Fraction(
                    first["fadeOut"]["num"],first["fadeOut"]["den"])
                scene_q=Fraction(
                    scene["duration"]["num"],scene["duration"]["den"])
                start_fade=max(Fraction(0),scene_q-fade_q)
                base_filters.append(
                    f"fade=t=out:st={_decimal_text(start_fade)}:"
                    f"d={_decimal_text(fade_q)}")
            current,label_index=emit_video_filter_chain(
                current,base_filters,filters,label_index)

        for layer in visual_layers[1:]:
            content=layer["content"]
            if content["kind"]=="Video":
                asset=assets[content["assetRef"]]
                asset_source=asset["source"]
                if asset_source["kind"]=="Generated":
                    generator=asset_source["generator"][0]
                    if not isinstance(generator,list):
                        raise Diagnostic("VDLS-PARSE-003","invalid visual generator")
                    argv.extend([
                        "-f","lavfi","-i",
                        generated_video_source(
                            generator,width,height,fps,duration,seed),
                    ])
                elif asset_source["kind"]=="File":
                    asset_path=(source.parent/asset_source["path"]).resolve()
                    if not asset_path.exists():
                        raise Diagnostic(
                            "VDLS-ASSET-001",
                            f"local asset not found: {asset_source['path']}")
                    if asset_path.suffix.lower() in {
                            ".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"}:
                        argv.extend(["-loop","1","-framerate",str(fps)])
                    argv.extend(["-i",str(asset_path)])
                else:
                    raise Diagnostic(
                        "VDLS-BACKEND-003",
                        f"asset source `{asset_source['kind']}` is not supported")
                chain=[]
                if content.get("sourceRange"):
                    source_start=_ratio_text(content["sourceRange"]["start"])
                    source_end=_ratio_text(content["sourceRange"]["end"])
                    chain.extend([
                        f"trim=start={source_start}:end={source_end}",
                        "setpts=PTS-STARTPTS",
                    ])
                transform=content.get("transform") or []
                transform_values={
                    str(item[0]):item[1:] for item in transform[1:]
                    if isinstance(item,list) and item}
                if transform_values.get("scale"):
                    values=transform_values["scale"]
                    if len(values)!=2:
                        raise Diagnostic(
                            "VDLS-PARSE-003","transform scale requires x and y")
                    scale_x=Fraction(_unit_scalar(values[0]))
                    scale_y=Fraction(_unit_scalar(values[1]))
                    chain.append(f"scale=w=iw*{scale_x}:h=ih*{scale_y}")
                if transform_values.get("rotation"):
                    rotation=transform_values["rotation"]
                    if len(rotation)!=1:
                        raise Diagnostic(
                            "VDLS-PARSE-003","rotation requires one angle")
                    chain.append(
                        f"rotate=angle={_unit_scalar(rotation[0])}:"
                        "fillcolor=black@0")
                layer_input_index=input_index
                input_index+=1
                chain.extend(attach_mask_inputs(
                    compile_visual_effects(content.get("effects",[]),fps)))
                if content.get("speed"):
                    speed=Fraction(
                        content["speed"]["num"],content["speed"]["den"])
                    chain.append(f"setpts=PTS/{speed}")
                opacity=Fraction(str(content.get("opacity","1")))
                blend=str(layer.get("blendMode","normal"))
                if not 0<=opacity<=1:
                    raise Diagnostic(
                        "VDLS-TYPE-009","opacity must be within [0,1]")
                if opacity!=1 and blend=="normal":
                    chain.extend([
                        "format=rgba",f"colorchannelmixer=aa={opacity}"])
                start=Fraction(layer["start"]["num"],layer["start"]["den"])
                layer_duration=layer["duration"] or scene["duration"]
                end=start+Fraction(
                    layer_duration["num"],layer_duration["den"])
                chain.extend([
                    f"trim=duration={_decimal_text(Fraction(layer_duration['num'],layer_duration['den']))}",
                    f"setpts=PTS-STARTPTS+{start}/TB",
                    "format=rgba",
                ])
                layer_label,label_index=emit_video_filter_chain(
                    f"[{layer_input_index}:v]",chain,filters,label_index)
                position=transform_values.get("position",["0","0"])
                if len(position)!=2:
                    raise Diagnostic(
                        "VDLS-PARSE-003","position requires x and y")
                x=_unit_scalar(position[0],"px",expression_variables)
                y=_unit_scalar(position[1],"px",expression_variables)
                anchor=str(transform_values.get("anchor",["top-left"])[0])
                if anchor in {"top","center","bottom"}: x=f"({x}-overlay_w/2)"
                elif anchor in {"top-right","right","bottom-right"}:
                    x=f"({x}-overlay_w)"
                if anchor in {"left","center","right"}: y=f"({y}-overlay_h/2)"
                elif anchor in {"bottom-left","bottom","bottom-right"}:
                    y=f"({y}-overlay_h)"
                output_label=f"[v{label_index:04d}]"; label_index+=1
                if blend=="normal":
                    filters.append(
                        f"{current}{layer_label}overlay=x='{x}':y='{y}':"
                        f"eval=frame:eof_action=pass:shortest=0:"
                        f"enable='between(t,{start},{end})'{output_label}")
                elif ffmpeg_blend_mode(blend):
                    canvas_label=f"[c{label_index:04d}]"; label_index+=1
                    placed_label=f"[p{label_index:04d}]"; label_index+=1
                    base_keep=f"[b{label_index:04d}]"; label_index+=1
                    base_blend=f"[b{label_index:04d}]"; label_index+=1
                    layer_blend=f"[l{label_index:04d}]"; label_index+=1
                    layer_alpha=f"[l{label_index:04d}]"; label_index+=1
                    blended=f"[m{label_index:04d}]"; label_index+=1
                    mask=f"[k{label_index:04d}]"; label_index+=1
                    filters.append(
                        f"color=c=black@0:s={width}x{height}:r={fps}:"
                        f"d={duration},"
                        f"format=rgba,colorchannelmixer=aa=0{canvas_label}")
                    filters.append(
                        f"{canvas_label}{layer_label}overlay=x='{x}':y='{y}':"
                        f"eval=frame:eof_action=pass:shortest=0:alpha=straight,"
                        f"format=rgba"
                        f"{placed_label}")
                    filters.append(
                        f"{current}split=2{base_keep}{base_blend}")
                    filters.append(
                        f"{placed_label}split=2{layer_blend}{layer_alpha}")
                    filters.append(
                        f"{base_blend}{layer_blend}"
                        f"blend=all_mode={ffmpeg_blend_mode(blend)}:"
                        f"all_opacity={opacity}:"
                        f"enable='between(t,{start},{end})'{blended}")
                    filters.append(
                        f"{layer_alpha}format=rgba,alphaextract,"
                        f"lut=y='val*{opacity}',format=rgba{mask}")
                    filters.append(
                        f"{base_keep}{blended}{mask}"
                        f"maskedmerge=planes=7,format=rgba"
                        f"{output_label}")
                else:
                    raise Diagnostic(
                        "VDLS-BACKEND-003",
                        f"blend mode `{blend}` is not implemented")
                current=output_label
                continue
            if content["kind"]=="Subtitles":
                asset_source=assets[content["assetRef"]]["source"]
                if asset_source["kind"]!="File":
                    raise Diagnostic("VDLS-BACKEND-003",
                                     "FFmpeg subtitles require a file asset")
                subtitle_path=(source.parent/asset_source["path"]).resolve()
                if not subtitle_path.exists():
                    raise Diagnostic("VDLS-ASSET-001",
                                     f"subtitle asset not found: {asset_source['path']}")
                output_label=f"[v{label_index:04d}]"; label_index+=1
                escaped_path=_ffmpeg_escape_filter_path(subtitle_path)
                filters.append(
                    f"{current}subtitles=filename='{escaped_path}'{output_label}")
                current=output_label
                continue
            if content["kind"]!="Text":
                raise Diagnostic("VDLS-BACKEND-003",
                                 f"visual layer `{content['kind']}` is not yet lowerable to FFmpeg")
            expression=content["content"]
            if expression.get("kind")!="Literal" or not isinstance(expression.get("value"),str):
                raise Diagnostic("VDLS-FFMPEG-005","dynamic text expression cannot be lowered")
            style=content.get("style",{}); layout=content.get("layout",{})
            font_size=72.0; font_weight=400; font_italic=False
            font_family=None
            font=style.get("font")
            chosen_font=None
            if font:
                size=next((item[1] for item in font if isinstance(item,list)
                           and len(item)==2 and item[0]=="size"),None)
                if size and NUMBER.match(size):
                    size_value=ratio(size)
                    if size_value.get("unit")=="px":
                        font_size=float(Fraction(size_value["num"],size_value["den"]))
                family=next((item[1] for item in font if isinstance(item,list)
                             and len(item)==2 and item[0]=="family"),None)
                if family: font_family=str(family)
                weight=next((item[1] for item in font if isinstance(item,list)
                             and len(item)==2 and item[0]=="weight"),None)
                if weight and str(weight).isdigit(): font_weight=int(weight)
                slant=next((item[1] for item in font if isinstance(item,list)
                            and len(item)==2 and item[0]=="style"),None)
                font_italic=slant in {"italic","oblique"}
                asset_ref=next((item[1] for item in font if isinstance(item,list)
                                and len(item)==2 and item[0]=="asset-ref"),None)
                if asset_ref:
                    if asset_ref not in assets:
                        raise Diagnostic("VDLS-NAME-007",
                                         f"font asset `{asset_ref}` does not resolve")
                    font_source=assets[asset_ref]["source"]
                    if font_source["kind"]!="File":
                        raise Diagnostic("VDLS-TEXT-001","font asset must be a file")
                    chosen_font=(source.parent/font_source["path"]).resolve()
                    if not chosen_font.exists():
                        raise Diagnostic("VDLS-TEXT-001",
                                         f"font asset not found: {font_source['path']}")
            fill=style.get("fill")
            font_color=str(fill[0]) if fill and fill[0] else "#ffffffff"
            position=layout.get("position")
            x=(_unit_scalar(position[0],variables=expression_variables)
               if position and len(position)>1 else "w/2")
            y=(_unit_scalar(position[1],variables=expression_variables)
               if position and len(position)>1 else "h/2")
            anchor=str(layout.get("anchor",["top-left"])[0])
            surface_width=width; surface_height=height; wrap_mode="none"
            overflow="clip"; max_lines=None; line_height=1.2
            if layout.get("box"):
                box=layout["box"]
                if not all(isinstance(item,list) and item for item in box):
                    raise Diagnostic("VDLS-PARSE-003","invalid text box")
                box_values={str(item[0]):item[1:] for item in box}
                if box_values.get("width"):
                    surface_width=round(float(Fraction(
                        _unit_scalar(box_values["width"][0],"px"))))
                if box_values.get("height"):
                    surface_height=round(float(Fraction(
                        _unit_scalar(box_values["height"][0],"px"))))
                wrap_mode=str(box_values.get("wrap",["word"])[0])
                overflow=str(box_values.get("overflow",["clip"])[0])
                if box_values.get("max-lines"):
                    value=str(box_values["max-lines"][0])
                    if not value.isdigit() or int(value)<1:
                        raise Diagnostic(
                            "VDLS-TYPE-009","max-lines must be positive")
                    max_lines=int(value)
                if box_values.get("line-height"):
                    value=str(box_values["line-height"][0])
                    if not PLAIN_NUMBER.match(value) or Fraction(value)<=0:
                        raise Diagnostic(
                            "VDLS-TYPE-009","line-height must be positive")
                    line_height=float(Fraction(value))
                if surface_width<1 or surface_height<1:
                    raise Diagnostic(
                        "VDLS-TYPE-004","text box dimensions must be positive")
            alpha=None
            animation_start=Fraction(layer["start"]["num"],layer["start"]["den"])
            for animation in content.get("animations",[]):
                expression_value=compile_animation_ffexpr(
                    animation,animation_start,expression_variables)
                if animation["property"] in {"position.x","x"}: x=expression_value
                elif animation["property"] in {"position.y","y"}: y=expression_value
                elif animation["property"]=="opacity": alpha=expression_value
                else:
                    raise Diagnostic("VDLS-TYPE-011",
                                     f"property `{animation['property']}` is not animatable")
            font_path=chosen_font or _default_font_file(expression["value"])
            font_files.append(font_path)
            start=Fraction(layer["start"]["num"],layer["start"]["den"])
            layer_duration=layer["duration"] or scene["duration"]
            end=start+Fraction(layer_duration["num"],layer_duration["den"])
            stroke_color=None; stroke_width=0.0
            stroke=style.get("stroke")
            if stroke:
                if len(stroke)!=2:
                    raise Diagnostic("VDLS-PARSE-003",
                                     "stroke requires color and width")
                stroke_color=str(stroke[0])
                stroke_width=float(Fraction(_unit_scalar(stroke[1],"px")))
            shadow_color=None; sx=sy=shadow_blur=0.0
            shadow=style.get("shadow")
            if shadow:
                if shadow and all(isinstance(item,list) for item in shadow):
                    offset=next((item for item in shadow if item and item[0]=="offset"),None)
                    shadow_color=str(next((item[1] for item in shadow
                                           if len(item)==2 and item[0]=="color"),
                                          "#00000080"))
                    blur=next((item[1] for item in shadow
                               if len(item)==2 and item[0]=="blur"),"0px")
                    shadow_blur=float(Fraction(_unit_scalar(blur,"px")))
                    sx=float(Fraction(_unit_scalar(offset[1],"px"))) if offset and len(offset)>2 else 0.0
                    sy=float(Fraction(_unit_scalar(offset[2],"px"))) if offset and len(offset)>2 else 0.0
                elif len(shadow)==4:
                    sx=float(Fraction(_unit_scalar(shadow[0],"px")))
                    sy=float(Fraction(_unit_scalar(shadow[1],"px")))
                    shadow_blur=float(Fraction(_unit_scalar(shadow[2],"px")))
                    shadow_color=str(shadow[3])
                else:
                    raise Diagnostic("VDLS-PARSE-003","invalid shadow")
            try:
                typewriter=next((
                    effect["duration"] for effect in content.get("textEffects",[])
                    if effect["kind"]=="typewriter"),None)
                reveal_lines=next((
                    effect["duration"] for effect in content.get("textEffects",[])
                    if effect["kind"]=="reveal-lines"),None)
                fade_in=next((
                    effect["duration"] for effect in content.get("textEffects",[])
                    if effect["kind"]=="text-fade-in"),None)
                highlights=next((
                    effect["timings"] for effect in content.get("textEffects",[])
                    if effect["kind"]=="highlight-words"),None)
                unsupported_text_effect=next((
                    effect for effect in content.get("textEffects",[])
                    if effect["kind"] not in {
                        "typewriter","reveal-lines","text-fade-in",
                        "highlight-words"}),None)
                if unsupported_text_effect:
                    raise TextEngineError(
                        "VDLS-BACKEND-003",
                        f"text effect `{unsupported_text_effect['kind']}` "
                        "is not implemented by the Text Engine adapter")
                text_layout=layout_text(TextRequest(
                    content=expression["value"],
                    font=FontRequest(
                        str(font_path),font_family or font_path.stem,font_size,
                        font_weight,font_italic,
                    ),
                    paint=Paint(
                        font_color,stroke_color,stroke_width,shadow_color,
                        sx,sy,shadow_blur,
                    ),
                    frame_width=surface_width,frame_height=surface_height,
                    anchor=anchor,
                    align=str(layout.get("align",["left"])[0]),
                    language=str(content.get("language","und")),
                    direction=str(content.get("direction","auto")),
                    normalization=str(content.get("normalization","preserve")),
                    typewriter_duration=(
                        (typewriter["num"],typewriter["den"])
                        if typewriter else None),
                    reveal_lines_duration=(
                        (reveal_lines["num"],reveal_lines["den"])
                        if reveal_lines else None),
                    fade_in_duration=(
                        (fade_in["num"],fade_in["den"])
                        if fade_in else None),
                    word_highlights=tuple(
                        (timing["start"]["num"],timing["start"]["den"],
                         timing["end"]["num"],timing["end"]["den"])
                        for timing in (highlights or [])),
                    wrap_mode=wrap_mode,
                    overflow=overflow,
                    max_lines=max_lines,
                    line_height=line_height,
                    timeline_start=(layer["start"]["num"],layer["start"]["den"]),
                ))
                text_cache=(Path(output_dir).resolve() if output_dir
                            else source.parent)/".vdls-text"
                surface=render_ass_surface(text_layout,text_cache)
            except TextEngineError as error:
                raise Diagnostic(error.code,error.message) from error
            # Text is rasterized into an RGBA input surface before composition.
            # Position and animation remain ordinary surface operations.
            argv.extend([
                "-f","lavfi","-i",
                f"color=c=black@0.0:"
                f"s={text_layout.frame_width}x{text_layout.frame_height}:"
                f"r={fps}:d={duration},format=rgba",
            ])
            surface_label=f"[s{label_index:04d}]"; label_index+=1
            surface_filters=[ffmpeg_ass_filter(surface)]
            if alpha:
                surface_filters.append(
                    f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                    f"a='alpha(X,Y)*({alpha})'"
                )
            filters.append(
                f"[{input_index}:v]{','.join(surface_filters)}{surface_label}")
            input_index+=1
            output_label=f"[v{label_index:04d}]"; label_index+=1
            x=f"({x}-{surface.anchor_x:g})"
            y=f"({y}-{surface.anchor_y:g})"
            filters.append(
                f"{current}{surface_label}overlay=x='{x}':y='{y}':eval=frame:"
                f"eof_action=pass:shortest=0:enable='between(t,{start},{end})'"
                f"{output_label}"
            )
            current=output_label

        audio_labels=[]
        if audio:
            sample_rate=audio["sampleRate"]
            def append_audio_input(asset_id: str) -> int:
                nonlocal input_index
                if asset_id not in assets:
                    raise Diagnostic(
                        "VDLS-NAME-007",
                        f"audio asset `{asset_id}` does not resolve")
                audio_source=assets[asset_id]["source"]
                if audio_source["kind"]=="Generated":
                    generator=audio_source["generator"][0]
                    if not isinstance(generator,list) or not generator:
                        raise Diagnostic(
                            "VDLS-BACKEND-003","invalid audio generator")
                    if generator[0]=="silence":
                        generated_duration=_ratio_text(ratio(generator[1]))
                        lavfi=(
                            f"anullsrc=r={sample_rate}:cl=stereo:"
                            f"d={generated_duration}")
                    elif generator[0]=="tone":
                        frequency=ratio(generator[1])
                        if frequency.get("unit")!="Hz":
                            raise Diagnostic(
                                "VDLS-TYPE-004",
                                "tone frequency requires Hz")
                        hz=Fraction(
                            frequency["num"],frequency["den"])
                        generated_duration=_ratio_text(ratio(generator[2]))
                        lavfi=(
                            f"sine=frequency={hz}:"
                            f"duration={generated_duration}:"
                            f"sample_rate={sample_rate}")
                    else:
                        raise Diagnostic(
                            "VDLS-BACKEND-003",
                            f"audio generator `{generator[0]}` is unsupported")
                    argv.extend(["-f","lavfi","-i",lavfi])
                elif audio_source["kind"]=="File":
                    audio_path=(
                        source.parent/audio_source["path"]).resolve()
                    if not audio_path.exists():
                        raise Diagnostic(
                            "VDLS-ASSET-001",
                            f"local asset not found: {audio_source['path']}")
                    argv.extend(["-i",str(audio_path)])
                else:
                    raise Diagnostic(
                        "VDLS-BACKEND-003",
                        f"audio source `{audio_source['kind']}` is unsupported")
                result=input_index; input_index+=1
                return result
            for audio_index,layer in enumerate(audio_layers):
                content=layer["content"]; asset=assets[content["assetRef"]]
                main_input_index=append_audio_input(content["assetRef"])
                chain=[]
                if content.get("sourceRange"):
                    start=_ratio_text(content["sourceRange"]["start"])
                    end=_ratio_text(content["sourceRange"]["end"])
                    chain.extend([f"atrim=start={start}:end={end}","asetpts=PTS-STARTPTS"])
                if content.get("speed"):
                    speed=Fraction(content["speed"]["num"],content["speed"]["den"])
                    remaining=speed
                    while remaining>2:
                        chain.append("atempo=2"); remaining/=2
                    while remaining<Fraction(1,2):
                        chain.append("atempo=0.5"); remaining*=2
                    if remaining!=1: chain.append(f"atempo={remaining}")
                gain=str(content.get("gain","0dB"))
                if NUMBER.match(gain):
                    parsed_gain=ratio(gain)
                    if parsed_gain.get("unit")=="dB":
                        db=float(Fraction(parsed_gain["num"],parsed_gain["den"]))
                        chain.append(f"volume={math.pow(10,db/20):.12g}")
                pan=Fraction(str(content.get("pan","0")))
                if not -1<=pan<=1:
                    raise Diagnostic("VDLS-TYPE-009","pan must be within [-1,1]")
                if pan: chain.append(f"stereotools=balance_out={pan}")
                for effect in compile_audio_effects(
                        content.get("effects",[])):
                    if not effect.startswith("__vdls_duck__="):
                        chain.append(effect); continue
                    descriptor=json.loads(effect.split("=",1)[1])
                    if descriptor["target"]!=content["assetRef"]:
                        raise Diagnostic(
                            "VDLS-NAME-007",
                            f"duck target `{descriptor['target']}` does not "
                            f"match owning audio asset `{content['assetRef']}`")
                    sidechain_index=append_audio_input(
                        descriptor["sidechain"])
                    sidechain_layer=next((
                        candidate for candidate in audio_layers
                        if candidate["content"]["assetRef"]==
                        descriptor["sidechain"]),None)
                    if sidechain_layer is None:
                        raise Diagnostic(
                            "VDLS-NAME-007",
                            f"duck sidechain `{descriptor['sidechain']}` "
                            "is not placed on the scene timeline")
                    sidechain_start=Fraction(
                        sidechain_layer["start"]["num"],
                        sidechain_layer["start"]["den"])
                    descriptor["sidechainStartMs"]=round(
                        sidechain_start*1000)
                    descriptor["timelineDuration"]=duration
                    chain.append(
                        f"__vdls_duck_input__={sidechain_index}|"
                        f"{json.dumps(descriptor,sort_keys=True,separators=(',',':'))}")
                if content.get("fadeIn"):
                    fade_in_q=Fraction(
                        content["fadeIn"]["num"],content["fadeIn"]["den"])
                    chain.append(
                        f"afade=t=in:st=0:d={_decimal_text(fade_in_q)}")
                if content.get("fadeOut"):
                    fade_q=Fraction(
                        content["fadeOut"]["num"],content["fadeOut"]["den"])
                    layer_duration=content.get("duration") or scene["duration"]
                    duration_q=Fraction(
                        layer_duration["num"],layer_duration["den"])
                    chain.append(
                        f"afade=t=out:st="
                        f"{_decimal_text(max(Fraction(0),duration_q-fade_q))}:"
                        f"d={_decimal_text(fade_q)}")
                chain.append(f"aresample={sample_rate}")
                placement=Fraction(layer["start"]["num"],layer["start"]["den"])
                if placement:
                    chain.append(f"asetpts=PTS+{placement}/TB")
                label,label_index=emit_audio_filter_chain(
                    f"[{main_input_index}:a]",chain,filters,
                    label_index,sample_rate)
                audio_labels.append(label)
            if not audio_labels:
                raise Diagnostic("VDLS-GRAPH-002","audio output has no audio layer")
            if len(audio_labels)==1:
                filters.append(f"{audio_labels[0]}anull[aout]")
            else:
                filters.append(
                    f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:"
                    f"duration=longest:normalize=0[aout]"
                )

        if video:
            color_filters=compile_output_color_filters(
                ast["node"]["colorManagement"],assets)
            filters.append(
                f"{current}{','.join(color_filters) if color_filters else 'null'}"
                "[vout]")
        filter_script=";\n".join(filters)+"\n"
        target=(Path(output_dir).resolve()/Path(output["path"]).name
                if output_dir else (source.parent/output["path"]).resolve())
        temporary=target.with_name(f".{target.stem}.vdls-tmp{target.suffix}")
        script=target.with_name(f".{target.stem}.vdls-filter.txt")
        argv.extend(["-filter_complex_script",str(script)])
        if video:
            argv.extend(["-map","[vout]","-c:v","libx264","-pix_fmt","yuv420p",
                         "-r",str(fps)])
            color_names=output_color_ffmpeg_names(
                ast["node"]["colorManagement"])
            if all(value!="unknown" for value in color_names.values()):
                argv.extend([
                    "-color_primaries",color_names["primaries"],
                    "-color_trc",color_names["transfer"],
                    "-colorspace",color_names["matrix"],
                    "-color_range",color_names["range"],
                ])
            if reproducible:
                argv.extend(["-threads","1","-flags:v","+bitexact",
                             "-x264-params","threads=1:lookahead_threads=1"])
        else:
            argv.append("-vn")
        if audio:
            argv.extend(["-map","[aout]","-c:a","aac","-ar",str(audio["sampleRate"])])
            if audio["channelLayout"]=="stereo": argv.extend(["-ac","2"])
            if reproducible: argv.extend(["-flags:a","+bitexact"])
        else:
            argv.append("-an")
        if reproducible:
            argv.extend([
                "-map_metadata","-1",
                "-metadata","creation_time=1970-01-01T00:00:00Z",
            ])
        for metadata_key,metadata_value in sorted(
                output.get("metadata",{}).items()):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",metadata_key):
                raise Diagnostic(
                    "VDLS-TYPE-004",
                    f"invalid output metadata key `{metadata_key}`")
            if reproducible and metadata_key=="creation_time": continue
            if any(character in str(metadata_value) for character in "\x00\r\n"):
                raise Diagnostic(
                    "VDLS-TYPE-004",
                    f"invalid output metadata value for `{metadata_key}`")
            argv.extend(["-metadata",f"{metadata_key}={metadata_value}"])
        argv.extend(["-t",duration,str(temporary)])
        asset_digests={}; asset_metadata={}
        for asset_id,asset_value in assets.items():
            asset_source=asset_value["source"]
            if asset_source["kind"]=="File":
                asset_path=(source.parent/asset_source["path"]).resolve()
                if asset_path.exists():
                    asset_digests[asset_id]="sha256:"+hashlib.sha256(
                        asset_path.read_bytes()).hexdigest()
                    try:
                        summary=exif_manifest_summary(read_exif(asset_path))
                    except RuntimeError as error:
                        raise Diagnostic("VDLS-FFMPEG-004",str(error)) from error
                    if summary is not None: asset_metadata[asset_id]=summary
        cache_payload={
            "specMajor":1,"compilerVersion":VERSION,
            "backend":"org.vdls.backend.ffmpeg/1",
            "output":output,"scene":scene,"filterScript":filter_script,
            "assetDigests":asset_digests,"reproducible":reproducible,
        }
        cache_key=hashlib.sha256(
            canonical_json(cache_payload).encode("utf-8")).hexdigest()
        filter_names=set(re.findall(
            r"(?:^|[\],;])([a-z][a-z0-9_]*)(?==|\[|,|;)",
            filter_script,re.MULTILINE))
        # Lavfi inputs are filter graphs too and participate in capability
        # analysis even though they are not in filter_complex_script.
        for index,value in enumerate(argv[:-1]):
            if value=="-i" and index and argv[index-1]=="lavfi":
                filter_names.update(re.findall(
                    r"(?:^|,)([a-z][a-z0-9_]*)(?==|,|$)",argv[index+1]))
        plans.append({"targetId":output["outputId"],"argv":argv,
                      "filterScript":filter_script,"filterScriptPath":str(script),
                      "temporaryPath":str(temporary),"outputPath":str(target),
                      "cacheKey":"sha256:"+cache_key,
                      "reproducible":reproducible,
                      "requirements":{
                          "filters":sorted(filter_names),
                          "encoders":sorted(
                              (["libx264"] if video else [])+
                              (["aac"] if audio else [])),
                          "pixel_formats":["yuv420p"] if video else [],
                      },
                      "expected":{
                          "video":video,"audio":audio,
                          "duration":scene["duration"],
                          "color":(
                              ast["node"]["colorManagement"]["output"]
                              if video else None),
                      },
                      "assetDigests":asset_digests,
                      "assetMetadata":asset_metadata,
                      "sidecars":sidecars,
                      "fontFiles":[str(path) for path in sorted(set(font_files))]})
    return plans

def validate_ffmpeg_plan_capabilities(plans: list[dict[str,Any]]) -> None:
    if not plans: return
    executable=plans[0]["argv"][0]
    capabilities=probe_ffmpeg(str(executable),Diagnostic)
    for plan in plans:
        require_capabilities(capabilities,plan["requirements"],Diagnostic)
        plan["backendCapabilities"]=capabilities.manifest()

def _prepare_sidecars(plan: dict[str,Any]) -> list[tuple[Path,Path,dict[str,Any]]]:
    prepared=[]
    try:
        for descriptor in plan.get("sidecars",[]):
            target=Path(descriptor["path"])
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(f".{target.name}.vdls-tmp")
            try:
                content=serialize_sidecar(
                    descriptor["cues"],descriptor["format"])
            except ValueError as error:
                raise Diagnostic("VDLS-SUB-004",str(error)) from error
            temporary.write_text(content,encoding="utf-8",newline="\n")
            prepared.append((temporary,target,descriptor))
        return prepared
    except BaseException:
        for temporary,_,_ in prepared: temporary.unlink(missing_ok=True)
        raise

def _publish_sidecars(
    prepared: list[tuple[Path,Path,dict[str,Any]]]
) -> list[dict[str,Any]]:
    artifacts=[]
    for temporary,target,descriptor in prepared:
        os.replace(temporary,target)
        digest="sha256:"+hashlib.sha256(target.read_bytes()).hexdigest()
        artifacts.append({
            "id":descriptor["id"],"path":str(target),"sha256":digest,
            "media":{
                "kind":"subtitle","format":descriptor["format"],
                "language":descriptor.get("language"),
                "cueCount":len(descriptor["cues"]),
            },
            "cacheKey":None,
        })
    return artifacts

def reproducibility_evidence(
    ast: dict[str,Any], source: Path, plans: list[dict[str,Any]],
    lock_path: Path,
) -> dict[str,Any]:
    if not plans:
        raise Diagnostic("VDLS-CONFIG-011","reproducible build has no backend plan")
    try: lock=json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError,UnicodeDecodeError):
        raise Diagnostic("VDLS-CONFIG-012","lockfile format unsupported")
    capability=plans[0].get("backendCapabilities",{})
    backend=next((entry for entry in lock.get("backends",[])
                  if entry.get("id")=="org.vdls.backend.ffmpeg"),None)
    if (not backend
            or backend.get("version")!=capability.get("version")
            or backend.get("capabilityDigest")!=capability.get("digest")):
        raise Diagnostic(
            "VDLS-CONFIG-011",
            "lockfile does not pin the detected FFmpeg version and capabilities")
    pinned_paths=set()
    asset_evidence=[]
    for asset in ast["node"]["assets"]:
        kind=asset["source"]["kind"]
        if kind in {"Url","Plugin"}:
            raise Diagnostic(
                "VDLS-MACRO-010",
                f"asset `{asset['assetId']}` is not immutable in reproducible mode")
        if kind!="File": continue
        if not asset.get("integrity"):
            raise Diagnostic(
                "VDLS-CONFIG-011",
                f"file asset `{asset['assetId']}` lacks pinned integrity")
        path=(source.parent/asset["source"]["path"]).resolve()
        if not path.is_file():
            raise Diagnostic("VDLS-ASSET-001",f"asset not found: {path}")
        actual="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()
        if actual!=asset["integrity"]:
            raise Diagnostic(
                "VDLS-ASSET-003",
                f"asset integrity mismatch: {asset['assetId']}")
        pinned_paths.add(path)
        asset_evidence.append({
            "id":asset["assetId"],"path":str(path),"sha256":actual})
    for plan in plans:
        for font in plan.get("fontFiles",[]):
            if Path(font).resolve() not in pinned_paths:
                raise Diagnostic(
                    "VDLS-CONFIG-011",
                    "reproducible text requires a pinned font asset")
    settings=ast["node"].get("settings") or {}
    seed_clause=next((clause for clause in settings.get("clauses",[])
                      if isinstance(clause,list) and len(clause)==2
                      and clause[0]=="seed"),None)
    seed=int(seed_clause[1]) if seed_clause else 0
    return {
        "mode":"strict","offline":True,"locked":True,
        "seed":seed,
        "backend":{
            "id":"org.vdls.backend.ffmpeg",
            "version":capability["version"],
            "capabilityDigest":capability["digest"],
        },
        "assets":asset_evidence,
        "metadataTimestamps":"normalized",
        "encoderThreads":1,
    }

def execute_ffmpeg_plans(plans: list[dict[str,Any]],
                         cache_dir: Path | None=None,
                         no_cache: bool=False) -> tuple[list[dict[str,Any]],int,int]:
    probe=shutil.which("ffprobe")
    if not probe: raise Diagnostic("VDLS-FFMPEG-002","FFprobe executable not found")
    validate_ffmpeg_plan_capabilities(plans)
    artifacts=[]; cache_hits=0; cache_misses=0
    for plan in plans:
        target=Path(plan["outputPath"]); temporary=Path(plan["temporaryPath"])
        script=Path(plan["filterScriptPath"])
        target.parent.mkdir(parents=True,exist_ok=True)
        prepared_sidecars=_prepare_sidecars(plan)
        key=plan["cacheKey"].split(":",1)[1]
        cache_object=cache_metadata=None
        if cache_dir and not no_cache:
            cache_object=cache_dir/"objects"/key[:2]/key[2:]
            cache_metadata=cache_dir/"metadata"/key[:2]/f"{key[2:]}.json"
            if cache_object.exists() and cache_metadata.exists():
                try: metadata=json.loads(cache_metadata.read_text(encoding="utf-8"))
                except (json.JSONDecodeError,UnicodeDecodeError):
                    raise Diagnostic("VDLS-CACHE-003","cache metadata invalid")
                digest=hashlib.sha256(cache_object.read_bytes()).hexdigest()
                if metadata.get("sha256")!="sha256:"+digest:
                    raise Diagnostic("VDLS-CACHE-002","cache object digest mismatch")
                shutil.copyfile(cache_object,temporary)
                os.replace(temporary,target)
                validate_artifact(target,metadata["media"],plan["expected"],Diagnostic)
                artifacts.append({"id":plan["targetId"],"path":str(target),
                                  "sha256":metadata["sha256"],
                                  "media":metadata["media"],
                                  "cacheKey":plan["cacheKey"]})
                artifacts.extend(_publish_sidecars(prepared_sidecars))
                cache_hits+=1
                continue
        cache_misses+=1
        script.write_text(plan["filterScript"],encoding="utf-8")
        try:
            completed=run_external(plan["argv"])
        except (ProcessInterrupted,ProcessTimedOut):
            temporary.unlink(missing_ok=True)
            script.unlink(missing_ok=True)
            for item,_,_ in prepared_sidecars: item.unlink(missing_ok=True)
            raise
        if completed.returncode:
            excerpt=completed.stderr[-4000:]
            for item,_,_ in prepared_sidecars: item.unlink(missing_ok=True)
            raise Diagnostic("VDLS-FFMPEG-006",
                             f"FFmpeg failed for target `{plan['targetId']}`",
                             notes=(excerpt,))
        try:
            inspected=run_external(
                [probe,"-v","error","-show_entries",
                 "format=duration:stream=codec_type,width,height,r_frame_rate,"
                 "color_space,color_transfer,color_primaries,color_range",
                 "-of","json",str(temporary)],timeout=30)
        except (ProcessInterrupted,ProcessTimedOut):
            temporary.unlink(missing_ok=True)
            script.unlink(missing_ok=True)
            for item,_,_ in prepared_sidecars: item.unlink(missing_ok=True)
            raise
        if inspected.returncode:
            temporary.unlink(missing_ok=True)
            script.unlink(missing_ok=True)
            for item,_,_ in prepared_sidecars: item.unlink(missing_ok=True)
            raise Diagnostic("VDLS-FFMPEG-011",
                             f"FFprobe failed for target `{plan['targetId']}`")
        media=json.loads(inspected.stdout)
        validate_artifact(temporary,media,plan["expected"],Diagnostic)
        os.replace(temporary,target)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        artifacts.append({"id":plan["targetId"],"path":str(target),
                          "sha256":"sha256:"+digest,"media":media,
                          "cacheKey":plan["cacheKey"]})
        artifacts.extend(_publish_sidecars(prepared_sidecars))
        if cache_object and cache_metadata:
            cache_object.parent.mkdir(parents=True,exist_ok=True)
            cache_metadata.parent.mkdir(parents=True,exist_ok=True)
            object_temp=cache_object.with_name(
                f"{cache_object.name}.tmp.{os.getpid()}")
            metadata_temp=cache_metadata.with_name(
                f"{cache_metadata.name}.tmp.{os.getpid()}")
            shutil.copyfile(target,object_temp)
            os.replace(object_temp,cache_object)
            metadata_temp.write_text(canonical_json({
                "schema":"vdls.cache-metadata/1","cacheKey":plan["cacheKey"],
                "sha256":"sha256:"+digest,"media":media,
                "targetId":plan["targetId"],
            }),encoding="utf-8")
            os.replace(metadata_temp,cache_metadata)
        script.unlink(missing_ok=True)
    return artifacts,cache_hits,cache_misses

def inspect_media(path: Path) -> dict[str,Any]:
    probe=shutil.which("ffprobe")
    if not probe: raise Diagnostic("VDLS-FFMPEG-002","FFprobe executable not found")
    if not path.exists(): raise Diagnostic("VDLS-ASSET-001",f"asset not found: {path}")
    completed=run_external(
        [probe,"-v","error","-show_format","-show_streams","-of","json",str(path)],
        timeout=30)
    if completed.returncode:
        raise Diagnostic("VDLS-MEDIA-001","media probe failed",
                         notes=(completed.stderr[-4000:],))
    raw=json.loads(completed.stdout)
    try: exif=read_exif(path)
    except RuntimeError as error:
        raise Diagnostic("VDLS-FFMPEG-004",str(error)) from error
    return {
        "schema":"vdls.media-inspect/1","path":str(path.resolve()),
        "sha256":"sha256:"+hashlib.sha256(path.read_bytes()).hexdigest(),
        "format":raw.get("format",{}),"streams":raw.get("streams",[]),
        "exif":exif,
    }

def doctor_result() -> dict[str,Any]:
    checks=[]; success=True
    for program in ("ffmpeg","ffprobe"):
        executable=shutil.which(program)
        if not executable:
            checks.append({"id":program,"success":False,"message":"not found"})
            success=False; continue
        completed=run_external([executable,"-version"],timeout=30)
        first_line=completed.stdout.splitlines()[0] if completed.stdout else ""
        checks.append({"id":program,"success":completed.returncode==0,
                       "path":executable,"version":first_line})
        success=success and completed.returncode==0
    font=None
    try: font=_default_font_file()
    except Diagnostic: success=False
    checks.append({"id":"text-font","success":font is not None,
                   "path":str(font) if font else None})
    return {"schema":"vdls.doctor/1","success":success,"checks":checks}

def format_vdls(text: str) -> str:
    """Deterministic two-space formatter for the portable reader subset."""
    has_lang=bool(re.match(r"\s*#lang\s+vdls",text))
    reader_text=re.sub(r"^\s*#lang\s+vdls[^\n]*(?:\n|$)","",text,count=1)
    forms=parse(reader_text)

    def atom(value: Any) -> str:
        if isinstance(value,Symbol): return str(value)
        if isinstance(value,str): return json.dumps(value,ensure_ascii=False)
        return str(value)

    def render(value: Any, depth: int) -> list[str]:
        prefix="  "*depth
        if not isinstance(value,list): return [prefix+atom(value)]
        if not value: return [prefix+"()"]
        if all(not isinstance(item,list) for item in value):
            return [prefix+"("+" ".join(atom(item) for item in value)+")"]
        head=[]; rest=[]; nested=False
        for item in value:
            if isinstance(item,list): nested=True
            if nested: rest.append(item)
            else: head.append(item)
        lines=[prefix+"("+" ".join(atom(item) for item in head)]
        for item in rest:
            lines.extend(render(item,depth+1))
        lines.append(prefix+")")
        return lines

    comments=[]
    in_string=False; escaped=False
    for line in text.splitlines():
        comment_at=None
        for index,char in enumerate(line):
            if escaped: escaped=False; continue
            if char=="\\" and in_string: escaped=True; continue
            if char=='"': in_string=not in_string; continue
            if char==";" and not in_string:
                comment_at=index; break
        if comment_at is not None: comments.append(line[comment_at:].rstrip())
    lines=["#lang vdls"] if has_lang else []
    lines.extend(comments)
    for form in forms: lines.extend(render(form,0))
    return "\n".join(lines)+"\n"

def build_manifest_document(
    source: Path, source_text: str, lock_hash: str | None,
    rendered: list[dict[str,Any]], plans: list[dict[str,Any]],
    plugins: list[dict[str,Any]],
    reproducibility: dict[str,Any] | None=None,
) -> dict[str,Any]:
    font_paths=sorted({font for plan in plans
                       for font in plan.get("fontFiles",[])})
    asset_digests={}
    for plan in plans: asset_digests.update(plan.get("assetDigests",{}))
    asset_metadata={}
    for plan in plans: asset_metadata.update(plan.get("assetMetadata",{}))
    toolchain=[]
    if plans:
        capability=plans[0].get("backendCapabilities",{})
        toolchain.append({
            "id":"org.vdls.backend.ffmpeg",
            "backendVersion":"1.0.0",
            "path":plans[0]["argv"][0],
            "version":capability.get("version"),
            "capabilityDigest":capability.get("digest"),
        })
    toolchain.extend({
        "id":"font","path":font,
        "sha256":"sha256:"+hashlib.sha256(Path(font).read_bytes()).hexdigest()
    } for font in font_paths)
    toolchain.extend({
        "id":plugin["id"],"type":"plugin","version":plugin["version"],
        "abi":plugin["abi"],
    } for plugin in plugins)
    return {
        "schema":"vdls.build-manifest/1","spec_version":"1.0.0",
        "compiler":{"id":"org.vdls.reference","version":VERSION},
        "project_hash":"sha256:"+hashlib.sha256(
            source_text.encode("utf-8")).hexdigest(),
        "sources":[{
            "path":str(source.resolve()),
            "sha256":"sha256:"+hashlib.sha256(
                source_text.encode("utf-8")).hexdigest(),
        }],
        "assets":[{"id":asset_id,"sha256":digest,
                   "metadata":asset_metadata.get(asset_id)}
                  for asset_id,digest in sorted(asset_digests.items())],
        "lock_hash":lock_hash,"targets":rendered,"toolchain":toolchain,
        "reproducible":reproducibility is not None,
        "reproducibility":reproducibility,
        "impureNodesExecuted":False,
    }

def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+".tmp")
    temporary.write_text(content,encoding="utf-8")
    os.replace(temporary,path)

def diagnostic_exit_status(code: str) -> int:
    if code=="VDLS-PLUGIN-008": return 124
    if code in {
        "VDLS-FFMPEG-001","VDLS-FFMPEG-002","VDLS-FFMPEG-004",
        "VDLS-TEXT-001",
    }: return 11
    if code in {"VDLS-PLUGIN-006"}: return 12
    if code.startswith("VDLS-CLI"): return 2
    if code.startswith(("VDLS-ASSET","VDLS-MEDIA")): return 4
    if code.startswith("VDLS-PLUGIN"): return 5
    if code.startswith(("VDLS-BACKEND","VDLS-FFMPEG","VDLS-GRAPH")): return 6
    if code.startswith(("VDLS-ENCODE","VDLS-OUTPUT")): return 7
    if code.startswith("VDLS-CONFIG"): return 8
    if code.startswith("VDLS-CACHE"): return 9
    if code.startswith("VDLS-INTERNAL"): return 10
    if code.startswith("VDLS-SECURITY"): return 12
    return 3

def apply_preview_profile(
    ast: dict[str,Any], resolution: str | None,
    selected: list[str],
) -> None:
    dimensions=None
    if resolution:
        match=re.fullmatch(r"([1-9]\d*)x([1-9]\d*)",resolution)
        if not match:
            raise Diagnostic(
                "VDLS-CLI-007",
                "preview resolution must be WIDTHxHEIGHT")
        dimensions=(int(match.group(1)),int(match.group(2)))
        if dimensions[0]>16384 or dimensions[1]>16384:
            raise Diagnostic(
                "VDLS-CLI-007","preview resolution exceeds 16384 pixels")
    for output in ast["node"]["outputs"]:
        if selected and output["outputId"] not in selected:
            continue
        if dimensions:
            if not output.get("video"):
                raise Diagnostic(
                    "VDLS-CLI-007",
                    f"preview target `{output['outputId']}` has no video")
            output["video"]["width"],output["video"]["height"]=dimensions
        metadata=output.setdefault("metadata",{})
        metadata["vdls.preview"]="non-conformant"
        marker="VDLS preview; non-conformant for final delivery"
        metadata["comment"]=(
            f"{metadata['comment']}; {marker}"
            if metadata.get("comment") else marker)

def preview_watch_snapshot(root: Path) -> dict[str,tuple[int,int]]:
    ignored={".git",".vdls","build","dist","__pycache__"}
    result={}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        try:
            stat=path.stat()
        except OSError:
            continue
        result[str(path.resolve())]=(stat.st_mtime_ns,stat.st_size)
    return result

def _cancel_preview_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is not None: return
    try:
        if os.name=="nt":
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(child.pid,signal.SIGINT)
        child.wait(timeout=2)
    except (OSError,subprocess.TimeoutExpired):
        child.kill()
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired: pass

def run_preview_watch(arguments: list[str], source: Path) -> int:
    child_arguments=[item for item in arguments if item!="--watch"]
    command=[sys.executable,str(Path(__file__).resolve()),*child_arguments]
    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
    child=None
    snapshot=preview_watch_snapshot(source.parent)
    try:
        while True:
            child=subprocess.Popen(
                command,creationflags=creationflags,
                start_new_session=(os.name!="nt"))
            while True:
                time.sleep(0.1)
                current=preview_watch_snapshot(source.parent)
                if current!=snapshot:
                    # Require 250 ms of filesystem quiet before rebuilding.
                    stable_since=time.monotonic()
                    snapshot=current
                    while time.monotonic()-stable_since<0.25:
                        time.sleep(0.05)
                        latest=preview_watch_snapshot(source.parent)
                        if latest!=snapshot:
                            snapshot=latest
                            stable_since=time.monotonic()
                    _cancel_preview_child(child)
                    break
                if child.poll() is not None:
                    # A completed preview remains live until an input changes.
                    time.sleep(0.15)
    except KeyboardInterrupt:
        if child is not None: _cancel_preview_child(child)
        return 130

def main(argv: list[str] | None=None) -> int:
    raw_arguments=list(sys.argv[1:] if argv is None else argv)
    ap=CliParser(prog="vdls")
    ap.add_argument("--project")
    ap.add_argument("--config")
    ap.add_argument("--profile")
    ap.add_argument("--locale")
    ap.add_argument("--color",choices=["auto","always","never"],default="auto")
    ap.add_argument("--log-level",choices=["trace","debug","info","warn","error","silent"],default="info")
    ap.add_argument("--diagnostic-format",choices=["human","json","json-lines"],default="human")
    ap.add_argument("--offline",action="store_true")
    ap.add_argument("--locked",action="store_true")
    ap.add_argument("--frozen",action="store_true")
    ap.add_argument("--jobs",type=int)
    ap.add_argument("--cache-dir")
    ap.add_argument("--no-cache",action="store_true")
    ap.add_argument("--reproducible",action="store_true")
    ap.add_argument("--version",action="store_true")
    ap.add_argument("command",nargs="?")
    ap.add_argument("source",nargs="?")
    ap.add_argument("--target",action="append",default=[])
    ap.add_argument("--output-dir")
    ap.add_argument("--emit",action="append",default=[])
    ap.add_argument("--keep-going",action="store_true")
    ap.add_argument("--format",choices=["json","dot","text"],default="json")
    ap.add_argument("--unoptimized",action="store_true")
    ap.add_argument("--check",action="store_true")
    ap.add_argument("--stdin",action="store_true")
    ap.add_argument("--yes",action="store_true")
    ap.add_argument("--time")
    ap.add_argument("--resolution")
    ap.add_argument("--output")
    ap.add_argument("--watch",action="store_true")
    ap.add_argument("--max-size")
    ap.add_argument("--older-than")
    ap.add_argument("operands",nargs="*")
    started=time.perf_counter()
    try:
        ns=ap.parse_args(raw_arguments)
        if ns.frozen:
            ns.offline=True; ns.locked=True
        if ns.reproducible:
            ns.offline=True; ns.locked=True
        if ns.version: print(f"vdls {VERSION} (spec 1.0.0)"); return 0
        commands={"build","check","graph","preview","render-frame","inspect","fmt",
                  "lock","plugin","cache","doctor"}
        if ns.command not in commands:
            raise Diagnostic("VDLS-CLI-004",
                             "expected a VDLS command")
        if ns.command=="preview" and ns.watch:
            watch_source=discover(ns.source or ns.project)
            return run_preview_watch(raw_arguments,watch_source)
        if ns.command=="doctor":
            result=doctor_result()
            if ns.diagnostic_format.startswith("json"):
                print(canonical_json(result),end="")
            else:
                for check in result["checks"]:
                    print(f"{'ok' if check['success'] else 'error'}: {check['id']}")
            return 0 if result["success"] else 11
        if ns.command=="inspect":
            if not ns.source: raise Diagnostic("VDLS-CLI-004","inspect requires an asset path")
            result=inspect_media(Path(ns.source))
            if ns.format=="json" or ns.diagnostic_format.startswith("json"):
                print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
            else:
                print(f"{result['path']}\nstreams: {len(result['streams'])}")
            return 0
        if ns.command=="fmt":
            paths=([Path(ns.source)] if ns.source else [])+[Path(item) for item in ns.operands]
            if ns.stdin:
                original=sys.stdin.read(); formatted=format_vdls(original)
                if ns.check: return 0 if formatted==original else 1
                print(formatted,end=""); return 0
            if not paths: raise Diagnostic("VDLS-CLI-004","fmt requires files or --stdin")
            different=False
            for path in paths:
                original=path.read_text(encoding="utf-8"); formatted=format_vdls(original)
                if formatted!=original:
                    different=True
                    if not ns.check:
                        temporary=path.with_name(path.name+".tmp")
                        temporary.write_text(formatted,encoding="utf-8")
                        os.replace(temporary,path)
            return 1 if ns.check and different else 0
        if ns.command=="lock":
            project_path=Path(ns.project).resolve() if ns.project else Path.cwd()
            if project_path.is_file(): project_path=project_path.parent
            lock_path=project_path/"vdls.lock"
            existing_plugins=[]
            if lock_path.exists():
                try:
                    existing=json.loads(lock_path.read_text(encoding="utf-8"))
                    existing_plugins=existing.get("plugins",[])
                except (json.JSONDecodeError,UnicodeDecodeError):
                    raise Diagnostic("VDLS-CONFIG-012","lockfile format unsupported")
            ffmpeg=shutil.which("ffmpeg")
            if not ffmpeg:
                raise Diagnostic("VDLS-FFMPEG-001","FFmpeg executable not found")
            capability=probe_ffmpeg(ffmpeg,Diagnostic)
            lock={
                "schema":"vdls.lock/1","specVersion":"1.0.0",
                "plugins":existing_plugins,
                "backends":[{
                    "id":"org.vdls.backend.ffmpeg","abiMajor":1,
                    "version":capability.version,
                    "capabilityDigest":capability.digest,
                }],
            }
            content=canonical_json(lock)
            if ns.check:
                return 0 if lock_path.exists() and lock_path.read_text(encoding="utf-8")==content else 8
            temporary=lock_path.with_name(lock_path.name+".tmp")
            temporary.write_text(content,encoding="utf-8"); os.replace(temporary,lock_path)
            print(lock_path); return 0
        if ns.command=="plugin":
            subcommand=ns.source or "list"
            plugin_source=discover(ns.project) if ns.project else discover(None)
            plugin_config=load_config(plugin_source,ns.config,ns.profile)
            plugins=load_locked_plugins(plugin_source.parent,plugin_config)
            if subcommand=="list":
                summaries=[{"id":item["id"],"name":item["name"],
                            "version":item["version"],"abi":item["abi"]}
                           for item in plugins]
                if ns.diagnostic_format.startswith("json"):
                    print(canonical_json(summaries),end="")
                elif summaries:
                    for item in summaries:
                        print(f"{item['id']} {item['version']} ({item['abi']})")
                else: print("No plugins locked.")
                return 0
            plugin_id=ns.operands[0] if ns.operands else None
            if not plugin_id:
                raise Diagnostic("VDLS-CLI-004",
                                 f"plugin {subcommand} requires an ID")
            plugin=next((item for item in plugins if item["id"]==plugin_id),None)
            if not plugin: raise Diagnostic("VDLS-PLUGIN-001","plugin not found")
            if subcommand=="inspect":
                print(json.dumps(plugin,ensure_ascii=False,sort_keys=True,indent=2))
                return 0
            if subcommand=="permissions":
                if ns.diagnostic_format.startswith("json"):
                    print(canonical_json(plugin["permissions"]),end="")
                else:
                    for permission in plugin["permissions"]: print(permission)
                return 0
            if subcommand=="doctor":
                entry=Path(plugin["entry"])
                success=entry.exists()
                if success:
                    cache_value=Path(plugin_config.get("cache_dir",".vdls/cache"))
                    cache_root=(cache_value if cache_value.is_absolute()
                                else plugin_source.parent/cache_value).resolve()
                    with PluginProcessHost(
                            plugin,plugin_source.parent,cache_root,
                            set(plugin["permissions"]),timeout=5) as host:
                        advertised=set(host.capabilities())
                        expected=set(plugin["capabilities"])
                        if not expected<=advertised:
                            raise Diagnostic("VDLS-PLUGIN-005",
                                             "plugin did not advertise locked capability")
                print(f"{'ok' if success else 'error'}: {plugin_id}")
                return 0 if success else 5
            raise Diagnostic("VDLS-PLUGIN-001","plugin not found")
        if ns.command=="cache":
            subcommand=ns.source or "status"
            if ns.cache_dir:
                cache_path=Path(ns.cache_dir).resolve()
            elif ns.project:
                cache_source=discover(ns.project)
                cache_config=load_config(cache_source,ns.config,ns.profile)
                configured=Path(cache_config.get("cache_dir",".vdls/cache"))
                cache_path=(configured if configured.is_absolute()
                            else cache_source.parent/configured).resolve()
            else:
                cache_path=Path(".vdls/cache").resolve()
            if subcommand=="status":
                files=list(cache_path.rglob("*")) if cache_path.exists() else []
                size=sum(item.stat().st_size for item in files if item.is_file())
                result={"schema":"vdls.cache-status/1","path":str(cache_path),
                        "files":sum(item.is_file() for item in files),"bytes":size}
                print(canonical_json(result) if ns.diagnostic_format.startswith("json")
                      else f"{cache_path}: {result['files']} files, {size} bytes",end="")
                return 0
            if subcommand=="verify":
                verified=0
                for metadata_path in ((cache_path/"metadata").rglob("*.json")
                                      if (cache_path/"metadata").exists() else []):
                    try: metadata=json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError,UnicodeDecodeError):
                        raise Diagnostic("VDLS-CACHE-003","cache metadata invalid")
                    relative=metadata_path.relative_to(cache_path/"metadata")
                    object_path=cache_path/"objects"/relative.parent/relative.stem
                    if not object_path.exists():
                        raise Diagnostic("VDLS-CACHE-001","cache object missing")
                    digest="sha256:"+hashlib.sha256(object_path.read_bytes()).hexdigest()
                    if digest!=metadata.get("sha256"):
                        raise Diagnostic("VDLS-CACHE-002","cache object digest mismatch")
                    verified+=1
                print(f"verified {verified} cache objects"); return 0
            if subcommand=="clean":
                if not ns.yes:
                    raise Diagnostic("VDLS-CLI-010",
                                     "cache clean requires --yes")
                resolved=cache_path.resolve()
                if resolved.exists():
                    if resolved==Path.cwd().resolve() or resolved.parent==resolved:
                        raise Diagnostic("VDLS-SECURITY-010","refusing to clean project root")
                    shutil.rmtree(resolved)
                print(f"cleaned {resolved}"); return 0
            if subcommand=="prune":
                max_bytes=None
                if ns.max_size:
                    size_match=re.fullmatch(r"(\d+)(B|KB|MB|GB)",ns.max_size,re.I)
                    if not size_match:
                        raise Diagnostic("VDLS-CONFIG-004","invalid max-size")
                    factor={"B":1,"KB":1024,"MB":1024**2,"GB":1024**3}[
                        size_match.group(2).upper()]
                    max_bytes=int(size_match.group(1))*factor
                older_seconds=None
                if ns.older_than:
                    older=ratio(ns.older_than)
                    older_seconds=float(Fraction(older["num"],older["den"]))
                entries=[]
                metadata_root=cache_path/"metadata"
                for metadata_path in (metadata_root.rglob("*.json")
                                      if metadata_root.exists() else []):
                    relative=metadata_path.relative_to(metadata_root)
                    object_path=cache_path/"objects"/relative.parent/relative.stem
                    size=object_path.stat().st_size if object_path.exists() else 0
                    entries.append((metadata_path.stat().st_mtime,size,
                                    metadata_path,object_path))
                remove=set()
                now=time.time()
                if older_seconds is not None:
                    remove.update(item[2] for item in entries
                                  if now-item[0]>older_seconds)
                if max_bytes is not None:
                    total=sum(item[1] for item in entries if item[2] not in remove)
                    for item in sorted(entries):
                        if total<=max_bytes: break
                        if item[2] not in remove:
                            remove.add(item[2]); total-=item[1]
                for metadata_path in remove:
                    relative=metadata_path.relative_to(metadata_root)
                    object_path=cache_path/"objects"/relative.parent/relative.stem
                    metadata_path.unlink(missing_ok=True)
                    object_path.unlink(missing_ok=True)
                print(f"pruned {len(remove)} cache objects"); return 0
            raise Diagnostic("VDLS-CLI-004",
                             f"cache subcommand `{subcommand}` is not implemented")
        try:
            if ns.config and not (ns.source or ns.project):
                explicit_path=Path(ns.config).resolve()
                explicit_data=tomllib.loads(explicit_path.read_text(encoding="utf-8"))
                source=(explicit_path.parent/explicit_data.get("entry","main.vdsl")).resolve()
            else:
                source=discover(ns.source or ns.project)
        except tomllib.TOMLDecodeError as error:
            raise Diagnostic("VDLS-CONFIG-002",f"invalid TOML: {error}")
        if not source.exists(): raise Diagnostic("VDLS-CLI-005",f"project entry not found: {source}")
        if ns.reproducible and ns.command!="build":
            raise Diagnostic(
                "VDLS-CLI-003","--reproducible is only valid with build")
        config=load_config(source,ns.config,ns.profile)
        if ns.jobs is not None:
            if ns.jobs<1: raise Diagnostic("VDLS-CONFIG-004","jobs must be positive")
            config["build"]["jobs"]=ns.jobs
        if ns.cache_dir: config["cache_dir"]=ns.cache_dir
        lock_hash=None
        if ns.locked:
            _,lock_hash=validate_lockfile(source.parent,config)
        plugin_lock=(source.parent/config.get("plugins",{}).get(
            "lockfile","vdls.lock")).resolve()
        plugins=load_locked_plugins(source.parent,config) if plugin_lock.exists() else []
        if lock_hash is None and plugin_lock.exists():
            lock_hash="sha256:"+hashlib.sha256(plugin_lock.read_bytes()).hexdigest()
        try:
            source_text=source.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise Diagnostic("VDLS-READ-001","invalid UTF-8 input",error.start)
        ast=compile_source(source_text,source)
        if ns.command=="preview":
            apply_preview_profile(ast,ns.resolution,ns.target)
        g=graph(ast)
        if ns.offline:
            remote=next((asset for asset in ast["node"]["assets"]
                         if asset["source"]["kind"]=="Url"),None)
            if remote:
                raise Diagnostic("VDLS-ASSET-004",
                                 f"remote asset `{remote['assetId']}` unavailable in offline mode")
        available={x["outputId"] for x in ast["node"]["outputs"]}
        missing=[target for target in ns.target if target not in available]
        if missing: raise Diagnostic("VDLS-CLI-008",f"requested target does not exist: {missing[0]}")
        if ns.command=="graph":
            if ns.format=="json": print(json.dumps(g,ensure_ascii=False,sort_keys=True,indent=2))
            elif ns.format=="dot": print(graph_dot(g),end="")
            else:
                for node in g["nodes"]: print(f"{node['id']} {node['kind']}")
            return 0
        if ns.command=="render-frame":
            if not ns.target or len(ns.target)!=1:
                raise Diagnostic("VDLS-CLI-004","render-frame requires exactly one --target")
            if not ns.time or not ns.output:
                raise Diagnostic("VDLS-CLI-004","render-frame requires --time and --output")
            frame_time=ratio(ns.time)
            if frame_time.get("unit"):
                raise Diagnostic("VDLS-CLI-007","invalid frame time")
            intermediate_dir=source.parent/".vdls"/"frame-source"
            frame_plans=ffmpeg_plans(ast,source,str(intermediate_dir),ns.target)
            rendered_source=execute_ffmpeg_plans(
                frame_plans,no_cache=True)[0][0]
            frame_output=Path(ns.output).resolve()
            frame_output.parent.mkdir(parents=True,exist_ok=True)
            temporary=frame_output.with_name(
                f".{frame_output.stem}.vdls-tmp{frame_output.suffix}")
            seconds=str(Fraction(frame_time["num"],frame_time["den"]))
            try:
                completed=run_external(
                    [shutil.which("ffmpeg") or "ffmpeg","-hide_banner","-nostdin","-y",
                     "-ss",seconds,"-i",rendered_source["path"],"-frames:v","1",
                     str(temporary)])
            except (ProcessInterrupted,ProcessTimedOut):
                temporary.unlink(missing_ok=True)
                raise
            if completed.returncode:
                raise Diagnostic("VDLS-FFMPEG-006","frame rendering failed",
                                 notes=(completed.stderr[-4000:],))
            os.replace(temporary,frame_output)
            print(frame_output); return 0
        effective_output_dir=ns.output_dir
        if effective_output_dir:
            output_path=Path(effective_output_dir)
            effective_output_dir=str(
                output_path.resolve() if output_path.is_absolute()
                else (source.parent/output_path).resolve())
        elif ns.command=="build" and config.get("output_dir"):
            effective_output_dir=str((source.parent/config["output_dir"]).resolve())
        if ns.command=="preview" and not effective_output_dir:
            effective_output_dir=str(source.parent/".vdls"/"preview")
        should_render=ns.command in {"build","preview"}
        should_plan=should_render or ns.command=="check"
        plans=ffmpeg_plans(
            ast,source,effective_output_dir,ns.target,ns.reproducible
        ) if should_plan else []
        cache_value=Path(config.get("cache_dir",".vdls/cache"))
        cache_root=(cache_value if cache_value.is_absolute()
                    else source.parent/cache_value).resolve()
        reproducibility=None
        if ns.reproducible:
            validate_ffmpeg_plan_capabilities(plans)
            reproducibility=reproducibility_evidence(
                ast,source,plans,plugin_lock)
        if should_render:
            rendered,cache_hits,cache_misses=execute_ffmpeg_plans(
                plans,cache_root,ns.no_cache or ns.command=="preview")
        elif ns.command=="check":
            validate_ffmpeg_plan_capabilities(plans)
            rendered=[]; cache_hits=cache_misses=0
        else:
            rendered=[]; cache_hits=cache_misses=0
        artifacts=[item["path"] for item in rendered]
        manifest_document=(build_manifest_document(
            source,source_text,lock_hash,rendered,plans,plugins,
            reproducibility)
            if ns.command=="build" else None)
        if manifest_document is not None:
            manifest_base=(Path(effective_output_dir) if effective_output_dir
                           else source.parent)
            manifest_target=manifest_base/f"{source.stem}.manifest.json"
            _atomic_write_text(manifest_target,canonical_json(manifest_document))
            artifacts.append(str(manifest_target.resolve()))
        for kind in ns.emit:
            suffix=".json"; content: str
            if kind=="expanded": suffix=".vdsl"; content=source_text
            elif kind=="ast-json": content=canonical_json(ast)
            elif kind=="graph-json": content=canonical_json(g)
            elif kind=="graph-dot": suffix=".dot"; content=graph_dot(g)
            elif kind=="commands":
                content=canonical_json({
                    "commands":[{
                        "targetId":plan["targetId"],
                        "argv":plan["argv"],
                        "filterScript":plan["filterScript"],
                    } for plan in plans]})
            elif kind=="backend-plan":
                content=canonical_json({"backend":"org.vdls.backend.ffmpeg",
                                        "backendVersion":"1.0.0","stages":plans})
            elif kind=="manifest":
                if manifest_document is None:
                    manifest_document=build_manifest_document(
                        source,source_text,lock_hash,rendered,plans,plugins,
                        reproducibility)
                content=canonical_json(manifest_document)
            else: raise Diagnostic("VDLS-CLI-004",f"unsupported emit kind `{kind}`")
            base=Path(effective_output_dir) if effective_output_dir else source.parent
            base.mkdir(parents=True,exist_ok=True)
            target=base/f"{source.stem}.{kind}{suffix}"
            _atomic_write_text(target,content)
            resolved_target=str(target.resolve())
            if resolved_target not in artifacts: artifacts.append(resolved_target)
        result={"schema":"vdls.cli-result/1","command":ns.command,"success":True,"project":str(source.parent.resolve()),"targets":[x["outputId"] for x in ast["node"]["outputs"] if not ns.target or x["outputId"] in ns.target],"artifacts":artifacts,"diagnostics":[],"statistics":{"elapsed_ms":round((time.perf_counter()-started)*1000),"cache_hits":cache_hits,"cache_misses":cache_misses}}
        if ns.diagnostic_format=="json": print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        elif ns.diagnostic_format=="json-lines": print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        elif ns.command=="check": print("check: OK")
        elif ns.command=="preview":
            for artifact in artifacts: print(artifact)
        return 0
    except Diagnostic as e:
        d=e.as_dict(locals().get("source"));
        if "ns" in locals() and ns.diagnostic_format.startswith("json"):
            failure={
                "schema":"vdls.cli-result/1",
                "command":ns.command,
                "success":False,
                "project":str(locals().get("source","")),
                "targets":[],
                "artifacts":[],
                "diagnostics":[d],
                "statistics":{
                    "elapsed_ms":round((time.perf_counter()-started)*1000),
                    "cache_hits":0,"cache_misses":0,
                },
            }
            print(json.dumps(failure,ensure_ascii=False,sort_keys=True))
        else: print(f"error[{e.code}]: {e.message}",file=sys.stderr)
        return diagnostic_exit_status(e.code)
    except ProcessInterrupted:
        print("error: interrupted",file=sys.stderr)
        return 130
    except ProcessTimedOut as error:
        print(f"error: {error}",file=sys.stderr)
        return 124

if __name__ == "__main__": raise SystemExit(main())
