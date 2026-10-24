"""Shared support for v0.7 zero-fill tests: synthetic fixtures, config setup,
mock providers. Everything is synthetic; short paths (runs/) are mandatory on
this Windows machine."""
import base64
import io
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jty_materials.common import Config, atomic_write  # noqa: E402
from jty_materials.store import Store  # noqa: E402
from jty_materials.normalize import uscc_complete  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# Synthetic identity (never a production entity).
NAME = '合成测试示例建筑工程有限公司'
CREDIT = uscc_complete('91JTY000000000X01')  # checksum-valid synthetic code
ESTABLISHED = '2001-02-03'
CREDIT_BAD = CREDIT[:-1] + ('0' if CREDIT[-1] != '0' else '1')  # same shape, broken check digit

LICENSE_TEXT = f'''营业执照
统一社会信用代码
{CREDIT}
名称
{NAME}
法定代表人 测试人员
成立日期 2001年02月03日
住所 测试地址
登记机关
2031年12月19日'''

TENDER_TEXT = f'''{NAME}招标文件
项目名称：合成测试市政道路工程项目
招标编号：JTYZB-2026-{uuid.uuid4().hex[:6].upper()}
招标人：合成测试城市建设开发有限公司
标段名称：一标段
投标人须知：本文件仅用于合成测试。'''


def make_license(path, credit=None, name=None, established='2001年02月03日'):
    image = Image.new('RGB', (1700, 1550), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 56)
    lines = ['营业执照', '统一社会信用代码', credit or CREDIT, '名称', name or NAME,
             '法定代表人 测试人员', '成立日期 ' + established, '住所 测试地址', '登记机关',
             '2031年12月19日']
    for i, line in enumerate(lines):
        draw.text((95, 65 + i * 130), line, font=font, fill='black')
    image.save(path)
    return path


def make_tender_pdf(path):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), '招标文件', fontname='china-s', fontsize=20)
    page.insert_text((72, 150), '项目名称：合成测试市政道路工程项目', fontname='china-s', fontsize=14)
    page.insert_text((72, 180), '招标编号：JTYZB-2026-SMOKE01', fontname='china-s', fontsize=14)
    page.insert_text((72, 210), '招标人：合成测试城市建设开发有限公司', fontname='china-s', fontsize=14)
    page.insert_text((72, 240), '标段名称：一标段', fontname='china-s', fontsize=14)
    doc.save(str(path))
    doc.close()
    return path


def fixture_config(label='zf'):
    base = Path(__file__).resolve().parents[1] / 'runs' / ('zero-fill-' + label + '-' + uuid.uuid4().hex[:6])
    raw = dict(repository_root='repo', db_path='repo/db/materials.db', state_dir='state', logs_dir='logs',
               backup_dir='backups', index_dir='index/dowse-index',
               umi_exe='rt/umi/Umi-OCR.exe', dowse_exe='rt/dowse/dowse.exe',
               umi_url='http://127.0.0.1:19999', fixture_only=True)
    base.mkdir(parents=True)
    atomic_write(base / 'config.json', json.dumps(raw))
    c = Config(base / 'config.json')
    c.materials.mkdir(parents=True)
    c.logs_dir.mkdir(parents=True)
    return base, c


def init_store(c):
    Store.initialize(c)
    return c


class MockVision:
    """Scriptable VisionProvider double: records calls, returns canned answers.
    No network, no secrets."""

    def __init__(self, classify_answer=None, extract_answers=None, fail=None):
        self.provider = 'mock'
        self.vision_model = 'mock-vision'
        self.classify_answer = classify_answer or {'document_type': 'business_license', 'confidence': 0.97}
        self.extract_answers = list(extract_answers or [])
        self.fail = fail  # ProviderError kind, raised on every call
        self.calls = []

    def _maybe_fail(self, purpose):
        self.calls.append(purpose)
        if self.fail:
            from jty_materials.providers import ProviderError
            raise ProviderError(self.fail, 'mock failure for ' + purpose)

    def classify(self, pages, type_labels, *, purpose):
        self._maybe_fail(purpose)
        from jty_materials.providers import ProviderError
        answer = dict(self.classify_answer)
        if answer.get('document_type') not in type_labels and answer['document_type'] != 'unknown':
            raise ProviderError('bad_response', 'mock answered unknown type')
        return answer['document_type'], answer.get('confidence', 0.5)

    def extract(self, pages, type_code, field_list, *, purpose):
        self._maybe_fail(purpose)
        answer = self.extract_answers.pop(0) if self.extract_answers else {'fields': {}}
        return {k: v for k, v in answer.items() if k in ('fields',)} or {'fields': {}}

    def complete(self, prompt, *, purpose, images=None, max_tokens=4000, json_mode=True):
        self._maybe_fail(purpose)
        return self.extract_answers.pop(0) if self.extract_answers else {}


class FakeOcr:
    """Canned OCR engine: writes the three derivative files without Umi."""

    def __init__(self, text):
        self.text = text

    def __call__(self, config):
        return self

    def start(self):
        pass

    def stop(self):
        pass

    def document(self, original):
        import uuid as _uuid
        from jty_materials.common import atomic_write, digest
        original = Path(original)
        raw_dir = original.parent / '.ocr-raw' / _uuid.uuid4().hex
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw = raw_dir / (original.name + '.ocr.txt')
        atomic_write(raw, self.text, immutable=True)
        atomic_write(raw.with_suffix('.jsonl'), json.dumps({'mock': True}), immutable=True)
        search = original.with_name(original.name + '.search.txt')
        atomic_write(search, self.text, immutable=True)
        return dict(original=str(original), raw=str(raw), jsonl=str(raw.with_suffix('.jsonl')),
                    search=str(search), task='mock', state='success', pages=1, processed=1,
                    raw_sha256=digest(raw),
                    options={'text_status': 'complete', 'errors': [], 'ocr_engine_rerun': False,
                             'method': 'mock'})


def write_provider_events(c):
    return c.logs_dir / 'events.jsonl'


def assert_no_secret(config, secret):
    """Secrets red line: the API key must never reach logs/events."""
    for path in (write_provider_events(config),):
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            assert secret not in text, 'SECRET LEAKED into ' + str(path)
