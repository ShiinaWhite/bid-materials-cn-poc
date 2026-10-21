# 投标素材库（bid-materials-cn-poc）

一个面向中小企业投标场景的**本地素材库**：把证书、合同业绩、人员、财务等投标材料原件集中保管，通过 OCR 与 SQLite 建立可检索、可核查的结构化事实层，并对外提供只读 MCP 工具和一个本地 WebUI。

- 运行平台：Windows 本地单机（已在 Windows 10 1809 / Python 3.13 实际验收）
- 核心链路仅用 Python 标准库；图片/PDF 派生能力依赖少量第三方包（见下文「第三方 Python 依赖」）；OCR 与全文检索由两个外部运行时提供
- 架构原则：**MCP 只读**，一切写入（入库、换版、核验、备份）由人工通过离线 CLI / WebUI 受控执行
- 本仓库**不包含任何真实投标材料**、真实公司数据或真实个人信息；仓库自身即可从零初始化一个空资料库

## 主要能力

- **原件保管**：PDF/PNG/JPEG 原件只复制、不修改，逐文件登记 SHA256，展示前强制校验；同件换版保留全部历史版本
- **OCR 派生层**：调用本机 Umi-OCR 生成逐页原始 OCR（不可覆盖保存）与 NFKC 检索投影；原件永不转换
- **结构化事实层**：SQLite 事实库，字段为封闭业务词汇（营业执照、安全生产许可证、承装修试、建筑业资质、ISO、合同业绩、人员、审计/财务等）；OCR 候选值与人工核验值严格分级，只有人工核验过的事实才作为已核验事实返回
- **全文检索**：dowse（Tantivy + jieba）本地索引，MCP `search_materials` 做 SQLite 过滤 + 中文相关度排序
- **只读 MCP**：stdio JSON-RPC，三个工具 `search_materials` / `get_material` / `get_material_text`，无任何写入工具
- **WebUI**：本地浏览器界面，覆盖浏览、检索、上传入库（PDF / PNG / JPG / JPEG）、人工核验、运行状态体检与用户反馈
- **批次入库**：离线批次计划 + 逐项回执，支持中断恢复，不提供批量写入 MCP
- **备份恢复**：带完成标记与哈希清单的一致性备份；索引与运行态可随时重建

## 目录结构

```
├─ README.md / LICENSE / .gitignore
├─ start-webui.bat            # WebUI 启动脚本（Windows）
├─ config/
│  └─ materials.example.json  # 唯一运行配置的示例（实际配置由 init 自动生成，不入库）
├─ mcp_v01/
│  ├─ jty_materials/          # 自包含运行代码（store/manager/workflow/query/mcp/schema 等）
│  ├─ webui/                  # WebUI 实现（stdlib HTTP 服务 + 静态资源）
│  ├─ webui_server.py         # WebUI 入口
│  ├─ cli.py                  # 离线 CLI：init/入库/换版/核验/维护/备份恢复
│  ├─ serve.py                # stdio MCP 启动入口
│  ├─ manage.py               # CLI 自动拉起的后台管理进程（无需手工运行）
│  ├─ batch.py                # 离线批次入库工具
│  ├─ requirements.local.txt  # 可选能力与测试所需的第三方包快照（见「第三方 Python 依赖」）
│  └─ test_*.py               # 正式回归测试（全部在临时目录运行）
└─ docs/
   ├─ architecture.md         # 架构与分层设计
   ├─ webui.md                # WebUI 功能与边界
   └─ data-model.md           # 事实库数据模型
```

## Windows 本地运行要求

1. **Python 3.13**。核心链路（PDF 入库走 Umi-OCR、MCP 查询、WebUI 浏览/反馈、CLI 维护）只使用标准库；仅下列能力需要 `pip install -r mcp_v01/requirements.local.txt`
2. **Umi-OCR**（Rapid 版，https://github.com/hiroi-sora/Umi-OCR ）——负责 OCR；验收版本为 Umi-OCR Rapid 2.1.5
3. **dowse**（https://github.com/ltspace/dowse ）——负责中文全文检索（Tantivy + jieba）
4. 运行**完整测试套件**必须安装 `mcp_v01/requirements.local.txt`（其中 3 个包也被下列可选能力使用）

## 第三方 Python 依赖

| 包 | 启用的能力 | 缺失时行为 | 许可证（以安装元数据为准） |
|---|---|---|---|
| Pillow | PNG/JPEG 材料入库（EXIF 摆正）、图片缩略图 | 图片材料无法入库/无缩略图；PDF 材料入库不受影响 | MIT-CMU |
| PyMuPDF | PDF 缩略图、PDF 原生文本抽取（native text）、文档富化 enrich、reextract | WebUI 缺 PDF 缩略图；上述命令返回明确错误，系统其余功能不受影响 | **AGPL-3.0 或 Artifex 商业许可（双许可）** |
| openpyxl | 内部 PhaseOcr 的 XLSX 原生文本抽取（普通 WebUI 上传暂不支持） | 仅 XLSX 抽取不可用 | MIT |
| python-docx | 仅测试夹具生成（test_phase1） | 不影响任何运行功能 | MIT |

降级是代码内建行为：WebUI 缩略图在缺包时自动跳过并记日志；缺包时的写入类命令经管理进程统一返回错误，不会中断服务。

**第三方组件许可说明**：本仓库自行编写的代码以 MIT License 发布。PyMuPDF 是可选的第三方组件，由其自身的 AGPL-3.0 或 Artifex Commercial License 管辖；本仓库不分发 PyMuPDF。安装、部署或通过网络提供使用包含 PyMuPDF 的组合系统时，使用者应自行确保满足适用的 PyMuPDF/AGPL 许可义务，或取得 Artifex 商业许可。

OCR 与检索运行时的安装包不随本仓库分发，请从上述官方发布页下载。`init` 会把两个运行时复制进 `local/runtime/` 并为本副本自动分配 Umi 端口。

> **路径长度提示**：本项目面向未启用 Windows 长路径策略的旧版系统设计，目录名刻意保持短小。请把仓库放在**短路径**下（如 `D:\repo`、`D:\bid-materials`），避免放在深层临时目录或桌面多层嵌套中——入库材料的版本目录嵌套较深，超长路径会导致备份/换版等文件操作失败（`WinError 3`）。

## 从零初始化空资料库

```powershell
# 在项目根目录执行；--umi/--dowse 指向你本机下载解压后的对应程序
python -B mcp_v01/cli.py init --umi "C:\tools\Umi-OCR.exe" --dowse "C:\tools\dowse.exe"
```

`init` 会：

- 生成 `config/materials.json`（唯一运行配置；所有路径相对配置文件所在目录，不受当前工作目录影响）
- 创建 `repository/`（原件与 SQLite 事实库）与 `local/runtime/`（Umi/dowse 私有副本）、`local/state|logs|backups|index`
- 初始化空 schema；不会覆盖任何已有配置或库

随后建议跑一次体检：

```powershell
python -B mcp_v01/cli.py doctor
python -B mcp_v01/cli.py health
```

## 启动 CLI / MCP / WebUI

```powershell
# CLI：入库 / 换版 / 人工核验 / 维护 / 备份恢复
python -B mcp_v01/cli.py import --source "C:\incoming\证书.pdf" --kind certificate --subtype business_license --title "营业执照" --actor "经办人"
python -B mcp_v01/cli.py get --material-id 1
python -B mcp_v01/cli.py review --record mcp_v01/review.example.json   # 核验记录样例见该文件

# MCP（stdio，只读）：接入 MCP 客户端
python -B mcp_v01/serve.py --config config/materials.json

# WebUI（本地浏览器界面；或直接双击 start-webui.bat）
python -B mcp_v01/webui_server.py --host 127.0.0.1 --port 8800
```

MCP 客户端配置：`python -B mcp_v01/cli.py client-config` 可按本机实际绝对路径生成客户端启动配置片段（绝对路径因机器而异，故不入库）。

全文索引由后台 watch 维护：

```powershell
python -B mcp_v01/cli.py watch start    # 无索引时自动重建；CLI 退出后后台继续运行
python -B mcp_v01/cli.py watch status
python -B mcp_v01/cli.py shutdown       # 停止管理进程
```

## WebUI 当前功能（v0.4.2）

- 首页与目录：关键词检索、按类型/状态过滤浏览
- 材料详情：分版本查看原件（SHA256 校验通过才允许展示）、缩略图懒加载、OCR 文本按页查看
- 结构化事实：已核验事实与待核验候选分级展示
- Web 上传入库：走与管理进程相同的串行 RPC；OCR 失败不发布新版本
- 人工核验：对照原件在 Web 端提交核验记录（证据绑定文件 + SHA256）
- 运行状态与深度体检（doctor）：库完整性、文件哈希、索引与运行时健康
- 用户反馈：右下角反馈入口；反馈数据默认保存在项目根 `feedback/`，**不属于资料库 repository**，首次使用时自动创建

普通 WebUI 上传和更新原件仅支持 **PDF / PNG / JPG / JPEG**（单文件不超过 200MB）。DOCX / XLSX 暂不支持普通上传，`.doc` 不支持；内部 PhaseOcr 的部分 Office 原生抽取能力不代表普通入库已支持 Office。完整 DOCX、大 PDF 与历史标书能力留待后续阶段。

办公室使用步骤与失败后的处理见 [办公室使用说明](docs/office-use.md)。

## 分层原则：原件 / OCR / 结构化事实 / MCP

1. **原件**是唯一权威：只复制、永不修改或转换；每个文件登记 SHA256，不一致立即拒绝展示
2. **OCR（含 NFKC 投影）**只是派生数据：原始逐页 OCR 不可覆盖保存，可随时从原件重做
3. **结构化事实**分级：OCR 自动抽取的候选值永远是“待核验”；只有人工对照原件提交核验记录（绑定证据文件与哈希）后才成为已核验事实；未复核时不返回旧事实
4. **MCP 只读**：模型能查不能改；文档内容（OCR/事实值）一律作为不可信数据处理，绝不当作指令执行

## 数据安全原则

- 默认只绑定 `127.0.0.1`；如需局域网访问请自行评估网络边界后修改绑定
- 所有浏览器 POST 校验直连 Host 与同源 Origin；跨站写入返回 403。本机、电脑名、当前 LAN IP 的同源访问均可；无 Origin 的受控调用保留兼容。此机制不提供登录或用户权限，反向代理不在当前支持范围。
- 一切写入集中在离线 CLI / WebUI → 管理进程串行 RPC；管理通道经认证的本地命名管道
- 全部写操作进入追加式审计台账；SQLite 触发器兜底拦截越权 SQL 写入
- `config/materials.json`、`repository/`、`local/`、`feedback/`、运行日志与缓存均已列入 `.gitignore`，真实业务数据与真实配置不应进入任何 Git 仓库
- 反馈数据（含截图附件）独立存放在 `feedback/`，与资料库物理隔离

## 运行测试

```powershell
cd mcp_v01
python -m unittest discover -p "test_*.py"
```

测试全部在临时目录创建独立资料库运行，不读取、不修改真实库与真实配置。

## 许可证

见 [LICENSE](LICENSE)（MIT）。第三方 Python 依赖的许可证见上文「第三方 Python 依赖」；Umi-OCR 与 dowse 为外部运行时，其许可证与分发遵循各自上游项目（分别为 MIT、Apache-2.0/MIT 双许可）。
