"""Tiny stdlib HTTP server for the local materials UI.

Design constraints: no third-party web framework, threads per request,
read-only Store connections opened per request, all writes delegated to the
serialized manager RPC. Binding is loopback by default; --host 0.0.0.0 is the
phase-three LAN switch.
"""
import argparse
import json
import mimetypes
import re
import shutil
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jty_materials.common import Config  # noqa: E402

from webui import api, feedback, pages, projects, service, thumbs, uploads  # noqa: E402
from webui import render  # noqa: E402
from webui import WEBUI_VERSION  # noqa: E402
from webui.same_origin import post_allowed  # noqa: E402


class Request:
    def __init__(self, handler, params, query):
        self.h = handler
        self.params = params
        self.query = query
        self.rfile = handler.rfile
        self.headers = handler.headers

    def json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class Response:
    def __init__(self, status=200, body=b"", content_type="text/html; charset=utf-8",
                 headers=None, stream_path=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.content_type = content_type
        self.headers = headers or {}
        self.stream_path = stream_path


def json_response(status, data):
    return Response(status, json.dumps(data, ensure_ascii=False),
                    content_type="application/json; charset=utf-8")


def html_response(text, status=200):
    return Response(status, text, headers={"Cache-Control": "no-store"})


STATIC_DIR = Path(__file__).with_name("static")
ROUTES = []


def route(method, pattern):
    compiled = re.compile("^" + pattern + "$")

    def decorate(fn):
        ROUTES.append((method, compiled, fn))
        return fn
    return decorate


# ------------------------------------------------------------ handlers ----

@route('GET', '/projects')
def page_projects(req, ctx):
    return html_response(projects.list_page(ctx.config, req.query))


@route('GET', '/projects/new')
def page_project_new(req, ctx):
    return html_response(projects.edit_page(ctx.config))


@route('GET', r'/project/(?P<pid>\d+)')
def page_project(req, ctx):
    return html_response(projects.detail_page(ctx.config, int(req.params['pid']), req.query))


@route('GET', r'/project/(?P<pid>\d+)/edit')
def page_project_edit(req, ctx):
    return html_response(projects.edit_page(ctx.config, int(req.params['pid'])))


@route('GET', r'/project/(?P<pid>\d+)/search')
def page_project_search(req, ctx):
    return html_response(projects.search_page(ctx.config, int(req.params['pid']), req.query))


@route('GET', r'/api/project/(?P<pid>\d+)/documents')
def api_project_documents(req, ctx):
    # Progress commits are brief and frequent. Retry a generation conflict
    # locally so ordinary polling does not emit spurious HTTP/console errors.
    import time
    for attempt in range(3):
        try:
            rows = projects.Documents(ctx.config).status(int(req.params['pid']))
            break
        except RuntimeError as error:
            if 'RETRY:' not in str(error) or attempt == 2:
                raise
            time.sleep(.02)
    return json_response(200, dict(items=[dict(project_file_id=d['project_file_id'], status=d['status'],
        label=projects.document_state(d), index_status=d['index_status']) for d in rows]))


@route('POST', r'/api/project/document/(?P<action>extract|retry|rebuild)')
def api_project_document_write(req, ctx):
    return json_response(*projects.write_api(ctx.config, req, 'project.document.' + req.params['action']))


@route('GET', r'/project/(?P<pid>\d+)/upload')
def page_project_upload(req, ctx):
    return html_response(projects.upload_page(ctx.config, int(req.params['pid'])))


@route('POST', r'/api/project/(?P<action>create|update|archive|restore|material/link|material/unlink)')
def api_project_write(req, ctx):
    action = 'project.' + req.params['action'].replace('/', '.')
    return json_response(*projects.write_api(ctx.config, req, action))


@route('POST', '/api/project/file')
def api_project_file(req, ctx):
    return json_response(*projects.upload_api(ctx.config, ctx.project_jobs, req))


@route('GET', r'/api/project/jobs/(?P<job>\w+)')
def api_project_job(req, ctx):
    job = ctx.project_jobs.get(req.params['job'])
    return json_response(200, job.to_dict()) if job else json_response(404, dict(error='上传任务不存在'))


@route('GET', r'/project-file/(?P<pid>\d+)/(?P<fid>[a-f0-9]{32})')
def serve_project_file(req, ctx):
    from jty_materials.projects import Projects, INLINE
    from urllib.parse import quote
    try:
        row = Projects(ctx.config).resolve_file(int(req.params['pid']), req.params['fid'])
    except (ValueError, OSError) as e:
        return html_response(render.error_page(str(e)), status=409)
    if row is None:
        return html_response(render.error_page('项目文件不存在'), status=404)
    ext = '.' + row['extension']
    inline = ext in INLINE and not req.query.get('download')
    disposition = 'inline' if inline else 'attachment'
    return Response(200, stream_path=row['path'], content_type=INLINE.get(ext, 'application/octet-stream'), headers={
        'Content-Disposition': disposition + "; filename*=UTF-8''" + quote(row['filename'], safe=''),
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

@route("GET", "/")
def page_home(req, ctx):
    return html_response(pages.home(ctx.config))


@route("GET", "/browse")
def page_browse(req, ctx):
    return html_response(pages.browse(ctx.config, req.query))


@route("GET", r"/material/(?P<mid>\d+)")
def page_material(req, ctx):
    return html_response(pages.material_page(ctx.config, int(req.params["mid"])))


@route("GET", "/upload")
def page_upload(req, ctx):
    return html_response(pages.upload_page(ctx.config, req.query))


@route("GET", "/status")
def page_status(req, ctx):
    return html_response(pages.status_page(ctx.config))


@route("GET", "/feedback")
def page_feedback(req, ctx):
    return html_response(pages.feedback_page(ctx.config))


@route("GET", r"/api/jobs/(?P<job>\w+)")
def api_job(req, ctx):
    return json_response(*ctx.api.job(req.params["job"]))


@route("POST", "/api/upload")
def api_upload(req, ctx):
    return json_response(*ctx.api.upload(req))


@route("GET", r"/api/material/(?P<mid>\d+)/text")
def api_text(req, ctx):
    return json_response(*ctx.api.material_text(int(req.params["mid"])))


@route("POST", "/api/review")
def api_review(req, ctx):
    return json_response(*ctx.api.submit_review(req))


@route("POST", "/api/feedback")
def api_feedback(req, ctx):
    return json_response(*ctx.api.submit_feedback(req))


@route("GET", "/api/doctor")
def api_doctor_get(req, ctx):
    return json_response(*ctx.api.doctor_status())


@route("POST", "/api/doctor")
def api_doctor_start(req, ctx):
    return json_response(*ctx.api.doctor_start())


@route("GET", r"/files/(?P<mid>\d+)/(?P<fid>\d+)")
def serve_file(req, ctx):
    try:
        row = service.resolve_file(ctx.config, int(req.params["mid"]), int(req.params["fid"]))
    except ValueError as e:
        return html_response(render.error_page(str(e)), status=400)
    if row is None:
        return html_response(render.error_page("文件不存在或未登记。"), status=404)
    path = row["path"]
    if not path.is_file():
        return html_response(render.error_page("已登记的文件在磁盘上缺失，请运行系统体检。"), status=404)
    from jty_materials.common import digest as sha_digest
    if sha_digest(path) != row["sha256"]:
        return html_response(render.error_page(
            "文件校验失败：磁盘内容与登记 SHA256 不一致。原件保护机制阻止了展示，请立即运行系统体检并停止使用该文件。"),
            status=500)
    ext = "." + (row["extension"] or "")
    content_type = pages.catalog.EXT_CONTENT_TYPES.get(ext) or mimetypes.guess_type(row["filename"])[0] \
        or "application/octet-stream"
    download = req.query.get("download")
    disposition = f'attachment; filename="file{ext}"' if download else "inline"
    return Response(200, stream_path=path, content_type=content_type, headers={
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300"})


@route("GET", r"/static/(?P<name>[\w.-]+)")
def serve_static(req, ctx):
    name = req.params["name"]
    path = STATIC_DIR / name
    if not path.is_file():
        return Response(404, b"not found", content_type="text/plain")
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(200, path.read_bytes(), content_type=content_type,
                    headers={"Cache-Control": "no-cache"})


@route("GET", "/favicon.ico")
def favicon(req, ctx):
    path = STATIC_DIR / "favicon.svg"
    return Response(200, path.read_bytes(), content_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


# thumbnail endpoint: only registered original files
@route("GET", r"/thumb/(?P<mid>\d+)/(?P<fid>\d+)\.png")
def serve_thumb(req, ctx):
    try:
        row = service.resolve_file(ctx.config, int(req.params["mid"]), int(req.params["fid"]))
    except ValueError:
        row = None
    if row is None or row["role"] != "original":
        return Response(404, b"not found", content_type="text/plain")
    generated = thumbs.ensure_thumbnail(ctx.config, row)
    if generated is None:
        return Response(404, b"no preview", content_type="text/plain")
    return Response(200, stream_path=generated, content_type="image/png",
                    headers={"Cache-Control": "max-age=604800, immutable",
                             "Content-Length": str(generated.stat().st_size)})


# feedback attachments: only system-generated names inside the matching
# feedback folder are ever served; the feedback tree is not a static directory
@route("GET", r"/feedback-file/(?P<fbid>FB-\d{8}-\d{4})/(?P<name>[A-Za-z0-9._-]+)")
def serve_feedback_attachment(req, ctx):
    try:
        path = ctx.feedback.attachment_path(req.params["fbid"], req.params["name"])
    except feedback.FeedbackError as e:
        return html_response(render.error_page(str(e)), status=400)
    if path is None:
        return html_response(render.error_page("附件不存在。"), status=404)
    content_type = feedback.CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return Response(200, stream_path=path, content_type=content_type, headers={
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600"})


class AppContext:
    def __init__(self, config):
        self.config = config
        self.api = api.Api(config)
        self.project_jobs = projects.FileJobs(config)
        self.feedback = feedback.store_for(config)
        self.search_gate = threading.Semaphore(2)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "materials-webui/" + WEBUI_VERSION
    app = None  # set by serve()

    def log_message(self, fmt, *args):
        try:
            self.app.config.log("webui.access", request=self.requestline,
                                client=self.client_address[0])
        except Exception:
            pass

    def _dispatch(self, method):
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        path = unquote(parts.path)
        for route_method, pattern, fn in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    response = fn(Request(self, match.groupdict(), query), self.app)
                except Exception as error:
                    self.app.config.log("webui.error", path=path, method=method,
                                        error=type(error).__name__ + ": " + str(error),
                                        trace=traceback.format_exc())
                    message = str(error)
                    hint = None
                    status = 500
                    if isinstance(error, (ValueError, KeyError, TypeError)) and path.startswith(('/projects', '/project/')):
                        status = 400
                    if message.startswith("MAINTENANCE"):
                        status, message = 503, "系统正在进行写入维护（如 OCR 入库/备份），请稍后自动重试。"
                        hint = "维护通常在几秒到几分钟内结束。"
                    elif message.startswith("RETRY"):
                        status, message = 503, "库在查询期间发生了变更，请刷新重试。"
                    response = html_response(render.error_page(message, hint), status=status)
                if method == 'POST' and response.status >= 400:
                    # Validation may reject before consuming an upload body.
                    # Never interpret those remaining bytes as another request.
                    self.close_connection = True
                    response.headers['Connection'] = 'close'
                self._send(response)
                return
        if method == "GET":
            self._send(html_response(render.error_page("页面不存在。", "请从顶部导航进入。"), status=404))
        else:
            self._send(json_response(404, {"error": "not found"}))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        if not post_allowed(self):
            # Do not parse/stage a rejected body, nor reuse its unread bytes as
            # the next HTTP request on this connection.
            self.close_connection = True
            response = json_response(403, {"error": "禁止跨站写入请求，请从本资料库页面提交。"})
            response.headers['Connection'] = 'close'
            self._send(response)
            return
        self._dispatch("POST")

    def _send(self, response):
        try:
            self.send_response(response.status)
            body = response.body
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(response.stream_path.stat().st_size
                                                    if response.stream_path else len(body)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if response.stream_path:
                with open(response.stream_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve():
    parser = argparse.ArgumentParser(description="投标素材库 Web UI")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[2] / "config/materials.json"))
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；第三阶段局域网访问时改为 0.0.0.0")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    config = Config(args.config)
    app = AppContext(config)
    Handler.app = app
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    config.log("webui.start", host=args.host, port=args.port)
    print(f"投标素材库 WebUI 已启动: http://{args.host}:{args.port}/  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        config.log("webui.stop")


if __name__ == "__main__":
    serve()
