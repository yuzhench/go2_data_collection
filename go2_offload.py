"""
go2_offload.py — Copy recorded sessions off the Go2 robot onto this computer.

Plug this computer into the robot's Ethernet port, run

    python go2_offload.py

and a page opens in your browser. It lists every session stored on the
robot; you pick the ones to copy, and the tool then

    1. copies them here            (resumes if the cable is pulled)
    2. verifies every file         (sha256 against the robot's copy)
    3. offers to delete them from the robot

The robot's round screen shows the same progress. Nothing is deleted from
the robot before its copy here has been verified.

Only the Python standard library is used (Python 3.8+).
"""

import argparse
import hashlib
import http.client
import json
import shutil
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_ROBOT = "192.168.123.18:8010"
DEFAULT_DEST = Path.home() / "Go2Data"
DEFAULT_UI_PORT = 8765

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
VERIFIED_MARKER = ".verified.json"

CHUNK = 4 << 20                 # download / hashing block size
PACK_MAX_FILE = 1 << 20         # files up to this size are fetched in packs
PACK_FILES = 400                # files per pack request
CHECKSUM_BATCH_FILES = 200      # files per checksum request to the robot
CHECKSUM_BATCH_BYTES = 2 << 30  # ...or this many bytes, whichever comes first
HEARTBEAT_S = 1.0               # how often the robot is told our phase
RETRIES = 5                     # per-request retries before the job fails

# Job phase -> phase reported to the robot (shown on its screen).
ROBOT_PHASE = {
    "idle": "linked", "transfer": "transfer", "verify": "verify",
    "await_delete": "verify", "delete": "delete", "done": "done", "error": "linked",
}


class Robot:
    """Minimal HTTP client for the robot's record daemon."""

    def __init__(self, address: str):
        self.host, _, port = address.partition(":")
        self.port = int(port or 8010)

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def get_json(self, path: str, timeout: float = 5.0):
        conn = self._connect(timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise IOError(f"GET {path} -> {resp.status}: {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def post_json(self, path: str, payload: dict, timeout: float = 30.0):
        conn = self._connect(timeout)
        try:
            conn.request("POST", path, body=json.dumps(payload),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise IOError(f"POST {path} -> {resp.status}: {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def get_bytes(self, path: str, timeout: float = 10.0) -> bytes:
        conn = self._connect(timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise IOError(f"GET {path} -> {resp.status}")
            return body
        finally:
            conn.close()


def _q(**params) -> str:
    return urllib.parse.urlencode(params)


def sha256_file(path: Path, on_bytes=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.hexdigest()


class Offloader:
    """All tool state plus the background threads that act on it."""

    def __init__(self, robot: Robot, dest: Path):
        self.robot = robot
        self.dest = dest
        self.lock = threading.Lock()

        self.connected = False
        self.clock = None            # result of the one-off clock sync
        self.sessions = []
        self.disk = {"total": 0, "free": 0}

        self.phase = "idle"
        self.selected = []           # session ids of the current job
        self.verified = []           # ids copied and verified in this job
        self.current = 0             # 1-based index into selected
        self.bytes_done = 0
        self.bytes_total = 0
        self.speed = 0.0             # bytes/s, recent
        self.message = ""
        self.error = ""
        self.deleted = []
        self._speed_marks = []
        self._worker = None
        self._conn = None            # kept open across file downloads

    # ------------------------------------------------------------------
    # Link to the robot
    # ------------------------------------------------------------------

    def poll_loop(self):
        while True:
            try:
                data = self.robot.get_json("/sessions", timeout=3.0)
                first = not self.connected
                with self.lock:
                    self.connected = True
                    self.sessions = data["sessions"]
                    self.disk = {"total": data["disk_total"], "free": data["disk_free"]}
                if first:
                    self._sync_clock()
                self._heartbeat()
            except (OSError, ValueError, KeyError):
                with self.lock:
                    self.connected = False
            time.sleep(HEARTBEAT_S)

    def _sync_clock(self):
        """Give the robot this computer's time; it has no clock battery."""
        try:
            result = self.robot.post_json("/time", {"unix_time": time.time()}, timeout=8.0)
        except (OSError, ValueError) as e:
            result = {"status": "failed", "detail": str(e)}
        with self.lock:
            self.clock = result

    def _heartbeat(self):
        with self.lock:
            phase = ROBOT_PHASE[self.phase]
            total = self.bytes_total
            progress = 100.0 * self.bytes_done / total if total else 0.0
            payload = {"phase": phase, "progress": progress,
                       "current": self.current, "total": len(self.selected)}
        try:
            self.robot.post_json("/export/state", payload, timeout=3.0)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Job control (called from the UI)
    # ------------------------------------------------------------------

    def start_export(self, ids):
        with self.lock:
            if self.phase not in ("idle", "error", "done"):
                raise ValueError(f"Busy ({self.phase})")
            known = {s["id"]: s for s in self.sessions}
            ids = [i for i in ids if i in known]
            if not ids:
                raise ValueError("No sessions selected")
            if any(known[i]["recording"] for i in ids):
                raise ValueError("A selected session is still being recorded")
            need = sum(known[i]["size_bytes"] for i in ids)
            self.dest.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.dest).free
            if need > free:
                raise ValueError(
                    f"Not enough disk space in {self.dest}: need "
                    f"{need / 1e9:.1f} GB, {free / 1e9:.1f} GB free")
            self.phase = "transfer"
            self.selected, self.verified, self.deleted = ids, [], []
            self.current, self.bytes_done, self.bytes_total = 0, 0, need
            self.speed, self._speed_marks = 0.0, []
            self.message, self.error = "", ""
        self._worker = threading.Thread(target=self._run_job, daemon=True)
        self._worker.start()

    def start_delete(self, ids):
        with self.lock:
            if self.phase != "await_delete":
                raise ValueError(f"Not ready to delete ({self.phase})")
            self.phase = "delete"
            self.message = ""
        threading.Thread(target=self._run_delete, args=(list(ids),), daemon=True).start()

    def reset(self):
        with self.lock:
            if self.phase in ("done", "error", "await_delete"):
                self.phase = "idle"
                self.selected, self.message, self.error = [], "", ""
                self.current, self.bytes_done, self.bytes_total = 0, 0, 0

    # ------------------------------------------------------------------
    # Job
    # ------------------------------------------------------------------

    def _add_bytes(self, n: int):
        now = time.monotonic()
        with self.lock:
            self.bytes_done += n
            marks = self._speed_marks
            marks.append((now, self.bytes_done))
            while marks and now - marks[0][0] > 3.0:
                marks.pop(0)
            if len(marks) > 1 and marks[-1][0] > marks[0][0]:
                self.speed = (marks[-1][1] - marks[0][1]) / (marks[-1][0] - marks[0][0])

    def _retry(self, what, fn):
        """Run fn, retrying while the link is flaky (cable bumped, etc.)."""
        delay = 1.0
        for attempt in range(RETRIES):
            try:
                return fn()
            except (OSError, ValueError, http.client.HTTPException) as e:
                if attempt == RETRIES - 1:
                    raise IOError(f"{what}: {e}") from e
                with self.lock:
                    self.message = f"{what} failed, retrying… ({e})"
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
        return None

    def _run_job(self):
        try:
            plan = []
            for sid in self.selected:
                listing = self._retry("Listing files", lambda: self.robot.get_json(
                    "/sessions/files?" + _q(id=sid), timeout=60.0))
                plan.append((sid, listing["files"]))
            with self.lock:
                self.bytes_total = sum(f["size"] for _, files in plan for f in files)

            for index, (sid, files) in enumerate(plan, start=1):
                with self.lock:
                    self.current, self.message = index, f"Copying {sid}"
                small = [f for f in files if f["size"] <= PACK_MAX_FILE]
                for i in range(0, len(small), PACK_FILES):
                    self._download_pack(sid, small[i:i + PACK_FILES])
                for f in files:
                    if f["size"] > PACK_MAX_FILE:
                        self._download(sid, f["path"], f["size"])

            with self.lock:
                self.phase = "verify"
                self.bytes_done, self._speed_marks, self.speed = 0, [], 0.0
            for index, (sid, files) in enumerate(plan, start=1):
                with self.lock:
                    self.current, self.message = index, f"Verifying {sid}"
                self._verify(sid, files)
                self.robot.post_json("/sessions/mark_exported",
                                     {"id": sid, "host": socket.gethostname()})
                (self.dest / sid / VERIFIED_MARKER).write_text(json.dumps(
                    {"time": time.time(), "files": len(files)}))
                with self.lock:
                    self.verified.append(sid)

            with self.lock:
                self.phase, self.message = "await_delete", ""
        except Exception as e:  # surfaced in the UI; nothing was deleted
            with self.lock:
                self.phase, self.error = "error", str(e)

    def _download_pack(self, sid: str, files):
        """Fetch a batch of small files in one request (see /sessions/pack)."""
        base = self.dest / sid
        missing = []
        for f in files:
            target = base / f["path"]
            if target.exists() and target.stat().st_size == f["size"]:
                self._add_bytes(f["size"])      # already here from an earlier run
            else:
                missing.append(f)
        if not missing:
            return

        def attempt():
            # Files finished by a failed earlier attempt are not asked for again.
            todo = [f for f in missing if not (base / f["path"]).exists()]
            if not todo:
                return
            conn = http.client.HTTPConnection(self.robot.host, self.robot.port, timeout=60)
            try:
                conn.request("POST", "/sessions/pack",
                             body=json.dumps({"id": sid, "paths": [f["path"] for f in todo]}),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise IOError(f"pack -> {resp.status}")
                for f in todo:
                    header = resp.read(8)
                    if len(header) != 8:
                        raise IOError("pack stream ended early")
                    size = int.from_bytes(header, "little")
                    data = resp.read(size)
                    if size != f["size"] or len(data) != size:
                        raise IOError(f"{f['path']}: got {len(data)} of {f['size']} bytes")
                    target = base / f["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part = target.with_name(target.name + ".part")
                    part.write_bytes(data)
                    part.replace(target)
                    self._add_bytes(size)
            finally:
                conn.close()

        self._retry("Copying small files", attempt)

    def _download(self, sid: str, rel: str, size: int):
        target = self.dest / sid / rel
        if target.exists() and target.stat().st_size == size:
            self._add_bytes(size)       # already here from an earlier run
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        path = "/sessions/file?" + _q(id=sid, path=rel)

        def attempt():
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            self._add_bytes(have - attempt.counted)
            attempt.counted = have
            if have < size:
                # One connection is reused for every file: a session holds
                # thousands of small LiDAR scans, and reconnecting for each
                # would dominate the copy time.
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(
                        self.robot.host, self.robot.port, timeout=30)
                conn = self._conn
                try:
                    headers = {"Range": f"bytes={have}-"} if have else {}
                    conn.request("GET", path, headers=headers)
                    resp = conn.getresponse()
                    if resp.status not in (200, 206):
                        resp.read()
                        raise IOError(f"GET {rel} -> {resp.status}")
                    if have and resp.status == 200:
                        # Server ignored the range: start this file over.
                        self._add_bytes(-have)
                        attempt.counted = have = 0
                    with open(part, "ab" if have else "wb") as out:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            attempt.counted += len(chunk)
                            self._add_bytes(len(chunk))
                except Exception:
                    # The connection is in an unknown state; start a new one.
                    conn.close()
                    self._conn = None
                    raise
            if part.stat().st_size != size:
                raise IOError(f"{rel}: got {part.stat().st_size} of {size} bytes")

        attempt.counted = 0
        if size == 0:
            part.write_bytes(b"")
        else:
            self._retry(f"Copying {rel}", attempt)
        part.replace(target)

    def _verify(self, sid: str, files):
        batch, batch_bytes = [], 0
        for f in files:
            batch.append(f)
            batch_bytes += f["size"]
            if len(batch) >= CHECKSUM_BATCH_FILES or batch_bytes >= CHECKSUM_BATCH_BYTES:
                self._verify_batch(sid, batch)
                batch, batch_bytes = [], 0
        if batch:
            self._verify_batch(sid, batch)

    def _verify_batch(self, sid: str, batch, second_try: bool = False):
        paths = [f["path"] for f in batch]
        remote = self._retry("Checksums", lambda: self.robot.post_json(
            "/sessions/checksums", {"id": sid, "paths": paths}, timeout=1800.0))["sha256"]
        bad = []
        for f in batch:
            local = sha256_file(self.dest / sid / f["path"],
                                None if second_try else self._add_bytes)
            if local != remote[f["path"]]:
                bad.append(f)
        if not bad:
            return
        if second_try:
            raise IOError(f"{sid}: {len(bad)} file(s) still differ after re-copying, "
                          f"e.g. {bad[0]['path']}")
        # Copy the damaged files again, once, and check only those.
        for f in bad:
            (self.dest / sid / f["path"]).unlink()
            self._add_bytes(-f["size"])
            self._download(sid, f["path"], f["size"])
        self._verify_batch(sid, bad, second_try=True)

    def _run_delete(self, ids):
        try:
            result = {"deleted": [], "skipped": []}
            if ids:
                result = self.robot.post_json("/sessions/delete", {"ids": ids}, timeout=600.0)
            with self.lock:
                self.deleted = result["deleted"]
                skipped = result.get("skipped", [])
                self.message = (f"{len(skipped)} session(s) could not be deleted"
                                if skipped else "")
                self.phase = "done"
        except Exception as e:
            with self.lock:
                self.phase, self.error = "error", f"Delete failed: {e}"

    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            remaining = self.bytes_total - self.bytes_done
            return {
                "connected":   self.connected,
                "robot":       f"{self.robot.host}:{self.robot.port}",
                "dest":        str(self.dest),
                "clock":       self.clock,
                "sessions":    self.sessions,
                "disk":        self.disk,
                "phase":       self.phase,
                "selected":    self.selected,
                "verified":    self.verified,
                "deleted":     self.deleted,
                "current":     self.current,
                "bytes_done":  self.bytes_done,
                "bytes_total": self.bytes_total,
                "speed":       self.speed,
                "eta_s":       remaining / self.speed if self.speed > 0 else None,
                "message":     self.message,
                "error":       self.error,
            }


def make_handler(off: Offloader):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json(off.snapshot())
            elif url.path == "/api/thumb":
                sid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
                try:
                    jpeg = off.robot.get_bytes("/sessions/thumbnail?" + _q(id=sid))
                    self._send(200, jpeg, "image/jpeg")
                except OSError:
                    self._send(404, b"", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/export":
                    off.start_export(payload.get("ids", []))
                elif self.path == "/api/delete":
                    off.start_delete(payload.get("ids", []))
                elif self.path == "/api/reset":
                    off.reset()
                else:
                    return self._send(404, b"not found", "text/plain")
                self._json({"ok": True})
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, code=400)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Copy recorded sessions off the Go2 robot")
    parser.add_argument("--robot", default=DEFAULT_ROBOT,
                        help=f"robot address host:port (default {DEFAULT_ROBOT})")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help=f"where sessions are copied to (default {DEFAULT_DEST})")
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT,
                        help=f"local port of the browser page (default {DEFAULT_UI_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser automatically")
    args = parser.parse_args()

    off = Offloader(Robot(args.robot), args.dest.expanduser())
    threading.Thread(target=off.poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(off))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Go2 offload tool running at {url}")
    print(f"Robot: {args.robot}   Destination: {off.dest}")
    print("Press Ctrl+C to quit.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
