"""Server-side HTML rendering. No template engine: plain string building with
explicit escaping, so the whole UI stays dependency-free and auditable."""
import datetime as dt
import html

from . import WEBUI_VERSION
from . import catalog
from .display import valid_person_name


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def fmt_dt(iso):
    if not iso:
        return "—"
    try:
        moment = dt.datetime.fromisoformat(iso)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


def fmt_date(iso):
    if not iso:
        return "—"
    return str(iso)[:10]


def fmt_bytes(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.1f} {unit}"
        n /= 1024


def fmt_cents(cents):
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}¥{cents // 100:,}.{cents % 100:02d}"


ICONS = {
    "shield": '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z"/>',
    "doc": '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>',
    "person": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c1-4 3.5-6 7-6s6 2 7 6"/>',
    "bulb": '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 1 3.6 10.8c-.6.5-.6 1.2-.6 2.2h-6c0-1-.0-1.7-.6-2.2A6 6 0 0 1 12 3z"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/>',
    "stamp": '<path d="M9 4h6l-1 6h3l1 4H6l1-4h3L9 4z"/><path d="M5 18h14v3H5z"/>',
    "medal": '<circle cx="12" cy="9" r="5"/><path d="M8.5 13.5L7 21l5-2.5L17 21l-1.5-7.5"/>',
    "box": '<path d="M4 8l8-4 8 4v8l-8 4-8-4z"/><path d="M4 8l8 4 8-4M12 12v8"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20"/>',
    "upload": '<path d="M12 16V5M7 9l5-5 5 5"/><path d="M4 20h16"/>',
    "open": '<path d="M14 4h6v6"/><path d="M20 4L11 13"/><path d="M19 14v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "download": '<path d="M12 5v11M7 12l5 5 5-5"/><path d="M4 20h16"/>',
    "check": '<path d="M5 13l4 4L19 7"/>',
    "warn": '<path d="M12 4l9 15H3l9-15z"/><path d="M12 10v4M12 17.5v.5"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    "db": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    "edit": '<path d="M4 20l4-1L20 7l-3-3L5 16l-1 4z"/>',
    "history": '<path d="M4 12a8 8 0 1 1 3 6.2"/><path d="M4 13v-4h4"/><path d="M12 8v4l3 2"/>',
    "link": '<path d="M9 15l6-6"/><path d="M11 6l1-1a4 4 0 0 1 6 6l-1 1"/><path d="M13 18l-1 1a4 4 0 0 1-6-6l1-1"/>',
    "text": '<path d="M5 5h14M5 10h14M5 15h9M5 20h6"/>',
}


def icon(name, size=16):
    body = ICONS.get(name, ICONS["box"])
    return (f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


def chip(label, kind, title=None):
    tip = f' title="{esc(title)}"' if title else ""
    return f'<span class="chip {kind}"{tip}>{esc(label)}</span>'


def status_chips(item):
    """List status column: is the material in use, is anything expiring, and a
    soft hint about pending information — never a loud per-material verdict,
    never internal candidate counts."""
    chips = []
    label, kind = catalog.STATUS_LABELS.get(item["business_status"], ("在用", "chip-blue"))
    chips.append(chip(label, kind))
    if item.get("valid_state") == "expired":
        chips.append(chip("已过期", "chip-red"))
    if item["verification_status"] == "human_verified":
        chips.append(chip("信息完整", "chip-green-soft"))
    elif item.get("candidate_count"):
        chips.append('<span class="todo-hint">信息待完善</span>')
    return "".join(chips)


def person_cells(item):
    names = [n for n in (item["persons"] or ()) if valid_person_name(n)]
    if not names:
        return '<span class="muted">—</span>'
    shown = "、".join(esc(n) for n in names[:3])
    extra = f'<span class="muted"> 等{len(names)}人</span>' if len(names) > 3 else ""
    return f'<span class="chip chip-amber" title="人员信息待确认">{shown}</span>{extra}'


def type_badge(code):
    return f'<span class="type-badge">{esc(catalog.TYPE_LABELS.get(code, code))}</span>'


def build_url(path, **params):
    from urllib.parse import urlencode
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


def pagination(path, page, pages, total, **params):
    if pages <= 1:
        return f'<div class="pagebar"><span class="muted">共 {total} 项</span></div>'
    parts = []

    def link(p, label, disabled=False, current=False):
        if disabled:
            return f'<span class="page-cell disabled">{label}</span>'
        if current:
            return f'<span class="page-cell current">{label}</span>'
        return f'<a class="page-cell" href="{esc(build_url(path, page=p, **params))}">{label}</a>'

    parts.append(link(page - 1, "上一页", disabled=page <= 1))
    window = sorted({1, 2, page - 1, page, page + 1, pages - 1, pages})
    prev = 0
    for p in window:
        if p < 1 or p > pages:
            continue
        if p - prev > 1:
            parts.append('<span class="page-cell gap">…</span>')
        parts.append(link(p, str(p), current=(p == page)))
        prev = p
    parts.append(link(page + 1, "下一页", disabled=page >= pages))
    return (f'<div class="pagebar"><span class="muted">共 {total} 项 · 第 {page}/{pages} 页</span>'
            f'<div class="pages">{"".join(parts)}</div></div>')


def fact_legend():
    items = "".join(
        f'<span class="legend-item">{chip(label, kind)}<span>{esc(desc)}</span></span>'
        for kind, label, desc in catalog.FACT_LEGEND)
    return f'<div class="legend">{items}</div>'


def fact_legend_compact():
    """One-line legend for detail pages — states are explained, not displayed
    as a big block on every page."""
    parts = [f"{esc(label)}＝{esc(desc)}" for _, label, desc in catalog.FACT_LEGEND]
    return f'<p class="muted small legend-line">{" · ".join(parts)}</p>'


def feedback_widget():
    """Low-interference corner entry (v0.4.2): a fixed button plus one shared
    dialog. Wire-up lives in app.js; present on every page."""
    return f"""
<button id="feedback-fab" type="button" class="feedback-fab" title="提交使用问题或建议">{icon("bulb")}<span>反馈</span></button>
<dialog id="feedback-dialog" class="dialog feedback-dialog">
  <form id="feedback-form">
    <h3>{icon("bulb")} 提交反馈</h3>
    <p class="muted small" style="margin-top:-8px">使用中遇到的问题或建议都可以写在这里，欢迎附上截图。</p>
    <label class="field"><span>反馈内容（必填）</span>
      <textarea class="input" name="content" rows="4" maxlength="5000"
        placeholder="例如：这里的有效期显示好像不对。"></textarea></label>
    <div class="field"><span>截图（可选，PNG / JPG / WEBP，最多 5 张，每张不超过 20MB）</span>
      <div class="dropzone fb-drop" tabindex="0">{icon("upload", 22)}
        <div><strong>点击选择截图</strong><div class="muted small">或拖拽图片到这里，也可以直接 Ctrl+V 粘贴</div></div>
        <input type="file" class="file-input" multiple accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"></div>
      <div class="fb-files"></div>
    </div>
    <label class="field"><span>经办人（可选）</span>
      <input class="input" name="actor" maxlength="100" placeholder="例如：张三"></label>
    <div class="error-text hidden"></div>
    <div class="fb-result"></div>
    <div class="form-actions">
      <a class="muted small" href="/feedback" style="margin-right:auto">查看反馈记录</a>
      <button class="btn" type="button" data-close>关闭</button>
      <button class="btn btn-primary" type="submit">提交反馈</button>
    </div>
  </form>
</dialog>"""


def layout(title, content, active="", q="", sys_ok=True, hide_search=False):
    navs = [("首页", "/", "home"), ("资料分类", "/browse", "browse"),
            ("上传资料", "/upload", "upload")]
    nav_html = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for label, href, key in navs)
    if sys_ok:
        sys_html = ('<a class="sys-state sys-ok" href="/status" title="系统维护">'
                    '<span class="sys-dot"></span>系统运行正常</a>')
    else:
        sys_html = ('<a class="sys-state sys-busy" href="/status" title="系统维护">'
                    '<span class="sys-dot"></span>系统维护中，请稍候</a>')
    search_html = "" if hide_search else f"""
    <form class="topsearch" action="/browse" method="get" role="search">
      <span class="search-ico">{icon("search")}</span>
      <input type="search" name="q" placeholder="搜索：项目、人员、证书编号或任意文字" value="{esc(q)}" autocomplete="off">
    </form>"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 投标素材库</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="/"><span class="brand-mark">材</span><span class="brand-text">投标素材库</span></a>
    <nav class="nav">{nav_html}</nav>
    {search_html}
  </div>
</header>
<main class="container">
{content}
</main>
<footer class="footer">
  <span>本地投标素材库</span>
  <span class="footer-sep">·</span>
  {sys_html}
  <span class="footer-sep">·</span>
  <span>标“待完善”的信息建议使用前核对</span>
  <span class="footer-sep">·</span>
  <span class="muted small">WebUI v{WEBUI_VERSION}</span>
</footer>
{feedback_widget()}
<script src="/static/app.js" defer></script>
</body>
</html>"""


def error_page(message, hint=None, sys_ok=True):
    content = f"""<div class="card empty-state">
  <div class="empty-icon">{icon("warn", 28)}</div>
  <h2>出了一点问题</h2>
  <p>{esc(message)}</p>
  {f'<p class="muted">{esc(hint)}</p>' if hint else ''}
  <p><a class="btn btn-primary" href="/">返回首页</a></p>
</div>"""
    return layout("错误", content, sys_ok=sys_ok)
