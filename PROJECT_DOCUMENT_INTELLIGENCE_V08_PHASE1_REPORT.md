# Project Document Intelligence — v0.8 Phase 1 Report

分支：`project-document-intelligence-v08`（worktree `D:\workSpace\bmc-project-intelligence-v08`）
Phase 1 vertical slice：上传 → 自动整理 → DOCX 原生文字 + 内嵌图片 → 逐图 OCR → 图片文字入 FTS → 素材候选发现（WHO / WHAT / LIFECYCLE）→ 项目页可见。

```text
BASE_HEAD        = 8455167 (origin/main, 含生产验证 purge 能力纳入)
FINAL_BRANCH_HEAD= 见本文件提交（commit 后填写于 git log）

AUTO_EXTRACTION  = PASS   上传/自动建档成功即入队整理，无第二次点击；
                          "重新整理/提取内容"保留为恢复入口
SUMMARY_UI       = PASS   文件卡结构化摘要（✓/⚠ 逐行）+ 处理中进度
                          （正在读取文档… / 图片 OCR n / N）+ 失败明细

DOCX_EMBEDDED_IMAGE_DISCOVERY = PASS  53/53 张，锚点（document_order、
                          relationship_id、nearest_heading、section_path、
                          preceding/following_text、dims、mime、sha256）
DOCX_IMAGE_OCR   = PASS    真实 Umi-OCR（生产同源运行时副本），串行并发 1，
                          逐图失败隔离，cache key = 源SHA|图SHA|umi-ocr|v1
DOCX_IMAGE_FTS   = PASS    embedded_image 单元进入独立 FTS5 索引并可搜索
IMAGE_ANCHOR_CONTEXT = PASS 图片 ↔ 章节面包屑 ↔ 前后文均已入库（§7 要求字段齐）

MATERIAL_CANDIDATE_DISCOVERY = PASS  复用 classify.ocr_classify 单一词汇表
MULTI_SUBJECT_MODEL = PASS           无任何"是不是晋铁源"类字段/分支；
                          主体=org/person 平等候选（§14）
SUBJECT_IDENTIFICATION = PARTIAL     USCC/证件号/名称/法定代表人确定性提取；
                          上下文推断与 Vision 双链确认留待后续（§19 seam 预留）
EXACT_MATERIAL_MATCH = PASS          USCC 精确 + 证号精确（e2e 走正规导入
                          +verify 证据链验证；audit=project.material.autolink
                          method=project_document_exact_match）
NEW_MATERIAL_CANDIDATE = PASS        新素材只进"发现素材"区，不写素材库
NO_SINGLE_COMPANY_ASSUMPTION = PASS

LARGE_REAL_DOCX  = PASS   全部 53 张图片 OCR 成功，0 失败 0 跳过，见下
```

## 真实 83MiB 响应文件验证（local-fixture，git-ignored）

输入为生产 PRJ-000001 的 `临汾市人民医院_中保源_响应文件 2.docx`（87,506,547 字节）
只读复制副本；OCR 引擎为生产同源 Umi-OCR 的私有副本（端口 20222），
生产工作区零触碰。运行于真实目标硬件（Ryzen 3 3200G / 6GB 可用内存 / HDD）。

```text
REAL_DOCX_FILE_SIZE       = 87,506,547 bytes (83.4 MiB)
REAL_DOCX_IMAGES          = 53（48 PNG + 5 JPEG，占文件体积 99.9%）
REAL_DOCX_IMAGES_OCR_OK   = 53
REAL_DOCX_IMAGES_OCR_FAILED = 0
REAL_DOCX_IMAGES_SKIPPED  = 0（无装饰图误杀：阈值 2KB/40px 仅拦小图标）

REAL_DOCX_NATIVE_CHARS    = 63,904（73 单元，与 v0.7 生产值一致）
REAL_DOCX_OCR_CHARS       = 23,533（53 个 embedded_image 单元）
REAL_DOCX_TOTAL_UNITS     = 126（73 native + 53 image）= 87,437 字符全部可搜
DOCUMENT_STATUS           = complete（v0.7 时代为 partial:deferred）
PROCESSING_TIME           = 296.5 s 全量（OCR 引擎纯耗时 282.4 s，串行）
RESUME_PROOF              = retry 复用 53/53，OCR 引擎调用 0 次，7.0 s 完成

搜索实测（Project FTS）：营业执照 8 命中 / 中保源 35 / 法定代表人 10 /
91140931MA7YM0KQ09 6（图片内 USCC 已可搜）/ 安全生产许可证 0
（53 图中无该证书的清晰页；如实记录，非缺陷）
```

### 发现的素材候选（10 个，全部 new_candidate，exact=0）

素材库唯一 USCC 主体为晋铁源，本文件主体均不相同 → 0 精确匹配是正确结果
（与 Phase 0 调查结论一致）。

```text
类型分布： business_license 2 · iso_cert 3 · audit_report 1 · financial_statement 1
           tax_payment_cert 1 · award_notice 1 · person_id_card 1
主体（5 个，平等多主体）： 山西中保源建设工程有限公司（USCC 91140931MA7YM0KQ09, high）
           珠峰电缆大名有限公司（USCC 91130425319821329Q, high, 3 张体系证书）
           山西闻达会计师事务所有限公司 · 担保公司（OCR"产州"系原文噪声，候选仅作 hint）
           1 个人员主体（身份证号脱敏 142601********1234）
置信度： high 5 / medium 5；生命周期： reusable 10（无 transient 触发词命中）
来源： 9 个 embedded_image + 1 个 text_unit（商务表格中的执照号行）
验收要点达成： 识别出"中保源"主体线索 ✓；识别出 91140931MA7YM0KQ09 ✓（图片 OCR 实读）
```

## 资源观察（仅有证据时报告）

- 进程内存快照（tasklist 实测）：RapidOCR-json ≈ 996MB（引擎模型+推理）、
  Umi-OCR ≈ 133MB、驱动 Python 进程 ≈ 209MB —— 合计 ≈ 1.3GB，6GB 目标机可承受；
  单图逐张处理，任意时刻仅一张图驻留内存。
- 峰值工作集自动采样（ctypes psapi）失败（恒 0），不报告臆测值；
  上表进程快照为唯一内存证据。

## 测试

```text
TESTS（synthetic, v0.8 新增）= 11/11 OK  test_v08_project_intelligence.py
  覆盖：图片发现/锚点/顺序、OCR 成功入 FTS、装饰图跳过不 OCR、
  重复图片 OCR 一次、单图失败隔离+retry 只重跑失败项（resume 断言到 cache key）、
  候选分类/主体/生命周期、TOC 反例（原生文本须凭证号）、
  EXACT 匹配 e2e（正规导入+verify 证据链）+ autolink audit + 幂等、
  match_library 单元路径（USCC/证号/名称/歧义）、自动入队 + 重复上传不入队、
  不支持扩展不自动整理、摘要 UI 文案

TESTS（回归）= 全绿：
  documents 套件 5 件（schema/extract/manager/search/webui）
  project 套件 4 件 · zero_fill 3 件 · upload_repair · mobile_ui · feedback
  p05 · provenance · reextract · v01 · v02 · v03 · batch
  注：v01/v02/v03/batch 须在短路径（D:\b8 副本）运行——本 worktree 名称比
  公开库长 1 字符，深路径恰好触及 Windows MAX_PATH(260)，属环境约束非代码回归

TESTS（真实引擎门禁，补跑）= 全绿：
  test_project_documents_integration 2/2（真实 Umi 扫描 PDF 可搜）
  test_upload_repair_integration 1/1 · test_project_manager 7/7（真实 dowse）

PY_COMPILE    = PASS（全部改动文件）
GIT_DIFF_CHECK= PASS（19 文件：schema_v08/discovery 新增、document_extract/
  project_documents/manager/store/cli/webui 修改、测试更新、.gitignore 增
  local-fixture/；生产工作区无任何写入）

PRODUCTION_TOUCHED = NO   （真实 fixture 在 worktree local-fixture/，git-ignored；
                            Umi 运行时为私有副本；生产 DB/项目/日志零触碰）
MERGED_TO_MAIN      = NO
DEPLOYED            = NO
```

## 本轮发现并修复的既有问题

1. `webui/projects.py detail_page`：按钮文案复用外层 bucket 循环变量 `label`，
   导致分组标题被最后一个文件的按钮文字覆盖（生产页面可见 `<h3>重新整理</h3>`）。
   已修复（`button` 局部变量）。
2. v0.8 自动整理使后台 publish 频率上升，浏览/项目页的乐观读会撞
   `RETRY: repository changed during query`（真实 manager 门禁测试稳定复现）。
   已按 documents API 既有语义在 WebUI 读入口（browse/项目页/搜索）加
   `tolerant_read` 短退避重试（`webui/service.py`）。
3. 测试基建：worktree 长路径触发 MAX_PATH（见上），属环境约束。

## Preflight 记录（§3）

```text
SOURCE_CONTROL_DRIFT = YES（v0.7 部署后两轮生产清理未入 git）
处置：3 项 delta（jty_materials/purge.py、manager.py purge 分支、
webui/service.py TEST_MATERIAL_IDS={1096..1099}）逐字并入
main @ 8455167 并推送；与生产 diff --strip-trailing-cr 归零后建 v0.8 分支。
其余文件差异均为 CRLF/品牌中性化/仓库独有测试基建，非漂移。
```

## 设计边界（Phase 1 明确不做 / 未做）

- Candidate 永不直接写 Material 事实层；EXACT_MATCH 仅建项目引用（§17/§18）。
- 无 Vision/LLM 依赖：无 OPENCODE_GO_API_KEY 时全链可用（§19）；
  provider seam 留待双链确认轮。
- 不伪造 DOCX 页码；锚点 = document_order + section_path + heading + 前后文（§10）。
- PDF 拆分、向量检索、自动合并模糊重复、标书生成均未做（§28）。
- SUBJECT_IDENTIFICATION = PARTIAL：上下文推断（无证件号的主体归属）留待
  下一轮 + Vision 双链。

## STOP

Phase 1 完成：commit → push `project-document-intelligence-v08` → 停止。
未合入 main、未部署。等待独立 code review。
