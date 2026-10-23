# v0.6.0 项目文档内容

Project File 是不可改写的项目原件。Project Document 是依附 `project_file_id + source_sha256` 的派生文本，不是公司权威 Material。历史标书中的旧资质只表示当时文件的写法，不能成为当前有效资质。文档提取不写任何 Material / extracted_fact / verification_event，不增加 MCP 工具，不执行 AI。

## 使用与迁移

本分支 schema 为 `jty-mcp-0.6.0`、WebUI 为 `0.6.0`。升级已有库必须明确执行迁移，先产生 SQLite 迁移前备份，再在单个事务中升级；失败回滚，可重试。发布部署不在本分支自动进行。

```powershell
python -B mcp_v01/cli.py --config <隔离配置.json> migrate-v06
```

上传仍只快速保存原件并登记 pending。项目详情选择“提取内容”，页面显示等待、处理中、已可搜索、部分内容未识别或失败；可重新整理。搜索入口 `/project/{id}/search` 只查询本项目，PDF 链接带 `#page=N`（实际跳转取决于浏览器 PDF viewer），DOCX / XLSX 显示章节或 Sheet/行范围并下载原件，不声称 Office 精确跳转。

## 数据和目录

新增 `project_document`、`project_document_run`、`project_document_unit` 三表；一份原件只有一个当前 document，保留运行回执；成功重新提取后替换当前 units。单元保存 raw / NFKC 文本、1-based 页码、章节路径、Sheet / cell_range / 行范围、页级 native/ocr/empty/failed、投影路径和 SHA。失败运行保留之前成功提交的文本。

```text
repository/projects/PRJ-*/files/<file_id>.<ext>   原件，备份，immutable
repository/db/materials.db                     文档/运行/单元及 raw、normalized 文本，备份
<state_dir.parent>/project-documents/
  <document_id>/<run_id>/unit-000001.txt        normalized 文本投影，可重建，不备份
<index_dir.parent>/project-documents/
  index.sqlite3                               独立 SQLite FTS5 trigram，可重建，不备份
<state_dir.parent>/tmp/project-documents/
  <run_id>/units.jsonl                         临时提取结果，完成或重启后清理
  <run_id>/projections/unit-000001.txt         锁外生成的文本投影，manager 短窗口 rename 发布
```

默认配置下以上 runtime 目录都在 `local/`。Material 原有 `repository/materials` 和 dowse 索引不变。不向 Material dowse 添加项目目录，也不复制原始 PDF/Office 文件作搜索投影。

FTS5 trigram 支持中文和编号子串。1–2 字查询在独立索引中作项目限定的 literal 检索，避免漏掉“工期”“质量”等短词；较长词用 trigram phrase。所有查询都固定 project_id，结果再次用 DB 校验 project/file/document/run/unit 绑定；从 DB raw 文本生成片段。没有向量、分词服务或额外常驻进程。索引按需打开/关闭，doctor 检查完整性及 identity/text 映射。参见 [SQLite 官方 trigram 文档](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)。

## 提取策略与格式边界

| 格式 | 提取和定位 |
|---|---|
| PDF | optional PyMuPDF，逐页 native text；不足 30 个非空白字符或存在明显替换乱码时，仅本页调用现有 Umi-OCR 2.1.5。长边最多 2000px，内存渲染、不保存 PNG。逐页失败仍保留其他页，document 可 partial。页码从 1 开始 |
| DOCX | optional python-docx，基于 word/document.xml、styles/继承 outline level，标题路径 + 普通段落 + 表格行，约 6000 字目标但按段落/表格行边界分组，不伪造 Word 页码。内嵌图片 OCR deferred；有图片时明确 partial 提示，避免宣称全部提取 |
| XLSX | optional openpyxl read_only，keep_links=False，公式文本及可取得的缓存值，按 Sheet / 连续非空行区间；空行处分组，最多 40 行或约 6000 字目标；不计算公式、不执行宏或 DDE |
| TXT / MD | UTF-8 原文，按行组，作为正文定位 |
| DOC / XLS / PPT / PPTX / 图片 / ZIP | 原件可存可下载；本阶段提取 unsupported/deferred。不引入 Office COM、LibreOffice 或第二 OCR 引擎 |

PDF native 门槛是可审查的启发式：有充足 native 文字的混合页不会默认 OCR 图片内的额外文字；原生文字极少的封面可能触发一次 OCR。没有全页图片 OCR 识别率承诺。DOCX 标题、表格保留文本语义，不还原排版、浮动文本框、图表或像素坐标；XLSX 不执行外部链接、宏或公式重算。

安全预算：Office 解压声明总大小 ≤256MiB、条目 ≤20000；宏容器拒绝；XLSX 每 Sheet 声明范围 ≤500 万 cells；文档 ≤50000 units / 1 亿字符；超预算明确失败，原件保留。异常大的单段落/单行保持其位置边界，不按固定 500 字丢失定位。

PyMuPDF 维持 optional local dependency，不 vendored、不随公开仓库分发。缺包时 PDF extraction=unavailable/unsupported，核心 WebUI/原件保管仍可运行。许可继续为 AGPL-3.0 / 商业双许可；本项目许可证不变，使用 PyMuPDF 的部署须遵守其许可要求。见 [PyMuPDF 官方许可](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。

## manager、失败恢复和备份

所有写入经 authenticated manager RPC。新增 `project.document.extract/retry/rebuild/index.rebuild`，内部进度/完成 RPC 需要 manager 内存持有的一次性 capability。公开请求只收 project_id、project_file_id、actor；拒绝任意文件路径或提取文本。所有 POST 沿用同源检查。

manager 注册 queued run 后，由单个 compute thread 只读校验过的原件，生成临时 JSONL 和 staging 文本文件。HTTP 返回排队回执，不等待长 OCR。后台线程不能写 DB、正式投影或索引；进度与发布回到 manager dispatcher，经 operation.lock 串行写。文本 fsync 在锁外，正式发布只做受控目录 rename、验证与 transaction/index commit。PDF/Sheet 进度节流到最多约每秒一次；DOCX 只显示 processing，不造百分比。没有为文档任务设置长时间全库 maintenance，也不停止 Material watch。

发布先在事务中写 units 和投影映射，再更新独立索引。索引失败会明确标记、保留 DB 文本，并只阻止受影响项目的文档搜索；Material 浏览、项目浏览与原件下载仍可用。DB/磁盘错误回滚本次文本更新；错误状态若也无法写入，下一次 retry 或 manager 重启将中断运行标为 failed。重启不自动重放 OCR，用户可重试。源文件大小/SHA 不符拒绝整理，不借提取“修复”原件。

doctor 区分 source evidence error 与 rebuildable derivative error，检查原件/源 SHA、计数、run 绑定、孤儿文档/单元/投影、投影 SHA 和索引映射。备份包含原件、DB 元数据及 raw / normalized unit 文本；投影和索引排除，避免重复保存三份全文。恢复后明确要求重建；重建不需再次 OCR。

```powershell
python -B mcp_v01/cli.py --config <隔离配置.json> project-document-extract <project_id> <project_file_id>
python -B mcp_v01/cli.py --config <隔离配置.json> project-document-retry <project_id> <project_file_id>
python -B mcp_v01/cli.py --config <隔离配置.json> project-document-rebuild <project_id> <project_file_id>
python -B mcp_v01/cli.py --config <隔离配置.json> project-document-index-rebuild
python -B mcp_v01/cli.py --config <隔离配置.json> doctor
```

## 可复现验证

```powershell
$env:P05_DOWSE_EXE = '<真实 dowse.exe>'
$env:LARGE_DOC_UMI_EXE = '<真实 Umi-OCR 2.1.5/Umi-OCR.exe>'
python -B mcp_v01/run_project_center_tests.py
python -B mcp_v01/large_document_smoke.py
python -B mcp_v01/large_document_browser_qa.py
```

所有生成文件在 worktree 的 `runs/large-document/`；fixture 不使用生产文件，真实运行时先复制后启动，端口独立。缺少真实运行时属于失败，不用 mock 冒充 integration、不跳过。浏览器 QA 使用已安装的 Chrome（可按环境调整脚本中的 executable_path）；会话没有 Browser 插件，Playwright 记录桌面/手机截图、状态转换、搜索和无横向溢出。最终数据见分支实现报告。
