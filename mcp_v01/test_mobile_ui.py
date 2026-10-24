"""Mobile WebUI (v0.4.3): server-rendered phone cards/filter + touch hardening.

Pages are rendered in-process against an isolated empty library (list results
mocked for card content), so no server or OCR runtime is needed. Guards: the
desktop table still exists next to the mobile cards, upload/feedback format
whitelists do not regress, the camera assist stays optional, and P0.5 POST
routing is untouched.
"""
import hashlib
import inspect
import json
import re
import uuid
import unittest
from pathlib import Path
from unittest import mock

from jty_materials.common import Config, atomic_write
from jty_materials.store import Store

from webui import app, catalog, pages, render
from webui.app import STATIC_DIR

BASE = Path(__file__).resolve().parents[1] / "runs/large-document" / ("mobile-ui-" + uuid.uuid4().hex[:8])

UPLOAD_ACCEPT = 'accept=".pdf,.png,.jpg,.jpeg"'
FEEDBACK_ACCEPT = 'accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"'


def list_item(mid, mtype, **kw):
    """Shape returned by webui.service.summarize (only the fields pages read)."""
    item = {"material_id": mid, "material_code": "MAT-%06d" % mid, "title": "测试资料",
            "verification_status": "unverified", "business_status": "unverified",
            "created_at": "2026-10-01T10:00:00", "material_type": mtype,
            "persons": [], "person_status": None, "candidate_count": 0, "original": None,
            "cert_no": None, "hints": {}, "display": None, "client": None, "amount": None,
            "contract_date": None, "year": None, "rating": None, "valid_until": None,
            "long_term": False, "valid_state": None}
    item.update(kw)
    return item


class MobileUiTests(unittest.TestCase):
    def setUp(self):
        self.base = BASE / hashlib.sha256((self.id() + uuid.uuid4().hex).encode()).hexdigest()[:8]
        self.base.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        raw = {"repository_root": "repository", "db_path": "repository/db/materials.db",
               "index_dir": "index/dowse-index", "state_dir": "state", "logs_dir": "logs",
               "backup_dir": "backups", "umi_url": "http://127.0.0.1:1224",
               "umi_exe": str(project / 'local/runtime/umi/Umi-OCR.exe'),
               "dowse_exe": str(project / 'local/runtime/dowse/dowse.exe'), "fixture_only": True}
        atomic_write(self.base / 'config.json', json.dumps(raw))
        self.c = Config(self.base / 'config.json')
        self.c.materials.mkdir(parents=True)
        Store.initialize(self.c)

    # ------------------------------------------------------------- helpers --

    def browse_html(self, items, params=None):
        result = {"items": items, "total": len(items), "page": 1, "pages": 1, "unmapped": []}
        counts = dict.fromkeys(catalog.TYPE_LABELS, 3)
        groups = {g["key"]: 3 for g in catalog.GROUPS}
        with mock.patch.object(pages.service, 'list_materials', return_value=result), \
                mock.patch.object(pages.service, 'type_counts', return_value=(counts, groups, len(counts))), \
                mock.patch.object(pages.service, 'system_ok', return_value=True), \
                mock.patch('jty_materials.store.Store'):
            return pages.browse(self.c, params or {})

    def card_labels(self, group, type_code, m):
        cols, _ = pages._scope_columns(group, type_code)
        return re.findall(r'm-field-label">([^<]+)<', pages._card_fields(cols, m))

    # ------------------------------------------------------------- layout --

    def test_layout_viewport_and_feedback_widget_mobile_ready(self):
        html = render.layout("测试", "<p>正文</p>")
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn('id="feedback-fab"', html)
        self.assertIn(FEEDBACK_ACCEPT, html)
        self.assertIn('maxlength="5000"', html)
        # desktop drag/Ctrl+V hint kept, phone wording added, camera entry optional
        self.assertIn("Ctrl+V", html)
        self.assertIn('fb-hint-desktop', html)
        self.assertIn('fb-hint-mobile', html)
        self.assertIn('fb-camera-input', html)
        self.assertIn('capture="environment"', html)
        self.assertIn('accept="image/jpeg,image/png,image/webp"', html)

    # ------------------------------------------------------------- browse --

    def test_browse_keeps_desktop_table_and_adds_mobile_cards(self):
        items = [
            list_item(2001, "safety_prod_license", cert_no="SP-2026-001", display="安全生产许可证 SP-2026-001",
                      valid_until="2027-01-01", valid_state="valid",
                      original={"extension": "pdf", "file_id": 11}),
            list_item(2002, "contract", display="某市政道路工程 · 施工合同", client="某建设开发公司",
                      amount=1234500, original={"extension": "pdf", "file_id": 12}),
        ]
        html = self.browse_html(items)
        # desktop presentation unchanged: real table, sidebar, toolbar
        self.assertIn('<table class="table list-table">', html)
        self.assertIn('class="side-item', html)
        self.assertIn('href="/material/2001"', html)
        # mobile presentation: collapsible filter panel + card list
        self.assertIn('<details class="m-filter">', html)
        self.assertIn('class="m-cards" role="list"', html)
        self.assertEqual(html.count('class="toolbar"'), 2)  # desktop + filter panel share one form
        self.assertIn('m-card-name', html)
        self.assertIn('type-badge', html)
        # default-scope card: status + the scoped field (validity), whole card links to detail
        self.assertIn("在用", html)
        self.assertIn("2027-01-01", html)
        self.assertIn('href="/material/2002"', html)
        # view/download entry on the card reuses the desktop row action
        self.assertIn('查看', html)
        # scoped browse: a performance card carries client + amount fields
        scoped = self.browse_html(items[1:], {"group": "performance"})
        self.assertIn("客户/发包方", scoped)
        self.assertIn("某建设开发公司", scoped)
        self.assertIn("¥12,345.00", scoped)
        # filter panel state and clear entry (clear link only when filtered)
        self.assertIn('筛选与分类', scoped)
        self.assertIn('清除全部筛选', scoped)
        self.assertNotIn('清除全部筛选', html)
        # GET form semantics untouched
        self.assertIn('<form class="toolbar" method="get" action="/browse">', html)

    def test_browse_empty_state_still_works_on_mobile_structure(self):
        html = self.browse_html([])
        self.assertIn('没有找到相关资料', html)
        self.assertIn('<details class="m-filter">', html)
        self.assertNotIn('m-cards', html)

    def test_mobile_card_fields_follow_scope(self):
        self.assertEqual(self.card_labels("company", None, list_item(1, "safety_prod_license",
                         cert_no="SP-1", valid_until="2027-01-01", valid_state="valid")),
                         ["编号", "有效期"])
        self.assertEqual(self.card_labels("person", None, list_item(2, "person_title_cert",
                         persons=["张三"], cert_no="PC-9")),
                         ["人员", "编号"])
        self.assertEqual(self.card_labels("performance", None, list_item(3, "contract",
                         client="某公司", amount=1000)),
                         ["客户/发包方", "金额 / 日期"])
        self.assertEqual(self.card_labels("finance", None, list_item(4, "audit_report", year="2024")),
                         ["年度"])
        self.assertEqual(self.card_labels("tax", None, list_item(5, "tax_credit", year="2024", rating="A")),
                         ["年度", "等级"])
        # types without extra business fields degrade to badge + name only
        self.assertEqual(self.card_labels(None, None, list_item(6, "other")), [])

    def test_browse_html_exposes_no_internal_terms(self):
        html = self.browse_html([list_item(2101, "business_license", cert_no="91xxxx")])
        for token in ("SHA256", "sha256", "MCP", "candidate_count", "候选", "OCR"):
            self.assertNotIn(token, html)

    # ------------------------------------------------------------- upload --

    def test_upload_whitelist_unchanged_and_camera_entry_optional(self):
        html = pages.upload_page(self.c, {})
        self.assertIn(UPLOAD_ACCEPT, html)
        self.assertIn("DOCX / XLSX 暂不支持，.doc 不支持", html)
        for bad in ('accept=".docx', 'accept=".xlsx', 'accept=".doc"', 'accept="image/*"'):
            self.assertNotIn(bad, html)
        self.assertIn('name="subtype"', html)
        self.assertIn('name="title"', html)
        self.assertIn('name="actor"', html)
        self.assertIn('camera-input', html)
        self.assertIn('accept="image/jpeg,image/png"', html)
        self.assertIn('capture="environment"', html)
        self.assertEqual(html.count('class="file-input camera-input"'), 1)

    def test_replace_mode_keeps_same_boundary_and_camera_entry(self):
        detail = {"material_id": 1, "title": "fixture"}
        with mock.patch.object(pages.Query, 'get_material', return_value=detail), \
                mock.patch.object(pages.display, 'display_from_detail', return_value='fixture'), \
                mock.patch.object(pages.service, 'system_ok', return_value=True):
            html = pages.upload_page(self.c, {"material": "1"})
        self.assertIn(UPLOAD_ACCEPT, html)
        self.assertIn('data-material="1"', html)
        self.assertIn('camera-input', html)
        self.assertIn("DOCX / XLSX 暂不支持，.doc 不支持", html)

    # --------------------------------------------------------------- css/js --

    def test_mobile_css_rules_and_fallbacks(self):
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        # phone components are hidden outside the phone breakpoint
        self.assertTrue(re.search(r'\.m-filter[^{]*\{[^}]*display:\s*none', css))
        self.assertTrue(re.search(r'\.m-cards[^{]*\{[^}]*display:\s*none', css))
        self.assertIn("@media (max-width: 640px)", css)
        mobile = css.split("@media (max-width: 640px)", 1)[1]
        self.assertIn(".browse-main .list-table { display: none; }", mobile)
        self.assertIn(".sidebar { display: none; }", mobile)
        self.assertIn("font-size: 16px", mobile)  # no focus zoom on phones
        self.assertIn("grid-template-columns: 1fr 1fr", mobile)  # detail action buttons
        # safe-area and dvh usage always carry a plain fallback
        self.assertIn("env(safe-area-inset-bottom", css)
        self.assertLess(css.index("max-height: 86vh"), css.index("100dvh"))

    def test_camera_wiring_is_the_only_new_js_and_lists_stay_server_rendered(self):
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("camera-input", js)
        self.assertIn("fb-camera-input", js)
        # no client-side list rebuilding: no fetch of browse/list endpoints
        self.assertNotIn('fetch("/browse', js)
        self.assertNotIn("XMLHttpRequest(\"GET\", \"/browse", js)

    # ---------------------------------------------------------------- p0.5 --

    def test_p05_post_routing_and_same_origin_guard_unchanged(self):
        routes = {p.pattern for method, p, _ in app.ROUTES if method == "POST"}
        self.assertEqual(routes, {"^/api/reextract$", "^/api/upload$", "^/api/review$", "^/api/feedback$", "^/api/doctor$", "^/api/project/(?P<action>create|update|archive|restore|material/link|material/unlink)$", "^/api/project/file$", "^/api/project/document/(?P<action>extract|retry|rebuild)$"})
        self.assertIn("post_allowed", inspect.getsource(app.Handler.do_POST))
        self.assertEqual(catalog.UPLOAD_EXTS, (".pdf", ".png", ".jpg", ".jpeg"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
