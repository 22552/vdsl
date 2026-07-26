"""Independent VDLS text-layout boundary and libass surface adapter.

No FFmpeg ``drawtext`` option crosses this API. Text is normalized into a
cacheable layout first, then a rendering adapter serializes it for libass'
complex HarfBuzz-compatible shaper and rasterizer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import hashlib, json, math, unicodedata
from pathlib import Path
from fractions import Fraction
import struct
import regex
from PIL import ImageFont, features


class TextEngineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class FontRequest:
    file: str
    family: str
    size: float
    weight: int = 400
    italic: bool = False


@dataclass(frozen=True)
class Paint:
    fill: str = "#ffffffff"
    stroke: str | None = None
    stroke_width: float = 0.0
    shadow: str | None = None
    shadow_x: float = 0.0
    shadow_y: float = 0.0
    shadow_blur: float = 0.0


@dataclass(frozen=True)
class TextRequest:
    content: str
    font: FontRequest
    paint: Paint
    frame_width: int
    frame_height: int
    anchor: str = "top-left"
    align: str = "left"
    language: str = "und"
    direction: str = "auto"
    normalization: str = "preserve"
    typewriter_duration: tuple[int,int] | None = None
    reveal_lines_duration: tuple[int,int] | None = None
    fade_in_duration: tuple[int,int] | None = None
    word_highlights: tuple[tuple[int,int,int,int],...] = ()
    wrap_mode: str = "word"
    overflow: str = "clip"
    max_lines: int | None = None
    line_height: float = 1.2
    timeline_start: tuple[int,int] = (0,1)


@dataclass(frozen=True)
class ShapedGlyph:
    """A renderer-independent glyph placement in design-space pixels.

    ``cluster`` is a half-open Unicode-string offset range.  Keeping this
    boundary explicit lets a future native HarfBuzz adapter substitute or
    position glyphs without changing the AST/TextLayout contract.
    """
    glyph_id: int
    cluster: tuple[int,int]
    advance_x: float
    advance_y: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0


@dataclass(frozen=True)
class ShapedRun:
    text: str
    language: str
    direction: str
    font_file: str
    glyphs: tuple[ShapedGlyph,...]


@dataclass(frozen=True)
class TextLayout:
    normalized_text: str
    lines: tuple[str, ...]
    language: str
    direction: str
    anchor: str
    align: str
    font: FontRequest
    paint: Paint
    frame_width: int
    frame_height: int
    box_width: int
    box_height: int
    anchor_x: float
    anchor_y: float
    typewriter_duration: tuple[int,int] | None
    reveal_lines_duration: tuple[int,int] | None
    fade_in_duration: tuple[int,int] | None
    word_highlights: tuple[tuple[int,int,int,int],...]
    wrap_mode: str
    overflow: str
    max_lines: int | None
    line_height: float
    timeline_start: tuple[int,int]
    shaped_runs: tuple[ShapedRun,...]
    digest: str


@dataclass(frozen=True)
class TextSurface:
    layout: TextLayout
    ass_path: Path
    anchor_x: float
    anchor_y: float
    fonts_dir: Path


def _font(file: str, size: float):
    try:
        if not features.check_feature("raqm"):
            return ImageFont.truetype(file,max(1,round(size)))
        return ImageFont.truetype(
            file,max(1,round(size)),
            layout_engine=ImageFont.Layout.RAQM)
    except (AttributeError,ValueError):
        return ImageFont.truetype(file,max(1,round(size)))


def _measure(font, value: str) -> float:
    return float(font.getlength(value))


def _greedy_wrap(text: str, font, width: int, mode: str) -> list[str]:
    result=[]
    paragraphs=text.splitlines() or [""]
    for paragraph in paragraphs:
        if mode=="none" or not paragraph:
            result.append(paragraph)
            continue
        units=(list(grapheme_clusters(paragraph)) if mode=="grapheme"
               else regex.findall(r"\S+\s*",paragraph))
        line=""
        for unit in units:
            candidate=line+unit
            if line and _measure(font,candidate.rstrip())>width:
                result.append(line.rstrip())
                line=unit.lstrip()
            else:
                line=candidate
        result.append(line.rstrip())
    return result


def _balanced_wrap(text: str, font, width: int) -> list[str]:
    result=[]
    for paragraph in text.splitlines() or [""]:
        words=paragraph.split()
        if not words:
            result.append("")
            continue
        greedy=_greedy_wrap(paragraph,font,width,"word")
        line_count=max(1,len(greedy))
        prefix=[0.0]
        space=_measure(font," ")
        for word in words:
            prefix.append(prefix[-1]+_measure(font,word))
        target=min(float(width),(prefix[-1]+space*(len(words)-1))/line_count)
        # Dynamic programming minimizes squared line-width deviation while
        # preserving source order. Ties choose the earliest break.
        costs=[float("inf")]*(len(words)+1); paths=[[] for _ in costs]
        costs[0]=0.0
        for end in range(1,len(words)+1):
            for start in range(end):
                line_width=prefix[end]-prefix[start]+space*max(0,end-start-1)
                if line_width>width and end-start>1:
                    continue
                penalty=(target-line_width)**2
                candidate=costs[start]+penalty
                if candidate<costs[end]:
                    costs[end]=candidate
                    paths[end]=paths[start]+[start]
        starts=paths[-1]+[len(words)]
        result.extend(" ".join(words[starts[i]:starts[i+1]])
                      for i in range(len(starts)-1))
    return result


def _ellipsis(line: str, font, width: int) -> str:
    marker="…"
    clusters=list(grapheme_clusters(line.rstrip()))
    while clusters and _measure(font,"".join(clusters)+marker)>width:
        clusters.pop()
    return "".join(clusters)+marker


def _arrange(request: TextRequest, size: float) -> tuple[list[str],Any]:
    font=_font(request.font.file,size)
    if request.wrap_mode=="balanced":
        lines=_balanced_wrap(request.content,font,request.frame_width)
    else:
        lines=_greedy_wrap(
            request.content,font,request.frame_width,request.wrap_mode)
    return lines,font


@lru_cache(maxsize=32)
def _ttf_metrics(font_file: str) -> tuple[dict[int,int], tuple[int,...], int]:
    """Read the small SFNT subset required for deterministic fallback runs.

    The reference profile intentionally has no Python HarfBuzz dependency.
    This parser exposes the font's real cmap glyph IDs and hmtx advances; the
    libass surface remains the complex-script renderer.  Unsupported font
    containers fail closed instead of silently inventing glyph identifiers.
    """
    data=Path(font_file).read_bytes()
    sfnt_start=0
    if data[:4]==b"ttcf" and len(data)>=16:
        face_count=struct.unpack_from(">I",data,8)[0]
        if face_count:
            sfnt_start=struct.unpack_from(">I",data,12)[0]
    if (sfnt_start+12>len(data) or data[sfnt_start:sfnt_start+4] not in {
            b"\x00\x01\x00\x00",b"OTTO",b"true",b"typ1"}):
        raise TextEngineError("VDLS-TEXT-007","unsupported font container")
    tables={}
    count=struct.unpack_from(">H",data,sfnt_start+4)[0]
    for index in range(count):
        offset=sfnt_start+12+index*16
        if offset+16>len(data): break
        tag,_,table_offset,length=struct.unpack_from(">4sIII",data,offset)
        if table_offset+length<=len(data):
            tables[tag]=(table_offset,length)
    try:
        cmap_offset,_=tables[b"cmap"]
        head_offset,_=tables[b"head"]
        hhea_offset,_=tables[b"hhea"]
        hmtx_offset,_=tables[b"hmtx"]
    except KeyError as error:
        raise TextEngineError("VDLS-TEXT-007","font lacks required SFNT table") from error
    units=struct.unpack_from(">H",data,head_offset+18)[0]
    metric_count=struct.unpack_from(">H",data,hhea_offset+34)[0]
    advances=tuple(
        struct.unpack_from(">H",data,hmtx_offset+index*4)[0]
        for index in range(metric_count)
        if hmtx_offset+index*4+4<=len(data))
    cmap={}
    encoding_count=struct.unpack_from(">H",data,cmap_offset+2)[0]
    candidates=[]
    for index in range(encoding_count):
        offset=cmap_offset+4+index*8
        if offset+8>len(data): continue
        platform,encoding,sub_offset=struct.unpack_from(">HHI",data,offset)
        absolute=cmap_offset+sub_offset
        if absolute+2<=len(data):
            candidates.append((0 if (platform,encoding)==(3,10) else
                               1 if platform==3 else 2,absolute))
    if not candidates:
        raise TextEngineError("VDLS-TEXT-007","font lacks a Unicode cmap")
    _,cmap_start=min(candidates)
    format_code=struct.unpack_from(">H",data,cmap_start)[0]
    if format_code==12:
        groups=struct.unpack_from(">I",data,cmap_start+12)[0]
        for index in range(groups):
            offset=cmap_start+16+index*12
            if offset+12>len(data): break
            first,last,glyph=struct.unpack_from(">III",data,offset)
            for codepoint in range(first,last+1):
                cmap[codepoint]=glyph+codepoint-first
    elif format_code==4:
        seg_count=struct.unpack_from(">H",data,cmap_start+6)[0]//2
        end_start=cmap_start+14
        start_start=end_start+2*seg_count+2
        delta_start=start_start+2*seg_count
        range_start=delta_start+2*seg_count
        for index in range(seg_count):
            end=struct.unpack_from(">H",data,end_start+2*index)[0]
            start=struct.unpack_from(">H",data,start_start+2*index)[0]
            delta=struct.unpack_from(">h",data,delta_start+2*index)[0]
            range_offset=struct.unpack_from(">H",data,range_start+2*index)[0]
            for codepoint in range(start,end+1):
                if range_offset:
                    glyph_offset=range_start+2*index+range_offset+2*(codepoint-start)
                    glyph=(struct.unpack_from(">H",data,glyph_offset)[0]
                           if glyph_offset+2<=len(data) else 0)
                    if glyph: glyph=(glyph+delta)&0xffff
                else:
                    glyph=(codepoint+delta)&0xffff
                cmap[codepoint]=glyph
    else:
        raise TextEngineError("VDLS-TEXT-007",f"unsupported cmap format {format_code}")
    return cmap,advances,units


def shape_text_runs(text: str, font: FontRequest, language: str,
                    direction: str) -> tuple[ShapedRun,...]:
    """Expose stable shaped-run data before the libass rasterization adapter.

    Runs are split on explicit line breaks.  Each glyph carries its source
    cluster, real cmap glyph ID, and scaled font advance.  RAQM/libass remains
    authoritative for contextual substitutions until an optional native
    HarfBuzz adapter is installed.
    """
    cmap,advances,units=_ttf_metrics(font.file)
    runs=[]; offset=0
    for line in text.split("\n"):
        glyphs=[]
        for index,character in enumerate(line):
            glyph_id=cmap.get(ord(character),0)
            advance=(advances[min(glyph_id,len(advances)-1)] if advances
                     else 0)
            # Combining marks attach to the prior cluster in the fallback.
            cluster_start=offset+index
            if unicodedata.combining(character) and glyphs:
                cluster_start=glyphs[-1].cluster[0]
                advance=0
            glyphs.append(ShapedGlyph(
                glyph_id,(cluster_start,offset+index+1),
                advance*font.size/max(1,units)))
        runs.append(ShapedRun(line,language,direction,font.file,tuple(glyphs)))
        offset+=len(line)+1
    return tuple(runs)


def layout_text(request: TextRequest) -> TextLayout:
    if request.normalization not in {"preserve", "nfc", "nfkc"}:
        raise TextEngineError(
            "VDLS-TEXT-006",
            f"unsupported normalization policy `{request.normalization}`",
        )
    if request.direction not in {"auto", "ltr", "rtl"}:
        raise TextEngineError(
            "VDLS-TEXT-005", f"unsupported text direction `{request.direction}`"
        )
    if not Path(request.font.file).is_file():
        raise TextEngineError(
            "VDLS-TEXT-001", f"font file not found: {request.font.file}"
        )
    if request.wrap_mode not in {"none","word","grapheme","balanced"}:
        raise TextEngineError(
            "VDLS-TEXT-005",f"unsupported wrap mode `{request.wrap_mode}`")
    if request.overflow not in {"visible","clip","ellipsis","shrink"}:
        raise TextEngineError(
            "VDLS-TEXT-005",f"unsupported overflow mode `{request.overflow}`")
    if request.max_lines is not None and request.max_lines<1:
        raise TextEngineError("VDLS-TYPE-009","max-lines must be positive")
    if request.line_height<=0:
        raise TextEngineError("VDLS-TYPE-009","line-height must be positive")
    text=request.content
    if request.normalization!="preserve":
        text=unicodedata.normalize(request.normalization.upper(),text)
    if any(0xD800<=ord(character)<=0xDFFF for character in text):
        raise TextEngineError("VDLS-TEXT-006","text contains an isolated surrogate")
    request=replace(request,content=text)
    effective_size=request.font.size
    lines,font=_arrange(request,effective_size)
    height_capacity=max(
        1,int(request.frame_height/(effective_size*request.line_height)))
    allowed=(min(height_capacity,request.max_lines)
             if request.max_lines is not None else height_capacity)
    if request.overflow=="shrink":
        low=1; high=max(1,round(request.font.size*4)); best=1
        while low<=high:
            middle=(low+high)//2
            candidate_size=middle/4
            candidate_lines,candidate_font=_arrange(request,candidate_size)
            candidate_capacity=max(
                1,int(request.frame_height/
                      (candidate_size*request.line_height)))
            candidate_allowed=(
                min(candidate_capacity,request.max_lines)
                if request.max_lines is not None else candidate_capacity)
            if len(candidate_lines)<=candidate_allowed:
                best=middle; lines,font=candidate_lines,candidate_font
                low=middle+1
            else:
                high=middle-1
        effective_size=best/4
        allowed=max(
            1,int(request.frame_height/
                  (effective_size*request.line_height)))
        if request.max_lines is not None:
            allowed=min(allowed,request.max_lines)
    elif request.overflow=="ellipsis" and len(lines)>allowed:
        lines=lines[:allowed]
        lines[-1]=_ellipsis(lines[-1],font,request.frame_width)
    effective_font=replace(request.font,size=effective_size)
    text="\n".join(lines)
    shaped_runs=shape_text_runs(
        text,effective_font,request.language,request.direction)
    horizontal={
        "top-left":0.0,"left":0.0,"bottom-left":0.0,
        "top":0.5,"center":0.5,"bottom":0.5,
        "top-right":1.0,"right":1.0,"bottom-right":1.0,
    }
    vertical={
        "top-left":0.0,"top":0.0,"top-right":0.0,
        "left":0.5,"center":0.5,"right":0.5,
        "bottom-left":1.0,"bottom":1.0,"bottom-right":1.0,
    }
    if request.anchor not in horizontal:
        raise TextEngineError(
            "VDLS-TYPE-004",f"invalid text anchor `{request.anchor}`")
    h=horizontal[request.anchor]; v=vertical[request.anchor]
    text_width=max((_measure(font,line) for line in lines),default=0.0)
    text_height=max(
        effective_size,
        len(lines)*effective_size*request.line_height)
    if request.overflow=="visible":
        paint_extent=(
            max(0.0,request.paint.stroke_width)+
            max(0.0,request.paint.shadow_blur)*2)
        left=max(request.frame_width*h,text_width*h)+paint_extent+max(
            0.0,-request.paint.shadow_x)
        right=max(
            request.frame_width*(1-h),text_width*(1-h)
        )+paint_extent+max(0.0,request.paint.shadow_x)
        top=max(request.frame_height*v,text_height*v)+paint_extent+max(
            0.0,-request.paint.shadow_y)
        bottom=max(
            request.frame_height*(1-v),text_height*(1-v)
        )+paint_extent+max(0.0,request.paint.shadow_y)
        surface_width=max(1,int(math.ceil(left+right)))
        surface_height=max(1,int(math.ceil(top+bottom)))
        anchor_x=left; anchor_y=top
    else:
        surface_width=request.frame_width
        surface_height=request.frame_height
        anchor_x=request.frame_width*h
        anchor_y=request.frame_height*v
    payload={
        "schema":"org.vdls.text-layout/1",
        "text":text,
        "lines":text.splitlines() or [""],
        "language":request.language,
        "direction":request.direction,
        "anchor":request.anchor,
        "align":request.align,
        "font":asdict(effective_font),
        "paint":asdict(request.paint),
        "frame":[surface_width,surface_height],
        "box":[request.frame_width,request.frame_height],
        "anchorPoint":[anchor_x,anchor_y],
        "typewriterDuration":request.typewriter_duration,
        "revealLinesDuration":request.reveal_lines_duration,
        "fadeInDuration":request.fade_in_duration,
        "wordHighlights":request.word_highlights,
        "wrapMode":request.wrap_mode,
        "overflow":request.overflow,
        "maxLines":request.max_lines,
        "lineHeight":request.line_height,
        "timelineStart":request.timeline_start,
        "shapedRuns":[{
            "text":run.text,
            "language":run.language,
            "direction":run.direction,
            "fontFile":run.font_file,
            "glyphs":[asdict(glyph) for glyph in run.glyphs],
        } for run in shaped_runs],
    }
    digest=hashlib.sha256(json.dumps(
        payload,ensure_ascii=False,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    return TextLayout(
        text,tuple(payload["lines"]),request.language,request.direction,
        request.anchor,request.align,effective_font,request.paint,
        surface_width,surface_height,request.frame_width,request.frame_height,
        anchor_x,anchor_y,request.typewriter_duration,
        request.reveal_lines_duration,request.fade_in_duration,
        request.word_highlights,request.wrap_mode,request.overflow,
        request.max_lines,request.line_height,
        request.timeline_start,shaped_runs,f"sha256:{digest}",
    )


def grapheme_clusters(text: str) -> tuple[str,...]:
    """Unicode extended grapheme clusters (UAX #29 via regex ``\\X``)."""
    return tuple(regex.findall(r"\X",text))


def _ass_color(value: str) -> str:
    raw=value.lstrip("#")
    if len(raw)==6: raw+="ff"
    if len(raw)!=8 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise TextEngineError("VDLS-TYPE-004",f"invalid RGBA color `{value}`")
    red,green,blue,alpha=raw[0:2],raw[2:4],raw[4:6],raw[6:8]
    return f"&H{255-int(alpha,16):02X}{blue}{green}{red}".upper()


def _ass_text(value: str) -> str:
    return (value.replace("\\",r"\\").replace("{",r"\{").replace("}",r"\}")
            .replace("\r\n",r"\N").replace("\r",r"\N").replace("\n",r"\N"))


def _alignment(anchor: str) -> int:
    values={
        "bottom-left":1,"bottom":2,"bottom-right":3,
        "left":4,"center":5,"right":6,
        "top-left":7,"top":8,"top-right":9,
    }
    if anchor not in values:
        raise TextEngineError("VDLS-TYPE-004",f"invalid text anchor `{anchor}`")
    return values[anchor]


def render_ass_surface(layout: TextLayout, cache_dir: Path) -> TextSurface:
    """Serialize one layout for a transparent full-frame libass surface."""
    cache_dir.mkdir(parents=True,exist_ok=True)
    ass_path=cache_dir/f"{layout.digest.removeprefix('sha256:')}.ass"
    alignment=_alignment(layout.anchor)
    x=layout.anchor_x
    y=layout.anchor_y
    paint=layout.paint
    outline=max(0.0,paint.stroke_width)
    shadow=max(abs(paint.shadow_x),abs(paint.shadow_y))
    if paint.shadow_blur<0:
        raise TextEngineError(
            "VDLS-TYPE-009","text shadow blur must be non-negative")
    text=layout.normalized_text
    if layout.direction=="rtl": text="\u2067"+text+"\u2069"
    elif layout.direction=="ltr": text="\u2066"+text+"\u2069"
    fade_tag=""
    if layout.fade_in_duration:
        fade=Fraction(*layout.fade_in_duration)
        if fade<=0:
            raise TextEngineError(
                "VDLS-TYPE-004","text-fade-in duration must be positive")
        fade_tag=f"\\fad({round(fade*1000)},0)"
    # Line breaks are resolved before backend lowering for every wrap mode.
    wrap_tag="\\q2"
    tag=(f"{{\\an{alignment}\\pos({x:g},{y:g}){wrap_tag}"
         f"\\bord{outline:g}\\shad0{fade_tag}}}")
    shadow_tag=(
        f"{{\\an{alignment}"
        f"\\pos({x+paint.shadow_x:g},{y+paint.shadow_y:g}){wrap_tag}"
        f"\\bord0\\shad0\\blur{paint.shadow_blur:g}"
        f"\\1c{_ass_color(paint.shadow or '#00000000')}{fade_tag}}}")
    events=[]
    start=Fraction(*layout.timeline_start)
    reveal_effects=sum(bool(value) for value in (
        layout.typewriter_duration,layout.reveal_lines_duration,
        layout.word_highlights))
    if reveal_effects>1:
        raise TextEngineError(
            "VDLS-TYPE-001",
            "typewriter, reveal-lines, and highlight-words are mutually exclusive")
    if layout.typewriter_duration:
        duration=Fraction(*layout.typewriter_duration)
        clusters=grapheme_clusters(text)
        if len(clusters)>4096:
            raise TextEngineError(
                "VDLS-TEXT-004","typewriter text exceeds 4096 grapheme clusters")
        if duration<=0:
            raise TextEngineError(
                "VDLS-TYPE-004","typewriter duration must be positive")
        for index in range(1,len(clusters)+1):
            event_start=start+duration*index/len(clusters)
            event_end=(start+duration*(index+1)/len(clusters)
                       if index<len(clusters) else Fraction(35999))
            events.append((event_start,event_end,
                           tag+_ass_text("".join(clusters[:index]))))
    elif layout.reveal_lines_duration:
        duration=Fraction(*layout.reveal_lines_duration)
        lines=text.splitlines() or [""]
        if duration<=0:
            raise TextEngineError(
                "VDLS-TYPE-004","reveal-lines duration must be positive")
        for index in range(1,len(lines)+1):
            event_start=start+duration*index/len(lines)
            event_end=(start+duration*(index+1)/len(lines)
                       if index<len(lines) else Fraction(35999))
            events.append((event_start,event_end,
                           tag+_ass_text("\n".join(lines[:index]))))
    elif layout.word_highlights:
        word_matches=list(regex.finditer(r"\w+(?:['’]\w+)*",text))
        if len(word_matches)!=len(layout.word_highlights):
            raise TextEngineError(
                "VDLS-TEXT-004",
                "highlight timing count must equal the number of words")
        cursor=start
        base_text=tag+_ass_text(text)
        primary=_ass_color(paint.fill)
        for match,timing in zip(word_matches,layout.word_highlights):
            highlight_start=start+Fraction(timing[0],timing[1])
            highlight_end=start+Fraction(timing[2],timing[3])
            if highlight_start>cursor:
                events.append((cursor,highlight_start,base_text))
            before=_ass_text(text[:match.start()])
            word=_ass_text(match.group())
            after=_ass_text(text[match.end():])
            highlighted=(
                tag+before+"{\\1c&H00FFFF&}"+word+
                f"{{\\1c{primary}&}}"+after)
            events.append((highlight_start,highlight_end,highlighted))
            cursor=highlight_end
        events.append((cursor,Fraction(35999),base_text))
    else:
        events.append((start,Fraction(35999),tag+_ass_text(text)))
    script=(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {layout.frame_width}\nPlayResY: {layout.frame_height}\n"
        "YCbCr Matrix: None\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: VDLS,{layout.font.family},{layout.font.size:g},"
        f"{_ass_color(paint.fill)},{_ass_color(paint.fill)},"
        f"{_ass_color(paint.stroke or '#00000000')},"
        f"{_ass_color(paint.shadow or '#00000000')},"
        f"{-1 if layout.font.weight>=600 else 0},{-1 if layout.font.italic else 0},"
        f"0,0,100,100,0,0,1,{outline:g},{shadow:g},{alignment},0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    def ass_time(value: Fraction) -> str:
        centiseconds=max(0,round(value*100))
        hours,remainder=divmod(centiseconds,360000)
        minutes,remainder=divmod(remainder,6000)
        seconds,centis=divmod(remainder,100)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"
    if paint.shadow and (shadow or paint.shadow_blur):
        script+="".join(
            f"Dialogue: 0,{ass_time(event_start)},{ass_time(event_end)},"
            f"VDLS,,0,0,0,,{event_text.replace(tag,shadow_tag,1)}\n"
            for event_start,event_end,event_text in events)
    script+="".join(
        f"Dialogue: 1,{ass_time(event_start)},{ass_time(event_end)},"
        f"VDLS,,0,0,0,,{event_text}\n"
        for event_start,event_end,event_text in events)
    if not ass_path.exists() or ass_path.read_text(encoding="utf-8")!=script:
        ass_path.write_text(script,encoding="utf-8",newline="\n")
    font_path=Path(layout.font.file)
    return TextSurface(layout,ass_path,x,y,font_path.parent)


def ffmpeg_ass_filter(surface: TextSurface) -> str:
    def escape(path: Path) -> str:
        value=str(path.resolve()).replace("\\","/")
        return value.replace("'","\\'").replace(":","\\:")
    return (f"ass=filename='{escape(surface.ass_path)}':"
            f"fontsdir='{escape(surface.fonts_dir)}':alpha=1:shaping=complex")
