#!/usr/bin/env python3
"""Local server for echo-english: serves the static site and the lesson-editing API.

    GET    /api/voices
    POST   /api/generate        {id?, title, text, voice}
    DELETE /api/lessons/<id>
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import edge_tts

ROOT = Path(__file__).resolve().parent
LESSONS = ROOT / "lessons"
HOST, PORT = "127.0.0.1", 8000
AAC_BITRATE = "64000"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

# The first entry is pre-selected in admin.html.
VOICES = {
    "en-US-BrianNeural": "Brian（美式男聲）",
    "en-US-AndrewNeural": "Andrew（美式男聲）",
    "en-US-GuyNeural": "Guy（美式男聲）",
    "en-US-AriaNeural": "Aria（美式女聲）",
    "en-US-JennyNeural": "Jenny（美式女聲）",
    "en-US-AvaNeural": "Ava（美式女聲）",
    "en-GB-RyanNeural": "Ryan（英式男聲）",
    "en-GB-SoniaNeural": "Sonia（英式女聲）",
}

write_lock = threading.Lock()


class LessonError(Exception):
    pass


def parse_text(text):
    """Return items like data.json: {"title": ...} or {"segments": [{"text": ...}]}."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            items.append({"title": line.lstrip("#").strip()})
            continue
        parts = [p.strip() for p in line.split("/")]
        segments = [{"text": p} for p in parts if re.search(r"\w", p)]
        if segments:
            items.append({"segments": segments})
    return items


def norm(s):
    return re.sub(r"\W", "", s).lower()


async def synthesize(spoken, voice, mp3_path):
    comm = edge_tts.Communicate(spoken, voice, boundary="WordBoundary")
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(chunk)
    return words


def assign_times(items, words):
    """Walk the spoken words and give each segment the start/end of its words."""
    i = 0
    for item in items:
        for seg in item.get("segments", []):
            need = len(norm(seg["text"]))
            first = i
            got = 0
            while got < need and i < len(words):
                got += len(norm(words[i]["text"]))
                i += 1
            if got != need or i == first:
                raise LessonError(f"無法對齊語音與文字：{seg['text']!r}")
            last = words[i - 1]
            seg["start"] = round(words[first]["offset"] / 1e7, 3)
            seg["end"] = round((last["offset"] + last["duration"]) / 1e7, 3)


def audio_duration(path):
    out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, check=True).stdout
    m = re.search(r"estimated duration:\s*([\d.]+)", out)
    return round(float(m.group(1)), 3)


def build_lesson(lesson_id, title, text, voice, dest):
    """Write data.json and audio.m4a for one lesson into the dest directory."""
    items = parse_text(text)
    lines = [" ".join(s["text"] for s in it["segments"]) for it in items if "segments" in it]
    if not lines:
        raise LessonError("內文沒有可以唸的句子")
    dest = Path(dest)
    mp3 = dest / "audio.mp3"
    words = asyncio.run(synthesize("\n".join(lines), voice, mp3))
    assign_times(items, words)
    subprocess.run(
        ["afconvert", "-f", "m4af", "-d", "aac", "-b", AAC_BITRATE, str(mp3), str(dest / "audio.m4a")],
        check=True, capture_output=True,
    )
    mp3.unlink()
    now = time.time()
    data = {
        "id": lesson_id,
        "title": title,
        "voice": voice,
        "duration": audio_duration(dest / "audio.m4a"),
        "updated": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "version": int(now),
        "text": text,
        "items": items,
    }
    write_json(dest / "data.json", data)
    return data


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def read_index():
    path = LESSONS / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def write_index(entries):
    entries.sort(key=lambda e: e["updated"], reverse=True)
    write_json(LESSONS / "index.json", entries)


def make_id(title):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50].strip("-") or f"lesson-{int(time.time())}"
    lesson_id, n = base, 2
    while (LESSONS / lesson_id).exists():
        lesson_id = f"{base}-{n}"
        n += 1
    return lesson_id


def generate(payload):
    title = (payload.get("title") or "").strip()
    text = payload.get("text") or ""
    voice = payload.get("voice") or next(iter(VOICES))
    if not title:
        raise LessonError("請填標題")
    if voice not in VOICES:
        raise LessonError(f"不支援的聲音：{voice}")
    with write_lock:
        lesson_id = payload.get("id") or make_id(title)
        if not ID_RE.match(lesson_id):
            raise LessonError("課程 id 只能用小寫英文、數字和 -")
        LESSONS.mkdir(exist_ok=True)
        tmp = tempfile.mkdtemp(dir=LESSONS, prefix=".tmp-")
        try:
            data = build_lesson(lesson_id, title, text, voice, tmp)
            target = LESSONS / lesson_id
            if target.exists():
                shutil.rmtree(target)
            shutil.move(tmp, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        entries = [e for e in read_index() if e["id"] != lesson_id]
        entries.append({k: data[k] for k in ("id", "title", "voice", "duration", "updated")})
        write_index(entries)
    return lesson_id


def delete(lesson_id):
    if not ID_RE.match(lesson_id):
        raise LessonError("課程 id 不合法")
    with write_lock:
        target = LESSONS / lesson_id
        if not target.is_dir():
            raise FileNotFoundError(lesson_id)
        shutil.rmtree(target)
        write_index([e for e in read_index() if e["id"] != lesson_id])


class LimitedFile:
    """File-like view of the next `length` bytes, so copyfile() sends only the requested range."""

    def __init__(self, f, length):
        self.f, self.left = f, length

    def read(self, n=-1):
        n = self.left if n < 0 else min(n, self.left)
        data = self.f.read(n)
        self.left -= len(data)
        return data

    def close(self):
        self.f.close()


class Handler(SimpleHTTPRequestHandler):
    # Safari needs byte-range support to play and seek audio; the stock handler has none.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".m4a": "audio/mp4"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    protocol_version = "HTTP/1.1"  # keep-alive, like GitHub Pages; Safari's media stack issues several ranged requests

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        if not self.path.split("?")[0].endswith(".m4a"):
            self.send_header("Cache-Control", "no-cache")  # Safari otherwise keeps serving a stale lesson.html
        super().end_headers()

    def send_head(self):
        spec = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        path = Path(self.translate_path(self.path))
        if not spec or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        first, last = spec.groups()
        if first == "":  # suffix range: the final N bytes
            start, end = max(0, size - int(last or 0)), size - 1
        else:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        if start > end or start >= size:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        f = path.open("rb")
        f.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.end_headers()
        return LimitedFile(f, end - start + 1)

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def route(self):
        return unquote(urlparse(self.path).path)

    def do_GET(self):
        if self.route() == "/api/voices":
            return self.send_json(HTTPStatus.OK, VOICES)
        super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))  # always drain it: the connection is kept alive
        if self.route() != "/api/generate":
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            payload = json.loads(body or b"{}")
            self.send_json(HTTPStatus.OK, {"id": generate(payload)})
        except (LessonError, json.JSONDecodeError) as e:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except Exception as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"產生失敗：{e}"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/lessons/([^/]+)", self.route())
        if not m:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            delete(m.group(1))
            self.send_json(HTTPStatus.OK, {"ok": True})
        except FileNotFoundError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "找不到這個課程"})
        except LessonError as e:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})


if __name__ == "__main__":
    print(f"echo-english: http://{HOST}:{PORT}/  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
