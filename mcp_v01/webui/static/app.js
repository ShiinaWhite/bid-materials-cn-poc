/* 投标素材库 WebUI 交互：上传流程、人工核验对话框、OCR 懒加载、深度体检 */
"use strict";

function $(sel, root) { return (root || document).querySelector(sel); }
function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

/* Project files use their own endpoint and job registry, without OCR/import. */
document.addEventListener("DOMContentLoaded", () => {
  const project = document.querySelector('[data-document-project]');
  if (project) {
    let wasBusy = false;
    const poll = async () => {
      try {
        const response = await fetch(`/api/project/${project.dataset.documentProject}/documents`);
        if (!response.ok) throw new Error('status unavailable');
        const data = await response.json();
        let busy = false;
        for (const item of data.items) {
          const node = document.querySelector(`[data-document-file="${item.project_file_id}"]`);
          if (node) node.textContent = '文档内容：' + item.label;
          busy ||= ['queued', 'processing'].includes(item.status);
        }
        if (wasBusy && !busy) { window.location.reload(); return; }
        wasBusy = busy;
        if (busy) setTimeout(poll, 1500);
      } catch (_) {
        // A transient read conflict should not leave a stale processing screen.
        setTimeout(poll, 2500);
      }
    };
    poll();
  }
  for (const form of document.querySelectorAll(".project-action")) {
    const fileInput = form.querySelector('input[type="file"]');
    const drop = form.querySelector(".project-drop");
    if (drop && fileInput) {
      drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("dragging"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
      drop.addEventListener("drop", event => {
        event.preventDefault(); drop.classList.remove("dragging");
        if (event.dataTransfer.files.length === 1) fileInput.files = event.dataTransfer.files;
      });
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const error = form.querySelector(".project-error");
      const button = form.querySelector('button[type="submit"]');
      const label = button.textContent;
      error.textContent = "";
      button.disabled = true;
      try {
        const fields = Object.fromEntries(new FormData(form));
        let response;
        if (form.dataset.action === "project.file.add") {
          const file = fileInput.files[0];
          if (!file || file.size === 0 || file.size > 200 * 1024 * 1024) throw new Error("请选择非空文件，单文件最大 200MB");
          delete fields.file;
          fields.filename = file.name;
          button.textContent = "正在上传…";
          response = await fetch("/api/project/file?" + new URLSearchParams(fields), {method: "POST", body: file});
          const received = await response.json();
          if (!response.ok) throw new Error(received.error || "上传失败");
          button.textContent = "正在保存…";
          for (;;) {
            await new Promise(resolve => setTimeout(resolve, 700));
            const poll = await fetch("/api/project/jobs/" + received.job_id);
            const job = await poll.json();
            if (!poll.ok || job.state === "error") throw new Error(job.error || "上传任务读取失败");
            if (job.state === "done") {
              window.location.assign("/project/" + job.result.project_id);
              return;
            }
          }
        } else {
          const action = form.dataset.action.slice("project.".length).replaceAll(".", "/");
          response = await fetch("/api/project/" + action, {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(fields)
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "保存失败");
          window.location.assign(result.redirect);
        }
      } catch (failure) {
        error.textContent = failure.message;
        button.disabled = false;
        button.textContent = label;
      }
    });
  }
});

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children || []) node.append(child);
  return node;
}

/* ---------------- 上传 / 换版 ---------------- */

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function initUploadForm(form) {
  const drop = $(".dropzone", form);
  const fileInput = $(".file-input", form);
  const submit = $('button[type="submit"]', form);
  const progress = $(".upload-progress", form);
  const barFill = $(".bar-fill", form);
  const progressText = $(".progress-text", form);
  const resultBox = $(".upload-result", form);
  const mode = form.dataset.mode;
  let file = null;

  const setFile = (f) => {
    file = f;
    if (f) {
      drop.querySelector("strong").textContent = f.name;
      drop.querySelector(".muted.small").textContent = fmtBytes(f.size) + " · 点击可重新选择";
      if (!$("input[name=title]", form)) {
        const title = document.querySelector('input[name="title"]');
        if (title && !title.value) title.value = f.name.replace(/\.[^.]+$/, "");
      }
    }
    submit.disabled = !f;
  };
  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("drag"); if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
  // phone-only camera assist: same form state, same upload path
  const cameraBtn = $(".camera-btn", form);
  const cameraInput = $(".camera-input", form);
  if (cameraBtn && cameraInput) {
    cameraBtn.addEventListener("click", () => cameraInput.click());
    cameraInput.addEventListener("change", () => setFile(cameraInput.files[0]));
  }

  const actorInput = form.querySelector('input[name="actor"]');
  if (actorInput) {
    actorInput.value = localStorage.getItem("jty-actor") || "";
    actorInput.addEventListener("change", () => localStorage.setItem("jty-actor", actorInput.value.trim()));
  }

  const showResult = (kind, nodes) => {
    resultBox.innerHTML = "";
    resultBox.append(el("div", { class: kind }, nodes));
  };

  const poll = (job) => {
    const startedAt = Date.now();
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/jobs/" + job.job_id);
        const data = await res.json();
        const secs = Math.round((Date.now() - startedAt) / 1000);
        if (data.state === "done") {
          clearInterval(timer);
          finish(data);
        } else if (data.state === "error") {
          clearInterval(timer);
          showResult("err-box", [document.createTextNode("处理失败：" + (data.error || "未知错误"))]);
          submit.disabled = false;
          submit.textContent = mode === "import" ? "重新上传" : "重新提交";
        } else {
          progressText.textContent = (data.step || "正在处理") + "… 已进行 " + secs + " 秒，请勿关闭页面。";
        }
      } catch (e) { /* transient network error: keep polling */ }
    }, 1500);
  };

  const finish = (data) => {
    progress.classList.add("hidden");
    const r = data.result || {};
    if (r.duplicate) {
      showResult("warn-box", [
        el("strong", {}, [document.createTextNode("这份文件已经保存过：")]),
        document.createTextNode("为避免重复，未再次保存。"),
        el("br"), el("a", { href: "/material/" + r.material_id }, [document.createTextNode("查看已有资料 →")]),
      ]);
    } else if (r.unchanged) {
      showResult("warn-box", [
        document.createTextNode("新文件与当前使用的文件相同，未做更改。"),
      ]);
    } else if (data.state === "done" && r.material_id) {
      const nodes = [
        el("strong", {}, [document.createTextNode("上传完成。")]),
      ];
      if (mode === "import") {
        nodes.push(el("br"));
        nodes.push(document.createTextNode("资料里的关键信息目前是“待确认”状态，请打开核对确认后使用。"));
      }
      nodes.push(el("br"));
      nodes.push(el("a", { href: "/material/" + r.material_id }, [document.createTextNode("查看资料 →")]));
      showResult("ok-box", nodes);
    }
    submit.disabled = false;
    submit.textContent = mode === "import" ? "继续上传下一份" : "再次提交";
  };

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!file) return;
    const actor = (actorInput && actorInput.value.trim()) || "";
    if (!actor) { alert("请填写操作人（将写入审计记录）"); return; }
    const subtypeEl = form.querySelector('select[name="subtype"]');
    const titleEl = form.querySelector('input[name="title"]');
    if (mode === "import" && subtypeEl && !subtypeEl.value) { alert("请选择材料类别"); return; }

    submit.disabled = true;
    progress.classList.remove("hidden");
    resultBox.innerHTML = "";
    barFill.style.width = "0%";
    progressText.textContent = "正在上传文件…";

    const xhr = new XMLHttpRequest();
    // Metadata goes in the query string: XHR headers must be ByteString and
    // silently throw on Chinese values; URLSearchParams percent-encodes UTF-8.
    const meta = new URLSearchParams({ mode: mode, actor: actor });
    if (subtypeEl) meta.set("subtype", subtypeEl.value);
    if (titleEl) meta.set("title", titleEl.value.trim());
    meta.set("filename", file.name);
    if (form.dataset.material) meta.set("material", form.dataset.material);
    xhr.open("POST", "/api/upload?" + meta.toString());
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        barFill.style.width = pct + "%";
        progressText.textContent = pct < 100 ? "正在上传文件… " + pct + "%" : "上传完成，正在进入安全处理链路…";
      }
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) {}
      if (xhr.status === 202) {
        barFill.style.width = "100%";
        poll(data);
      } else {
        progress.classList.add("hidden");
        showResult("err-box", [document.createTextNode("上传被拒绝：" + (data.error || ("HTTP " + xhr.status)))]);
        submit.disabled = false;
      }
    };
    xhr.onerror = () => {
      progress.classList.add("hidden");
      showResult("err-box", [document.createTextNode("网络错误，请重试。")]);
      submit.disabled = false;
    };
    xhr.send(file);
  });
}

/* ---------------- 人工核验对话框 ---------------- */

function valueInput(field) {
  const kind = field.kind;
  if (kind === "date") return el("input", { class: "input", type: "date" });
  if (kind === "year" || kind === "int") return el("input", { class: "input", type: "number", step: "1", min: kind === "year" ? "1900" : "0", max: kind === "year" ? "2200" : "" });
  if (kind === "grade") return el("input", { class: "input", type: "number", step: "1", min: "1", max: "5" });
  if (kind === "amount" || kind === "signed_amount") return el("input", { class: "input", type: "text", placeholder: kind === "signed_amount" ? "可负，例如 -12345.67" : "例如 1234567.89" });
  if (kind === "bool") {
    return el("select", { class: "input" }, [
      el("option", { value: "true" }, [document.createTextNode("是（长期有效）")]),
      el("option", { value: "false" }, [document.createTextNode("否（按日期）")]),
    ]);
  }
  return el("input", { class: "input", type: "text", maxlength: "4000" });
}

function initReextract() {
  const button = $('[data-reextract]');
  if (!button) return;
  const box = $('.detail-titlebox');
  const status = $('[data-reextract-status]');
  button.addEventListener('click', async () => {
    const actor = prompt('不重新上传文件，只重新整理当前文件中的可识别信息。\n请填写经办人（将写入审计记录）：');
    if (!actor || !actor.trim()) return;
    button.disabled = true;
    status.textContent = '正在重新识别…';
    try {
      const response = await fetch('/api/reextract', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({material_id: Number(box.dataset.mid), actor: actor.trim()})});
      let job = await response.json();
      if (!response.ok) throw new Error(job.error || '无法开始重新识别');
      while (job.state !== 'done' && job.state !== 'error') {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const poll = await fetch('/api/jobs/' + job.job_id);
        job = await poll.json();
        if (!poll.ok) throw new Error(job.error || '无法读取识别进度');
        status.textContent = job.step || '正在重新识别…';
      }
      if (job.state === 'error') throw new Error(job.error);
      location.reload();
    } catch (error) {
      status.textContent = '重新识别失败：' + error.message;
      button.disabled = false;
    }
  });
}

function initReviewDialog() {
  const dialog = $("#review-dialog");
  if (!dialog) return;
  let config = {};
  try { config = JSON.parse(dialog.dataset.config || "{}"); } catch (e) {}
  const detailBox = $(".detail-titlebox");
  const evidenceFile = detailBox && detailBox.dataset.evidenceFile;
  const evidenceSha = detailBox && detailBox.dataset.evidenceSha;
  const materialId = detailBox && detailBox.dataset.mid;

  const open = (preset) => {
    if (!evidenceFile) {
      alert("这份资料当前没有可用文件，无法完善信息。");
      return;
    }
    // deep link /programmatic preset: <dialog data-autofield="certificate_no">
    if (!preset || !preset.field) {
      const auto = dialog.dataset.autofield;
      if (auto) preset = { field: auto, value: "", candidate: "" };
      dialog.dataset.autofield = "";
    }
    dialog.innerHTML = "";
    const title = el("h3", {}, [document.createTextNode("完善资料")]);
    const fieldSelect = el("select", { class: "input" });
    for (const f of config.fields || []) {
      fieldSelect.append(el("option", { value: f.field }, [document.createTextNode(f.label)]));
    }
    if ((config.quals || []).length) {
      const group = el("optgroup", { label: "已有资质项" });
      for (const q of config.quals) group.append(el("option", { value: "qualification:" + q }, [document.createTextNode("资质：" + q)]));
      const custom = el("optgroup", { label: "新增资质项" });
      custom.append(el("option", { value: "__new_qual__" }, [document.createTextNode("＋ 新增资质项…")]));
      fieldSelect.append(group); fieldSelect.append(custom);
    }
    const customName = el("input", { class: "input hidden", placeholder: "新资质类别名称，例如：输变电工程专业承包" });
    const candBox = el("div", { class: "cand-pick hidden" });
    const valueWrap = el("div", { class: "field" });
    const hint = el("div", { class: "muted small", style: "margin:-8px 0 10px" });
    const actor = el("input", { class: "input", value: localStorage.getItem("jty-actor") || "", placeholder: "例如：张三" });
    const reason = el("input", { class: "input", placeholder: "例如：与资料内容一致" });
    const errorLine = el("div", { class: "error-text hidden" });
    const ok = el("button", { class: "btn btn-primary", type: "submit" }, [document.createTextNode("确认")]);
    const cancel = el("button", { class: "btn", type: "button" }, [document.createTextNode("取消")]);

    const rebuildValue = (keepValue) => {
      const fieldName = fieldSelect.value;
      if (fieldName === "__new_qual__") return;
      const meta = (config.fields || []).find((f) => f.field === fieldName) ||
        { kind: fieldName.startsWith("qualification:") ? "text" : "text", hint: "" };
      const input = valueInput(meta);
      input.name = "value";
      if (keepValue) input.value = keepValue;
      valueWrap.innerHTML = "";
      valueWrap.append(el("span", {}, [document.createTextNode("确认的内容（清空并提交表示取消这项信息）")]), input);
      hint.textContent = meta.hint || "";
      candBox.innerHTML = "";
      const cands = (config.candidates || {})[fieldName] || [];
      if (cands.length) {
        candBox.classList.remove("hidden");
        candBox.append(el("span", { class: "muted small" }, [document.createTextNode("识别到的参考值，点击填入：")]));
        for (const c of cands.slice(0, 6)) {
          candBox.append(el("button", {
            class: "btn btn-tiny", type: "button",
            onclick: () => {
              const input = valueWrap.querySelector("[name=value]");
              if (input) input.value = c.display;
              input && input.dispatchEvent(new Event("change"));
              candBox.dataset.candidate = c.id;
            },
          }, [document.createTextNode(c.display)]));
          if (c.evidence && c.file_id) {
            candBox.append(el('details', {}, [
              el('summary', {class: 'muted small'}, [document.createTextNode('查看识别依据')]),
              el('div', {class: 'muted small', style: 'white-space:pre-wrap;overflow-wrap:anywhere'}, [document.createTextNode(c.evidence)]),
              el('a', {href: '/files/' + materialId + '/' + c.file_id, target: '_blank'}, [document.createTextNode('查看当前原件')]),
            ]));
          }
        }
        candBox.dataset.candidate = "";
      } else {
        candBox.classList.add("hidden");
        candBox.dataset.candidate = "";
      }
    };
    fieldSelect.addEventListener("change", () => {
      customName.classList.toggle("hidden", fieldSelect.value !== "__new_qual__");
      rebuildValue("");
    });
    if (preset && preset.field) {
      if (config.fields.some((f) => f.field === preset.field)) fieldSelect.value = preset.field;
      else if (preset.field.startsWith("qualification:")) {
        const name = preset.field.split(":")[1];
        const option = fieldSelect.querySelector('option[value="qualification:' + name + '"]');
        if (option) fieldSelect.value = preset.field;
        else if ((config.quals || []).includes(name)) fieldSelect.value = preset.field;
        else { fieldSelect.value = "__new_qual__"; customName.value = name; customName.classList.remove("hidden"); }
      }
    }
    rebuildValue(preset && preset.value ? preset.value : "");
    if (preset && preset.candidate) candBox.dataset.candidate = preset.candidate;

    const form = el("form", {}, [
      title,
      el("label", { class: "field" }, [el("span", {}, [document.createTextNode("信息项")]), fieldSelect, customName]),
      candBox,
      valueWrap, hint,
      el("label", { class: "field" }, [el("span", {}, [document.createTextNode("经办人")]), actor]),
      el("label", { class: "field" }, [el("span", {}, [document.createTextNode("确认说明（必填）")]), reason]),
      errorLine,
      el("div", { class: "form-actions" }, [cancel, ok]),
    ]);
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      let field = fieldSelect.value;
      if (field === "__new_qual__") {
        const name = customName.value.trim();
        if (!name) { errorLine.textContent = "请填写新资质类别名称"; errorLine.classList.remove("hidden"); return; }
        field = "qualification:" + name;
      }
      const input = form.querySelector("[name=value]");
      const payload = {
        material_id: Number(materialId),
        field: field,
        value: input ? input.value.trim() : "",
        actor: actor.value.trim(),
        reason: reason.value.trim(),
        evidence_file_id: Number(evidenceFile),
        evidence_sha256: evidenceSha,
      };
      if (candBox.dataset.candidate) payload.candidate_id = Number(candBox.dataset.candidate);
      if (!payload.actor) { errorLine.textContent = "请填写经办人"; errorLine.classList.remove("hidden"); return; }
      if (!payload.reason) { errorLine.textContent = "请填写确认说明"; errorLine.classList.remove("hidden"); return; }
      ok.disabled = true;
      errorLine.classList.add("hidden");
      try {
        localStorage.setItem("jty-actor", payload.actor);
        const res = await fetch("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        if (res.ok) { location.reload(); return; }
        errorLine.textContent = data.error || ("提交失败（HTTP " + res.status + "）");
        if (data.retry) errorLine.textContent += "（系统维护中，请稍后重试）";
        errorLine.classList.remove("hidden");
      } catch (err) {
        errorLine.textContent = "网络错误，请重试";
        errorLine.classList.remove("hidden");
      }
      ok.disabled = false;
    });
    dialog.append(form);
    dialog.showModal();
  };

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-review]");
    if (!btn) return;
    open({ field: btn.dataset.field || "", value: btn.dataset.value || "", candidate: btn.dataset.candidate || "" });
  });
}

/* ---------------- OCR 全文懒加载 ---------------- */
function initOcrDetails() {
  for (const details of $all(".ocr-details[data-text-url]")) {
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "1";
      const body = $(".ocr-body", details);
      try {
        const res = await fetch(details.dataset.textUrl);
        const data = await res.json();
        if (data.state === "ok" || data.state === "truncated") {
          body.classList.remove("muted");
          body.textContent = data.text;
          if (data.note) {
            body.after(el("p", { class: "muted small" }, [document.createTextNode(data.note)]));
          }
        } else {
          body.textContent = data.note || "当前版本没有可用检索文本。";
        }
      } catch (e) {
        body.textContent = "加载失败，请稍后重试。";
        details.dataset.loaded = "";
      }
    });
  }
}

/* ---------------- 深度体检 ---------------- */
function initDoctor() {
  const run = $("#doctor-run");
  const box = $("#doctor-result");
  if (!run || !box) return;
  const renderResult = (data) => {
    if (data.state === "running") { box.textContent = "体检进行中，请稍候…"; return false; }
    if (data.state === "done" && data.result) {
      const r = data.result;
      if (r.error) { box.textContent = "体检失败：" + r.error; return true; }
      const okAll = r.integrity === "ok" && !r.foreign_key_errors && !r.file_errors && !r.project_file_errors && !r.projection_errors && !r.project_document_errors;
      box.innerHTML = "";
      const line = (label, value, good) => {
        const chip = document.createElement("span");
        chip.className = "chip " + (good ? "chip-green" : "chip-red");
        chip.textContent = good ? "正常" : "异常";
        const div = document.createElement("div");
        div.style.margin = "4px 0";
        div.append(document.createTextNode(label + "：" + value + " "), chip);
        return div;
      };
      box.append(
        line("数据库完整性", r.integrity, r.integrity === "ok"),
        line("外键错误", String(r.foreign_key_errors), !r.foreign_key_errors),
        line("素材文件校验", r.files_checked + " 个文件，错误 " + r.file_errors, !r.file_errors),
        line("项目文件校验", r.project_files_checked + " 个文件，错误 " + r.project_file_errors, !r.project_file_errors),
        line("项目文档内容", String(r.project_documents?.documents ?? 0) + " 份文档", !r.project_document_errors),
        line("检索投影校验", r.projections_checked + " 个投影，错误 " + r.projection_errors, !r.projection_errors),
        line("全文索引文档数", String(r.index_docs), Number(r.index_docs) > 0),
      );
      const summary = document.createElement("p");
      summary.className = okAll ? "ok" : "";
      summary.style.marginTop = "8px";
      summary.textContent = okAll ? "全部通过。" : "存在异常，请暂缓写入并联系维护人员。";
      box.append(summary);
      return true;
    }
    box.textContent = "尚未运行过体检。";
    return true;
  };
  run.addEventListener("click", async () => {
    run.disabled = true;
    box.textContent = "体检进行中，请稍候…（可能需要几分钟）";
    await fetch("/api/doctor", { method: "POST" });
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/doctor");
        const data = await res.json();
        if (renderResult(data)) { clearInterval(timer); run.disabled = false; }
      } catch (e) {}
    }, 3000);
  });
  fetch("/api/doctor").then((r) => r.json()).then(renderResult).catch(() => {});
}

/* ---------------- 反馈入口（v0.4.2） ---------------- */

const FB_MAX_FILES = 5;
const FB_ACCEPT = ["image/png", "image/jpeg", "image/webp"];

function initFeedback() {
  const fab = $("#feedback-fab");
  const dlg = $("#feedback-dialog");
  if (!fab || !dlg) return;
  const form = $("#feedback-form", dlg);
  const drop = $(".fb-drop", dlg);
  const fileInput = $(".file-input", dlg);
  const listBox = $(".fb-files", dlg);
  const resultBox = $(".fb-result", dlg);
  const errLine = $(".error-text", dlg);
  const contentInput = form.querySelector('textarea[name="content"]');
  const actorInput = form.querySelector('input[name="actor"]');
  const submitBtn = form.querySelector('button[type="submit"]');
  let files = [];

  actorInput.value = localStorage.getItem("jty-actor") || "";

  const showErr = (msg) => {
    errLine.textContent = msg || "";
    errLine.classList.toggle("hidden", !msg);
  };

  const renderFiles = () => {
    listBox.innerHTML = "";
    for (const [i, f] of files.entries()) {
      const row = el("div", { class: "fb-file" }, [
        el("span", { class: "fb-file-name" }, [document.createTextNode(f.name || "剪贴板截图")]),
        el("span", { class: "muted small" }, [document.createTextNode(fmtBytes(f.size))]),
        el("button", {
          class: "btn btn-tiny", type: "button", title: "移除",
          onclick: () => { files.splice(i, 1); renderFiles(); },
        }, [document.createTextNode("移除")]),
      ]);
      listBox.append(row);
    }
    drop.classList.toggle("fb-full", files.length >= FB_MAX_FILES);
  };

  const addFiles = (incoming) => {
    for (const f of incoming || []) {
      if (files.length >= FB_MAX_FILES) { showErr("每条反馈最多 5 张截图"); break; }
      const okType = FB_ACCEPT.includes(f.type) ||
        /\.(png|jpe?g|webp)$/i.test(f.name || "");
      if (!okType) { showErr("只支持 PNG / JPG / WEBP 图片截图"); continue; }
      if (f.size > 20 * 1024 * 1024) { showErr("单个截图超过 20MB 上限"); continue; }
      showErr("");
      files.push(f);
    }
    renderFiles();
  };

  const setOpen = (open) => {
    if (open) {
      showErr("");
      dlg.showModal();
      contentInput.focus();
    } else {
      dlg.close();
    }
  };

  fab.addEventListener("click", () => setOpen(true));
  form.querySelector("[data-close]").addEventListener("click", () => dlg.close());

  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    addFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  // phone-only camera assist: same validation/limit path as picked files
  const cameraBtn = $(".fb-camera-btn", dlg);
  const cameraInput = $(".fb-camera-input", dlg);
  if (cameraBtn && cameraInput) {
    cameraBtn.addEventListener("click", () => cameraInput.click());
    cameraInput.addEventListener("change", () => { addFiles(cameraInput.files); cameraInput.value = ""; });
  }

  // Ctrl+V paste works anywhere while the dialog is open
  document.addEventListener("paste", (e) => {
    if (!dlg.open) return;
    const pasted = Array.from((e.clipboardData || {}).files || [])
      .filter((f) => !f.type || FB_ACCEPT.includes(f.type));
    if (!pasted.length) return;
    e.preventDefault();
    addFiles(pasted);
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const content = contentInput.value.trim();
    if (!content) { showErr("请填写反馈内容"); contentInput.focus(); return; }
    const actor = actorInput.value.trim();
    submitBtn.disabled = true;
    showErr("");
    resultBox.innerHTML = "";

    const data = new FormData();
    data.append("content", content);
    data.append("actor", actor);
    data.append("page_path", location.pathname + location.search);
    data.append("page_title", document.title);
    data.append("page_url", location.href);
    for (const f of files) data.append("attachment", f, f.name || "clipboard.png");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/feedback");
    xhr.onload = () => {
      submitBtn.disabled = false;
      let resp = {};
      try { resp = JSON.parse(xhr.responseText); } catch (err) {}
      if (xhr.status === 200 && resp.ok) {
        if (actor) localStorage.setItem("jty-actor", actor);
        contentInput.value = "";
        files = [];
        renderFiles();
        resultBox.append(el("div", { class: "ok-box" }, [
          el("strong", {}, [document.createTextNode("提交成功，感谢反馈！")]),
          document.createTextNode(" 编号 " + resp.feedback_id + "。"),
          el("br"),
          el("a", { href: "/feedback" }, [document.createTextNode("查看反馈记录 →")]),
        ]));
      } else {
        showErr(resp.error || ("提交失败（HTTP " + xhr.status + "）"));
      }
    };
    xhr.onerror = () => {
      submitBtn.disabled = false;
      showErr("网络错误，请重试。");
    };
    xhr.send(data);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  for (const form of $all(".upload-form")) initUploadForm(form);
  initReviewDialog();
  initReextract();
  initOcrDetails();
  initDoctor();
  initFeedback();
  // deep link: /material/6#确认=certificate_no opens the 完善资料 dialog directly
  try {
    const raw = decodeURIComponent(location.hash);
    if (raw.startsWith("#确认=")) {
      const field = raw.slice(4);
      const dialog = $("#review-dialog");
      if (dialog) {
        dialog.dataset.autofield = field;
        const btn = $('[data-review]');
        if (btn) btn.click();
      }
    }
  } catch (e) { /* ignore malformed hash */ }
});
