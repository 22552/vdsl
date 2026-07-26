"""Normalized image metadata readers used by inspect and provenance."""
from __future__ import annotations

from fractions import Fraction
import hashlib, json
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES={".jpg",".jpeg",".tif",".tiff",".webp",".png",".heic",".avif"}


def _json_value(value: Any) -> Any:
    if isinstance(value,bytes):
        try: return value.decode("utf-8")
        except UnicodeDecodeError: return {"encoding":"hex","value":value.hex()}
    if isinstance(value,(str,int,float,bool)) or value is None: return value
    if isinstance(value,Fraction):
        return {"num":value.numerator,"den":value.denominator}
    if hasattr(value,"numerator") and hasattr(value,"denominator"):
        denominator=int(value.denominator)
        return {"num":int(value.numerator),"den":denominator}
    if isinstance(value,(tuple,list)):
        return [_json_value(item) for item in value]
    if isinstance(value,dict):
        return {str(key):_json_value(item) for key,item in value.items()}
    return str(value)


def read_exif(path: Path) -> dict[str,Any] | None:
    if path.suffix.lower() not in IMAGE_SUFFIXES: return None
    try:
        from PIL import ExifTags, Image
    except ImportError as error:
        raise RuntimeError(
            "Pillow is required to inspect EXIF image metadata") from error
    with Image.open(path) as image:
        raw=image.getexif()
        if not raw: return {
            "schema":"vdls.exif/1","tags":{},"gps":None,
            "digest":"sha256:"+hashlib.sha256(b"{}").hexdigest(),
        }
        tags={}
        for tag_id,value in raw.items():
            name=ExifTags.TAGS.get(tag_id,f"Tag-{tag_id}")
            if name=="GPSInfo": continue
            tags[str(name)]=_json_value(value)
        gps=None
        try:
            gps_ifd=raw.get_ifd(ExifTags.IFD.GPSInfo)
        except (AttributeError,KeyError,TypeError):
            gps_ifd={}
        if gps_ifd:
            gps={str(ExifTags.GPSTAGS.get(tag_id,f"GPS-{tag_id}")):
                 _json_value(value) for tag_id,value in gps_ifd.items()}
        canonical=json.dumps(
            {"tags":tags,"gps":gps},ensure_ascii=False,sort_keys=True,
            separators=(",",":")).encode("utf-8")
        return {
            "schema":"vdls.exif/1","tags":tags,"gps":gps,
            "digest":"sha256:"+hashlib.sha256(canonical).hexdigest(),
        }


def exif_manifest_summary(exif: dict[str,Any] | None) -> dict[str,Any] | None:
    if exif is None: return None
    tags=exif["tags"]
    selected={
        key:tags[key] for key in
        ("Orientation","DateTimeOriginal","DateTimeDigitized","Make","Model",
         "Software","ColorSpace")
        if key in tags
    }
    return {
        "schema":"vdls.exif-summary/1",
        "digest":exif["digest"],
        "tagCount":len(tags)+(len(exif.get("gps") or {})),
        "hasGps":bool(exif.get("gps")),
        "selected":selected,
    }
