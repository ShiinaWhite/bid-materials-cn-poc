# Large Document & Historical Bid Foundation — v0.6.0

```text
BASE_HEAD = 584a8fd81b865230cf651aab659376ea76fc499a
BRANCH = large-document-foundation
BRANCH_HEAD = 5bca77738921859e72ede194d144a21ffc7ece62
SCHEMA_VERSION = jty-mcp-0.6.0
WEBUI_VERSION = 0.6.0

MIGRATION_05_TO_06 = PASS
PDF_NATIVE = PASS
PDF_OCR_FALLBACK = PASS
PDF_MIXED = PASS
DOCX = PASS
XLSX = PASS
PROJECT_SEARCH = PASS
PROJECT_ISOLATION = PASS
MATERIAL_INDEX_ISOLATION = PASS
SOURCE_TRACEABILITY = PASS
DOCTOR = PASS
BACKUP_RESTORE = PASS
FAILURE_RECOVERY = PASS
LARGE_DOCUMENT_SMOKE = PASS
DESKTOP_QA = PASS
MOBILE_QA = PASS
FULL_REGRESSION = PASS
PRODUCTION_TOUCHED = NO
LARGE_DOCUMENT_FOUNDATION = PASS
```

`BRANCH_HEAD` 记录受测实现提交；本报告在其后的文档提交中登记，最终分支 HEAD 以交付回执及 `git rev-parse HEAD` 为准。

## 基线与开发边界

- 实际 Git 主检出为 `D:\workSpace\bid-materials-cn-poc-public`；用户给定的 `D:\workSpace\bid-materials-cn-poc` 是无 Git 元数据的生产部署副本。公开仓库、当地 origin/main 和远端 main 均核对为指定 `584a8fd`，起始 schema/WebUI 都是 0.5.0，没有版本偏差。
- 从指定提交创建独立 `D:\workSpace\bmc-large-doc` / `large-document-foundation`。没有修改生产配置、数据库、索引、服务或生产 ROADMAP；没有上传生产测试文件、merge main、部署、force push 或删除分支。
- 仅复用已有运行时文件并复制到 fixture 后启动；Umi 使用独立端口，dowse 使用独立运行目录及 DOWSE_INDEX_DIR。没有读取生产项目原件作为 fixture。
- 没有 AI 总结/问答/写标书、素材晋升、AgentCase、零填写、自动项目创建、自动引用、向量库、Embedding 或新常驻服务。三个只读 Material MCP 工具及其既有 API 版本不变。

## 数据模型与写入

| 新增表 | 内容 |
|---|---|
| project_document | 每个 project_file_id 唯一 document；绑定 source_sha256；extractor、状态、时间、当前运行、字数/单元数、错误、索引状态 |
| project_document_run | queued/processing/complete/partial/failed/unsupported 回执、经办人、粗粒度进度、错误；重试新增运行 |
| project_document_unit | 当前派生单元，raw 与 NFKC 文本、page/paragraph_group/sheet_chunk、页/章节/Sheet/行/cell_range、native/ocr/empty/failed、投影路径/SHA |

没有 EAV 或 Project File 版本系统，没有复用 human_verified / candidate 等事实语义。document 源身份不可改；unit/run 绑定受 FK/触发器保护；普通 SQL 无 document_writer 上下文不能写三张新表。原有原件、核验与 append-only ledger 约束继续有效。

迁移先用 SQLite backup API 生成 `schema-before-v06-*.db`，再在单个事务中升级及审计。现存 Project File 只获得 pending 元数据，不自动提取。真实 0.5 fixture 验证迁移前后原件/身份保持、失败回滚、备份与幂等重试。新库初始化也改为单事务创建完整 schema 和守卫，避免部分硬化及数百次 DDL fsync。

长任务链路：

```text
WebUI POST → same-origin → manager RPC → operation.lock → 注册 queued run → 快速响应
单个 compute thread：只读原件 → 校验 size/SHA → native/OCR → staging JSONL + 文本 fsync
内部 capability RPC → manager operation.lock → 校验源/派生文本 → rename 目录 → units transaction
manager 发布独立索引 → generation → 清理旧派生物/临时目录
```

后台线程不写 DB、正式投影或索引；进度与发布均回到 manager dispatcher。并发为 1，PDF/Sheet 进度约每秒最多一次，DOCX 不伪造百分比。文档任务不设置全库 maintenance，不停止 Material watch。长时间计算及文本 fsync 在锁外，正式发布使用受控目录 rename。

## 格式、定位与索引

| 格式 | 本阶段行为 |
|---|---|
| PDF | optional PyMuPDF 1.28.2；逐页 native first，少于 30 个非空白字符或明显替换乱码才本页 fallback 到现有 Umi-OCR 2.1.5；1-based page_no |
| DOCX | python-docx 1.2.0 原生 XML/样式关系；Heading/outline 继承、段落和表格行；section_path，按语义边界分组，目标约 6000 字 |
| XLSX | openpyxl 3.1.5 read_only；Sheet/连续非空行区间，最多 40 行或约 6000 字目标；保留公式文本及可取得的缓存值，不计算公式 |
| TXT/MD | UTF-8 按行组，正文定位 |
| DOC/XLS/PPT/PPTX/图片/ZIP | stored supported、extraction unsupported/deferred；可下载原件；没有 Office COM、LibreOffice 或第二 OCR 引擎 |

PDF 只在内存渲染必要页面，长边最多 2000px，不保存 OCR PNG；单页失败可 partial，不丢其他页。OCR 退出清理异常也保留已提取文本并标 partial。DOCX 内嵌图片 OCR 明确 deferred，有图片时保守提示 partial，不宣称识别全部内容。DOCX 不还原 Word 分页、浮动排版；XLSX 不还原图表、样式、VBA，不执行宏、外部链接或 DDE；带宏 Office 容器拒绝。

安全预算为 Office 解压声明总量 256MiB / 20000 entries、每 Sheet 500 万 declared cells、每文档 50000 units / 1 亿字符；超过预算明确失败，原件保持。单个异常长段落/行仍保持边界，不机械每 500 字切割。

```text
repository/projects/PRJ-*/files/<file_id>.<ext>       immutable 原件
repository/db/materials.db                         metadata + raw/norm units
<state_dir.parent>/project-documents/<doc>/<run>/unit-000001.txt
<state_dir.parent>/tmp/project-documents/<run>/units.jsonl + projections/
<index_dir.parent>/project-documents/index.sqlite3   独立 FTS5 派生索引
```

默认配置下派生物在 local/。Material 的 `repository/materials` 与 dowse index 未改变，没有将项目目录加入 Material 索引。项目索引使用标准库 SQLite FTS5 trigram，无额外服务：按需打开、发布时 integrity-check、doctor 校验映射/文本、按需重建、连接关闭即停止。1–2 字关键词在独立索引内按 project_id 作 literal 查询；较长词使用 trigram phrase。结果再次通过 DB 校验 project/file/document/run/unit 身份，并从 raw 文本产生片段。

PyMuPDF 仍是 optional local dependency，缺包只使 PDF extraction unavailable/unsupported，核心 WebUI/项目/原件保管可运行。没有 vendored 二进制，没有改变仓库 MIT 许可证；部署含 PyMuPDF 的组合系统仍须遵守其 AGPL-3.0 / 商业许可要求。依据：[PyMuPDF 官方许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)、[SQLite FTS5 官方说明](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)。

## Actions、路由和用户界面

```text
manager: migrate-v06
manager: project.document.extract / retry / rebuild / index.rebuild
internal capability only: project.document._progress / _finish

GET  /project/{id}/search
GET  /api/project/{id}/documents
POST /api/project/document/extract
POST /api/project/document/retry
POST /api/project/document/rebuild
```

公开提取 action 只接受 project_id、project_file_id、actor，不能指定文件系统路径、stage 或提取文本。所有新 POST 都由既有同源防护在读取 body 前检查。CLI 支持对应提取/重试/重建和独立索引重建；具体命令见 [项目文档说明](docs/project-documents.md)。

项目详情显示未处理、等待整理、正在整理内容、已可搜索、部分内容未识别、失败；提供提取/重新整理和“搜索项目文件”。历史内容仅供参考提示始终可见。PDF 结果链接 `#page=N`，章节和 Sheet/行范围清楚显示；Office 仅下载原件，不伪造精确跳转。

## doctor、备份和失败恢复

- doctor 扩展到 documents/units/projections/index：source exists、size/SHA、document source_sha、计数/run、投影 SHA/规范映射、孤儿 document/unit/projection、索引 identity/text。逐单元检查，不把全库文本一次装入内存；原件损坏与 rebuildable derivative error 分开报告。
- 备份包含 Project File originals、DB 文档/运行元数据、raw/norm unit 文本；排除投影和搜索索引，避免多份全文。restore 明确返回 project_document_rebuild_required，manager index.rebuild 可从 DB 恢复，无需重复 OCR。
- native/parser/Office 异常、Umi unavailable/crash、磁盘满、DB/索引故障均不更改原件。事务失败保留上一批成功 units；索引失败保留已提交 DB 文本，明确标记并允许独立重建，不把全库锁入 maintenance。
- manager 重启把 queued/processing 标为 failed，并清临时派生物；用户可 retry，不自动重放 OCR。客户端丢响应不影响已注册任务；重复请求在同一 active run 去重。注册失败不遗留内存 ghost job。

## 最终测试

```text
TEST_TOTALS = 176 / 176 PASS (original 140 + new 36)
FAIL = 0
ERROR = 0
SKIP = 0
```

最终完整回归耗时 669.111 秒，176/176 PASS，FAIL/ERROR/SKIP 均为 0。AST 对比证明原 140 个测试方法全部保留，新增 36 个。新增六组 schema/extract/search/manager/webui/integration 测试；原 HTTP harness 提取为共享 fixture，未移除原测试或用 skip 隐藏失败。

覆盖真实 native/scanned/mixed PDF、页级 OCR 失败及退出异常、DOCX heading/table/image deferred、XLSX Sheet/稀疏行/公式、旧 Office unsupported、三种坏文件、optional PyMuPDF、源 size/SHA 变化、客户端断开、manager 重启、DB/索引/磁盘/注册失败、重试/重提取、同源、迁移回滚、doctor 和 backup/restore。

真实 Umi 集成用 copied 2.1.5 识别 image-only PDF，项目搜索找到“项目经理”和“998877”，page_status=ocr。不是 mock；缺少真实程序会失败，不 skip。真实 named-pipe manager 与真实 dowse 验证：

| 硬隔离门禁 | 结果 |
|---|---|
| MATERIAL_SENTINEL_123456 只在 Material 中命中 | PASS |
| PROJECT_DOC_SENTINEL_654321 只在对应项目中命中 | PASS |
| HISTORICAL_FAKE_CERTIFICATE_998877 可在项目找到，Material/search/browse 均不返回 | PASS |
| Material count、Material index num_docs、candidate facts、verification_event、certificate_detail 不变 | PASS |
| Project A/B SECRET TERM 互不可见；伪造/陈旧 index identity 不返回文本 | PASS |
| 原件字节/SHA 不变；结果包含 project_id/file_id/document_id/unit_id 与适用位置 | PASS |
| staging 文本 fsync 时可取得 operation.lock；提取期间可浏览、下载，无全库 maintenance | PASS |

最终运行证据：`runs/large-document/regression-20261023-185118/tests.log` / `summary.json`；`source-audit-final.json`；真实 OCR 和隔离证据在相应 fixture 的 `real-umi-evidence.json` / `isolation-evidence.json`。这些都是忽略的本地测试产物，不进公开 Git。

一次开发中并行执行回归、smoke 与 OCR QA 引发了旧 dowse health timeout，且初版 QA 的 Umi 首次启动超时后如实显示 partial；没有将其计为 PASS。修正工作流后，最终门禁串行执行。旧迁移测试的目标表集合已准确扩充三张新表；最终实现以 frozen-source-final.json 校验，冻结后没有功能改动，不以早期失败运行代替最终结果。

## 浏览器 QA

环境：http://127.0.0.1:61175；仅隔离 loopback fixture，结束后停止。Browser plugin not available，使用本机 Chrome + Playwright；1365×900、390×844，各验证一次完整流程。

| 检查 | 桌面 | 手机 |
|---|---|---|
| 页面 URL/title、有效内容、无错误覆盖层 | PASS | PASS |
| console/page errors | 0 | 0 |
| 项目详情、pending/processing/complete/partial/failed | PASS | PASS |
| 提取按钮 → 实时页数 → 完成 → 搜索 | PASS | PASS |
| PDF 页、DOCX 章节、XLSX Sheet/行及原件链接 | PASS | PASS |
| 横向 overflow | 无 | 无 |

交互路径为 Project detail → 真实 300 页 native 文件“提取内容/重新整理” → processing → 期间访问 Material browse 和下载原件 → complete → 项目内搜索三种文件结果。没有用静态状态截图代替提取按钮交互。partial/failed 另由实际不完整/坏文件显示。

12 张截图与 summary 在 `runs/large-document/browser-qa/`：每个宽度的 detail、processing、complete、pdf-result、docx-result、xlsx-result。人工查看并修正了结果横排和内部 action 文案，最终截图复核通过。可复现命令为 `python -B mcp_v01/large_document_browser_qa.py`。

## 大文档 smoke 与体积

全部是生成的真实文件，不含生产资料。PDF 为逐页 native 文本及唯一合成插图；DOCX 为 500 个结构章节；XLSX 为 4 Sheet × 10000 行及稀疏尾行，约 12 万有值 cells。精确增量为同一 fixture 每次提取前后 SQLite/索引文件（含 WAL）之差。

| fixture | original bytes | units | raw bytes | norm bytes | SQLite 增量 bytes | index 增量 bytes | elapsed s | 进程峰值 MiB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| PDF 100 页 | 7,967,252 | 100 | 187,840 | 187,840 | 442,368 | 495,616 | 5.495 | 75.2 |
| PDF 300 页 | 23,903,382 | 300 | 567,840 | 567,840 | 1,413,120 | 1,404,928 | 15.063 | 95.6 |
| DOCX 500 章节 | 41,295 | 1,500 | 101,390 | 98,390 | 1,122,304 | 499,712 | 67.968 | 96.2 |
| XLSX 4 Sheet | 707,598 | 1,008 | 2,656,224 | 2,656,224 | 6,344,704 | 7,987,200 | 41.971 | 122.5 |

两组大 PDF 全部 native，OCR 页数为 0。PDF raw+norm 合计约原件 4.7–4.8%，DB 增量约 5.6–5.9%。Office 是压缩容器，尤其本次重复结构 DOCX 压缩很强：文本/元数据不普遍小于原件；DOCX DB 增量约 27.2×压缩原件，XLSX 约 9.0×。不把这个比值伪报为“文本必然更小”。XLSX 实际 DB 增加约 6.05MiB，索引约 7.62MiB；备份保留 DB，排除另外两份派生层。

peak-ish 内存通过 GetProcessMemoryInfo 取得，范围包括整个 smoke 进程和 fixture 生成，不代表全机峰值，也不包含 Umi 子进程；这组 native smoke 没有启动 Umi。实测 Windows 10 17763（1809）可见内存约 5.93GiB，native 大文档运行稳定。该结果是合成烟测，不是所有扫描 PDF 的 OCR 耗时承诺。

生成器、命令与原始数据：`mcp_v01/large_document_smoke.py` / `document_test_support.py`；`runs/large-document/smoke.json`。大型 PDF、DOCX、XLSX、OCR runtime、DB、索引、截图和临时文件均不提交。

## 变更与交付审计

实现统计（不含本报告）：42 files，+2027 / -54 lines。含报告的最终统计：43 files，+2213 / -54 lines。

- 模型/运行核心：schema_v06.py、store.py、common.py、projects.py、project_documents.py、document_extract.py、document_index.py、manager.py、doctor.py、backup.py、cli.py。
- WebUI：webui/__init__.py、api.py、app.py、projects.py、static/app.js、static/style.css。
- 测试：document_test_support.py，六个 test_project_documents_*.py，project_test_support.py、run_project_center_tests.py，原 test_mobile_ui.py、test_p05.py、test_project_schema.py、test_project_webui.py、test_v01.py、test_v02.py、test_v03.py。
- 复现/说明：large_document_smoke.py、large_document_browser_qa.py、requirements.local.txt、README.md、docs/project-documents.md、architecture.md、data-model.md、webui.md、office-use.md，以及本报告。

`git diff --check` / staged diff check、敏感 token/private-key 模式扫描、原测试保留比对、root-drive 禁止前缀扫描均通过。最终产物只在 worktree 的 runs/large-document，TEMP/TMP 也由最终测试脚本限制到该目录。main 和生产保持基线；只正常提交并推送 origin/large-document-foundation，等待代码级 review。
