"""H.264 helpers for the live stream, checked against a real BlueStacks Air config packet."""

from __future__ import annotations

import pytest

from stream.h264 import codec_string, find_sps, nal_type, nal_units, starts_with_sps

# SPS + PPS exactly as scrcpy 4.1 sent them from a BlueStacks Air instance
# (576x1280, Baseline profile, level 4.1).
CONFIG = bytes.fromhex(
    "000000016742c0298d680900a1a420202020f08846a00000000168ce01a835c8"
)
IDR = b"\x00\x00\x00\x01\x65\x88\x84\x00"


def test_nal_units_split_on_four_byte_start_codes() -> None:
    assert [nal_type(unit) for unit in nal_units(CONFIG)] == [7, 8]


def test_nal_units_split_on_three_byte_start_codes() -> None:
    stream = b"\x00\x00\x01\x67\x42\xc0\x29\x00\x00\x01\x68\xce"
    assert nal_units(stream) == [b"\x67\x42\xc0\x29", b"\x68\xce"]


def test_codec_string_reads_profile_constraints_and_level() -> None:
    sps = find_sps(CONFIG)
    assert sps is not None
    assert codec_string(sps) == "avc1.42C029"


def test_codec_string_rejects_a_non_sps_unit() -> None:
    with pytest.raises(ValueError):
        codec_string(nal_units(CONFIG)[1])


def test_find_sps_is_none_without_one() -> None:
    assert find_sps(IDR) is None


def test_starts_with_sps() -> None:
    assert starts_with_sps(CONFIG + IDR)
    assert not starts_with_sps(IDR)
    assert not starts_with_sps(b"")
