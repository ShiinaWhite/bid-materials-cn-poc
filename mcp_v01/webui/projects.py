"""Server-rendered project pages and thin manager RPC adapters."""
from pathlib import Path
import time
import json
import sqlite3

from jty_materials import projects as model
from jty_materials.manager import request
from jty_materials.project_documents import Documents
from jty_materials.document_extract import SUPPORTED
from . import render, service, uploads

esc = render.esc
LABELS = dict(zip(model.CATEGORIES, ('招标资料', '投标成果', '项目证明资料', '项目过程资料', '项目结果', '其他')))
EVENTS = {'project.create': '创建项目', 'project.update': '编辑项目', 'project.archive': '归档项目',
          'project.restore': '恢复项目', 'project.file.add': '上传项目文件',
          'project.document.extract': '整理文档内容', 'project.document.retry': '重试整理文档内容',
          'project.document.rebuild': '重新整理文档内容',
          'project.material.link': '引用素材', 'project.material.unlink': '取消素材引用',
          'project.material.autolink': '自动关联素材', 'project.material.discover': '识别素材候选'}


def layout(config, title, content):
    return render.layout(title, '<div class="project-center">' + content + '</div>',
                         active='projects', hide_search=True, sys_ok=service.system_ok(config))


def form(action, pid=None, mid=None, label='保存', body='', css=''):
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{value}">' for key, value in
                     [('project_id', pid), ('material_id', mid)] if value is not None)
    return (f'<form class="project-action {css}" data-action="{action}">{hidden}{body}'
            '<p class="project-error" role="alert"></p>'
            f'<button class="btn btn-primary" type="submit">{label}</button></form>')


def list_page(config, params):
    q, status = params.get('q', ''), params.get('status', 'active')
    result = model.Projects(config).list(q, status, params.get('page', 1))
    cards = []
    for p in result['items']:
        cards.append(f'''<article class="card project-card">
            <div class="project-card-head"><span class="muted small">{esc(p['project_code'])}</span>
            {render.chip('进行中' if p['status']=='active' else '已归档', 'chip-blue' if p['status']=='active' else 'chip-gray')}</div>
            <h2><a href="/project/{p['project_id']}">{esc(p['name'])}</a></h2>
            <dl><dt>招标人</dt><dd>{esc(p['tenderer'] or '—')}</dd><dt>标段</dt><dd>{esc(p['lot_name'] or '—')}</dd></dl>
            <p>项目文件 {p['file_count']} · 引用素材 {p['material_count']}</p>
            <p class="muted small">最近更新 {render.fmt_dt(p['updated_at'])}</p></article>''')
    filters = f'''<form class="project-filters" action="/projects" method="get">
        <label class="field"><span>关键词搜索</span><input class="input" type="search" name="q" maxlength="500" value="{esc(q)}" placeholder="项目名称、招标编号、招标人或标段"></label>
        <label class="field"><span>项目状态</span><select class="input" name="status">
        <option value="active" {'selected' if status=='active' else ''}>进行中</option>
        <option value="archived" {'selected' if status=='archived' else ''}>已归档</option></select></label>
        <button class="btn" type="submit">筛选</button></form>'''
    content = f'''<div class="project-heading"><div><h1>项目中心</h1><p class="muted">按项目保存投标原件，引用公司已有素材。</p></div>
        <a class="btn btn-primary" href="/projects/new">新建项目</a></div>{filters}
        <div class="project-grid">{''.join(cards) or '<p class="muted">暂无符合条件的项目。</p>'}</div>
        {render.pagination('/projects', result['page'], result['pages'], result['total'], q=q, status=status)}'''
    return layout(config, '项目中心', content)


def edit_page(config, pid=None):
    p = model.Projects(config).detail(pid) if pid else {}
    if p.get('status') == 'archived':
        raise ValueError('项目已归档，请先恢复项目')
    fields = ''.join(f'''<label class="field"><span>{label}{' *' if key=='name' else ''}</span>
        <input class="input" name="{key}" value="{esc(p.get(key))}" maxlength="500" {'required' if key=='name' else ''}></label>'''
        for key, label in [('name', '项目名称'), ('tender_no', '招标编号'), ('tenderer', '招标人'), ('lot_name', '标段名称')])
    # Manual creation is the advanced path (zero-fill normal path is dropping a
    # tender document onto the new-project page). Actor is injected server-side.
    body = fields
    title = '手动创建项目' if pid else '编辑项目'
    heading = '编辑项目' if pid else '手动创建项目（高级）'
    return layout(config, title, f'<p><a href="{f"/project/{pid}" if pid else "/projects"}">返回项目</a></p><section class="card project-form"><h1>{heading}</h1>' +
                  form('project.update' if pid else 'project.create', pid, body=body, label='保存项目') + '</section>')


def reverse_section(config, mid):
    rows = model.Projects(config).reverse(mid)
    if not rows:
        return ''
    links = ''.join(f'<li><a href="/project/{p["project_id"]}">{esc(p["name"])}</a>'
                    f'{" <span class=muted>（已归档）</span>" if p["status"]=="archived" else ""}</li>' for p in rows)
    return f'<section class="card"><h2>用于项目</h2><p>用于 {len(rows)} 个项目</p><ul class="plain-list">{links}</ul></section>'


def detail_page(config, pid, params):
    p = service.tolerant_read(model.Projects(config).detail, pid)
    active = p['status'] == 'active'
    controls = (f'<a class="btn" href="/project/{pid}/edit">编辑项目</a>'
                f'<a class="btn btn-primary" href="/project/{pid}/upload">上传项目文件</a>') if active else ''
    status = '进行中' if active else '已归档'
    content = f'''<p><a href="/projects">项目中心</a> / {esc(p['project_code'])}</p>
        <div class="project-heading"><div><h1>{esc(p['name'])}</h1><p>{render.chip(status, 'chip-blue' if active else 'chip-gray')}</p></div><div class="project-controls">{controls}</div></div>
        <section class="card"><dl class="project-meta"><dt>招标编号</dt><dd>{esc(p['tender_no'] or '—')}</dd>
        <dt>招标人</dt><dd>{esc(p['tenderer'] or '—')}</dd><dt>标段名称</dt><dd>{esc(p['lot_name'] or '—')}</dd></dl></section>
        <section class="card"><h2>项目文件</h2>'''
    documents = {d['project_file_id']: d for d in service.tolerant_read(Documents(config).status, pid)}
    content += f'<p><a class="btn" href="/project/{pid}/search">搜索项目文件</a></p><p class="muted small">历史文件仅供参考，内容不代表公司当前有效资质。</p>'
    for key, label in LABELS.items():
        files = []
        for f in p['files']:
            if f['category'] != key:
                continue
            url = f'/project-file/{pid}/{f["project_file_id"]}'
            view = f'<a class="btn" href="{url}" target="_blank">查看</a>' if '.'+f['extension'] in model.INLINE else ''
            doc = documents.get(f['project_file_id'], {})
            doc_state = document_state(doc)
            action = ''
            if active and f['extension'] in SUPPORTED and doc.get('status') not in ('queued','processing'):
                # Recovery entry only; the normal path extracts on upload.
                button = '提取内容' if doc.get('status') == 'pending' else '重新整理'
                action = form('project.document.extract', pid, label=button, body=f'<input type="hidden" name="project_file_id" value="{f["project_file_id"]}">')
            files.append(f'''<li class="project-row"><div><strong>{esc(f['filename'])}</strong>
                <p class="muted small">{render.fmt_bytes(f['size_bytes'])} · {render.fmt_dt(f['imported_at'])} · {esc(f['imported_by'])}</p>
                <p class="muted small" data-document-file="{f['project_file_id']}">文档内容：{esc(doc_state)}</p>
                {document_summary_html(doc)}</div>
                <div class="project-controls">{action}{view}<a class="btn" href="{url}?download=1">下载</a></div></li>''')
        content += f'<h3>{label}</h3><ul class="plain-list">{"".join(files) or "<li class=muted>暂无文件</li>"}</ul>'
    content += '</section>' + candidates_section(config, pid, active) + '<section class="card" id="references"><h2>引用素材</h2>'
    summarized = service.summarize(config, [r['material_id'] for r in p['references']])
    content += '<ul class="plain-list">'
    for m in summarized:
        button = form('project.material.unlink', pid, m['material_id'], '取消引用') if active else ''
        content += f'<li class="project-row"><div><a href="/material/{m["material_id"]}">{esc(m["display"])}</a>{render.type_badge(m["material_type"])}</div>{button}</li>'
    content += ('<li class="muted">尚未引用素材。</li>' if not summarized else '') + '</ul>'
    if active:
        q = params.get('q', '').strip()
        content += f'''<h3>搜索已有素材</h3><form action="/project/{pid}#references" class="project-filters" method="get">
            <label class="field"><span>素材关键词</span><input class="input" name="q" maxlength="500" required value="{esc(q)}" placeholder="项目经理、安全生产许可证、10kV"></label>
            <button class="btn" type="submit">搜索已有素材</button></form>'''
        if q:
            result = service.tolerant_read(service.list_materials, config, q=q, page=int(params.get('page', 1)))
            linked = {r['material_id'] for r in p['references']}
            content += '<ul class="plain-list">'
            for m in result['items']:
                button = '<span class="muted">已引用</span>' if m['material_id'] in linked else form('project.material.link', pid, m['material_id'], '引用到项目')
                content += f'''<li class="project-row"><div><a href="/material/{m['material_id']}">{esc(m['display'])}</a>
                    <p>{render.type_badge(m['material_type'])} {render.status_chips(m)}</p>
                    <p class="muted small">有效期：{esc('长期' if m['long_term'] else m['valid_until'] or '待完善')}</p></div>{button}</li>'''
            content += ('<li class="muted">没有找到匹配的素材。</li>' if not result['items'] else '') + '</ul>'
            content += render.pagination(f'/project/{pid}', result['page'], result['pages'], result['total'], q=q)
    content += '</section><section class="card"><h2>项目记录</h2><ul class="plain-list">'
    for row in p['audit']:
        detail = json.loads(row['detail_json'] or '{}')
        subject = detail.get('filename') or (f'素材 #{detail["material_id"]}' if detail.get('material_id') else '')
        content += f'<li>{esc(EVENTS.get(row["action"],row["action"]))} {esc(subject)} · {esc(row["actor"])} · {render.fmt_dt(row["at"])}</li>'
    content += '</ul></section><section class="card"><h2>项目归档</h2><p class="muted">归档后仍可查看原件与引用素材；恢复后可继续编辑。</p>'
    content += form('project.archive' if active else 'project.restore', pid,
                    label='归档项目' if active else '恢复项目') + '</section>'
    content += f'<div data-document-project="{pid}"></div>'
    return layout(config, p['name'], content)


def _fmt(n):
    return f'{n:,}'.replace(',', '，') if isinstance(n, int) else str(n)


def _summary(doc):
    try:
        value = json.loads(doc.get('summary_json') or '{}')
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def document_state(doc):
    """One honest, information-carrying line per document. This is the live
    label the WebUI polls; the structured card below it renders the same
    summary with full detail after each reload."""
    status = doc.get('status', 'pending')
    summary = _summary(doc)
    if doc.get('extension') not in SUPPORTED:
        return '暂不支持内容提取，可下载原件'
    if status == 'processing':
        kind, processed, total = doc.get('progress_kind'), doc.get('processed_units') or 0, doc.get('total_units')
        if kind == 'image' and total:
            return f'正在识别扫描图片 {processed} / {total}……'
        if kind == 'native_text':
            return '正文读取完成，正在识别扫描图片……'
        return '正在读取文档……'
    if status == 'queued':
        return '等待自动整理……'
    if status == 'pending':
        return '等待整理'
    if status == 'failed':
        return '内容提取失败：' + (doc.get('error') or '未知错误')[:120]
    native = summary.get('native_text') or {}
    images = summary.get('embedded_images')
    discovery = summary.get('discovery') or {}
    parts = []
    if native.get('units'):
        parts.append(f"✓ {_fmt(native.get('chars') or 0)} 字正文 · {native['units']} 个文本单元")
    if images is not None and images.get('total'):
        failed = images.get('failed') or 0
        note = f"⚠ {images['total']} 张图片（{failed} 张识别失败）" if failed else \
            f"✓ {images['total']} 张图片已识别（{_fmt(images.get('ocr_chars') or 0)} 字）"
        parts.append(note)
    if doc.get('index_status') == 'ready' and (native.get('units') or (images or {}).get('ocr_units')):
        parts.append('✓ 全文搜索已就绪')
    if discovery.get('candidates'):
        parts.append(f"发现素材候选 {discovery['candidates']}")
    problems = summary.get('problems') or []
    if status == 'partial' and problems:
        parts.append('⚠ ' + '；'.join(str(p) for p in problems)[:120])
    elif status == 'partial' and not parts:
        parts.append('部分内容未识别')
    return ' · '.join(parts) or '已保存原件'


def document_summary_html(doc):
    """Structured extraction card: every ✓/⚠ line the DB can honestly report."""
    summary = _summary(doc)
    rows = []

    def row(ok, text):
        rows.append(f'<li class="{"ok" if ok else "warn"}">{"✓" if ok else "⚠"} {text}</li>')

    native = summary.get('native_text') or {}
    if native.get('units'):
        row(True, f"{_fmt(native.get('chars') or 0)} 字正文，{native['units']} 个文本单元")
    pages = summary.get('pages') or {}
    if pages.get('native'):
        row(True, f"{sum(v for k, v in pages.items() if k != 'failed')} 页（原生 {pages.get('native', 0)}"
                  f" / 扫描识别 {pages.get('ocr', 0)} / 空白 {pages.get('empty', 0)}）")
    if pages.get('failed'):
        row(False, f"{pages['failed']} 页提取失败")
    images = summary.get('embedded_images')
    if images is not None:
        if images.get('total'):
            failed, skipped = images.get('failed') or 0, images.get('skipped') or 0
            if failed:
                row(False, f"扫描图片 {images['total']} 张：识别 {images.get('done', 0) - skipped}，"
                           f"失败 {failed}，跳过 {skipped}（装饰图/超限）")
            else:
                row(True, f"扫描图片 {images['total']} 张已识别（OCR 文字 {_fmt(images.get('ocr_chars') or 0)} 字）")
            if images.get('reused'):
                rows.append(f'<li class="muted small">复用上次识别结果 {images["reused"]} 张，仅重试失败项</li>')
        else:
            row(True, '未发现扫描图片')
    if doc.get('index_status') == 'ready':
        row(True, '全文搜索已就绪')
    elif doc.get('index_status') == 'failed':
        row(False, '搜索索引待修复，请重新整理')
    discovery = summary.get('discovery') or {}
    if discovery.get('candidates'):
        detail = []
        if discovery.get('exact_matches'):
            detail.append(f"{discovery['exact_matches']} 项已自动关联素材库")
        if discovery.get('new_candidates'):
            detail.append(f"{discovery['new_candidates']} 项新素材待确认")
        if discovery.get('possible_matches'):
            detail.append(f"{discovery['possible_matches']} 项疑似已有")
        row(True, f"发现素材候选 {discovery['candidates']}" + ('（' + '，'.join(detail) + '）' if detail else ''))
    problems = summary.get('problems') or []
    for problem in problems:
        row(False, esc(str(problem)))
    if doc.get('error') and doc.get('status') == 'failed':
        row(False, esc(doc['error'][:200]))
    if not rows:
        rows.append('<li class="muted">尚未整理内容</li>')
    return '<ul class="doc-summary">' + ''.join(rows) + '</ul>'


_MATCH_LABELS = dict(new_candidate=('新素材', 'chip-blue'), possible_match=('疑似已有', 'chip-amber'),
                     exact_match=('已关联素材库', 'chip-green'))
_LIFECYCLE_LABELS = dict(reusable='可复用', project_only='项目专用', transient='时效性', unknown='待判定')


def candidates_section(config, pid, active):
    rows = service.tolerant_read(Documents(config).candidates, pid)
    if not rows:
        return ''
    groups = {}
    for row in rows:
        subject = row['subject_name'] or row['subject_person'] or '主体待识别'
        groups.setdefault(subject, []).append(row)
    items = []
    for subject, group in groups.items():
        entries = []
        for row in group:
            label, kind = _MATCH_LABELS.get(row['match_status'], ('新素材', 'chip-blue'))
            identity = row['subject_uscc'] or row['identifier'] or row['subject_id_masked'] or ''
            location = f"第 {row['image_order']} 张扫描图片" if row['source_kind'] == 'embedded_image' \
                else f"正文单元 {row['source_unit_index']}"
            heading = row['nearest_heading'] or row['source_section'] or ''
            source = (f"{esc(row['filename'])} · {location}"
                      + (f"（{esc(heading[:40])}）" if heading else ''))
            link = f'<a href="/material/{row["matched_material_id"]}">查看素材 →</a>' \
                if row['matched_material_id'] else \
                f'<a href="{render.build_url(f"/project/{pid}/search", q=(row["subject_uscc"] or row["identifier"] or row["subject_name"] or row["candidate_type"]))}">查看识别文字 →</a>'
            entries.append(
                f'''<li class="candidate-row"><div>{render.type_badge(row['candidate_type'])}
                    {render.chip(label, kind)}{render.chip(_LIFECYCLE_LABELS.get(row['lifecycle'], '待判定'), 'chip-gray')}
                    <p class="muted small">{esc(identity)}{('（置信 ' + row['confidence'] + '）') if row['confidence'] != 'high' else ''}</p></div>
                    <p class="muted small">来源：{source}</p><div>{link}</div></li>''')
        uscc = next((r['subject_uscc'] for r in group if r['subject_uscc']), '')
        items.append(f'<li><strong>{esc(subject)}</strong>'
                     + (f'<p class="muted small">统一社会信用代码 {esc(uscc)}</p>' if uscc else '')
                     + f'<ul class="plain-list">{"".join(entries)}</ul></li>')
    return f'''<section class="card" id="candidates"><h2>发现素材（{len(rows)}）</h2>
        <p class="muted small">以下素材由文档内容自动识别，未经人工确认前不会写入素材库；“已关联素材库”表示确定性匹配（证件号码/信用代码精确一致）。</p>
        <ul class="plain-list candidate-groups">{''.join(items)}</ul></section>'''


def search_page(config, pid, params):
    p = service.tolerant_read(model.Projects(config).detail, pid)
    q = params.get('q', '').strip()
    content = f'''<p><a href="/project/{pid}">返回 {esc(p['name'])}</a></p><section class="card">
        <h1>搜索项目文件</h1><p class="muted">仅搜索当前项目。历史文件仅供参考，不代表公司当前有效资质。</p>
        <form class="project-filters" action="/project/{pid}/search" method="get">
        <label class="field"><span>文件内容关键词</span><input class="input" type="search" name="q" value="{esc(q)}" maxlength="200" required placeholder="项目经理、工期、付款方式"></label>
        <button class="btn btn-primary" type="submit">搜索</button></form>'''
    if q:
        try:
            result = service.tolerant_read(Documents(config).search, pid, q, params.get('page', 1))
            content += f'<p>找到 {result["total"]} 处内容</p><ul class="plain-list document-results">'
            for item in result['items']:
                if item['unit_type'] == 'embedded_image':
                    location = f"扫描图片 第 {item['image_order']} 张"
                    if item.get('section_path'):
                        location += f" · {item['section_path']}"
                elif item['page_no']:
                    location = f'第 {item["page_no"]} 页'
                else:
                    location = item['section_path']
                if item['sheet_name']:
                    location = f'Sheet：{item["sheet_name"]} · 第 {item["row_start"]}–{item["row_end"]} 行'
                url = f'/project-file/{pid}/{item["project_file_id"]}'
                if item['page_no']:
                    url += f'#page={item["page_no"]}'
                content += f'''<li><h2><a href="{url}" target="_blank">{esc(item['filename'])}</a></h2>
                    <p class="muted">{esc(location)}</p><p class="document-snippet">{esc(item['snippet'])}</p></li>'''
            content += '</ul>' + render.pagination(f'/project/{pid}/search', result['page'], result['pages'], result['total'], q=q)
        except (OSError, RuntimeError, sqlite3.Error):
            content += '<p role="alert">项目搜索暂不可用，请重新整理文件或请管理员重建项目搜索。原件仍可下载。</p>'
    content += '</section>'
    return layout(config, '搜索项目文件', content)


def new_page(config):
    """Zero-fill project creation: drop a tender document; fields, matching and
    bucket are automatic. Manual creation stays as an advanced fallback."""
    content = f'''<p><a href="/projects">返回项目</a></p>
    <section class="card project-form"><h1>新建项目</h1>
    <p class="muted">把招标文件放到这里：系统自动提取项目名称、招标编号、招标人与标段，匹配已有项目或自动新建，文件自动归入“招标资料”。</p>
    <form class="project-action intake-form" data-action="project.auto_intake">
      <span class="project-drop">点击选择或拖入招标文件到这里
      <input class="input" type="file" name="file" accept="{','.join(model.EXTENSIONS)}"></span>
      <p class="muted small">支持 PDF / Word / Excel / TXT / MD；单文件最大 200MB。</p>
      <p class="project-error" role="alert"></p>
      <button class="btn btn-primary" type="submit">自动建档</button>
    </form>
    <p class="muted small">如果系统找到多个可能的项目，会提示你手动确认，不会猜。</p></section>
    <details class="card info-collapse"><summary class="card-head"><h2>手动创建（高级）</h2></summary>'''
    content += form('project.create', body='<label class="field"><span>项目名称 *</span><input class="input" name="name" required maxlength="500"></label>'
                    '<label class="field"><span>招标编号</span><input class="input" name="tender_no" maxlength="500"></label>'
                    '<label class="field"><span>招标人</span><input class="input" name="tenderer" maxlength="500"></label>'
                    '<label class="field"><span>标段名称</span><input class="input" name="lot_name" maxlength="500"></label>', label='创建项目')
    content += '</details>'
    return layout(config, '新建项目', content)


def upload_page(config, pid):
    p = model.Projects(config).detail(pid)
    if p['status'] != 'active':
        raise ValueError('项目已归档，请先恢复项目')
    body = f'''<label class="field"><span>文件 *</span>
        <span class="project-drop">点击选择或拖拽项目文件到这里
        <input class="input" type="file" name="file" accept="{','.join(model.EXTENSIONS)}" required></span></label>
        <p class="muted small">支持 PDF、PNG/JPG、Word、Excel、PowerPoint、TXT、MD、ZIP；单文件最大 200MB。
        系统会根据文件名及可识别内容自动分类；上传后自动整理可读内容（含 Word 内嵌扫描图片 OCR）并识别素材候选。</p>'''
    return layout(config, '上传项目文件', f'<p><a href="/project/{pid}">返回 {esc(p["name"])}</a></p><section class="card project-form"><h1>上传项目文件</h1>' + form('project.file.add', pid, label='上传项目文件', body=body) + '</section>')


class FileJobs(uploads.UploadManager):
    """Independent project upload/intake action; never calls the material importer."""
    def _run(self, job, staged_path):
        with self.sem:
            try:
                job.state, job.step = 'running', ('正在解析招标文件' if job.mode == 'project.auto_intake' else '正在保存项目原件')
                job.result = request(self.c, job.mode, dict(job.params, source=str(staged_path), filename=job.filename), timeout=1800)
                job.state, job.step = 'done', '完成'
            except Exception as error:
                job.state, job.error = 'error', str(error)
            finally:
                job.finished_at = time.time()
                Path(staged_path).unlink(missing_ok=True)


def write_api(config, req, action):
    payload = req.json()
    if not isinstance(payload, dict):
        return 400, dict(error='请求格式无效')
    # Zero-fill identity: the workstation writes as local_webui unless a future
    # account system supplies a real user.
    payload.setdefault('actor', 'local_webui')
    try:
        result = request(config, action, payload)
        return 200, dict(ok=True, result=result, redirect=f'/project/{result["project_id"]}')
    except (ValueError, KeyError, TypeError) as e:
        return 400, dict(error=str(e))
    except (RuntimeError, TimeoutError, OSError) as e:
        return 503, dict(error=str(e))


def upload_api(config, jobs, req):
    q = req.query
    intake = q.get('intake') == '1'
    try:
        filename, _ = model.validate_filename(q.get('filename'))
        actor = model.text(q.get('actor'), '经办人') or 'local_webui'
        category = q.get('category')
        if not intake:
            pid = int(q.get('project_id', ''))
            if category is not None and category not in LABELS:
                raise ValueError('项目文件分类无效')
            p = model.Projects(config).detail(pid)
            if p['status'] != 'active':
                raise ValueError('项目已归档，请先恢复项目')
        size = int(req.headers.get('Content-Length', ''))
        if not 0 < size <= model.MAX_BYTES:
            return 413, dict(error='文件为空或超过 200MB 上限')
        staged, sha, size = uploads.stage_body(config, req.rfile, size)
        try:
            model.validate_signature(staged, Path(filename).suffix.lower())
            params = dict(filename=filename, actor=actor, expected_sha256=sha)
            if intake:
                job = jobs.submit('project.auto_intake', staged, filename, size, params)
            else:
                params.update(project_id=pid, category=category)
                job = jobs.submit('project.file.add', staged, filename, size, params)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return 202, dict(job_id=job.id)
    except (ValueError, KeyError, TypeError) as e:
        return 400, dict(error=str(e))
