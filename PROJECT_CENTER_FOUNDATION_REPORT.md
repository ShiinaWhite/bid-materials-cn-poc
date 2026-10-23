# Project Center Foundation 实现报告

```text
BASE_HEAD = 451102f0bdfa1cf9adae099ed7a24b91129cba55
BRANCH = project-center-foundation
BRANCH_HEAD = 41ab6c9ae6f825cf15aa9aefed10c1975886a53d  # 受测实现提交
SCHEMA_VERSION = jty-mcp-0.5.0
WEBUI_VERSION = 0.5.0

PROJECT_SCHEMA = PASS
MIGRATION_04_TO_05 = PASS
PROJECT_FILE_IMMUTABILITY = PASS
MATERIAL_REFERENCE_NO_COPY = PASS
GLOBAL_SEARCH_ISOLATION = PASS
DOCTOR = PASS
BACKUP_RESTORE = PASS
DESKTOP_QA = PASS
MOBILE_QA = PASS
FULL_REGRESSION = PASS
PRODUCTION_TOUCHED = NO
PROJECT_CENTER_FOUNDATION = PASS
```

## 范围与环境

- 从指定 `origin/main` 创建独立 worktree `D:\workSpace\bmc-project-center`。公开 Git 主检出在 `D:\workSpace\bid-materials-cn-poc-public`，生产部署目录为独立副本。
- 实际起始版本确认为 schema `jty-mcp-0.4.0` / WebUI `0.4.3`，与任务基线一致；本分支升级至 `0.5.0`。现有三个只读 Material MCP 工具不变，MCP `serverInfo.version` 仍为其现有 API 版本 `0.4.0`，不与 schema/WebUI 版本混用。
- 没有使用生产配置或生产数据库，没有部署、停止或重启生产服务，没有 merge main。真实 dowse 仅从已有程序读取并复制到隔离 fixture 后启动。
- 没有引入新框架、后台服务、数据库、Node 生产运行时。只实现项目基础，不包含大文档、历史标书、素材晋升、AgentCase 或 AI 能力。

## 数据与写边界

新增三张表：

| 表 | 作用与关键约束 |
|---|---|
| `project` | 稳定整数 ID、`PRJ-000001` 系统编号；名称/招标编号/招标人/标段；active/archived；身份与创建时间不可改，不可删除/替换 |
| `project_file` | 独立 UUID 身份、六个宽分类、原文件显示名、SHA256、大小、经办人；整行不可修改、不可删除/替换；项目内 SHA 去重；路径忽略大小写唯一 |
| `project_material` | 唯一 project/material 关系；active/removed；取消引用与再次引用保留同一关系身份，完整变化历史进入 audit |

`audit_log` 仍为唯一 append-only 审计台账：在迁移事务中保留旧记录及整数 ID，扩展实体类型为 project/project_file/project_material，STRICT `entity_id ANY` 兼容 UUID。原有核验/业务触发器继续有效，禁止 audit UPDATE/DELETE/REPLACE。没有新增 EAV、标签、任务、权限或 AI 表。

新增 manager actions：

```text
project.create
project.update
project.archive
project.restore
project.file.add
project.material.link
project.material.unlink
migrate-v05
```

所有项目业务写入通过 WebUI → authenticated manager RPC → operation.lock → maintenance → transaction + audit → generation。WebUI 不写 SQLite；项目读服务使用只读 Store 及一致性检查。归档后阻止编辑、上传、引用及取消引用，仍可查看文件与既有引用。

## 文件、恢复与格式边界

```text
repository/
├─ materials/MAT-*/...                     全局素材及唯一全文索引根
└─ projects/
   ├─ .staging/<uuid>.pending              未提交的流式暂存
   └─ PRJ-000001/files/<uuid>.<extension>   已登记的项目原件

state/project-file-journal/<uuid>.json     中断恢复意图（恢复完成前不可清空 state）
```

- 磁盘身份只由项目编号和 UUID 决定，不使用项目名称、上传文件名确定唯一位置。
- safe_path 检查规范路径与解析后的边界；原始显示文件名独立保存；不覆盖已有最终路径。
- 上传计算 SHA256、大小，写持久化意图，流式 staging + fsync，再次核验 SHA，发布最终路径，在事务中登记 project_file / 项目更新时间 / audit。提交前失败清理未登记文件，提交后失败保留并验证登记证据；恢复不重放写入。发布前路径碰撞直接拒绝；发布时突然出现占用文件也保留其原字节，恢复不会误删别人的文件。
- manager recovery 先校验 DB integrity/FK，再根据已提交状态处理意图，验证所有 Material / Project 原件，复用原有投影/索引/watch 恢复与 generation 流程。
- 单文件最大 **200MB**；项目支持 **PDF / PNG / JPG / JPEG / DOC / DOCX / XLS / XLSX / PPT / PPTX / TXT / MD / ZIP**，未缩窄要求。
- 检查常见格式容器签名以拒绝明显伪扩展名；**不解析 Office、不鉴别 Office 容器内部子类型、不解压 ZIP，也不承诺检查整个文档结构完整性**。PDF/图片可 inline，其余强制下载；每次读取都核对磁盘 SHA 与登记值，失败拒绝展示，响应 no-store/nosniff。
- 普通 Material WebUI 上传仍严格 **PDF / PNG / JPG / JPEG**，没有开放 Office。
- 项目文件不经过 import_material、OCR、search_projection 或 dowse index；引用操作仅写关系、项目时间与审计，不修改 Material、material_file、project_file，不复制原件字节。

## 页面与路由

顶部导航新增项目中心；新建只要求项目名称，其余基本信息可选，系统分配编号。项目列表有关键词、active/archived、分页与手机卡片。详情包括基础信息、六类项目文件、独立引用素材、项目记录、归档恢复。

```text
GET  /projects                       /projects/new
GET  /project/{id}                   /project/{id}/edit
GET  /project/{id}/upload
GET  /project-file/{project_id}/{file_id}
POST /api/project/create            /api/project/update
POST /api/project/archive           /api/project/restore
POST /api/project/file
POST /api/project/material/link     /api/project/material/unlink
GET  /api/project/jobs/{job_id}
```

所有 POST 仍统一经过 `Handler.do_POST` 同源检查，没有旁路写入口。项目上传有独立异步任务注册和 manager action；拖拽/系统文件选择均可使用。素材检索复用已有查询能力，结果包含类型/状态/有效期；素材详情仅在存在 active 引用时显示“用于 N 个项目”，包括保留引用的已归档项目。基础编辑/引用审计使用“本机用户”，上传/归档可填经办人；本阶段没有账户和权限系统。

## 显式迁移、doctor、backup / restore

```powershell
python -B mcp_v01/cli.py --config <隔离配置.json> migrate-v05
python -B mcp_v01/cli.py --config <隔离配置.json> doctor
```

- Store / WebUI 启动不隐式迁移，旧 schema 打开时明确拒绝并提示迁移。
- 迁移先经 SQLite backup API 写 `schema-before-v05-*.db`，在 `BEGIN IMMEDIATE` 中创建模型、保留并扩展审计台账、更新 schema 及迁移审计。故障注入证明 schema 和台账全部回滚，可再次迁移；幂等重跑不重复建表。
- 使用真实 0.4 schema 创建含 Material 原件和人工核验历史的隔离 fixture，验证升级后旧数据/台账/核验事件保持、触发器继续拦截未授权修改、integrity/FK/文件 doctor PASS。
- CLI doctor 与 WebUI 体检均分别报告 `files_checked/file_errors` 和 `project_files_checked/project_file_errors`；路径不安全、缺失与 SHA 损坏均报错。WebUI 总体结果包括项目文件错误。
- 现有 backup 原先只按登记表收集文件，已补入 `project_file`，复用原 manifest / COMPLETE / DB backup，不建第二套备份。恢复测试证明三张项目表、UUID、文件 SHA、原始字节和 Material 引用均保持。

## 测试与硬门禁

最终完整回归：**140/140 PASS，FAIL=0 / ERROR=0 / SKIP=0**，耗时 **1253.081 秒**。原有 **116** 项全部保留并通过，新增 **24** 项。最终源码冻结后执行，之后仅提交报告，不再改实现。

```powershell
$env:P05_DOWSE_EXE = '<真实 dowse.exe 绝对路径>'
python -B mcp_v01/run_project_center_tests.py
```

本次显式提供已有真实程序，测试先复制到 fixture 再启动。结果及日志：

```text
runs/project-center/regression-20261023-150406/summary.json
runs/project-center/regression-20261023-150406/tests.log
```

runner 要求真实 P05_DOWSE_EXE，任何 skip 视为失败；TEMP/TMP、全部 fixture、日志、截图与 bootstrap 缓存统一在 runs/project-center 内。

| 硬门禁 | 证据 |
|---|---|
| 创建 Project ≠ 创建 Material | 创建前后 Material / material_file / 索引映射数量不变 |
| 上传 Project File ≠ 创建 Material | 专属 DOCX 只增加 project_file，Material / material_file / extraction_run / search_projection 不增加 |
| 引用 Material ≠ 复制 Material | Material 与 material_file 全行不变，project_file 不增加；原件树哈希集合不变，原始 SHA 只有一份；仅关系 +1，并更新项目/审计 |
| 取消引用 ≠ 删除 Material | 状态改为 removed，全部素材/文件/项目计数不减；再次引用复用 reference_id，audit 保留 link/unlink/link |
| 归档 Project ≠ 删除 Project / Project File | 默认列表隐藏；归档筛选可查；原件/引用仍可读；恢复可继续写 |
| Project File ≠ OCR / 全文索引 | 项目 action 不调用 OCR / Material import；真实 dowse index 文档数不增加 |
| 项目内容 ≠ 污染全局搜索 | 真 DOCX 包含 `PROJECT_ONLY_SENTINEL_847291`；真实 dowse、Material search、browse 渲染均无结果，项目仍能下载字节一致原件 |

新增测试文件为 `test_project_schema.py`、`test_project_service.py`、`test_project_manager.py`、`test_project_webui.py`。旧测试仅按新增 schema 表、受同源保护的 routes、显式新迁移 action 和统一临时产物路径更新断言，未删除或跳过旧测试。

覆盖迁移回滚、身份与证据触发器、路径穿越、伪扩展名、200MB 超限、SHA 变化、重复、错误项目/文件/素材 ID、归档写入、审计失败、数据库失败、复制/发布失败、提交前后中断、提交后丢响应重试、维护和 generation、真实 manager 客户端断开后的引用及文件提交。

## 浏览器 QA

使用 `build-web-apps:frontend-testing-debugging` 技能。Browser 插件不可用（Browser plugin not available），使用已安装的 Python Playwright + 系统 Chrome headless，未引入生产依赖。浏览器测试的是隔离 WebUI / manager / index。

流程：项目列表 → 新建 → 详情 → 上传/下载 → 错误提示 → 素材搜索与引用 → 素材反向项目 → 编辑 → 取消引用 → 归档 → 恢复。

| 检查 | 1365×900 | 390×844 |
|---|---|---|
| URL / title、非空、有实际内容 | PASS | PASS |
| 无框架错误遮罩、无非预期 console error | PASS | PASS |
| 列表/详情/新建/编辑/上传/素材反向关系无横向 overflow | PASS | PASS |
| 主按钮与完整数据变化交互 | PASS | PASS |
| 上传控件高度 ≥44px | PASS | PASS |
| 上传/下载原始字节一致 | PASS | PASS |
| 明显伪 PDF 显示错误，按钮恢复 | PASS | PASS |
| feedback dialog 位于 viewport 内 | PASS | PASS |
| 手机单列项目卡片 | — | PASS |

最终 QA：**80/80 PASS**。本机临时证据（不入 Git）：

```text
runs/project-center/qa/20261023-150019/qa-results.json
runs/project-center/qa/20261023-150019/1365-project-cards.png
runs/project-center/qa/20261023-150019/1365-reference-search.png
runs/project-center/qa/20261023-150019/1365-material-reverse.png
runs/project-center/qa/20261023-150019/390-project-cards.png
runs/project-center/qa/20261023-150019/390-upload.png
runs/project-center/qa/20261023-150019/390-upload-error.png
runs/project-center/qa/20261023-150019/390-reference-search.png
runs/project-center/qa/20261023-150019/390-material-reverse.png
runs/project-center/qa/20261023-150019/doctor-smoke.json
runs/project-center/qa/20261023-150019/backup-restore-smoke.json
```

浏览器入口为该次隔离服务器 `http://127.0.0.1:52248`，验证后已停止。每个视口执行真实新建、上传、下载、搜索、引用、取消引用、编辑、归档、恢复，并检查 URL/title、内容、布局及对话框。正常流程无 app console error；两个故意伪 PDF 上传各产生预期 HTTP 400，页面错误可见，按钮可再次操作。

附加 smoke：CLI / WebUI doctor 均检查 4 个 Material 文件、2 个 Project 文件，零错误；人为损坏一份 fixture 项目文件后两入口均检出，再还原原字节后零错误。备份 manifest 共 7 个文件，其中 2 个项目文件；未 mock DB/file backup/restore 或 runtime copy，恢复后引用与原字节保持。runtime copy 使用最小 Umi 配置 fixture，未启动 OCR。

验证环境记录：附加 doctor smoke 首次直接重启既有 QA watch 健康探针超时，尝试运行模式切换后仍超时；按照既有显式 `index` 维护流程重建该隔离索引后，watch health、doctor 与备份均通过。没有为此修改生产或绕过健康检查。正式自动化和浏览器 QA 的真实 dowse 均在隔离路径使用 `DOWSE_FORCE_SLOW_LANE=1`。

限定：为桌面与移动 viewport / touch 模拟，**不是新的物理手机验收**；不声称覆盖未测浏览器。本阶段基础适配不替代原 v0.4.3 的物理验收记录。

## 交付检查与变更统计

实现提交：`41ab6c9ae6f825cf15aa9aefed10c1975886a53d`，**29 files，+1577 / -44**。包含本报告的交付分支相对 BASE_HEAD 为 **30 files，+1806 / -44**。

以上 BRANCH_HEAD 指完成测试的实现提交；随后仅追加本报告的独立文档提交。最终远端 tip 以交付消息及 `git rev-parse origin/project-center-foundation` 为准。

变更文件：

```text
README.md
docs/architecture.md
docs/data-model.md
docs/webui.md
mcp_v01/cli.py
mcp_v01/jty_materials/backup.py
mcp_v01/jty_materials/doctor.py
mcp_v01/jty_materials/manager.py
mcp_v01/jty_materials/projects.py
mcp_v01/jty_materials/schema_v05.py
mcp_v01/jty_materials/store.py
mcp_v01/project_test_support.py
mcp_v01/run_project_center_tests.py
mcp_v01/test_mobile_ui.py
mcp_v01/test_p05.py
mcp_v01/test_project_manager.py
mcp_v01/test_project_schema.py
mcp_v01/test_project_service.py
mcp_v01/test_project_webui.py
mcp_v01/test_v02.py
mcp_v01/test_v03.py
mcp_v01/webui/__init__.py
mcp_v01/webui/api.py
mcp_v01/webui/app.py
mcp_v01/webui/pages.py
mcp_v01/webui/projects.py
mcp_v01/webui/render.py
mcp_v01/webui/static/app.js
mcp_v01/webui/static/style.css
PROJECT_CENTER_FOUNDATION_REPORT.md
```

最终检查本机证据：`runs/project-center/final-gates.json`、`runs/project-center/sensitive-added-content.json`。

敏感扫描、Python AST / JS 语法、`git diff --check`、盘符根临时产物检查均 PASS；没有跟踪运行 DB、密钥、真实配置、原件或截图。临时数据均在 `<worktree>/runs/project-center/`，C:/D: 根无本任务临时目录。

不 merge、不部署。分支完成后等待代码 review，不开始任何后续阶段。
