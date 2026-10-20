"""Launch the CUTROOM AppImage (or its AppRun) and drive a real editing session.

Run with --launcher <CutRoom.AppImage or CutRoom.AppDir/AppRun>. It starts the
launcher with a clean environment and a temporary home folder, imports a
generated clip through the HTTP API, cuts it, adds captions, exports an MP4,
checks that the caption is drawn on the picture, restarts the program at once
on the same port and reopens the saved project. Stabilization is exercised when
the bundled FFmpeg supports it and must be refused up front when it does not.

Nothing here replaces human review: the clip is synthetic and no speech model
runs. Only the standard library is used.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WIDTH, HEIGHT = 1280, 720
SRT = "1\n00:00:00,500 --> 00:00:02,500\nCUTROOM Linux check\n\n2\n00:00:04,000 --> 00:00:06,000\nSecond caption line\n"


class SmokeFailure(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Instance:
    """One launcher process and its HTTP address."""

    def __init__(self, launcher: Path, port: int, home: Path, log: Path):
        self.launcher, self.port, self.home, self.log = launcher, port, home, log
        self.base = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen | None = None

    def start(self, timeout: float = 240) -> None:
        env = {
            "HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "CUTROOM_NO_BROWSER": "1", "CUTROOM_PORT": str(self.port),
            # An AppImage cannot rely on FUSE inside CI containers.
            "APPIMAGE_EXTRACT_AND_RUN": "1",
        }
        with self.log.open("ab") as output:
            output.write(f"\n=== start {time.strftime('%H:%M:%S')} ===\n".encode())
            self.process = subprocess.Popen([str(self.launcher)], env=env, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise SmokeFailure(f"The launcher exited with {self.process.returncode} before it was ready:\n{self.tail()}")
            try:
                status, body = self.call("GET", "/api/health", timeout=3)
                if status == 200 and body.get("ok"):
                    return
            except (OSError, SmokeFailure):
                pass
            time.sleep(0.5)
        raise SmokeFailure(f"The launcher did not become ready in {timeout:.0f} s:\n{self.tail()}")

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def call(self, method: str, path: str, body=None, headers=None, raw: bool = False, timeout: float = 300):
        data, request_headers = None, dict(headers or {})
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                return response.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            try:
                return error.code, json.loads(text)
            except ValueError:
                return error.code, {"raw": text[:300]}


class Report:
    def __init__(self):
        self.steps: list[dict] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
        self.steps.append({"check": label, "ok": bool(ok), "detail": detail})
        if not ok:
            raise SmokeFailure(label + (f": {detail}" if detail else ""))


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, timeout=300, **kwargs)


def probe(ffprobe: str, path: Path) -> dict:
    return json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout)


def gray_frame(ffmpeg: str, video: Path, seconds: float) -> bytes:
    command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-ss", str(seconds), "-i", str(video),
               "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    frame = run(command).stdout
    if len(frame) != WIDTH * HEIGHT:
        raise SmokeFailure(f"Unexpected frame size {len(frame)} from {video.name} at {seconds}s")
    return frame


def mean_difference(first: bytes, second: bytes, rows: range) -> float:
    total = 0
    for row in rows:
        start = row * WIDTH
        total += sum(abs(a - b) for a, b in zip(first[start:start + WIDTH], second[start:start + WIDTH]))
    return total / (len(rows) * WIDTH)


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    found = {}
    for root, _directories, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            found[path] = (info.st_size, info.st_mtime_ns)
    return found


def edit(instance: Instance, report: Report, pid: str, label: str, payload: dict, revision: int) -> dict:
    status, body = instance.call("POST", f"/api/projects/{pid}/manual/edit", {**payload, "expected_revision": revision})
    report.check(label, status == 200, f"status {status} {json.dumps(body)[:160] if status != 200 else ''}")
    return body["project"]


def wait_for_job(instance: Instance, job_id: str, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _status, body = instance.call("GET", f"/api/jobs/{job_id}")
        job = body["job"]
        if job["status"] in ("completed", "succeeded", "done", "failed", "cancelled", "error"):
            return job
        time.sleep(0.7)
    raise SmokeFailure(f"Job {job_id} did not finish in {timeout:.0f} s")


def wait_idle(instance: Instance, pid: str, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _status, active = instance.call("GET", f"/api/projects/{pid}/jobs/active")
        if not active.get("jobs", active.get("job") or []):
            return
        time.sleep(0.5)
    raise SmokeFailure("Project jobs did not settle")


def editing_session(instance: Instance, report: Report, work: Path, ffmpeg: str, ffprobe: str) -> str:
    source = work / "synthetic-10s.mp4"
    run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate=30:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
    report.check("generated a 10 s 720p test clip with audio", source.stat().st_size > 10_000, f"{source.stat().st_size} bytes")

    status, body = instance.call("POST", "/api/projects", {
        "name": "Linux smoke", "initial_settings": {"workflow": "manual", "goal": "youtube", "aspect": "16:9"}})
    report.check("created a project", status in (200, 201), f"status {status}")
    pid = body["project"]["id"]

    boundary = uuid.uuid4().hex
    upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{source.name}"\r\n'
              "Content-Type: video/mp4\r\n\r\n").encode() + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, body = instance.call("POST", f"/api/projects/{pid}/sources/A", upload,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    report.check("imported the clip as source A", status in (200, 201, 202), f"status {status}")
    wait_idle(instance, pid)
    project = instance.call("GET", f"/api/projects/{pid}")[1]["project"]
    clip = project["sources"]["A"]
    report.check("probed the clip correctly",
                 (clip.get("width"), clip.get("height")) == (WIDTH, HEIGHT) and abs(float(clip["duration"]) - 10) < .2 and clip.get("has_audio"),
                 f"{clip.get('width')}x{clip.get('height')} {clip.get('duration')} s audio={clip.get('has_audio')}")

    status, body = instance.call("POST", f"/api/projects/{pid}/manual-draft", {"expected_revision": project["revision"]})
    report.check("started the manual timeline", status == 200 and abs(body["project"]["draft"]["output_duration"] - 10) < .2)
    project = edit(instance, report, pid, "deleted 3 s to 5 s", {"action": "delete_range", "start": 3, "end": 5}, body["project"]["revision"])
    report.check("timeline is 8 s", abs(project["draft"]["output_duration"] - 8) < .2, str(project["draft"]["output_duration"]))
    project = edit(instance, report, pid, "imported SRT captions",
                   {"action": "text_import", "format": "srt", "content": SRT, "replace": True}, project["revision"])

    status, body = instance.call("POST", f"/api/projects/{pid}/render", {"quality": "fast", "resolution": "720"})
    report.check("accepted the export job", status == 202, f"status {status}")
    job = wait_for_job(instance, body["job"]["id"])
    report.check("export finished", job["status"] in ("completed", "succeeded", "done"), f"{job['status']} {job.get('error') or ''}")
    export = job["result"]["export"]
    report.check("export used burned captions and both text clips",
                 export["captions_burned"] is True and export["manual_text_count"] == 2,
                 f"encoder={export['encoder']} warnings={export['review']['warnings']}")
    status, data = instance.call("GET", urllib.parse.quote(job["result"]["url"], safe="/%"), raw=True)
    output = work / "export.mp4"
    output.write_bytes(data)
    report.check("export downloads", status == 200 and len(data) > 20_000, f"{len(data)} bytes")

    info = probe(ffprobe, output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"]["duration"])
    report.check("export is H.264 at 720p with AAC audio", video["codec_name"] == "h264" and video["height"] == HEIGHT and audio["codec_name"] == "aac",
                 f"{video['codec_name']} {video['width']}x{video['height']} + {audio['codec_name']}")
    report.check("export is 8.00 s at a constant 30 fps",
                 abs(duration - 8) < .1 and video["r_frame_rate"] == "30/1" == video["avg_frame_rate"],
                 f"{duration:.2f} s, {video['r_frame_rate']}")

    # Edited 1.5 s is source 1.5 s (before the cut) and carries a caption; edited 3.0 s is source 5.0 s and has none.
    caption_rows, control_rows = range(630, 676), range(40, 86)
    with_caption = mean_difference(gray_frame(ffmpeg, output, 1.5), gray_frame(ffmpeg, source, 1.5), caption_rows)
    control = mean_difference(gray_frame(ffmpeg, output, 1.5), gray_frame(ffmpeg, source, 1.5), control_rows)
    without_caption = mean_difference(gray_frame(ffmpeg, output, 3.0), gray_frame(ffmpeg, source, 5.0), caption_rows)
    report.check("caption is drawn on the exported picture",
                 with_caption > max(4.0, 3 * control) and without_caption < max(4.0, 2 * control),
                 f"caption area differs by {with_caption:.1f}, same area without caption {without_caption:.1f}, untouched area {control:.1f}")
    return pid


def stabilization(instance: Instance, report: Report, work: Path, ffprobe: str, pid: str, required: bool) -> str:
    _status, capability = instance.call("GET", "/api/stabilization/capability")
    project = instance.call("GET", f"/api/projects/{pid}")[1]["project"]
    request = {"slot": "A", "expected_revision": project["revision"]}
    if not capability["available"]:
        report.check("stabilization is not required by this FFmpeg", not required, capability["message"])
        status, body = instance.call("POST", f"/api/projects/{pid}/stabilize", request)
        report.check("stabilization is refused up front when unavailable",
                     status == 422 and body.get("error") == "stabilization_unavailable", f"status {status}")
        return "unavailable: " + capability["message"]
    status, body = instance.call("POST", f"/api/projects/{pid}/stabilize", request)
    report.check("accepted the stabilization job", status == 202, f"status {status} {json.dumps(body)[:160] if status != 202 else ''}")
    asset_id = body["asset_id"]
    job = wait_for_job(instance, body["job"]["id"])
    report.check("stabilization finished", job["status"] in ("completed", "succeeded", "done"), f"{job['status']} {job.get('error') or ''}")
    asset = instance.call("GET", f"/api/projects/{pid}")[1]["project"]["assets"][asset_id]
    report.check("stabilized copy is ready", asset["status"] == "ready", asset["status"])
    status, data = instance.call("GET", f"/api/projects/{pid}/assets/{asset_id}/download", raw=True)
    copy = work / "stabilized.mp4"
    copy.write_bytes(data)
    stream = next(s for s in probe(ffprobe, copy)["streams"] if s["codec_type"] == "video")
    report.check("stabilized copy keeps the original size and frame rate",
                 status == 200 and (stream["width"], stream["height"]) == (WIDTH, HEIGHT) and stream["r_frame_rate"] == "30/1",
                 f"{stream['width']}x{stream['height']} {stream['r_frame_rate']}")
    return "available and exercised"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--launcher", type=Path, required=True, help="CutRoom .AppImage or CutRoom.AppDir/AppRun")
    parser.add_argument("--ffmpeg", required=True, help="FFmpeg used to generate and inspect the test media")
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--require-stabilization", action="store_true", help="fail unless the bundled FFmpeg supports stabilization")
    parser.add_argument("--watch-tree", type=Path, help="folder that must stay unchanged, such as the read-only app folder")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.launcher.exists():
        parser.error(f"No such launcher: {args.launcher}")

    report = Report()
    outcome = {"stabilization": None}
    with tempfile.TemporaryDirectory(prefix="cutroom-smoke-") as scratch:
        scratch_path = Path(scratch)
        home, work = scratch_path / "home", scratch_path / "work"
        home.mkdir()
        work.mkdir()
        instance = Instance(args.launcher.resolve(), args.port or free_port(), home, scratch_path / "launcher.log")
        before = snapshot(args.watch_tree) if args.watch_tree else None
        try:
            started = time.monotonic()
            instance.start()
            report.check("launcher serves the editor", True, f"ready after {time.monotonic() - started:.1f} s on port {instance.port}")
            status, health = instance.call("GET", "/api/health")
            report.check("bundled FFmpeg and FFprobe are found", health.get("ffmpeg") is True and health.get("ffprobe") is True, json.dumps(health))
            page = instance.call("GET", "/", raw=True)
            script = instance.call("GET", "/assets/app.js", raw=True)
            report.check("editor page and scripts load", page[0] == 200 and b"CUTROOM" in page[1] and script[0] == 200 and len(script[1]) > 10_000)

            pid = editing_session(instance, report, work, args.ffmpeg, args.ffprobe)
            outcome["stabilization"] = stabilization(instance, report, work, args.ffprobe, pid, args.require_stabilization)

            instance.stop()
            restart = time.monotonic()
            instance.start()   # Same port, straight away: leftover connections must not block it.
            report.check("restarted at once on the same port", True, f"ready after {time.monotonic() - restart:.1f} s")
            status, body = instance.call("GET", f"/api/projects/{pid}")
            project = body.get("project", {})
            report.check("saved project reopens with its cut", status == 200 and any(
                abs(cut["start"] - 3) < .01 and abs(cut["end"] - 5) < .01 for cut in project.get("manual", {}).get("cuts", [])))
            report.check("projects are stored in the user's data folder",
                         (home / ".local/share/cutroom/projects" / pid).is_dir(), str(home / ".local/share/cutroom"))
        except SmokeFailure as failure:
            print(f"SMOKE FAILED: {failure}\n--- launcher log ---\n{instance.tail(60)}", file=sys.stderr)
            return 1
        finally:
            instance.stop()
        if before is not None:
            after = snapshot(args.watch_tree)
            changed = sorted(path for path in after if before.get(path) != after[path]) + sorted(set(before) - set(after))
            if changed:
                print(f"SMOKE FAILED: the app folder changed while running: {changed[:5]}", file=sys.stderr)
                return 1
            report.check("nothing was written into the app folder", True, f"{len(after)} files unchanged")

    ffmpeg_version = run([args.ffmpeg, "-version"]).stdout.decode(errors="replace").splitlines()[0]
    if args.report:
        args.report.write_text(json.dumps({"ffmpeg": ffmpeg_version, **outcome, "steps": report.steps}, indent=2), encoding="utf-8")
    print(f"ALL SMOKE CHECKS PASSED ({ffmpeg_version}; stabilization {outcome['stabilization']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
