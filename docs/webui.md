# WebUI

WebUI 是资料库的本地浏览器界面（stdlib HTTP 服务，无第三方 Web 框架），当前版本 **v0.6.1**（版本号独立于 jty-mcp 存储 schema，本轮 schema 保持 `jty-mcp-0.6.0`）。

缩略图依赖：PDF 缩略图用 PyMuPDF、图片缩略图用 Pillow（均为函数级导入，包缺失时自动跳过缩略图并在日志记录 `webui.thumb_failed`，页面其余功能不受影响）。

## 启动

```powershell
# 方式一：双击项目根目录 start-webui.bat
# 方式二：命令行
python -B mcp_v01/webui_server.py --host 127.0.0.1 --port 8800
```

- 默认绑定 `127.0.0.1:8800`；`--host 0.0.0.0` 是显式局域网开关，请自行评估网络边界。
- 配置默认取 `config/materials.json`，可用 `--config` 指定其他位置。
- 关闭进程（或关闭 bat 窗口）即停止服务。

## 功能总览（v0.4.3）

| 页面 | 说明 |
|---|---|
| 首页 `/` | 关键词检索入口（项目、人员、证书编号或任意文字）、最近材料 |
| 目录 `/browse` | 按类型/核验状态过滤浏览全部材料 |
| 材料详情 `/material/{id}` | 分版本原件查看（SHA256 校验通过才允许展示）、缩略图、OCR 文本按页懒加载、结构化事实（已核验/待核验分级）、字段核验入口 |
| 上传 `/upload` | Web 端入库：选文件 → 后台任务 → 完成后跳转材料页；与管理进程共用同一串行 RPC |
| 状态 `/status` | 运行状态总览与深度体检（doctor）入口 |
| 反馈 `/feedback` | 用户反馈提交与反馈记录查看 |

普通上传和更新原件支持：**PDF / PNG / JPG / JPEG**，单文件不超过 200MB。DOCX / XLSX 暂不支持普通上传，`.doc` 明确不支持；文件选择器、API 和后台任务使用相同白名单。内部 PhaseOcr 的 Office 原生抽取能力保留，但不等于普通 import 支持 Office。完整 DOCX、大 PDF 与历史标书能力属于后续阶段。

资料名称可选：填写时保留用户标题，留空时以文件名 stem 保存非空标题。营业执照识别到唯一企业名称候选后，列表和详情可显示“企业名称 营业执照”的自动建议；详情明确标注其显示元数据属性，不代表企业名称已人工确认。企业名称、统一社会信用代码、成立日期均只产生“待完善”候选，仍须在“完善资料”中核对；对话框可查看原始识别依据及当前原件。

“重新识别”不需要再上传文件：填写经办人后，异步交由 manager 从 DB 查找当前原件，校验版本及 SHA。完整、校验通过的当前 OCR 可直接复用；不完整时才重新 OCR。仅替换待完善候选，不创建 Material 或原件版本，也不删除已核验事实。CLI 同样只接受资料 ID 和经办人（旧 `reextract --record` 用户入口已改为此方式）：

```powershell
python -B mcp_v01/cli.py --config config/materials.json reextract --material-id 123 --actor "实际经办人"
```

营业执照不再突出普通证书式“有效期未记录”；历史有效期字段和投标有效性规则保留，不自动推断长期有效。公开 fixture、OCR 样例及 QA 截图仅使用合成企业标识和测试日期，生产只读调查在公开报告中仅保留是否命中的结论。完整验收见 [v0.6.1 修复报告](../MATERIAL_UPLOAD_EXTRACTION_REPAIR_REPORT.md)。

OCR 或数据库提交前失败不会发布新文件版本。提交后若索引/回复失败，已提交版本和审计回执会保留；请先查看资料及审计，不要把报错视为未写入。办公室操作说明见 [office-use.md](office-use.md)。

## 移动端适配（v0.4.3）

同一套服务端渲染页面，按视口宽度自适应，不维护第二套业务页面：

- 手机（≤640px）上目录页显示**卡片式结果列表**（名称、类别、状态与该类别最相关的 1~2 个业务字段），桌面表格与侧栏仍由服务器同时渲染，仅通过 CSS 切换；筛选/分类收进一个可展开面板，沿用与桌面相同的链接与 GET 表单，URL 语义不变。
- 顶部导航在窄屏压缩为"品牌 + 导航一行、搜索一行"，无横向页面滚动。
- 详情页操作按钮、上传/反馈表单、对话框在触屏上保证约 44px 的点击高度；输入框在移动端使用 16px 字号以避免浏览器聚焦自动缩放。
- 上传页与反馈对话框提供可选的**拍照**辅助入口（`accept="image/*" + capture` 的文件选择变体）：仍走同一个 `/api/upload` 与 `/api/feedback`，格式白名单与 200MB / 20MB 上限不变；相册与文件选择不受影响。桌面端拖拽与 Ctrl+V 粘贴保持原样。
- 反馈按钮与对话框尊重 `env(safe-area-inset-*)`；对话框限高（`100dvh` 带兼容降级）且内部可滚动。
- 无新依赖、无客户端框架；移动端增强仅用 CSS media query 与少量原生 JS（拍照入口、筛选面板为纯 HTML `<details>`）。

## 人工核验（review）

材料详情页可对结构化字段提交人工核验：填写核验值、经办人、依据说明，并绑定当前原件（证据文件 ID + SHA256）。核验请求同样经管理进程串行执行并进入审计台账；换版后旧核验不再自动可信，需对照新版本重新核验。`mcp_v01/review.example.json` 是核验记录的填写样例。

## 反馈功能（v0.4.2）与公开边界

- 反馈入口收集：内容、所在页面（含材料上下文）、可选截图附件、可选经办人。
- **反馈数据与资料库物理隔离**：保存在项目根 `feedback/`（不属于 `repository/`，不入库、不参与 OCR、不进索引、不进备份），首次使用时自动创建：

```
feedback/
├─ index.jsonl              # 追加式索引
└─ FB-YYYYMMDD-NNNN/        # 每条反馈一个目录
   ├─ feedback.json
   ├─ feedback.md
   └─ attachments/
```

- 反馈存储是 append-only；页面标题中的站点后缀会被剥离后再落盘。
- 该目录已列入 `.gitignore`：真实反馈内容与截图不应进入任何 Git 仓库。
- 附件读取接口对路径做了规范化校验，只能访问反馈目录内部的文件。

## 项目中心（WebUI v0.6.0）

顶部提供“首页 / 项目中心 / 资料分类 / 上传资料”。`/projects` 支持关键词、进行中/已归档筛选与分页，手机显示项目卡片。新建项目只需名称，招标编号、招标人、标段可选；系统自动生成项目编号。

项目详情可编辑基础信息，按六个宽分类上传与下载原件，通过“搜索已有素材”复用 Material 检索并建立引用；取消引用保留关系审计，素材详情显示“用于 N 个项目”。归档后可继续查看原件和引用，恢复后可继续写入。项目记录显示创建、编辑、附件与引用事件。没有用户账户时，基础编辑与引用审计使用明确的本机身份“本机用户”；上传与归档表单可填写经办人，不代表权限认证。

项目上传使用独立 `/api/project/file`，支持拖拽和系统文件选择，最大 200MB，仅 PDF/PNG/JPG/JPEG 可 inline，其余项目格式强制下载。与 `/api/upload` 的普通素材上传格式和识别逻辑分开。全部项目 POST 继承 Handler 同源检查，全部业务写入经 manager 串行执行。项目页面与文件读取沿用 maintenance / generation 检查。

路由：

```text
GET  /projects                    /projects/new
GET  /project/{id}                /project/{id}/edit
GET  /project/{id}/upload
GET  /project-file/{id}/{file_id}
POST /api/project/create          /api/project/update
POST /api/project/archive         /api/project/restore
POST /api/project/file
POST /api/project/material/link   /api/project/material/unlink
GET  /api/project/jobs/{job_id}
```

项目文件上传后可手工“提取内容/重新整理”，异步显示等待、整理中、已可搜索、部分未识别、失败；`/project/{id}/search` 搜索本项目 PDF 页、DOCX 章节和 XLSX Sheet/行。状态为 `GET /api/project/{id}/documents`，整理操作为 `POST /api/project/document/{extract|retry|rebuild}`，继续由同源防护和 manager 串行写控制。详情见 [project-documents.md](project-documents.md)。不自动识别名称、不拆分/晋升素材，不增加 AI 或权限模块。

## 安全设计

- 读路径每请求打开只读 Store 连接；所有写操作转交管理进程认证 RPC，Web 层不新增写语义。
- 原件/缩略图服务前强制 SHA256 校验，磁盘内容与登记不一致立即拒绝展示并提示体检。
- 上传体积与类型受限；静态资源与文件路由均做路径规范化，防目录穿越。
- 默认仅回环绑定；Server 头不带敏感信息。
- 所有 POST 在读取请求体和执行 handler 前统一校验：Host 必须是本机名/FQDN、localhost 或当前连接的本地 IP，且端口匹配监听端口；Origin 存在时必须是同一个 HTTP scheme/host/有效端口。不同域名/端口/协议、`null`、畸形或重复头返回 403 并关闭连接。
- 无 Origin 的受控 HTTP 调用继续允许；`Sec-Fetch-Site: cross-site` 即使没有 Origin 也拒绝。浏览器正常使用相对 URL 发请求。CLI/批次工具走本地认证命名管道，不受 HTTP 检查影响。忽略 Forwarded/X-Forwarded-*；当前仅支持直连 HTTP，不配置反向代理信任。
- manager 写失败后在操作锁内验证 DB integrity/FK/已登记文件哈希，修复投影并重建索引、恢复并检查 watch；成功后更新 generation 再清理 maintenance。恢复失败保留 `recovery_required` 原因，后续写入先恢复再执行，不会绕过故障继续写。
- 旧 schema 的显式迁移重试会先独立验证 DB/外键/证据，在 maintenance 内执行原有迁移（保留迁移前备份）；普通业务写入不能绕过旧 schema 的维护状态。

## 与测试的关系

`mcp_v01/test_feedback.py` 覆盖反馈解析、落盘、隔离保证（反馈绝不改动资料库），全部在临时目录运行，不触碰真实 `feedback/` 树。

`mcp_v01/test_p05.py` 覆盖全部 POST 路由、同源/跨站/无 Origin、Office 边界、maintenance 恢复及真实 manager 失败注入。真实进程测试需要 dowse，可设置 `P05_DOWSE_EXE` 指向可用程序；测试会复制程序到临时目录，以独立 index/state/命名管道运行，不共享部署进程。
