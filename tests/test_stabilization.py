"""Opt-in stabilization must improve shake without replacing the original."""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest


VIDSTAB_FILTERS = " ... vidstabdetect V->V\n ... vidstabtransform V->V\n"
# FFmpeg 8.1 and later print two flag characters per filter instead of three.
VIDSTAB_FILTERS_TWO_FLAGS = " .. vidstabdetect     V->V       Detect.\n .. vidstabtransform  V->V       Transform.\n"
# Option list of `ffmpeg -h filter=vidstabdetect` on FFmpeg 6.1.1, which has no fileformat option.
VIDSTAB_OPTIONS_FFMPEG_6_1 = """Filter vidstabdetect
  Extract relative transformations, pass 1 of 2 for stabilization (see vidstabtransform for pass 2).
vidstabdetect AVOptions:
   result            <string>     ..FV....... path to the file used to write the transforms (default "transforms.trf")
   shakiness         <int>        ..FV....... how shaky is the video and how quick is the camera? 1: little (fast) 10: very strong/quick (slow) (from 1 to 10) (default 5)
   accuracy          <int>        ..FV....... (>=shakiness) 1: low 15: high (slow) (from 1 to 15) (default 15)
   stepsize          <int>        ..FV....... region around minimum is scanned with 1 pixel resolution (from 1 to 32) (default 6)
   mincontrast       <double>     ..FV....... below this contrast a field is discarded (0-1) (from 0 to 1) (default 0.25)
   show              <int>        ..FV....... 0: draw nothing; 1,2: show fields and transforms (from 0 to 2) (default 0)
   tripod            <int>        ..FV....... virtual tripod mode (if >0): motion is compared to a reference reference frame (frame # is the value) (from 0 to INT_MAX) (default 0)
"""
VIDSTAB_OPTIONS_WITH_FILEFORMAT = VIDSTAB_OPTIONS_FFMPEG_6_1 + (
    "   fileformat        <int>        ..FV....... transforms data file format (from 1 to 2) (default binary)\n")


def _settings():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg runtime is not installed")
    return SimpleNamespace(ffmpeg=ffmpeg, ffprobe=ffprobe, raw={"ffmpeg_threads": 2})


def _command(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=30)


def _make_shaky_video(folder, settings, *, audio_codec="aac", duration=2):
    import numpy as np
    folder.mkdir(parents=True, exist_ok=True)
    pattern = folder / "pattern.ppm"
    # A stationary textured scene with known camera-only translation. No user
    # footage and no moving subjects can disguise stabilization quality here.
    random = np.random.default_rng(817)
    tiles = random.integers(20, 236, size=(70, 90, 3), dtype=np.uint8)
    pixels = np.repeat(np.repeat(tiles, 4, axis=0), 4, axis=1)
    pattern.write_bytes(b"P6\n360 280\n255\n" + pixels.tobytes())
    source = folder / ("shaky source.mkv" if audio_codec != "aac" else "shaky source.mp4")
    _command([
        settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-threads", "2",
        "-loop", "1", "-framerate", "30", "-i", str(pattern),
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(duration), "-vf", "crop=320:240:x='20+8*sin(n*1.7)':y='20+6*cos(n*1.3)',format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-threads", "2",
        "-c:a", audio_codec, str(source),
    ])
    return source


def _streams(path, settings):
    return json.loads(_command([
        settings.ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path),
    ]).stdout)


def _jitter(path, settings):
    import cv2
    import numpy as np
    raw = _command([
        settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-vf", "crop=240:160:40:40,format=gray", "-f", "rawvideo", "pipe:1",
    ]).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 160, 240).astype(np.float32)
    shifts = [cv2.phaseCorrelate(frames[index], frames[index + 1])[0] for index in range(len(frames) - 1)]
    return float(np.mean([x * x + y * y for x, y in shifts]) ** .5)


def _pcm(path, settings):
    return _command([
        settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "48000", "pipe:1",
    ]).stdout


@pytest.mark.parametrize("threads", [1, 2, 4])
def test_real_two_pass_reduces_jitter_preserves_frame_clock_and_copied_audio(tmp_path, monkeypatch, threads):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = _settings()
    if not stabilization.stabilization_capability(settings)["available"]:
        pytest.skip("FFmpeg lacks the optional libvidstab filters")
    settings.raw["ffmpeg_threads"] = threads
    run = stabilization._run
    motion_headers = []
    def record_motion_data(args, **kwargs):
        result = run(args, **kwargs)
        if "-vf" in args and "vidstabdetect" in args[args.index("-vf") + 1]:
            motion_headers.append((kwargs["cwd"] / "transforms.trf").read_bytes()[:10])
        return result
    monkeypatch.setattr(stabilization, "_run", record_motion_data)
    folder = tmp_path / "creator footage \u05d1\u05d3\u05d9\u05e7\u05d4"
    source = _make_shaky_video(folder, settings)
    target = folder / "stabilized copy \u05e2\u05d5\u05ea\u05e7.mp4"
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    messages = []
    assert stabilization.stabilize_video(source, target, settings, lambda _, message: messages.append(message)) == target.resolve()
    # Binary motion data gave nondeterministic corrections on Windows FFmpeg7.1.1.
    # Verify the actual inter-pass format, while retaining the same quality gate.
    assert motion_headers == [b"VID.STAB 1"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    before, after = _streams(source, settings), _streams(target, settings)
    video_before = next(row for row in before["streams"] if row["codec_type"] == "video")
    video_after = next(row for row in after["streams"] if row["codec_type"] == "video")
    assert video_after["nb_read_frames"] == video_before["nb_read_frames"] == "60"
    assert video_after["avg_frame_rate"] == video_before["avg_frame_rate"]
    assert (video_after["width"], video_after["height"]) == (320, 240)
    assert abs(float(video_after["duration"]) - float(video_before["duration"])) < .001
    assert abs(float(after["format"]["duration"]) - float(before["format"]["duration"])) < .034
    assert _pcm(source, settings) == _pcm(target, settings)
    before_jitter, after_jitter = _jitter(source, settings), _jitter(target, settings)
    assert before_jitter > 4
    assert after_jitter < before_jitter * .5, (before_jitter, after_jitter)
    assert any("keeping audio unchanged" in message for message in messages)
    assert not list(folder.glob("*.partial.mp4"))


def test_real_incompatible_audio_is_explicitly_encoded_as_aac(tmp_path):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = _settings()
    if not stabilization.stabilization_capability(settings)["available"]:
        pytest.skip("FFmpeg lacks the optional libvidstab filters")
    source = _make_shaky_video(tmp_path, settings, audio_codec="pcm_s16le", duration=1)
    target = tmp_path / "aac copy.mp4"
    messages = []
    stabilization.stabilize_video(source, target, settings, lambda _, message: messages.append(message))
    streams = _streams(target, settings)["streams"]
    assert next(row for row in streams if row["codec_type"] == "audio")["codec_name"] == "aac"
    assert len(_pcm(target, settings)) / (48000 * 2) == pytest.approx(1, abs=.03)
    assert any("converting audio to AAC" in message for message in messages)


@pytest.fixture
def fake_backend(tmp_path, monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original bytes")
    target = tmp_path / "copy.mp4"
    settings = SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", raw={"ffmpeg_threads": 200})
    metadata = {"duration": 2., "video_duration": 2., "fps": 30., "width": 320, "height": 240,
                "has_audio": True, "audio_codec": "aac"}
    monkeypatch.setattr(stabilization, "probe_media", lambda *_, **__: dict(metadata))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "-filters" in args:
            return subprocess.CompletedProcess(args, 0, " ... vidstabdetect V->V\n ... vidstabtransform V->V\n", "")
        if "-h" in args:
            return subprocess.CompletedProcess(args, 0, VIDSTAB_OPTIONS_WITH_FILEFORMAT, "")
        if "vidstabdetect" in args[args.index("-vf") + 1]:
            (kwargs["cwd"] / "transforms.trf").write_text("motion")
        else:
            Path(args[-1]).write_bytes(b"complete stabilized video")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(stabilization, "_run", run)
    return stabilization, source, target, settings, calls


def test_cancelled_transform_does_not_commit_or_leave_partial_files(fake_backend, monkeypatch):
    stabilization, source, target, settings, calls = fake_backend
    from cutroom.jobs import JobCancelled
    original = stabilization._run
    def cancel(args, **kwargs):
        result = original(args, **kwargs)
        if "-vf" in args and "vidstabtransform" in args[args.index("-vf") + 1]:
            raise JobCancelled("Job cancelled")
        return result
    monkeypatch.setattr(stabilization, "_run", cancel)
    with pytest.raises(JobCancelled):
        stabilization.stabilize_video(source, target, settings)
    assert source.read_bytes() == b"original bytes" and not target.exists()
    assert not list(target.parent.glob("*.partial.mp4"))
    assert all(not call[1]["cwd"].exists() for call in calls if call[1].get("cwd"))


def test_missing_filters_fail_before_processing_and_keep_original(fake_backend, monkeypatch):
    stabilization, source, target, settings, _ = fake_backend
    monkeypatch.setattr(stabilization, "_run", lambda args, **_: subprocess.CompletedProcess(args, 0, " ... deshake V->V\n", ""))
    with pytest.raises(stabilization.StabilizationError, match="vidstab"):
        stabilization.stabilize_video(source, target, settings)
    assert source.read_bytes() == b"original bytes" and not target.exists()


def _fake_ffmpeg(monkeypatch, stabilization, *, filters=VIDSTAB_FILTERS, options=("", ""), help_error=None):
    """Answer the capability probe's two FFmpeg calls; any other call is a test failure."""
    calls = []
    def run(args, **_kwargs):
        calls.append(args)
        if "-filters" in args:
            return subprocess.CompletedProcess(args, 0, filters, "")
        if "-h" in args and "filter=vidstabdetect" in args:
            if help_error:
                raise help_error
            return subprocess.CompletedProcess(args, 0, *options)
        raise AssertionError(f"unexpected FFmpeg call: {args}")
    monkeypatch.setattr(stabilization, "_run", run)
    return calls


def test_capability_requires_the_fileformat_option_not_just_the_filters(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = SimpleNamespace(ffmpeg="ffmpeg")
    calls = _fake_ffmpeg(monkeypatch, stabilization, options=(VIDSTAB_OPTIONS_FFMPEG_6_1, ""))
    result = stabilization.stabilization_capability(settings)
    assert result["available"] is False and result["engine"] == "vidstab"
    assert "fileformat" in result["message"] and "newer FFmpeg" in result["message"]
    assert [call[-1] for call in calls] == ["-filters", "filter=vidstabdetect"]


@pytest.mark.parametrize("stdout,stderr", [
    (VIDSTAB_OPTIONS_WITH_FILEFORMAT, ""),
    ("", VIDSTAB_OPTIONS_WITH_FILEFORMAT),
])
def test_capability_accepts_filter_help_from_either_output_stream(monkeypatch, stdout, stderr):
    stabilization = importlib.import_module("cutroom.stabilization")
    _fake_ffmpeg(monkeypatch, stabilization, options=(stdout, stderr))
    result = stabilization.stabilization_capability(SimpleNamespace(ffmpeg="ffmpeg"))
    assert result == {"available": True, "engine": "vidstab", "message": "Local two-pass stabilization is available."}


def test_capability_ignores_other_options_that_merely_mention_fileformat(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    help_text = VIDSTAB_OPTIONS_FFMPEG_6_1 + "   result  <string> ..FV....... set the fileformat of the transforms file\n"
    _fake_ffmpeg(monkeypatch, stabilization, options=(help_text, ""))
    assert stabilization.stabilization_capability(SimpleNamespace(ffmpeg="ffmpeg"))["available"] is False


@pytest.mark.parametrize("filters", [VIDSTAB_FILTERS, VIDSTAB_FILTERS_TWO_FLAGS], ids=["three-flags", "two-flags"])
def test_capability_reads_the_filter_list_of_ffmpeg_with_two_or_three_flag_characters(monkeypatch, filters):
    stabilization = importlib.import_module("cutroom.stabilization")
    _fake_ffmpeg(monkeypatch, stabilization, filters=filters, options=(VIDSTAB_OPTIONS_WITH_FILEFORMAT, ""))
    assert stabilization.stabilization_capability(SimpleNamespace(ffmpeg="ffmpeg"))["available"] is True


def test_capability_without_the_filters_does_not_ask_for_filter_help(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    calls = _fake_ffmpeg(monkeypatch, stabilization, filters=" ... deshake V->V\n ... vidstabdetect V->V\n")
    result = stabilization.stabilization_capability(SimpleNamespace(ffmpeg="ffmpeg"))
    assert result["available"] is False and "vidstabdetect and vidstabtransform" in result["message"]
    assert len(calls) == 1


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, "ffmpeg"),
    subprocess.TimeoutExpired("ffmpeg", 15),
    FileNotFoundError("ffmpeg"),
])
def test_capability_reports_an_uninspectable_ffmpeg_when_filter_help_fails(monkeypatch, error):
    stabilization = importlib.import_module("cutroom.stabilization")
    _fake_ffmpeg(monkeypatch, stabilization, help_error=error)
    result = stabilization.stabilization_capability(SimpleNamespace(ffmpeg="ffmpeg"))
    assert result == {"available": False, "engine": "vidstab", "message": "CUTROOM could not inspect the installed FFmpeg."}


def test_ffmpeg_without_fileformat_fails_before_any_pass_and_keeps_the_original(fake_backend, monkeypatch):
    stabilization, source, target, settings, calls = fake_backend
    _fake_ffmpeg(monkeypatch, stabilization, options=(VIDSTAB_OPTIONS_FFMPEG_6_1, ""))
    with pytest.raises(stabilization.StabilizationError, match="fileformat"):
        stabilization.stabilize_video(source, target, settings)
    assert source.read_bytes() == b"original bytes" and not target.exists()
    assert not list(target.parent.glob("*.partial.mp4")) and not calls


def test_failed_pass_is_sanitized_and_leaves_no_output(fake_backend, monkeypatch):
    stabilization, source, target, settings, _ = fake_backend
    original = stabilization._run
    def fail(args, **kwargs):
        if "-vf" in args:
            raise subprocess.CalledProcessError(1, args, stderr="private creator file name")
        return original(args, **kwargs)
    monkeypatch.setattr(stabilization, "_run", fail)
    with pytest.raises(stabilization.StabilizationError) as error:
        stabilization.stabilize_video(source, target, settings)
    assert "private creator" not in str(error.value)
    assert not target.exists() and not list(target.parent.glob("*.partial.mp4"))
    assert source.read_bytes() == b"original bytes"


@pytest.mark.parametrize("options", [{"smoothing": True}, {"smoothing": -1}, {"smoothing": 61}, {"smoothing": 1.5}, {"shakiness": 0}, {"shakiness": 11}, {"shakiness": "5"}])
def test_invalid_parameters_fail_before_commands(fake_backend, options):
    stabilization, source, target, settings, calls = fake_backend
    with pytest.raises(stabilization.StabilizationError, match="integer"):
        stabilization.stabilize_video(source, target, settings, **options)
    assert not calls and not target.exists()


def test_output_never_overwrites_existing_file_or_original(fake_backend):
    stabilization, source, target, settings, calls = fake_backend
    target.write_bytes(b"previous good copy")
    with pytest.raises(stabilization.StabilizationError, match="existing"):
        stabilization.stabilize_video(source, target, settings)
    with pytest.raises(stabilization.StabilizationError, match="original"):
        stabilization.stabilize_video(source, source, settings)
    assert source.read_bytes() == b"original bytes" and target.read_bytes() == b"previous good copy"
    assert not calls


def test_copy_is_verified_then_published_with_bounded_cpu_and_local_protocols(fake_backend):
    stabilization, source, target, settings, calls = fake_backend
    stabilization.stabilize_video(source, target, settings)
    assert target.read_bytes() == b"complete stabilized video"
    # The capability probe asks for the filter list and the vidstabdetect options; the rest are passes.
    assert [args[-1] for args, _ in calls[:2]] == ["-filters", "filter=vidstabdetect"]
    for args, kwargs in calls[2:]:
        assert args[args.index("-threads") + 1] == "4"
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert kwargs["timeout"] <= 4 * 3600 and kwargs["threads"] == 4
    assert calls[-1][0][calls[-1][0].index("-fps_mode") + 1] == "passthrough"


def test_cancellation_after_atomic_publication_removes_only_our_new_copy(fake_backend):
    stabilization, source, target, settings, _ = fake_backend
    from cutroom.jobs import JobCancelled
    def progress(fraction, _):
        if fraction == 1:
            assert target.read_bytes() == b"complete stabilized video"
            raise JobCancelled("Job cancelled")
    with pytest.raises(JobCancelled):
        stabilization.stabilize_video(source, target, settings, progress)
    assert not target.exists() and source.read_bytes() == b"original bytes"


def test_new_file_claimed_during_processing_is_never_overwritten(fake_backend, monkeypatch):
    stabilization, source, target, settings, _ = fake_backend
    link = stabilization.os.link
    def claim_then_link(partial, destination):
        target.write_bytes(b"another completed copy")
        link(partial, destination)
    monkeypatch.setattr(stabilization.os, "link", claim_then_link)
    with pytest.raises(stabilization.StabilizationError, match="existing"):
        stabilization.stabilize_video(source, target, settings)
    assert target.read_bytes() == b"another completed copy" and source.read_bytes() == b"original bytes"


@pytest.mark.parametrize("value,expected", [
    (r"C:\creator footage\copy.mp4", r"\\?\C:\creator footage\copy.mp4"),
    ("C:\\creator \u05d1\u05d3\u05d9\u05e7\u05d4\\copy.mp4", "\\\\?\\C:\\creator \u05d1\u05d3\u05d9\u05e7\u05d4\\copy.mp4"),
    (r"\\server\share\copy.mp4", r"\\?\UNC\server\share\copy.mp4"),
    (r"\\?\C:\creator\copy.mp4", r"\\?\C:\creator\copy.mp4"),
    (r"\\?\UNC\server\share\copy.mp4", r"\\?\UNC\server\share\copy.mp4"),
])
def test_windows_native_filesystem_path_forms_are_explicit(monkeypatch, value, expected):
    stabilization = importlib.import_module("cutroom.stabilization")
    # Pure paths test serialization on every platform without changing global
    # os.name or invoking a network share/device.
    monkeypatch.setattr(stabilization, "os", SimpleNamespace(name="nt"))
    assert stabilization._filesystem_path(PureWindowsPath(value)) == expected


@pytest.mark.parametrize("value", [r"creator\copy.mp4", r"C:copy.mp4", r"\\.\device\copy.mp4"])
def test_windows_native_filesystem_path_rejects_relative_and_device_paths(monkeypatch, value):
    stabilization = importlib.import_module("cutroom.stabilization")
    monkeypatch.setattr(stabilization, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="absolute file paths"):
        stabilization._filesystem_path(PureWindowsPath(value))


def test_non_windows_filesystem_paths_remain_unchanged(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    monkeypatch.setattr(stabilization, "os", SimpleNamespace(name="posix"))
    for value in (Path("relative.mp4"), Path("/creator footage/copy.mp4")):
        assert stabilization._filesystem_path(value) == str(value)




def test_invalid_output_clock_is_rejected_before_publication(fake_backend, monkeypatch):
    stabilization, source, target, settings, _ = fake_backend
    probe = stabilization.probe_media
    def wrong_clock(path, *_args, **_kwargs):
        metadata = probe(path, settings)
        if path != source:
            metadata["video_duration"] = 5
        return metadata
    monkeypatch.setattr(stabilization, "probe_media", wrong_clock)
    with pytest.raises(stabilization.StabilizationError, match="clock"):
        stabilization.stabilize_video(source, target, settings)
    assert not target.exists() and not list(target.parent.glob("*.partial.mp4"))


def test_local_probe_restricts_protocols_and_obeys_cancellation(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = SimpleNamespace(ffprobe="ffprobe")
    captured = []
    cancel = lambda: None
    def inspect(args, **options):
        captured.append((args, options))
        return subprocess.CompletedProcess(args, 0, json.dumps({"streams": [
            {"codec_type": "video", "duration": "1", "width": 320, "height": 240, "avg_frame_rate": "30/1"},
        ]}), "")
    monkeypatch.setattr(stabilization, "_run", inspect)
    assert stabilization.probe_media(Path("local.mp4"), settings, cancel_check=cancel)["duration"] == 1
    args, options = captured[0]
    assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
    assert options["cancel_check"] is cancel and options["timeout"] == 45


def test_actual_cancellable_child_is_terminated_and_both_pipes_closed(monkeypatch):
    stabilization = importlib.import_module("cutroom.stabilization")
    from cutroom.jobs import JobCancelled
    processes = []
    popen = stabilization.subprocess.Popen
    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(stabilization.subprocess, "Popen", record)
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise JobCancelled("Job cancelled")
    start = time.monotonic()
    with pytest.raises(JobCancelled):
        stabilization._run([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                           timeout=3, cancel_check=cancel)
    assert time.monotonic() - start < 3
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_real_silent_source_produces_silent_copy(tmp_path):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = _settings()
    if not stabilization.stabilization_capability(settings)["available"]:
        pytest.skip("FFmpeg lacks the optional libvidstab filters")
    original = _make_shaky_video(tmp_path, settings, duration=1)
    silent = tmp_path / "silent.mp4"
    _command([settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(original),
              "-an", "-c:v", "copy", str(silent)])
    target = tmp_path / "silent stabilized.mp4"
    stabilization.stabilize_video(silent, target, settings)
    streams = _streams(target, settings)["streams"]
    assert all(row["codec_type"] != "audio" for row in streams)
    assert next(row for row in streams if row["codec_type"] == "video")["nb_read_frames"] == "30"


def test_real_variable_frame_timestamps_and_source_audio_offset_are_preserved(tmp_path):
    stabilization = importlib.import_module("cutroom.stabilization")
    settings = _settings()
    if not stabilization.stabilization_capability(settings)["available"]:
        pytest.skip("FFmpeg lacks the optional libvidstab filters")
    original = _make_shaky_video(tmp_path, settings, duration=1)
    source = tmp_path / "vfr offset source.mp4"
    _command([settings.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(original),
              "-vf", "setpts=PTS+0.1/TB+0.04*floor(N/5)/TB", "-c:v", "libx264", "-threads", "2",
              "-fps_mode", "passthrough", "-enc_time_base:v", "filter", "-c:a", "copy", str(source)])
    target = tmp_path / "vfr offset stabilized.mp4"
    stabilization.stabilize_video(source, target, settings)
    def frame_times(path):
        payload = json.loads(_command([
            settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
            "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path),
        ]).stdout)
        return [float(frame["best_effort_timestamp_time"]) for frame in payload["frames"]]
    before, after = frame_times(source), frame_times(target)
    assert len(before) == len(after) == 30
    assert before[0] == pytest.approx(.1, abs=.001)
    assert len({round(before[index + 1] - before[index], 4) for index in range(len(before) - 1)}) > 1
    assert after == pytest.approx(before, abs=.0001)
    assert _pcm(source, settings) == _pcm(target, settings)
