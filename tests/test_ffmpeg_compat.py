from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from cutroom import ffmpeg_compat as compat


@pytest.fixture(autouse=True)
def empty_capability_cache():
    compat._probe.cache_clear()
    yield
    compat._probe.cache_clear()


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "FFmpeg tools" / "ffmpeg.exe"
    path.parent.mkdir()
    path.write_bytes(b"test executable")
    return path


def fake_probe(monkeypatch, help_text):
    commands = []

    def capture(command):
        commands.append(command)
        if command[-2:] == ["-h", "full"]:
            return help_text
        option = "-filter_complex_script" if "-filter_complex_script" in command else "-/filter_complex"
        graph = Path(command[command.index(option) + 1])
        assert graph.read_text(encoding="utf-8") == "color=c=black:s=2x2:r=1:d=1[probe]\n"
        assert command[-3:] == ["-f", "null", "-"]
        assert command[command.index("-frames:v") + 1] == "1"
        return "frame=1\nout_time_us=1000000\nprogress=end\n"

    monkeypatch.setattr(compat, "_capture", capture)
    return commands


@pytest.mark.parametrize(("help_text", "graph", "cfr"), [
    ("-filter_complex_script filename\n-vsync method\n", "-filter_complex_script", ("-vsync", "1")),
    ("-filter_complex_script filename\n-fps_mode[:stream_specifier] mode\n-vsync method\n",
     "-filter_complex_script", ("-fps_mode:v", "cfr")),
    ("-filter_complex graph\n-fps_mode mode\n", "-/filter_complex", ("-fps_mode:v", "cfr")),
    ("-filter_complex graph\n-vsync method\n", "-/filter_complex", ("-vsync", "1")),
])
def test_capabilities_preserve_legacy_graph_loading_and_choose_supported_cfr(monkeypatch, executable, help_text, graph, cfr):
    commands = fake_probe(monkeypatch, help_text)
    result = compat.export_capabilities(str(executable))
    assert result.executable == str(executable.resolve())
    assert result.graph_file_option == graph and result.cfr_args == cfr
    assert cfr[0] in commands[1] and commands[1][commands[1].index(cfr[0]) + 1] == cfr[1]
    path = executable.parent / "עברית space;quote' filter.txt"
    assert result.graph_file_args(path) == [graph, str(path)]
    assert len(commands) == 2
    assert not Path(commands[1][commands[1].index(graph) + 1]).exists()


def test_filter_complex_advertisement_is_not_proof_of_slash_file_support(monkeypatch, executable):
    commands = []

    def capture(command):
        commands.append(command)
        if command[-2:] == ["-h", "full"]:
            return "-filter_complex graph\n-fps_mode mode\n"
        assert "-/filter_complex" in command and "-filter_complex" not in command
        raise compat.FFmpegCompatibilityError("Unrecognized option '/filter_complex'")

    monkeypatch.setattr(compat, "_capture", capture)
    with pytest.raises(compat.FFmpegCompatibilityError, match="Unrecognized option"):
        compat.export_capabilities(str(executable))
    assert len(commands) == 2
    assert compat._probe.cache_info().currsize == 0


@pytest.mark.parametrize(("help_text", "message"), [
    ("-fps_mode mode\n", "complex-filter"),
    ("-filter_complex_script file\n", "neither -fps_mode nor -vsync"),
    ("Example: -filter_complex_script file; -vsync 1\n", "complex-filter"),
    ("-filter_complex_script_extra file\n-vsync method\n", "complex-filter"),
])
def test_missing_options_fail_without_attempting_an_export(monkeypatch, executable, help_text, message):
    commands = fake_probe(monkeypatch, help_text)
    with pytest.raises(compat.FFmpegCompatibilityError, match=message):
        compat.export_capabilities(str(executable))
    assert len(commands) == 1


@pytest.mark.parametrize("output", ["", "frame=0\nprogress=end\n", "frame=1\nprogress=continue\n"])
def test_zero_exit_without_completed_probe_frame_is_not_accepted(monkeypatch, executable, output):
    monkeypatch.setattr(compat, "_capture", lambda command: "-filter_complex graph\n-fps_mode mode\n"
                        if command[-2:] == ["-h", "full"] else output)
    with pytest.raises(compat.FFmpegCompatibilityError, match="one-frame"):
        compat.export_capabilities(str(executable))


def test_capabilities_are_cached_until_same_path_binary_changes(monkeypatch, executable):
    commands = fake_probe(monkeypatch, "-filter_complex_script file\n-vsync method\n")
    first = compat.export_capabilities(str(executable))
    assert compat.export_capabilities(str(executable)) is first and len(commands) == 2
    old = executable.stat()
    os.utime(executable, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
    assert compat.export_capabilities(str(executable)) is not first and len(commands) == 4
    replacement = executable.with_name("replacement.exe")
    replacement.write_bytes(executable.read_bytes())
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, executable)
    compat.export_capabilities(str(executable))
    assert len(commands) == 6


def test_path_lookup_change_invalidates_cache(monkeypatch, executable, tmp_path):
    other = tmp_path / "other ffmpeg.exe"
    other.write_bytes(b"different binary")
    selected = [executable]
    monkeypatch.setattr(compat.shutil, "which", lambda _name: str(selected[0]))
    commands = fake_probe(monkeypatch, "-filter_complex_script file\n-vsync method\n")
    assert compat.export_capabilities("ffmpeg").executable == str(executable.resolve())
    selected[0] = other
    assert compat.export_capabilities("ffmpeg").executable == str(other.resolve())
    assert len(commands) == 4


def test_binary_replacement_during_probe_is_rejected(monkeypatch, executable):
    def capture(command):
        if command[-2:] == ["-h", "full"]:
            return "-filter_complex_script file\n-vsync method\n"
        executable.write_bytes(b"replacement binary with different identity")
        return "frame=1\nprogress=end\n"

    monkeypatch.setattr(compat, "_capture", capture)
    with pytest.raises(compat.FFmpegCompatibilityError, match="changed during"):
        compat.export_capabilities(str(executable))


@pytest.mark.parametrize("error", [PermissionError("denied"), subprocess.TimeoutExpired("ffmpeg", 8)])
def test_probe_failure_is_bounded_actionable_and_uses_argument_list(monkeypatch, error):
    def run(command, **kwargs):
        assert command == ["ffmpeg", "-h", "full"] and isinstance(command, list)
        assert kwargs["timeout"] == 8 and kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs
        raise error

    monkeypatch.setattr(compat.subprocess, "run", run)
    with pytest.raises(compat.FFmpegCompatibilityError, match="8s.*Check the configured executable"):
        compat._capture(["ffmpeg", "-h", "full"])


def test_diagnostic_is_bounded_and_strips_terminal_controls(monkeypatch):
    monkeypatch.setattr(compat.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout="X" * 5000 + "\x1b[31mUnsupported option"))
    with pytest.raises(compat.FFmpegCompatibilityError) as caught:
        compat._capture(["ffmpeg"])
    assert "Unsupported option" in str(caught.value) and "\x1b" not in str(caught.value)
    assert len(str(caught.value)) < 1500


def test_missing_executable_fails_without_launching_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(compat.shutil, "which", lambda _name: None)
    monkeypatch.setattr(compat, "_capture", lambda _command: pytest.fail("Must not launch an absent binary"))
    with pytest.raises(compat.FFmpegCompatibilityError, match="accessible FFmpeg"):
        compat.export_capabilities(str(tmp_path / "missing.exe"))


# Real `ffmpeg -filters` excerpts. FFmpeg 6.1 prints three flag characters per filter;
# the FFmpeg 8.1 and 9.0 builds print two and no longer list the command-support flag.
FILTERS_FFMPEG_6_1 = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 ... abench            A->A       Benchmark part of a filtergraph.
 ..C acompressor       A->A       Audio compressor.
 TSC chromakey         V->V       Turns a certain color into transparency. Operates on YUV colors.
 ... chromakey_cuda    V->V       GPU accelerated chromakey filter
 T.C cropdetect        V->V       Auto-detect crop size.
 ... vidstabdetect     V->V       Extract relative transformations, pass 1 of 2 for stabilization (see vidstabtransform for pass 2).
 ... vidstabtransform  V->V       Transform the frames, pass 2 of 2 for stabilization (see vidstabdetect for pass 1).
"""
FILTERS_FFMPEG_9_0 = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
  ------
 TS aap               AA->A      Apply Affine Projection algorithm to first audio stream.
 .. abench            A->A       Benchmark part of a filtergraph.
 .S acrossover        A->N       Split audio into per-bands streams.
 TS chromakey         V->V       Turns a certain color into transparency. Operates on YUV colors.
 .. chromakey_cuda    V->V       GPU accelerated chromakey filter
 T. cropdetect        V->V       Auto-detect crop size.
 .. vidstabdetect     V->V       Extract relative transformations, pass 1 of 2 for stabilization (see vidstabtransform for pass 2).
 .. vidstabtransform  V->V       Transform the frames, pass 2 of 2 for stabilization (see vidstabdetect for pass 1).
"""


@pytest.mark.parametrize("listing,expected", [
    (FILTERS_FFMPEG_6_1, {"abench", "acompressor", "chromakey", "chromakey_cuda", "cropdetect", "vidstabdetect", "vidstabtransform"}),
    (FILTERS_FFMPEG_9_0, {"aap", "abench", "acrossover", "chromakey", "chromakey_cuda", "cropdetect", "vidstabdetect", "vidstabtransform"}),
], ids=["ffmpeg-6.1", "ffmpeg-9.0"])
def test_listed_filters_reads_both_flag_column_widths(listing, expected):
    assert compat.listed_filters(listing) == expected


def test_listed_filters_ignores_the_legend_and_unrelated_text():
    legend = "Filters:\n  T.. = Timeline support\n  .S. = Slice threading\n  | = Source or sink filter\n  ------\n"
    assert compat.listed_filters(legend) == set()
    assert compat.listed_filters("") == set()
