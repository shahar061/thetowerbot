"""Just enough H.264 Annex-B parsing to name the codec and spot an SPS.

The live stream never decodes video on the host; the browser does. The host
only needs the SPS (to tell the browser which decoder to build) and to know
whether a keyframe already carries SPS/PPS.
"""

from __future__ import annotations

NAL_SPS = 7
_START = b"\x00\x00\x01"


def nal_units(annexb: bytes) -> list[bytes]:
    """Split Annex-B on 3- or 4-byte start codes; each unit starts with its header byte."""
    units: list[bytes] = []
    index = annexb.find(_START)
    while index != -1:
        begin = index + len(_START)
        following = annexb.find(_START, begin)
        unit = annexb[begin:] if following == -1 else annexb[begin:following]
        if following != -1 and unit.endswith(b"\x00"):
            # The first zero of a 4-byte start code. A NAL unit never ends in a
            # zero byte (rbsp trailing bits), so this cannot eat real data.
            unit = unit[:-1]
        if unit:
            units.append(unit)
        index = following
    return units


def nal_type(unit: bytes) -> int:
    return unit[0] & 0x1F


def find_sps(annexb: bytes) -> bytes | None:
    return next((unit for unit in nal_units(annexb) if nal_type(unit) == NAL_SPS), None)


def codec_string(sps: bytes) -> str:
    """The RFC 6381 name WebCodecs wants: avc1.PPCCLL (profile, constraint flags, level)."""
    if len(sps) < 4 or nal_type(sps) != NAL_SPS:
        raise ValueError("not an SPS NAL unit")
    return f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


def starts_with_sps(annexb: bytes) -> bool:
    units = nal_units(annexb)
    return bool(units) and nal_type(units[0]) == NAL_SPS
