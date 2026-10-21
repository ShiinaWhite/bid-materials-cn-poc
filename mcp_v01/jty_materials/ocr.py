import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

from .common import atomic_write, digest, normalize
from .windows import owned_processes


class Ocr:
    def __init__(self, config):
        self.c = config
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.p = None
        self.logfile = None

    def request(self, route, value=None, timeout=15):
        body = None if value is None else json.dumps(value).encode("utf-8")
        req = urllib.request.Request(self.c.umi_url + route, body,
                                     {"Content-Type": "application/json"})
        with self.http.open(req, timeout=timeout) as r:
            data = r.read()
        try:
            return json.loads(data)
        except ValueError:
            return data.decode("utf-8")

    def start(self):
        if owned_processes(self.c.umi_exe.parent):
            raise RuntimeError("test Umi already running; refusing to take ownership")
        parsed = urllib.parse.urlparse(self.c.umi_url)
        import socket
        with socket.socket() as s:
            if s.connect_ex((parsed.hostname, parsed.port)) == 0:
                raise RuntimeError("configured OCR port is already occupied")
        self.logfile = (self.c.logs_dir / "umi-console.log").open("ab")
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        self.p = subprocess.Popen([str(self.c.umi_exe), "--force"], cwd=self.c.umi_exe.parent,
                                  stdout=self.logfile, stderr=self.logfile,
                                  startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
        self.c.log("umi.start", pid=self.p.pid, exe=str(self.c.umi_exe), url=self.c.umi_url)
        until = time.monotonic() + 45
        while time.monotonic() < until:
            try:
                options = self.request("/api/doc/get_options")
                self.c.log("umi.ready", processes=owned_processes(self.c.umi_exe.parent), options=options)
                return
            except (OSError, ValueError):
                time.sleep(.5)
        raise TimeoutError("Umi failed to become ready; see umi-console.log")

    @staticmethod
    def validate(result):
        if not result.get("is_done") or result.get("state") != "success":
            raise RuntimeError("OCR task did not succeed: " + repr(result))
        if (not isinstance(result.get("data"), str) or not result["data"].strip()
                or result.get("pages_count", 0) <= 0
                or result.get("processed_count") != result.get("pages_count")):
            raise RuntimeError("OCR result is empty or incomplete")

    def document(self, pdf):
        pdf = Path(pdf)
        if pdf.suffix.lower() in ('.png','.jpg','.jpeg'):
            return self.image(pdf)
        boundary = "Pilot" + uuid.uuid4().hex
        options = {"doc.extractionMode": "fullPage", "ocr.language": "简体中文",
                   "ocr.maxSideLen": 1024, "tbpu.parser": "multi_para"}
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="json"\r\n\r\n'
                + json.dumps(options) + f'\r\n--{boundary}\r\nContent-Disposition: form-data; '
                f'name="file"; filename="{pdf.name}"\r\nContent-Type: application/pdf\r\n\r\n').encode("utf-8")
        body += pdf.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.c.umi_url + "/api/doc/upload", body,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.http.open(request, timeout=30) as response:
            uploaded = json.loads(response.read())
        if uploaded.get("code") != 100:
            raise RuntimeError("OCR upload failed: " + repr(uploaded))
        task = uploaded["data"]
        try:
            until = time.monotonic() + 120
            while True:
                result = self.request("/api/doc/result", {"id": task, "is_data": True,
                                      "is_unread": False, "format": "text"})
                if result.get("is_done"):
                    break
                if time.monotonic() > until:
                    raise TimeoutError("OCR task timeout")
                time.sleep(.5)
            self.validate(result)
            raw = pdf.parent / ".ocr-raw" / task / (pdf.name + ".ocr.txt")
            atomic_write(raw, result["data"], immutable=True)
            dl = self.request("/api/doc/download", {"id": task, "file_types": ["jsonl"]})
            if dl.get("code") != 100:
                raise RuntimeError("OCR JSONL download unavailable")
            if urllib.parse.urlparse(dl["data"]).netloc != urllib.parse.urlparse(self.c.umi_url).netloc:
                raise ValueError("refuse nonlocal OCR download")
            with self.http.open(dl["data"], timeout=20) as response:
                jsonl = response.read()
            raw_json = raw.with_suffix(".jsonl")
            atomic_write(raw_json, jsonl, immutable=True)
            search = pdf.with_name(pdf.name + ".search.txt")
            atomic_write(search, normalize(result["data"]))
            info = {"original": str(pdf), "raw": str(raw), "jsonl": str(raw_json),
                    "search": str(search), "task": task, "state": result["state"],
                    "pages": result["pages_count"], "processed": result["processed_count"],
                    "raw_sha256": digest(raw), "options": options}
            self.c.log("ocr.success", **info)
            return info
        finally:
            try:
                self.request("/api/doc/clear/" + task)
            except OSError as e:
                self.c.log("ocr.clear_failed", task=task, error=str(e))

    def image(self, original):
        """Use the installed native image API; never wrap/alter the original."""
        import base64, io, hashlib
        from PIL import Image, ImageOps, UnidentifiedImageError
        image_bytes=original.read_bytes()
        transform={'operation':'none','source_sha256':hashlib.sha256(image_bytes).hexdigest()}
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                orientation=image.getexif().get(274)
                if orientation in range(2,9):
                    derived=ImageOps.exif_transpose(image)
                    output=io.BytesIO();derived.save(output,format='PNG');image_bytes=output.getvalue()
                    transform.update(operation='EXIF transpose on in-memory OCR derivative only',orientation=orientation,
                                     input_sha256=hashlib.sha256(image_bytes).hexdigest(),input_format='PNG')
        except UnidentifiedImageError:
            pass  # Let the OCR API reject unsupported data; also permits mocked API fixtures.
        options={'ocr.language':'简体中文','ocr.maxSideLen':1024,'tbpu.parser':'multi_para','data.format':'dict'}
        result=self.request('/api/ocr',{'base64':base64.b64encode(image_bytes).decode('ascii'),'options':options},timeout=120)
        if not isinstance(result,dict) or result.get('code')!=100 or not isinstance(result.get('data'),list) or not result['data']:
            raise RuntimeError('image OCR did not succeed with nonempty blocks')
        if any(not isinstance(b,dict) or not isinstance(b.get('text'),str) for b in result['data']):
            raise RuntimeError('image OCR returned malformed blocks')
        text='\n'.join(b['text'] for b in result['data'])
        if not text.strip():raise RuntimeError('image OCR returned empty text')
        task=uuid.uuid4().hex;raw=original.parent/'.ocr-raw'/task/(original.name+'.ocr.txt')
        raw_json=raw.with_suffix('.jsonl');search=original.with_name(original.name+'.search.txt')
        atomic_write(raw,text,immutable=True)
        atomic_write(raw_json,json.dumps(result,ensure_ascii=False)+'\n',immutable=True)
        atomic_write(search,normalize(text))
        if digest(original)!=transform['source_sha256']:raise ValueError('original changed during image OCR')
        info=dict(original=str(original),raw=str(raw),jsonl=str(raw_json),search=str(search),task=task,state='success',pages=1,processed=1,raw_sha256=digest(raw),options=dict(options,input_transform=transform),api='/api/ocr')
        self.c.log('ocr.success',**info);return info

    def stop(self):
        if self.p is None:
            return
        before = owned_processes(self.c.umi_exe.parent)
        self.c.log("umi.stopping", processes=before)
        try:
            self.request("/argv", ["--quit"])
        except OSError:
            pass  # server can close the socket while quitting
        # The launcher can exit before the real GUI process; track executable
        # paths, not just the Popen handle. RapidOCR may survive the GUI.
        until = time.monotonic() + 12
        remaining = owned_processes(self.c.umi_exe.parent)
        while any(p["Name"].lower() != "rapidocr-json.exe" for p in remaining) and time.monotonic() < until:
            time.sleep(.3)
            remaining = owned_processes(self.c.umi_exe.parent)
        graceful = not any(p["Name"].lower() != "rapidocr-json.exe" for p in remaining)
        for p in remaining:
            # Only this run's executable paths; never kill by image name.
            subprocess.run(["taskkill", "/PID", str(p["ProcessId"]), "/F"],
                           capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.p.wait(timeout=5)
        self.logfile.close()
        after = owned_processes(self.c.umi_exe.parent)
        self.c.log("umi.stopped", graceful_main=graceful, engine_cleanup=remaining,
                   remaining=after, exit_code=self.p.returncode)
        self.p = None
        if after or not graceful:
            raise RuntimeError("Umi did not shut down cleanly; inspect events")
