"""Page handlers, user-facing v0.3.

Reading-first detail pages: business information grouped the way people read
documents, with pending values shown as soft "待完善" hints. Field-by-field
maintenance is concentrated in the single "完善资料" entry. Display names come
from webui.display; internal titles/files stay untouched underneath.
"""
import json

from jty_materials.query import Query

from . import catalog, display, feedback, render, review, service
from .render import esc, icon

PAGE_SIZE = 20
EXAMPLES = ["10kV", "项目经理", "安全生产许可证", "2024 审计", "中标通知书", "施工劳务"]

OPENABLE = catalog.OPENABLE_EXTS


def _fmt_field(field, value):
    kind = review.field_kind(field)
    if value is None:
        return "—"
    if kind == "bool":
        return "是" if value else "否"
    if kind in ("amount", "signed_amount"):
        return render.fmt_cents(value)
    if kind == "grade":
        return f"{value} 级"
    if field == "report_type":
        return catalog.TYPE_LABELS.get(str(value), str(value))
    return str(value)


def _field_status(field, verified, inherited, candidates):
    """Merge to three user states: ok / pending / unset."""
    if field in verified:
        return "ok", verified[field]["value"], ""
    if field in inherited:
        return "ok", inherited[field]["value"], ""
    cands = candidates.get(field)
    if cands:
        return "pending", cands[0]["value"], ""
    return "unset", None, ""


def merge_fields(detail):
    """All known display fields with honest states: ok / pending / unset."""
    mtype = detail["material_type"]
    display_fields = list(catalog.TYPE_FIELDS.get(mtype, ["title"]))
    verified, inherited = detail["verified_facts"], detail.get("inherited_facts", {})
    candidates = detail.get("candidate_fields", {})
    extra = sorted(f for f in dict.fromkeys([*verified, *inherited, *candidates])
                   if f not in display_fields and not f.startswith("qualification:"))
    rows = {}
    for field in display_fields + extra:
        status, value, _ = _field_status(field, verified, inherited, candidates)
        rows[field] = {"status": status, "display": _fmt_field(field, value),
                       "candidates": [{"extracted_id": c["extracted_id"],
                                       "display": _fmt_field(field, c["value"])}
                                      for c in candidates.get(field, [])]}
    return rows


def _qual_rows(detail):
    verified, inherited, candidates = detail["verified_facts"], detail.get("inherited_facts", {}), detail.get("candidate_fields", {})
    names = []
    for section in (verified, inherited, candidates):
        for f in section:
            if f.startswith("qualification:"):
                name = f.split(":", 1)[1]
                if name not in names:
                    names.append(name)
    for item in detail.get("construction_qual_items", []):
        if item["qualification_name"] not in names:
            names.append(item["qualification_name"])
    rows = []
    for name in names:
        field = "qualification:" + name
        status, value, _ = _field_status(field, verified, inherited, candidates)
        cand_items, seen = [], set()
        for c in candidates.get(field, []):
            shown = str(c["value"])
            if shown not in seen:
                seen.add(shown)
                cand_items.append({"extracted_id": c["extracted_id"], "display": shown})
        rows.append({"name": name, "status": status, "display": _fmt_field(field, value),
                     "candidates": cand_items})
    return rows


def _fact_chip(status):
    label, kind = {"ok": catalog.FACT_OK, "pending": catalog.FACT_PENDING,
                   "unset": catalog.FACT_UNSET}[status]
    return render.chip(label, kind)


def _pending_tag():
    return '<span class="pending-tag" title="系统识别到信息，建议使用前核对">待完善</span>'


def _validity_line(detail):
    """The single validity status statement. The date value itself is shown in
    the info blocks below — this line never repeats it."""
    v = detail["validity"]
    iv = detail.get("inherited_validity", {})
    if v["term"] == "long_term":
        return ("长期有效", "chip-green", "已确认")
    if v["term"] == "dated":
        if v["status"] == "expired":
            return ("已过期", "chip-red", "请及时更新资料")
        if v["status"] == "not_yet_valid":
            return ("尚未生效", "chip-blue", "已确认")
        return ("有效期内", "chip-green", "已确认")
    if iv.get("status") in ("valid", "expired"):
        if iv["status"] == "expired":
            return ("已过期", "chip-red", "按此前记录的日期，请核对并更新")
        return ("有效期内", "chip-green", "沿用此前确认的日期，供参考")
    return ("有效期未记录", "chip-gray", "还没有录入这项资料的有效日期")


def _current_original(detail):
    return next((f for f in detail["files"] if f["role"] == "original" and f["is_current"]), None)


def _file_actions(detail, current, primary_class="btn btn-primary btn-sm"):
    if current is None:
        return '<span class="muted small">暂无文件</span>'
    ext = "." + current["relative_path"].rsplit(".", 1)[-1].lower()
    base = f"/files/{detail['material_id']}/{current['file_id']}"
    if ext in OPENABLE:
        return (f'<a class="{primary_class}" href="{base}" target="_blank">{icon("open")} 查看文件</a>'
                f' <a class="btn btn-sm" href="{base}?download=1">{icon("download")} 下载</a>')
    return f'<a class="{primary_class}" href="{base}?download=1">{icon("download")} 下载文件</a>'


# ---------------------------------------------------------------- pages ----

def home(config):
    stats = service.home_stats(config)
    examples = "".join(f'<a class="example-chip" href="/browse?q={esc(e)}">{esc(e)}</a>' for e in EXAMPLES)
    tiles = "".join(f"""
      <a class="tile" href="/browse?group={g['key']}">
        <span class="tile-icon">{icon(g['icon'], 20)}</span>
        <span class="tile-label">{esc(g['label'])}</span>
        <span class="tile-count">{stats['groups'][g['key']]}</span>
      </a>""" for g in catalog.GROUPS)
    latest_rows = "".join(f"""
      <li><a href="/material/{m['material_id']}">{esc(m.get('display') or m['title'])}</a>
        <span class="muted small">{esc(catalog.TYPE_LABELS.get(m['material_type'], m['material_type']))} · {render.fmt_dt(m['created_at'])}</span>
      </li>""" for m in stats["latest"])
    attention_rows = []
    if stats["expiring"]:
        attention_rows.append(f'<li>{icon("clock")} <span class="att-label">90 天内到期</span>'
                              f'<a href="/browse?validity=expiring">{stats["expiring"]} 项，点击查看</a></li>')
    if stats["expired"]:
        attention_rows.append(f'<li>{icon("warn")} <span class="att-label">已过期，需要更新</span>'
                              f'<a href="/browse?validity=expired">{stats["expired"]} 项，点击查看</a></li>')
    if not attention_rows:
        attention_rows.append('<li class="muted">近 90 天内没有需要处理的到期资料。</li>')
    content = f"""
<section class="hero">
  <h1>查找投标资料</h1>
  <form class="hero-search" action="/browse" method="get">
    <span class="search-ico">{icon('search', 20)}</span>
    <input type="search" name="q" placeholder="输入项目、人员姓名、证书编号或任意文字…" autocomplete="off" autofocus>
    <button class="btn btn-primary" type="submit">搜索</button>
  </form>
  <div class="examples"><span class="muted small">试试：</span>{examples}</div>
  <div class="hero-actions"><a class="btn" href="/upload">{icon('upload')} 上传资料</a></div>
</section>
<section class="tiles">{tiles}</section>
<div class="home-grid">
  <section class="card">
    <div class="card-head"><h2>{icon('clock')} 最近资料</h2>
      <a class="muted small" href="/browse">查看全部 →</a></div>
    <ul class="plain-list">{latest_rows}</ul>
  </section>
  <section class="card">
    <div class="card-head"><h2>{icon('warn')} 到期提醒</h2></div>
    <ul class="plain-list attention">{''.join(attention_rows)}</ul>
    <div class="legend-box">{render.fact_legend_compact()}</div>
  </section>
</div>"""
    return render.layout("首页", content, active="home", sys_ok=service.system_ok(config), hide_search=True)


def _scope_columns(group, type_code):
    if type_code:
        key = catalog.TYPE_GROUP.get(type_code, "default")
    else:
        key = group or "default"
    cols = catalog.LIST_COLUMNS.get(key, catalog.LIST_COLUMNS["default"])
    return cols, [catalog.COLUMN_HEADERS[c] for c in cols]


def _row_action(m):
    orig = m.get("original")
    if not orig:
        return ""
    ext = "." + (orig["extension"] or "")
    mid = m["material_id"]
    if ext in OPENABLE:
        return f'<a class="btn btn-sm" href="/files/{mid}/{orig["file_id"]}" target="_blank">{icon("open")} 查看</a>'
    return f'<a class="btn btn-sm" href="/files/{mid}/{orig["file_id"]}?download=1">{icon("download")} 下载</a>'


def _column_cell(col, m):
    mid = m["material_id"]
    if col == "name":
        name = m.get("display") or m["title"]
        return f'<a class="row-title" href="/material/{mid}">{esc(name)}</a>'
    if col == "type":
        return render.type_badge(m["material_type"])
    if col == "persons":
        return render.person_cells(m)
    if col == "no":
        return esc(m["cert_no"]) if m.get("cert_no") else '<span class="muted">—</span>'
    if col == "validity":
        # expiry only exists as a concept for certain types; blank otherwise
        if m["material_type"] not in catalog.HAS_VALIDITY:
            return ""
        if m.get("valid_until"):
            cls = "valid-expired" if m.get("valid_state") == "expired" else ""
            suffix = "（长期）" if m.get("long_term") else ""
            return f'<span class="{cls}">{render.fmt_date(m["valid_until"])}</span>{suffix}'
        return '<span class="muted">—</span>'
    if col == "status":
        return render.status_chips(m)
    if col == "client":
        return esc(m["client"]) if m.get("client") else '<span class="muted">—</span>'
    if col == "amount_date":
        if m.get("amount") is not None:
            return render.fmt_cents(m["amount"])
        if m.get("contract_date"):
            return render.fmt_date(m["contract_date"])
        return '<span class="muted">—</span>'
    if col == "year":
        return esc(m["year"]) if m.get("year") else '<span class="muted">—</span>'
    if col == "rating":
        return esc(m["rating"]) if m.get("rating") else '<span class="muted">—</span>'
    return ""


def _card_fields(cols, m):
    """Mobile card: the 1-2 business fields that matter most in this scope.
    Values reuse the exact desktop column cells, so formatting never forks;
    the type is already the card's badge, so that column is not repeated."""
    parts = []
    for col in cols:
        if col in ("name", "status", "type") or len(parts) >= 2:
            continue
        cell = _column_cell(col, m)
        if not cell.strip() or cell.strip() == '<span class="muted">—</span>':
            continue
        label = catalog.COLUMN_HEADERS.get(col, col)
        parts.append(f'<div class="m-field"><span class="m-field-label">{esc(label)}</span>'
                     f'<span class="m-field-value">{cell}</span></div>')
    return "".join(parts)


def _mobile_cards(cols, items):
    """Card list for phones, rendered server-side next to the desktop table
    (media queries decide which one is visible). No client-side rebuilding."""
    if not items:
        return ""
    cards = []
    for m in items:
        mid = m["material_id"]
        name = m.get("display") or m["title"]
        action = _row_action(m)
        foot = action or f'<a class="btn btn-sm" href="/material/{mid}">查看详情</a>'
        fields = _card_fields(cols, m)
        cards.append(f"""<div class="m-card" role="listitem">
      <div class="m-card-head">{render.type_badge(m["material_type"])}<span class="m-card-status">{render.status_chips(m)}</span></div>
      <a class="m-card-name" href="/material/{mid}">{esc(name)}</a>
      {f'<div class="m-card-fields">{fields}</div>' if fields else ''}
      <div class="m-card-foot">{foot}</div>
    </div>""")
    return f'<div class="m-cards" role="list">{"".join(cards)}</div>'


def browse(config, params):
    q = (params.get("q") or "").strip()
    person = (params.get("person") or "").strip()
    group = params.get("group") or None
    type_code = params.get("type") or None
    validity = params.get("validity") or None
    order = params.get("order") or "new"
    page = max(1, int(params.get("page") or 1))
    try:
        result = service.list_materials(config, type_code=type_code, group=group, q=q,
                                        person=person, validity=validity, page=page, page_size=PAGE_SIZE, order=order)
    except ValueError as e:
        return render.error_page(str(e), sys_ok=service.system_ok(config))
    from jty_materials.store import Store
    with Store(config, readonly=True) as s:
        counts, groups_count, total_all = service.type_counts(s.db)
    if q:
        scope_note = f"搜索“{q}” · 找到 {result['total']} 项"
    elif person:
        scope_note = f"人员包含“{person}” · 共 {result['total']} 项"
    elif type_code:
        scope_note = f"{catalog.TYPE_LABELS.get(type_code, type_code)} · 共 {result['total']} 项"
    elif group:
        scope_note = f"{catalog.GROUP_LABEL.get(group, group)} · 共 {result['total']} 项"
    else:
        scope_note = f"全部资料 · 共 {result['total']} 项"
    sidebar_items = [f'<a class="side-item {"active" if not group and not type_code else ""}" href="{render.build_url("/browse", q=q)}">全部资料 <span>{total_all}</span></a>']
    for g in catalog.GROUPS:
        active_group = group == g["key"] and not type_code
        sidebar_items.append(f'<a class="side-item {"active" if active_group else ""}" '
                             f'href="{render.build_url("/browse", group=g["key"], q=q)}">{esc(g["label"])} <span>{groups_count[g["key"]]}</span></a>')
        if active_group:
            for t in g["types"]:
                sidebar_items.append(f'<a class="side-item sub {"active" if type_code == t else ""}" '
                                     f'href="{render.build_url("/browse", type=t, q=q)}">{esc(catalog.TYPE_LABELS.get(t, t))} <span>{counts.get(t, 0)}</span></a>')
    sidebar_html = "".join(sidebar_items)
    cols, headers = _scope_columns(group, type_code)
    rows = []
    for m in result["items"]:
        cells = "".join(f'<td class="cell-{c}">{_column_cell(c, m)}</td>' for c in cols)
        action = _row_action(m)
        if action:
            cells += f'<td class="nowrap cell-action">{action}</td>'
        rows.append(f"<tr>{cells}</tr>")
    if not rows:
        table = (f'<div class="empty-state"><div class="empty-icon">{icon("search", 26)}</div>'
                 f'<h3>没有找到相关资料</h3><p class="muted">试试更短的关键词，或清除筛选条件。</p>'
                 f'<p><a class="btn" href="/browse">清除筛选</a></p></div>')
    else:
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        head += "<th></th>"
        table = f"""<table class="table list-table">
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(rows)}</tbody></table>"""
    cards_html = _mobile_cards(cols, result["items"])
    person_filter = f"""<input class="input" type="search" name="person" value="{esc(person)}" placeholder="按人员姓名筛选">""" \
        if (group == "person" or person) else ""
    # the validity filter only makes sense for types that have that concept
    scope_types = [type_code] if type_code else (next((g["types"] for g in catalog.GROUPS if g["key"] == group), None) if group else None)
    validity_applies = scope_types is None or any(t in catalog.HAS_VALIDITY for t in scope_types)
    validity_filter = f"""<select class="input" name="validity">
        <option value="">全部有效期</option>
        <option value="expiring" {"selected" if validity == "expiring" else ""}>90 天内到期</option>
        <option value="expired" {"selected" if validity == "expired" else ""}>已过期</option>
      </select>""" if validity_applies else ""
    toolbar_html = f"""<form class="toolbar" method="get" action="/browse">
      <input type="hidden" name="group" value="{esc(group or '')}">
      <input type="hidden" name="type" value="{esc(type_code or '')}">
      <input class="input grow" type="search" name="q" value="{esc(q)}" placeholder="在当前范围内搜索…">
      {person_filter}
      {validity_filter}
      <select class="input" name="order">
        <option value="new" {"selected" if order == "new" else ""}>最新保存在前</option>
        <option value="old" {"selected" if order == "old" else ""}>最早保存在前</option>
      </select>
      <button class="btn" type="submit">筛选</button>
    </form>"""
    # phone-only collapsible filter/category panel: same links and the same
    # GET form as the desktop sidebar/toolbar, so URL semantics never fork
    state_bits = []
    if q:
        state_bits.append(f"“{q}”")
    if person:
        state_bits.append(f"人员 {person}")
    if validity == "expiring":
        state_bits.append("90 天内到期")
    elif validity == "expired":
        state_bits.append("已过期")
    scope_label = (catalog.TYPE_LABELS.get(type_code) if type_code
                   else catalog.GROUP_LABEL.get(group, "全部分类") if group else "全部分类")
    filter_state = scope_label + (" · " + " · ".join(state_bits) if state_bits else "")
    clear_html = '<a class="m-filter-clear" href="/browse">清除全部筛选</a>' \
        if (q or person or validity or group or type_code) else ""
    mobile_panel = f"""<details class="m-filter">
      <summary class="m-filter-summary">{icon('search', 18)}<span class="m-filter-title">筛选与分类</span>
        <span class="m-filter-state">{esc(filter_state)}</span><span class="m-filter-caret" aria-hidden="true"></span></summary>
      <div class="m-filter-body">
        <div class="m-filter-cats">{sidebar_html}</div>
        {toolbar_html}
        {clear_html}
      </div>
    </details>"""
    pagebar = render.pagination(render.build_url('/browse', q=q, group=group, type=type_code, person=person, validity=validity, order=order),
                                result['page'], result['pages'], result['total'])
    content = f"""{mobile_panel}
{cards_html}
<div class="browse-layout">
  <aside class="sidebar">{sidebar_html}</aside>
  <section class="browse-main">
    <div class="browse-head"><h1>{scope_note}</h1></div>
    {toolbar_html}
    {table}
  </section>
</div>
{pagebar}"""
    return render.layout("资料分类", content, active="browse", q=q, sys_ok=service.system_ok(config))


def material_page(config, material_id):
    from . import projects as project_pages
    try:
        detail = Query(config).get_material(material_id)
    except ValueError:
        return render.error_page("没有找到这份资料。", "它可能已被移动或链接有误。", sys_ok=service.system_ok(config))
    detail = service.annotate_detail(config, detail)
    mid = detail["material_id"]
    mtype = detail["material_type"]
    type_label = catalog.TYPE_LABELS.get(mtype, mtype)
    group_key = catalog.TYPE_GROUP.get(mtype, "other")
    name = display.display_from_detail(detail)
    current = _current_original(detail)
    reviewable, quals = review.reviewable_fields(config, mtype)
    candidates_map = {}
    for field, cands in (detail.get("candidate_fields") or {}).items():
        if field in reviewable or field.startswith("qualification:"):
            items, seen = [], set()
            for c in cands:
                shown = _fmt_field(field, c["value"])
                if shown in seen:
                    continue
                seen.add(shown)
                items.append({"id": c["extracted_id"], "display": shown})
            candidates_map[field] = items
    dialog_data = json.dumps({"type": mtype,
                              "fields": [{"field": f, "label": catalog.FIELD_LABELS.get(f, f),
                                          "kind": review.field_kind(f),
                                          "hint": catalog.FIELD_HINT.get(f, "")} for f in reviewable],
                              "quals": quals, "candidates": candidates_map}, ensure_ascii=False).replace("</", "<\\/")
    # ---- hero: preview + friendly name + key facts + actions ----
    rows = merge_fields(detail)
    pending_count = sum(1 for r in rows.values() if r["status"] == "pending")
    pending_count += sum(1 for q in _qual_rows(detail) if q["status"] == "pending")
    has_validity = mtype in catalog.HAS_VALIDITY
    if has_validity:
        valid_label, valid_kind, valid_note = _validity_line(detail)
    else:
        valid_label, valid_kind, valid_note = None, "chip-gray", None
    chips = f'<span class="chip chip-blue">{esc(type_label)}</span>'
    if mid in service.TEST_MATERIAL_IDS:
        chips += '<span class="chip chip-gray" title="v0.1 端到端测试产物，已从普通浏览中隐藏">测试资料</span>'
    if detail["verification_status"] == "human_verified":
        chips += render.chip("信息完整", "chip-green-soft")
    else:
        chips += '<span class="chip chip-gray">信息待完善</span>'
    who = ""
    org_name = (detail.get("subject_org") or {}).get("name") if detail.get("subject_org") else None
    if org_name:
        who += f'<div class="who-line"><span class="who-label">企业</span>{esc(org_name)}</div>'
    if detail.get("persons"):
        names = "、".join(esc(p["name"]) for p in detail["persons"][:6])
        who += f'<div class="who-line"><span class="who-label">人员</span><span class="chip chip-amber" title="人员信息待确认">{names}</span></div>'
    preview = ""
    if current is not None:
        ext = "." + current["relative_path"].rsplit(".", 1)[-1].lower()
        base = f"/files/{mid}/{current['file_id']}"
        glyph = f'<div class="file-glyph big">{esc(ext[1:].upper())}</div>'
        if ext in OPENABLE:
            preview = (f'<a class="preview-box" href="{base}" target="_blank" title="查看文件">{glyph}'
                       f'<img src="/thumb/{mid}/{current["file_id"]}.png" alt="" '
                       f'onerror="this.remove()"></a>')
        else:
            preview = f'<a class="preview-box no-img" href="{base}?download=1" title="下载文件">{glyph}</a>'
    evidence_attrs = ""
    if current is not None:
        evidence_attrs = f' data-mid="{mid}" data-evidence-file="{current["file_id"]}" data-evidence-sha="{current["sha256"]}"'
    validity_html = ""
    if has_validity:
        validity_html = (f'<div class="validity-line"><span class="chip {valid_kind}">{esc(valid_label)}</span>'
                         f'<span class="muted small">{esc(valid_note)}</span></div>')
    actions = _file_actions(detail, current, primary_class="btn btn-primary") if current else ""
    actions += f' <a class="btn" href="/upload?material={mid}">{icon("upload")} 更新资料</a>'
    if reviewable:
        actions += f' <button class="btn" data-review>{icon("edit")} 完善资料</button>'
    hero = f"""<div class="detail-hero">
  {preview}
  <div class="detail-titlebox"{evidence_attrs}>
    <nav class="crumbs"><a href="/browse">资料分类</a> › <a href="/browse?group={group_key}">{esc(catalog.GROUP_LABEL.get(group_key, ''))}</a> › <span>{esc(type_label)}</span></nav>
    <h1>{esc(name)}</h1>
    <div class="detail-meta">{chips}</div>
    {who}
    {validity_html}
    <div class="detail-actions">{actions}</div>
  </div>
</div>"""
    # ---- reading blocks ----
    blocks_html = []
    for block_title, fields in catalog.TYPE_BLOCKS.get(mtype, [("基本信息", ["title"])]):
        info_rows = []
        for field in fields:
            state = rows.get(field, {"status": "unset"})
            if state["status"] == "unset":
                continue  # reading view only shows what is actually known
            tag = _pending_tag() if state["status"] == "pending" else ""
            info_rows.append(f'<div class="info-row"><span class="info-label">{esc(catalog.FIELD_LABELS.get(field, field))}</span>'
                             f'<span class="info-value">{esc(state["display"])}{tag}</span></div>')
        if not info_rows:
            continue
        blocks_html.append(f"""<div class="info-block">
          <h3>{esc(block_title)}</h3>
          {''.join(info_rows)}
        </div>""")
    qual_rows = _qual_rows(detail)
    if mtype == "construction_qual" or qual_rows:
        qrows = "".join(f"""<div class="info-row"><span class="info-label">{esc(q['name'])}</span>
            <span class="info-value">{esc(q['display'])}{_pending_tag() if q['status'] == 'pending' else ''}</span></div>"""
            for q in qual_rows if q["status"] != "unset")
        if qrows:
            blocks_html.append(f'<div class="info-block"><h3>资质范围</h3>{qrows}</div>')
    if blocks_html:
        pending_note = ('<p class="muted small info-note">标“待完善”的信息来自系统识别，建议使用前核对；'
                        '点击上方“完善资料”可逐项确认。</p>') if pending_count else ""
        blocks_card = f"""<section class="card">
      <div class="card-head"><h2>{icon('check')} 资料信息</h2></div>
      <div class="info-blocks">{''.join(blocks_html)}</div>
      {pending_note or ''}
      {render.fact_legend_compact()}
    </section>"""
    else:
        empty_hint = catalog.EMPTY_INFO_HINTS.get(mtype, "暂未录入这项资料的详细信息。")
        blocks_card = ('<section class="card"><div class="card-head"><h2>'
                       + icon('check') + ' 资料信息</h2></div>'
                       f'<p class="muted">{esc(empty_hint)}</p>'
                       + render.fact_legend_compact() + '</section>')
    # ---- persons ----
    person_section = ""
    known_persons = [p for p in detail.get("persons", []) if display.valid_person_name(p.get("name"))]
    if known_persons:
        pchips = "".join(f'<span class="chip chip-amber" title="人员信息待确认">{esc(p["name"])}'
                         f'{f"<span class=chip-sub>{esc(p["job_title"])}</span>" if p.get("job_title") else ""}</span>'
                         for p in known_persons)
        person_section = f"""<section class="card"><div class="card-head"><h2>{icon('person')} 相关人员</h2></div>
          <div class="chip-flow">{pchips}</div>
          <p class="muted small">以上人员由资料命名自动识别，尚未人工确认，请以实际为准。</p></section>"""
    # ---- relations ----
    # ---- relations: group same-named materials into one readable row ----
    relation_section = ""
    if detail.get("relations_view"):
        groups = {}
        order_keys = []
        for r in detail["relations_view"][:12]:
            key = r.get("other_display") or r["other_title"] or f"资料 {r['other_id']}"
            if key not in groups:
                groups[key] = []
                order_keys.append(key)
            groups[key].append(r)
        rrows = []
        for key in order_keys:
            items = groups[key]
            first = items[0]
            type_labels = "、".join(sorted({catalog.TYPE_LABELS.get(i["other_type"], i["other_type"]) for i in items}))
            if len(items) == 1:
                rrows.append(f"""<li><a href="/material/{first['other_id']}">{esc(key)}</a>
                    <span class="muted small">{esc(type_labels)}</span>
                    {render.chip("待整理", "chip-amber", "系统提示可能相关，待人工整理")}
                  </li>""")
            else:
                links = " · ".join(f'<a href="/material/{i["other_id"]}">第 {n + 1} 份</a>'
                                   for n, i in enumerate(items))
                rrows.append(f"""<li><span class="row-title">{esc(key)}</span>
                    <span class="muted small">共 {len(items)} 份{esc(type_labels)}资料（{links}），可能是同一证书的不同扫描</span>
                    {render.chip("待整理", "chip-amber", "系统提示可能相关，待人工整理")}
                  </li>""")
        relation_section = f"""<section class="card"><div class="card-head"><h2>{icon('link')} 关联资料</h2></div>
          <ul class="plain-list">{rrows and ''.join(rrows)}</ul></section>"""
    # ---- file history ----
    vrows = []
    for v in detail["versions"]:
        o = v["original"]
        ext = "." + o["relative_path"].rsplit(".", 1)[-1].lower()
        cur = render.chip("当前文件", "chip-blue") if v["is_current"] else render.chip("历史文件", "chip-gray")
        base = f"/files/{mid}/{o['file_id']}"
        action = (f'<a href="{base}" target="_blank">查看</a> · <a href="{base}?download=1">下载</a>') if ext in OPENABLE \
            else f'<a href="{base}?download=1">下载</a>'
        vrows.append(f"""<tr>
          <td>{render.fmt_dt(v['imported_at'])}</td>
          <td>{cur}</td>
          <td class="nowrap">{action}</td></tr>""")
    history_card = f"""<section class="card"><div class="card-head"><h2>{icon('history')} 历史文件</h2></div>
      <table class="table"><thead><tr><th>保存时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{''.join(vrows) or '<tr><td colspan="3" class="muted">暂无</td></tr>'}</tbody></table>
      <p class="muted small">更新资料后，旧文件会保留在这里，随时可以查看；当前使用的始终是最上面一份。</p>
    </section>"""
    content = f"""{hero}
<div class="detail-grid">
  <div class="detail-main">
    {blocks_card}
    {person_section}
    {relation_section}
    {project_pages.reverse_section(config, mid)}
    {history_card}
  </div>
</div>
<dialog id="review-dialog" class="dialog" data-config="{esc(dialog_data)}"></dialog>"""
    return render.layout(name[:40], content, active="browse", sys_ok=service.system_ok(config))


def _camera_entry():
    """Optional phone assist: capture a photo with the camera app. Purely a
    front-end file-picker variant — the file still goes through the same
    /api/upload with the same extension whitelist and size cap."""
    return (f'<div class="camera-entry"><button type="button" class="btn camera-btn">'
            f'{icon("upload", 18)} 拍照上传</button>'
            f'<input type="file" class="file-input camera-input" accept="image/jpeg,image/png" capture="environment"></div>')


def upload_page(config, params):
    replace_mid = params.get("material")
    options = ""
    for g in catalog.GROUPS:
        types = [t for t in g["types"] if t in catalog.UPLOAD_TYPES]
        if not types:
            continue
        opt = "".join(f'<option value="{t}">{esc(catalog.TYPE_LABELS[t])}</option>' for t in types)
        options += f'<optgroup label="{esc(g["label"])}">{opt}</optgroup>'
    if replace_mid:
        try:
            detail = Query(config).get_material(int(replace_mid))
        except (ValueError, TypeError):
            return render.error_page("没有找到要更新的资料。", sys_ok=service.system_ok(config))
        name = display.display_from_detail(detail)
        content = f"""<div class="page-narrow">
  <h1>更新资料</h1>
  <p class="muted">为 <a href="/material/{detail['material_id']}">{esc(name)}</a> 上传新的文件。
  旧文件会自动保留为历史文件，随时可以查看；之后请在新文件页面上核对其中的信息。</p>
  <div class="card"><form class="upload-form" data-mode="replace" data-material="{detail['material_id']}">
    <div class="dropzone" tabindex="0">{icon('upload', 26)}<div><strong>点击或拖入文件</strong><div class="muted small">支持 PDF / PNG / JPG / JPEG，单个文件不超过 200MB；DOCX / XLSX 暂不支持，.doc 不支持。</div></div>
      <input type="file" class="file-input" accept="{','.join(catalog.UPLOAD_EXTS)}"></div>
    {_camera_entry()}
    <label class="field"><span>经办人（必填）</span>
      <input class="input" name="actor" placeholder="例如：张三" required></label>
    <div class="upload-progress hidden"><div class="bar"><div class="bar-fill"></div></div><div class="progress-text muted small"></div></div>
    <div class="upload-result"></div>
    <div class="form-actions"><button class="btn btn-primary" type="submit" disabled>开始上传</button>
      <a class="btn" href="/material/{detail['material_id']}">返回</a></div>
  </form></div>
  <div class="card muted small">提交后系统会自动识别并保存。如果新文件与当前文件完全相同，会提示未做更改。</div>
</div>"""
        return render.layout("更新资料", content, active="upload", sys_ok=service.system_ok(config))
    content = f"""<div class="page-narrow">
  <h1>上传资料</h1>
  <p class="muted">选择资料类别、填写名称后上传。系统会自动识别资料内容并保存；识别出的信息为“待完善”状态，请之后在资料页面上核对确认。</p>
  <div class="card"><form class="upload-form" data-mode="import">
    <div class="dropzone" tabindex="0">{icon('upload', 26)}<div><strong>点击或拖入文件</strong><div class="muted small">支持 PDF / PNG / JPG / JPEG，单个文件不超过 200MB；DOCX / XLSX 暂不支持，.doc 不支持。</div></div>
      <input type="file" class="file-input" accept="{','.join(catalog.UPLOAD_EXTS)}"></div>
    {_camera_entry()}
    <label class="field"><span>资料类别（必选）</span>
      <select class="input" name="subtype" required><option value="">请选择类别…</option>{options}</select></label>
    <label class="field"><span>资料名称（必填，将显示在列表中）</span>
      <input class="input" name="title" placeholder="例如：2024年度审计报告" required></label>
    <label class="field"><span>经办人（必填）</span>
      <input class="input" name="actor" placeholder="例如：张三" required></label>
    <div class="upload-progress hidden"><div class="bar"><div class="bar-fill"></div></div><div class="progress-text muted small"></div></div>
    <div class="upload-result"></div>
    <div class="form-actions"><button class="btn btn-primary" type="submit" disabled>开始上传</button></div>
  </form></div>
</div>"""
    return render.layout("上传资料", content, active="upload", sys_ok=service.system_ok(config))


def feedback_page(config):
    """Simple read-only feedback log (v0.4.2) — time, actor, summary, status."""
    entries = list(reversed(feedback.store_for(config).index_entries()))
    rows = []
    for e in entries:
        status = e.get("status") or "open"
        label = feedback.STATUS_LABELS.get(status, status)
        kind = {"open": "chip-amber", "resolved": "chip-green", "wont_fix": "chip-gray"}.get(status, "chip-gray")
        atts = e.get("attachments") or []
        att_cell = ""
        if atts:
            links = " · ".join(f'<a href="/feedback-file/{esc(e["feedback_id"])}/{esc(a)}" '
                               f'target="_blank">{esc(a.rsplit(".", 1)[-1].upper())}</a>' for a in atts)
            att_cell = f'<td class="cell-chips">{links}</td>'
        else:
            att_cell = '<td class="muted">—</td>'
        page = e.get("page_path") or ""
        page_cell = f'<a href="{esc(page)}">{esc(page)}</a>' if page.startswith("/") else '<span class="muted">—</span>'
        rows.append(f"""<tr>
          <td class="nowrap mono small">{esc(e['feedback_id'])}</td>
          <td class="nowrap">{render.fmt_dt(e.get('created_at'))}</td>
          <td>{esc(e.get('actor') or '—')}</td>
          <td>{esc(e.get('summary') or '')}</td>
          <td>{page_cell}</td>
          <td>{att_cell}</td>
          <td>{render.chip(label, kind)}</td>
        </tr>""")
    table = f"""<table class="table list-table">
      <thead><tr><th>编号</th><th>时间</th><th>经办人</th><th>内容</th><th>页面</th><th>附件</th><th>状态</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="7" class="muted">还没有收到反馈。</td></tr>'}</tbody></table>"""
    content = f"""<div class="page-mid">
  <div class="browse-head"><h1>反馈记录</h1></div>
  <p class="muted">使用中提交的问题与建议会保存在这里，由维护人员定期处理。</p>
  {table}
</div>"""
    return render.layout("反馈记录", content, active="feedback", sys_ok=service.system_ok(config))


def status_page(config):
    """Maintenance page — not in the main navigation."""
    st = service.quick_status(config)
    mgr = st["manager"]
    running = bool(mgr.get("running"))
    watch = "运行中" if mgr.get("watch_pid") else "未运行"
    backup_rows = "".join(f"""<tr><td class="mono small">{esc(b['name'])}</td>
        <td>{render.fmt_dt(b['at']) if b['at'] else '—'}</td>
        <td>{'<span class="chip chip-green">完整</span>' if b['complete'] else '<span class="chip chip-red">不完整</span>'}</td></tr>"""
        for b in st["backups"])
    content = f"""<div class="page-mid">
  <h1>系统维护</h1>
  <p class="muted">本页面供维护人员使用。日常使用无需关注这里，页脚的“系统运行正常”即为日常状态提示。</p>
  <div class="status-grid">
    <section class="card"><div class="card-head"><h2>{icon('db')} 管理进程与索引</h2></div>
      <table class="table kv-table"><tbody>
        <tr><td class="fact-label">管理进程</td><td>{'<span class="chip chip-green">运行中</span>' if running else '<span class="chip chip-red">未运行</span>'}
          <span class="muted small">PID {mgr.get('pid', '—')}</span></td></tr>
        <tr><td class="fact-label">全文索引 watch</td><td>{watch}
          <span class="muted small">PID {mgr.get('watch_pid') or '—'}</span></td></tr>
        <tr><td class="fact-label">索引目录</td><td>{'<span class="chip chip-green">已建立</span>' if st['index_exists'] else '<span class="chip chip-red">缺失</span>'}</td></tr>
        <tr><td class="fact-label">维护状态</td><td>{'<span class="chip chip-amber">写入维护中</span>' if st['maintenance'] else '<span class="chip chip-green">空闲</span>'}</td></tr>
        <tr><td class="fact-label">数据代际</td><td>{st['generation']}</td></tr>
      </tbody></table>
    </section>
    <section class="card"><div class="card-head"><h2>{icon('shield')} 数据库</h2></div>
      <table class="table kv-table"><tbody>
        <tr><td class="fact-label">资料总数</td><td>{st['counts']['materials']}</td></tr>
        <tr><td class="fact-label">登记文件</td><td>{st['counts']['files']}</td></tr>
        <tr><td class="fact-label">确认记录</td><td>{st['counts']['verification_events']}</td></tr>
        <tr><td class="fact-label">Schema</td><td class="mono small">{esc(st['counts']['schema'])}</td></tr>
      </tbody></table>
    </section>
    <section class="card"><div class="card-head"><h2>{icon('history')} 最近备份</h2></div>
      <table class="table"><thead><tr><th>名称</th><th>时间</th><th>状态</th></tr></thead>
      <tbody>{backup_rows or '<tr><td colspan="3" class="muted">暂无备份</td></tr>'}</tbody></table>
      <p class="muted small">备份命令：<code>python -B mcp_v01/cli.py backup</code></p>
    </section>
  </div>
  <section class="card"><div class="card-head"><h2>{icon('check')} 深度体检</h2>
      <button class="btn btn-sm" id="doctor-run">运行完整体检（校验全部文件与索引）</button></div>
    <div id="doctor-result" class="muted">完整体检会逐文件校验完整性、数据库、投影与全文索引健康，可能需要几分钟。</div>
  </section>
  <section class="card"><div class="card-head"><h2>{icon('warn')} 数据质量概况</h2></div>
    <ul class="plain-list small">
      <li>待完善信息占大多数（第一阶段如实状态，非故障）</li>
      <li>人员归属：由命名自动识别，待确认</li>
      <li>资料关联：多为待整理状态</li>
      <li>材料关系与审计明细：见数据库 audit_log / material_relation</li>
    </ul>
  </section>
</div>"""
    return render.layout("系统维护", content, active="status", sys_ok=service.system_ok(config))
