"""Read-only, streaming extraction into disposable staging. No SQLite writes.

Optional parsers are imported only inside their format handler. OCR uses the
existing Umi engine, one necessary page at a time, in memory (no page images).
"""
import base64
from contextlib import closing
import json
from pathlib import Path
import zipfile

from .common import atomic_write, digest, normalize
from .ocr import Ocr

SUPPORTED = {'pdf', 'docx', 'xlsx', 'txt', 'md'}
MAX_TEXT = 100_000_000
MAX_UNITS = 50_000


class Unavailable(RuntimeError):
    pass


def check_office(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 20_000 or sum(e.file_size for e in entries) > 256 * 1024 * 1024:
            raise ValueError('Office container exceeds safe extraction budget')
        if any(e.filename.lower().endswith('vbaproject.bin') for e in entries):
            raise ValueError('macro-bearing Office container is not supported')


class PageOcr:
    def __init__(self, config):
        self.c, self.engine = config, None

    def recognize(self, data):
        if self.engine is None:
            self.c.logs_dir.mkdir(parents=True, exist_ok=True)
            engine = Ocr(self.c)
            try:
                engine.start()
            except Exception:
                engine.stop()
                raise
            self.engine = engine
        try:
            response = self.engine.request('/api/ocr', dict(
                base64=base64.b64encode(data).decode('ascii'), options={
                    'ocr.language': '简体中文', 'ocr.maxSideLen': 2000,
                    'tbpu.parser': 'multi_para', 'data.format': 'dict'}), timeout=120)
            if not isinstance(response, dict) or response.get('code') not in (100, 101):
                raise RuntimeError('Umi page OCR failed')
            blocks = response.get('data', []) if response['code'] == 100 else []
            if not isinstance(blocks, list) or any(not isinstance(b, dict) or not isinstance(b.get('text'), str) for b in blocks):
                raise RuntimeError('Umi returned malformed text blocks')
            return '\n'.join(b['text'] for b in blocks)
        except OSError:
            self.close()
            raise

    def close(self):
        if self.engine is not None:
            engine, self.engine = self.engine, None
            engine.stop()


def native_sufficient(text):
    visible = ''.join(c for c in text if not c.isspace())
    return len(visible) >= 30 and visible.count('\ufffd') <= len(visible) * .02


def pdf_units(path, progress, ocr):
    try:
        import pymupdf
    except ImportError as error:
        raise Unavailable('PDF extraction unavailable: install optional PyMuPDF locally') from error
    with pymupdf.open(path) as document:
        if document.is_encrypted:
            raise ValueError('encrypted PDF is unsupported without a separately supplied decrypted original')
        total = len(document)
        progress(0, total, 'page')
        for i, page in enumerate(document):
            text, method, error = '', 'failed', None
            try:
                text = page.get_text('text', sort=True)
                if native_sufficient(text):
                    method = 'native'
                else:
                    # At most 2000 px on the long edge; discard the pixmap after
                    # this page. Retain scant native text if OCR fails.
                    zoom = min(2.5, 2000 / max(page.rect.width, page.rect.height))
                    scanned = ocr.recognize(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes('png'))
                    text = scanned if scanned.strip() else text
                    method = 'ocr' if text.strip() else 'empty'
            except Exception as failure:
                error = str(failure)[:1000]
            yield dict(unit_type='page', page_no=i + 1, text_raw=text, page_status=method, error=error)
            progress(i + 1, total, 'page')


def heading_level(paragraph):
    # python-docx safely handles XML/relationships. Follow basedOn styles too,
    # including localized/custom headings that carry outlineLvl.
    from docx.oxml.ns import qn
    props = paragraph._p.pPr
    outline = props.find(qn('w:outlineLvl')) if props is not None else None
    if outline is not None:
        return int(outline.get(qn('w:val'))) + 1
    style, seen = paragraph.style, set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        if style.name and style.name.lower().startswith('heading '):
            try:
                return int(style.name.split()[-1])
            except ValueError:
                pass
        props = style.element.pPr
        outline = props.find(qn('w:outlineLvl')) if props is not None else None
        if outline is not None:
            return int(outline.get(qn('w:val'))) + 1
        style = style.base_style
    return None


def docx_units(path, progress, warnings):
    try:
        from docx import Document
        from docx.text.paragraph import Paragraph
    except ImportError as error:
        raise Unavailable('DOCX extraction unavailable: install python-docx') from error
    check_office(path)
    document = Document(path)
    with zipfile.ZipFile(path) as archive:
        if any(n.startswith('word/media/') for n in archive.namelist()):
            warnings.append('embedded_image_ocr=deferred; only native DOCX text extracted')
    headings, group, length = {}, [], 0

    def unit():
        return dict(unit_type='paragraph_group', section_path=' → '.join(headings[k] for k in sorted(headings)) or '正文',
                    text_raw='\n'.join(group), page_status='native')

    for block in document.iter_inner_content():
        level = heading_level(block) if isinstance(block, Paragraph) else None
        if level is not None and level <= 9:
            if group:
                yield unit()
                group, length = [], 0
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = block.text
        lines = [block.text] if isinstance(block, Paragraph) else ['\t'.join(c.text for c in row.cells) for row in block.rows]
        for line in lines:
            if not line.strip():
                continue
            if group and length + len(line) > 6000:
                yield unit()
                group, length = [], 0
            group.append(line)
            length += len(line)
    if group:
        yield unit()


def xlsx_units(path, progress):
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError as error:
        raise Unavailable('XLSX extraction unavailable: install openpyxl') from error
    check_office(path)
    # No links, VBA, Excel runtime, DDE or recalculation. Formula text is inert.
    with closing(openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)) as formulas, \
            closing(openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)) as cached:
        total = len(formulas.worksheets)
        progress(0, total, 'sheet')
        for si, sheet in enumerate(formulas.worksheets):
            if (sheet.max_row or 0) * (sheet.max_column or 0) > 5_000_000:
                raise ValueError('XLSX declared cell region exceeds safe extraction budget')
            rows, start, end, width, length = [], None, None, 1, 0

            def unit():
                return dict(unit_type='sheet_chunk', sheet_name=sheet.title, row_start=start, row_end=end,
                            cell_range=f'A{start}:{get_column_letter(width)}{end}', text_raw='\n'.join(rows), page_status='native')

            for ri, (row, values) in enumerate(zip(sheet.iter_rows(values_only=True), cached[sheet.title].iter_rows(values_only=True)), 1):
                cells = []
                for ci, value in enumerate(row, 1):
                    if value is None:
                        continue
                    text = str(value)
                    if isinstance(value, str) and value.startswith('=') and ci <= len(values) and values[ci-1] is not None:
                        text += ' [cached: ' + str(values[ci-1]) + ']'
                    cells.append(f'{get_column_letter(ci)}{ri}: {text}')
                if not cells:
                    if rows:
                        yield unit()
                    rows, start, end, width, length = [], None, None, 1, 0
                    continue
                line = '\t'.join(cells)
                if rows and (len(rows) >= 40 or length + len(line) > 6000):
                    yield unit()
                    rows, start, end, width, length = [], None, None, 1, 0
                if start is None:
                    start = ri
                end, width = ri, max(width, len(row))
                rows.append(line)
                length += len(line)
            if rows:
                yield unit()
            progress(si + 1, total, 'sheet')


def extract(source, stage, config, progress=lambda *args: None, ocr_factory=PageOcr):
    """Stage bounded JSONL. Source is resolved by manager, never from Web input."""
    path = Path(source['path'])
    if not path.is_file() or path.stat().st_size != source['size_bytes'] or digest(path) != source['sha256']:
        raise ValueError('source size/SHA mismatch; original must be repaired before extraction')
    ext, warnings = source['extension'], []
    if ext not in SUPPORTED:
        return dict(status='unsupported', error='stored original supported; content extraction deferred', unit_count=0, text_length=0)
    ocr = ocr_factory(config)
    count, length, failed = 0, 0, 0
    result = None
    try:
        if ext == 'pdf':
            units = pdf_units(path, progress, ocr)
        elif ext == 'docx':
            units = docx_units(path, progress, warnings)
        elif ext == 'xlsx':
            units = xlsx_units(path, progress)
        else:
            def text_units():
                with path.open(encoding='utf-8-sig') as stream:
                    group = []
                    for line in stream:
                        group.append(line.rstrip('\n'))
                        if len(group) >= 40:
                            yield dict(unit_type='paragraph_group', section_path='正文', text_raw='\n'.join(group), page_status='native')
                            group = []
                    if group:
                        yield dict(unit_type='paragraph_group', section_path='正文', text_raw='\n'.join(group), page_status='native')
            units = text_units()
        with stage.open('w', encoding='utf-8', newline='\n') as output:
            for item in units:
                count += 1
                length += len(item['text_raw'])
                failed += item.get('page_status') == 'failed'
                if count > MAX_UNITS or length > MAX_TEXT:
                    raise ValueError('document exceeds safe text/unit extraction budget')
                item['text_norm'] = normalize(item['text_raw'])
                # Compute and fsync text derivatives outside the repository
                # lock. Only manager may rename/publish this staging folder.
                atomic_write(stage.parent/'projections'/f'unit-{count:06d}.txt', item['text_norm'])
                output.write(json.dumps(item, ensure_ascii=False) + '\n')
        if digest(path) != source['sha256']:
            raise ValueError('source SHA changed during extraction')
        result = dict(status='partial' if failed or warnings else 'complete', unit_count=count, text_length=length,
                    error='; '.join(warnings + ([f'{failed} pages failed'] if failed else [])) or None)
    finally:
        try:
            ocr.close()
        except Exception as error:
            config.log('project.document.ocr_cleanup_failed', error=str(error))
            if result is not None:
                result['status'] = 'partial'
                result['error'] = '; '.join(filter(None, [result.get('error'), 'OCR cleanup failed: '+str(error)[:1000]]))
    return result
