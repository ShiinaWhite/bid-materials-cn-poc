"""v0.4.2 feedback intake tests: parser, storage, isolation guarantees.
Runs entirely in temp directories; never touches the real feedback tree,
the repository or the database."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webui import feedback as fb

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
JPEG = b"\xff\xd8\xff\xe0JFIF-data" + bytes(range(64))
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"0" * 20
BND = "----jtyFeedbackBoundary01"


def multipart(parts, terminal=True):
    """parts: list of (name, value) or (name, filename, bytes)."""
    out = b""
    for part in parts:
        out += b"--" + BND.encode() + b"\r\n"
        if len(part) == 2:
            out += (f'Content-Disposition: form-data; name="{part[0]}"\r\n\r\n').encode()
            out += str(part[1]).encode("utf-8") + b"\r\n"
        else:
            out += (f'Content-Disposition: form-data; name="{part[0]}"; '
                    f'filename="{part[1]}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
            out += part[2] + b"\r\n"
    out += b"--" + BND.encode() + (b"--\r\n" if terminal else b"--")
    return out


class Chunky(io.BytesIO):
    """Reader that hands out 7-byte slices to stress boundary matching."""
    def read(self, n=-1):
        return super().read(min(n, 7))


class ParserTests(unittest.TestCase):
    ctype = "multipart/form-data; boundary=" + BND

    def parse(self, body, expected=(), **kw):
        fields, files = fb.parse_multipart(Chunky(body), self.ctype, len(body), **kw)
        for f, want in zip(files, expected):
            data = Path(f["path"]).read_bytes()
            self.assertEqual(data, want)
            self.assertEqual(f["sha256"], hashlib.sha256(want).hexdigest())
        return fields, files

    def test_fields_and_binary_file_survive_chunked_reads(self):
        tricky = PNG + b"\r\n--" + b"---" + BND.encode() + b"near-miss"
        fields, files = self.parse(multipart([
            ("content", "这里的有效期显示好像不对"),
            ("actor", "张三"),
            ("attachment", "屏幕 截图.png", tricky),
        ]), expected=[tricky])
        self.assertEqual(fields, {"content": "这里的有效期显示好像不对", "actor": "张三"})
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["original_name"], "屏幕 截图.png")
        self.assertEqual(Path(files[0]["path"]).read_bytes(), tricky)

    def test_empty_filename_part_is_skipped(self):
        fields, files = self.parse(multipart([
            ("content", "x"), ("attachment", "", PNG),
        ]))
        self.assertEqual(fields, {"content": "x"})
        self.assertEqual(files, [])

    def test_truncated_body_is_rejected(self):
        body = multipart([("content", "abc"), ("attachment", "a.png", PNG)])
        with self.assertRaises(fb.FeedbackError):
            fb.parse_multipart(Chunky(body[:len(body) // 2]), self.ctype, len(body) // 2)

    def test_oversize_file_is_rejected_mid_stream(self):
        body = multipart([("attachment", "big.png", b"\x89PNG\r\n\x1a\n" + b"x" * 300)])
        with self.assertRaises(fb.FeedbackError):
            self.parse(body, max_file=128)

    def test_missing_boundary_is_rejected(self):
        body = multipart([("content", "x")])
        with self.assertRaises(fb.FeedbackError):
            fb.parse_multipart(Chunky(body), "multipart/form-data", len(body))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fb.FeedbackStore(Path(self.tmp.name))
        self.png = Path(self.tmp.name) / "in.png"
        self.png.write_bytes(PNG)

    def submit(self, **kw):
        base = dict(content="默认反馈内容", page_path="/", page_title="首页 · 投标素材库")
        base.update(kw)
        return self.store.submit(**base)

    def test_submit_writes_json_md_attachment_and_index(self):
        r1 = self.submit(actor="张三", page_path="/material/123",
                         page_title="测试人员 · 高压电工作业证 · 投标素材库",
                         attachments=[{"path": self.png, "original_name": "屏幕截图.png"}])
        r2 = self.submit(content="第二条\n换行内容")
        self.assertTrue(r1["feedback_id"].endswith("-0001"))
        self.assertTrue(r2["feedback_id"].endswith("-0002"))
        self.assertEqual(r1["material_id"], 123)
        self.assertIsNone(r2["material_id"])
        self.assertEqual(r1["status"], "open")

        folder = Path(self.tmp.name) / r1["feedback_id"]
        self.assertEqual((folder / "attachments" / "screenshot-01.png").read_bytes(), PNG)
        record = json.loads((folder / "feedback.json").read_text(encoding="utf-8"))
        self.assertEqual(record["attachments"][0]["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(record["webui_version"], fb.WEBUI_VERSION)
        md = (folder / "feedback.md").read_text(encoding="utf-8")
        self.assertIn("# " + r1["feedback_id"], md)
        self.assertIn("状态：待处理（open）", md)
        self.assertIn("页面：/material/123", md)
        self.assertIn("页面标题：测试人员 · 高压电工作业证", md)
        self.assertIn("## 反馈", md)
        self.assertIn("- attachments/screenshot-01.png", md)
        self.assertNotIn("user_agent", md)  # internal JSON fields stay out
        lines = (Path(self.tmp.name) / "index.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["material_id"], 123)
        self.assertEqual(first["attachment_count"], 1)

    def test_index_summary_is_first_line_truncated(self):
        self.submit(content="第一行内容\n第二行")
        entry = self.store.index_entries()[0]
        self.assertEqual(entry["summary"], "第一行内容")

    def test_empty_content_rejected(self):
        with self.assertRaises(fb.FeedbackError):
            self.submit(content="   ")

    def test_non_image_rejected_without_residue(self):
        fake = Path(self.tmp.name) / "fake.png"
        fake.write_bytes(b"<html>definitely not an image</html> " * 4)
        with self.assertRaises(fb.FeedbackError):
            self.submit(attachments=[{"path": fake, "original_name": "fake.png"}])
        ids = [p for p in Path(self.tmp.name).iterdir() if p.name.startswith("FB-")]
        self.assertEqual(len(ids), 0)  # no half-written folder left behind

    def test_more_than_five_attachments_rejected(self):
        atts = []
        for i in range(6):
            p = Path(self.tmp.name) / f"w{i}.webp"
            p.write_bytes(WEBP)
            atts.append({"path": p, "original_name": f"w{i}"})
        with self.assertRaises(fb.FeedbackError):
            self.submit(attachments=atts)

    def test_image_extensions_follow_magic_bytes(self):
        jpg = Path(self.tmp.name) / "photo.dat"  # wrong extension on purpose
        jpg.write_bytes(JPEG)
        r = self.submit(attachments=[{"path": jpg, "original_name": "photo.dat"}])
        folder = Path(self.tmp.name) / r["feedback_id"] / "attachments"
        self.assertEqual([p.name for p in folder.iterdir()], ["screenshot-01.jpg"])
        self.assertEqual(folder.joinpath("screenshot-01.jpg").read_bytes(), JPEG)


class ServingPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fb.FeedbackStore(Path(self.tmp.name))
        png = Path(self.tmp.name) / "in.png"
        png.write_bytes(PNG)
        rec = self.store.submit(content="附截图", page_path="/",
                                attachments=[{"path": png, "original_name": "s.png"}])
        self.fbid = rec["feedback_id"]

    def test_valid_attachment_resolves(self):
        path = self.store.attachment_path(self.fbid, "screenshot-01.png")
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), PNG)

    def test_missing_attachment_returns_none(self):
        self.assertIsNone(self.store.attachment_path(self.fbid, "screenshot-09.png"))
        self.assertIsNone(self.store.attachment_path("FB-20200101-0001", "screenshot-01.png"))

    def test_traversal_and_forged_names_are_refused(self):
        for name in ("..", "../secret.txt", "screenshot-01.png/../../x",
                     "feedback.json", "..%2fscreenshot-01.png", "screenshot-01.PNG%00"):
            with self.assertRaises(fb.FeedbackError, msg=name):
                self.store.attachment_path(self.fbid, name)

    def test_malformed_feedback_ids_are_refused(self):
        for fbid in ("../../etc", "x", "FB-2020-1", "FB-20200101-1/../../x"):
            with self.assertRaises(fb.FeedbackError, msg=fbid):
                self.store.attachment_path(fbid, "screenshot-01.png")

    def test_all_image_types_accepted(self):
        for data, ext in ((JPEG, "jpg"), (WEBP, "webp")):
            src = Path(self.tmp.name) / ("t." + ext)
            src.write_bytes(data)
            r = self.store.submit(content="类型 " + ext,
                                  attachments=[{"path": src, "original_name": "t"}])
            saved = Path(self.tmp.name) / r["feedback_id"] / "attachments" / f"screenshot-01.{ext}"
            self.assertEqual(saved.read_bytes(), data)


class MdRenderTests(unittest.TestCase):
    def test_site_suffix_stripped_and_status_shown(self):
        md = fb.render_md({"feedback_id": "FB-20260101-0001", "status": "open",
                           "created_at": "2026-10-20T08:09:10+00:00", "actor": "",
                           "page_path": "/material/6", "page_title": "测试 · 投标素材库",
                           "content": "正文", "attachments": [{"name": "screenshot-01.png"}]})
        self.assertIn("状态：待处理（open）", md)
        self.assertIn("页面标题：测试", md)
        self.assertNotIn("投标素材库", md.split("页面标题：")[1].splitlines()[0])
        self.assertIn("经办人：（未填写）", md)


if __name__ == "__main__":
    unittest.main()
