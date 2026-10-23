"""Server-rendered project pages and thin manager RPC adapters."""
from pathlib import Path
import time
import json

from jty_materials import projects as model
from jty_materials.manager import request
from . import render, service, uploads

esc = render.esc
LABELS = dict(zip(model.CATEGORIES, ('招标资料', '投标成果', '项目证明资料', '项目过程资料', '项目结果', '其他')))
EVENTS = {'project.create': '创建项目', 'project.update': '编辑项目', 'project.archive': '归档项目',
          'project.restore': '恢复项目', 'project.file.add': '上传项目文件',
          'project.material.link': '引用素材', 'project.material.unlink': '取消素材引用'}


def layout(config, title, content):
    return render.layout(title, '<div class="project-center">' + content + '</div>',
                         active='projects', hide_search=True, sys_ok=service.system_ok(config))


def actor_field():
    return '<label class="field"><span>经办人 *</span><input class="input" name="actor" required maxlength="100" value="本机用户" autocomplete="name"></label>'


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
    # Basic project creation only requires the project name; actor is an explicit
    # local workstation identity until a future account system exists.
    body = fields + '<input type="hidden" name="actor" value="本机用户">'
    title = '编辑项目' if pid else '新建项目'
    return layout(config, title, f'<p><a href="{f"/project/{pid}" if pid else "/projects"}">返回项目</a></p><section class="card project-form"><h1>{title}</h1>' +
                  form('project.update' if pid else 'project.create', pid, body=body, label='保存项目') + '</section>')


def reverse_section(config, mid):
    rows = model.Projects(config).reverse(mid)
    if not rows:
        return ''
    links = ''.join(f'<li><a href="/project/{p["project_id"]}">{esc(p["name"])}</a>'
                    f'{" <span class=muted>（已归档）</span>" if p["status"]=="archived" else ""}</li>' for p in rows)
    return f'<section class="card"><h2>用于项目</h2><p>用于 {len(rows)} 个项目</p><ul class="plain-list">{links}</ul></section>'


def detail_page(config, pid, params):
    p = model.Projects(config).detail(pid)
    active = p['status'] == 'active'
    controls = (f'<a class="btn" href="/project/{pid}/edit">编辑项目</a>'
                f'<a class="btn btn-primary" href="/project/{pid}/upload">上传项目文件</a>') if active else ''
    status = '进行中' if active else '已归档'
    content = f'''<p><a href="/projects">项目中心</a> / {esc(p['project_code'])}</p>
        <div class="project-heading"><div><h1>{esc(p['name'])}</h1><p>{render.chip(status, 'chip-blue' if active else 'chip-gray')}</p></div><div class="project-controls">{controls}</div></div>
        <section class="card"><dl class="project-meta"><dt>招标编号</dt><dd>{esc(p['tender_no'] or '—')}</dd>
        <dt>招标人</dt><dd>{esc(p['tenderer'] or '—')}</dd><dt>标段名称</dt><dd>{esc(p['lot_name'] or '—')}</dd></dl></section>
        <section class="card"><h2>项目文件</h2>'''
    for key, label in LABELS.items():
        files = []
        for f in p['files']:
            if f['category'] != key:
                continue
            url = f'/project-file/{pid}/{f["project_file_id"]}'
            view = f'<a class="btn" href="{url}" target="_blank">查看</a>' if '.'+f['extension'] in model.INLINE else ''
            files.append(f'''<li class="project-row"><div><strong>{esc(f['filename'])}</strong>
                <p class="muted small">{render.fmt_bytes(f['size_bytes'])} · {render.fmt_dt(f['imported_at'])} · {esc(f['imported_by'])}</p></div>
                <div class="project-controls">{view}<a class="btn" href="{url}?download=1">下载</a></div></li>''')
        content += f'<h3>{label}</h3><ul class="plain-list">{"".join(files) or "<li class=muted>暂无文件</li>"}</ul>'
    content += '</section><section class="card" id="references"><h2>引用素材</h2>'
    summarized = service.summarize(config, [r['material_id'] for r in p['references']])
    content += '<ul class="plain-list">'
    for m in summarized:
        button = form('project.material.unlink', pid, m['material_id'], '取消引用',
                      '<input type="hidden" name="actor" value="本机用户">') if active else ''
        content += f'<li class="project-row"><div><a href="/material/{m["material_id"]}">{esc(m["display"])}</a>{render.type_badge(m["material_type"])}</div>{button}</li>'
    content += ('<li class="muted">尚未引用素材。</li>' if not summarized else '') + '</ul>'
    if active:
        q = params.get('q', '').strip()
        content += f'''<h3>搜索已有素材</h3><form action="/project/{pid}#references" class="project-filters" method="get">
            <label class="field"><span>素材关键词</span><input class="input" name="q" maxlength="500" required value="{esc(q)}" placeholder="项目经理、安全生产许可证、10kV"></label>
            <button class="btn" type="submit">搜索已有素材</button></form>'''
        if q:
            result = service.list_materials(config, q=q, page=int(params.get('page', 1)))
            linked = {r['material_id'] for r in p['references']}
            content += '<ul class="plain-list">'
            for m in result['items']:
                button = '<span class="muted">已引用</span>' if m['material_id'] in linked else form('project.material.link', pid, m['material_id'], '引用到项目', '<input type="hidden" name="actor" value="本机用户">')
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
    content += form('project.archive' if active else 'project.restore', pid, label='归档项目' if active else '恢复项目', body=actor_field()) + '</section>'
    return layout(config, p['name'], content)


def upload_page(config, pid):
    p = model.Projects(config).detail(pid)
    if p['status'] != 'active':
        raise ValueError('项目已归档，请先恢复项目')
    options = ''.join(f'<option value="{k}">{v}</option>' for k,v in LABELS.items())
    body = f'''<label class="field"><span>文件 *</span>
        <span class="project-drop">点击选择或拖拽项目文件到这里
        <input class="input" type="file" name="file" accept="{','.join(model.EXTENSIONS)}" required></span></label>
        <p class="muted small">支持 PDF、PNG/JPG、Word、Excel、PowerPoint、TXT、MD、ZIP；单文件最大 200MB。只保存原件，不识别、不解压。</p>
        <label class="field"><span>分类 *</span><select class="input" name="category">{options}</select></label>{actor_field()}'''
    return layout(config, '上传项目文件', f'<p><a href="/project/{pid}">返回 {esc(p["name"])}</a></p><section class="card project-form"><h1>上传项目文件</h1>' + form('project.file.add', pid, label='上传项目文件', body=body) + '</section>')


class FileJobs(uploads.UploadManager):
    """Independent project upload action; never calls the material importer."""
    def _run(self, job, staged_path):
        with self.sem:
            try:
                job.state, job.step = 'running', '正在保存项目原件'
                job.result = request(self.c, 'project.file.add', dict(job.params, source=str(staged_path), filename=job.filename), timeout=1800)
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
    try:
        result = request(config, action, payload)
        return 200, dict(ok=True, result=result, redirect=f'/project/{result["project_id"]}')
    except (ValueError, KeyError, TypeError) as e:
        return 400, dict(error=str(e))
    except (RuntimeError, TimeoutError, OSError) as e:
        return 503, dict(error=str(e))


def upload_api(config, jobs, req):
    q = req.query
    try:
        filename, _ = model.validate_filename(q.get('filename'))
        actor = model.text(q.get('actor'), '经办人', True, 100)
        pid = int(q.get('project_id', ''))
        if q.get('category') not in LABELS:
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
            job = jobs.submit('project.file.add', staged, filename, size,
                              dict(project_id=pid, category=q['category'], actor=actor, expected_sha256=sha))
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return 202, dict(job_id=job.id)
    except (ValueError, KeyError, TypeError) as e:
        return 400, dict(error=str(e))
