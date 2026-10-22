# 移动端 UX 收尾报告（WebUI v0.4.3）

日期：2026-10-22 ｜ 分支：`mobile-ux`（基于 origin/main `e471f73`）｜ 状态：**开发完成，待 review**

按约定：未 merge main、未部署生产、未改动生产工作区。
开发全程在独立 worktree（项目外的短路径目录，下称 `<worktree>`），所有临时测试目录位于 `<worktree>\runs\`，未向任何盘根写入临时目录。

---

## 1. 实际修改文件

| 文件 | 改动 |
|---|---|
| `mcp_v01/webui/pages.py` | +91/−12：browse 页服务端同时渲染桌面表格与手机卡片/筛选面板；上传页新增拍照辅助入口 |
| `mcp_v01/webui/static/style.css` | +143/−2：640px 手机断点全套、dialog 限高与降级、FAB safe-area、触控尺寸 |
| `mcp_v01/webui/render.py` | +7/−2：反馈对话框手机提示文案切换 + 拍照附图入口；theme-color meta |
| `mcp_v01/webui/static/app.js` | +14/−0：两处拍照输入接线（上传表单、反馈对话框），无其他 JS 改动 |
| `mcp_v01/webui/__init__.py` | +1/−1：`WEBUI_VERSION` 0.4.2 → **0.4.3** |
| `mcp_v01/test_mobile_ui.py` | **新增** +219/−0：10 个移动端 UI 测试 |
| `README.md` +2/−1、`docs/webui.md` +13/−2 | 版本号与移动端适配说明 |
| `MOBILE_UX_REPORT.md` | +107/−0：本报告 |

Git 权威统计（`git diff --numstat origin/main...HEAD`，merge-base＝origin/main＝`e471f73`）：
**9 files changed，+600 / −20**。其中产品源码 5 文件 +256/−17、文档 2 文件 +15/−3、新增测试 +219/0、本报告 +110/−0（净额，含 review gate 的 docs 修正 commit）。
`runs/` 全部临时产物已 gitignore，不计入源码改动。**无新增第三方依赖，无数据库/schema 改动，无后端语义变化。**

## 2. 手机端改了什么

### 顶部导航（纯 CSS）
- 窄屏（≤640px）压缩为两行：品牌+导航一行、全宽搜索一行；导航链接点击区 ≥40px；sticky topbar 高度约为视口的 13%（800px 高度下约 104px），远低于半屏。
- 无汉堡菜单；无横向页面滚动（QA 实测 5 个尺寸 `scrollWidth <= clientWidth`）。

### 首页
- hero 留白收紧、搜索框+按钮在 360px 不拥挤；分类 tile 改为 `auto-fill` 自适应网格（360~430px 两列、整齐）；最近资料列表在手机上纵向堆叠（标题/类别/日期不挤压）；到期提醒保留。

### 资料分类 / 搜索结果（本轮最高优先级）
- **服务端同时渲染桌面表格与手机卡片**（同一份 `result["items"]`，`_column_cell` 复用同一套取值与格式化逻辑，无第二套 Python 查询逻辑），由 media query 切换显示：
  - 手机（≤640px）：卡片列表。每卡=类别徽章+状态 chip（在用/已过期/信息完整）+资料名（整卡可点进详情）+该类别最相关的 1~2 个业务字段（复用 `LIST_COLUMNS`：证照=编号/有效期、合同业绩=客户/金额或日期、人员=人员/编号、财务/税务=年度/等级）+查看/下载按钮（复用桌面 `_row_action`）。
  - 平板（641~980px）与桌面：保持原表格与侧栏（平板沿用原有隐藏人员/编号列规则），桌面 1365px 完全不变。
- **筛选**：手机上是原生 `<details>` 折叠面板（零 JS）：summary 常驻显示当前范围与筛选状态（如"公司证照 · 90 天内到期"）；展开后为分类链接 + 与桌面完全相同的那张 GET 表单（同一 Python 字符串渲染两处）；有筛选时提供"清除全部筛选"。**URL query 语义未变**（`q/person/group/type/validity/order/page` 全部原样）。

### 资料详情页（本轮最高优先级）
- 预览图在手机上高度收敛为 210px，不占首屏绝大部分，`object-fit: contain` 无横向溢出。
- 操作按钮（查看文件/下载/更新资料/完善资料）在手机上 2×2 网格、高度 ≥44px；不再是小文字链接。
- 信息区窄屏改为纵向键值（证书编号 ↵ XXXX），不再为 150px label 宽度压扁 value。
- 长标题换行、chips 换行、人员/企业信息不再挤成一行；历史文件表与 OCR 折叠内容通过容器内滚动 containment 不破版。

### 上传页
- 表单输入 100% 宽、按钮全宽 ≥44px、进度条与错误提示位置不变。
- 新增**拍照上传**辅助入口（`accept="image/jpeg,image/png" capture="environment"` 的第二个 file input + 按钮）：仅手机显示（桌面 `display:none`），只是前端选择文件的方式，仍走同一个 `/api/upload`，格式白名单与 200MB 上限不变；主文件选择器 `accept=".pdf,.png,.jpg,.jpeg"` 原样保留，相册/文件选择不受影响。
- 未实现零填写（类别/标题/经办人必填保持不变）。

### 反馈入口
- FAB 尊重 `env(safe-area-inset-bottom/right)`（带 0px 回退），移动端只显示图标+"反馈"短文案，点击区加大。
- 对话框手机文案换成"或选择相册里的照片或截图"（桌面"拖拽/Ctrl+V"保留且功能未删）；新增**拍照附图**入口（同 capture 模式，仍走 `/api/feedback`）。
- 5 张/20MB/PNG-JPG-WEBP 限制与 accept 原样保留（QA 实测带图提交成功）。

### Dialog / 表单统一触控
- `.dialog` 限高 `max-height: 86vh` + `@supports (height:100dvh)` 时 `min(86vh, 100dvh-24px)`（**带兼容降级**），内部可滚动，宽度 `calc(100vw-16px)` 不超视口；操作按钮手机上拉伸 ≥44px；手机输入框统一 16px 字号避免聚焦自动放大。不引入第三方 modal 库。

## 3. 桌面端是否保持（禁止退化清单核对）

- 1365×900 实测：导航不变形、表格/侧栏/工具栏原样（截图 `1365x900-browse.png` 等）；手机组件（卡片/筛选面板/拍照入口）在桌面全部隐藏。
- 桌面拖拽上传、Ctrl+V 粘贴反馈、点击区域、对话框宽度均未改动。
- **P0.5 POST 同源保护保持**：`test_mobile_ui` 直接断言 POST 路由集合与 `do_POST` 中的 `post_allowed` 检查；`test_p05` 21 项（含真实进程回归）全部通过。
- **Office 上传边界保持**：accept 仍只有 `.pdf,.png,.jpg,.jpeg`，错误文案原样（`test_p05.OfficeTests` + mobile 测试双重断言）。
- upload/review/feedback API 无语义变化；原件 SHA256 校验链路未触碰；无 schema 改动。

## 4. 各 viewport QA（Playwright + 系统 Chrome channel，移动端 viewport / touch 模拟）

视口：360×800、390×844、430×932（is_mobile+touch）、768×1024、1365×900。
页面：首页、/browse、/browse?q=安全生产、material 详情（PDF 与 PNG 各一）、/upload、feedback 对话框、review 对话框。

| 检查项 | 结果 |
|---|---|
| 横向溢出（`document.documentElement.scrollWidth <= clientWidth`） | 5 尺寸 × 全部页面 通过 |
| sticky topbar 占视口 <25% | 通过（手机约 13%） |
| 滚动到页底后 FAB 不遮挡任何操作按钮 | 通过（≤980px 底部净空增至 96px） |
| dialog 在 viewport 内且超长时内部可滚动 | feedback/review 对话框全部通过 |
| 手机列表为卡片而非压缩表格；桌面为表格 | 通过 |
| 筛选面板：展开、分类跳转保持 URL 语义、状态显示、清除筛选 | 通过 |
| 手机输入框 16px（防自动缩放）、导航 ≥40px、详情主按钮 ≥44px | 通过 |
| 拍照入口仅手机可见；accept 白名单不变 | 通过 |

**合计 255/255 通过**（明细 `runs/mobile-ux/qa-results.json`，截图 45 张 `runs/mobile-ux/shots/`）。

### 交互流程（390×844 移动 viewport + touch 模拟；非物理手机）
- 上传：`/api/upload` POST + 后台任务轮询 + 结果 UI 实际通过 ✅（用同 SHA 的已入库 PDF 走"重复防重"快速路径）。**本轮没有完成一次新的真实 OCR material 入库**：fixture 环境冷启动的 Umi-OCR 进程收尾不干净会令完整 OCR 入库回滚（回滚机制本身工作正常），属开发机二次实例的环境问题；完整 OCR 后端能力由既有 P0.5 manager/dowse 综合测试提供证据，生产常驻 Umi 不受影响。本轮未为此改动任何产品逻辑。
- 反馈：带图片附件真实提交成功，返回反馈编号 ✅
- 人工核验：review 对话框真实提交 `/api/review` 成功并重载 ✅

## 5. 新增测试与回归数量

- 新增 `mcp_v01/test_mobile_ui.py`：**10 个测试**，覆盖：viewport meta、mobile 卡片/筛选面板 HTML、桌面表格共存、各 scope 卡片字段、上传 accept 白名单 + 拍照入口（import/replace 两种模式）、feedback accept/提示文案/拍照入口、CSS 断点与 dvh/safe-area 降级、JS 无客户端重建列表、P0.5 POST 路由与 `post_allowed` 保持、页面不暴露 material_id/SHA256/OCR/MCP/候选 等内部术语。无 skip。
- 完整套件（最终代码树，显式 `P05_DOWSE_EXE` 指向实际 dowse.exe，真实 manager/dowse 综合测试实际执行）：**116 ran / failures 0 / errors 0 / skipped 0**（106 基线 + 10 新增；P0.5 真实进程回归不再 skip）。
- 定向回归（同一 `P05_DOWSE_EXE`）：`test_mobile_ui + test_p05 + test_feedback` 共 **42 ran / failures 0 / errors 0 / skipped 0**（322.6s，其中 P0.5 真实命名管道 manager 失败注入/回执重放综合测试实际执行）。

## 6. 其他声明

- **新增依赖：无**（Playwright 仅为本机 QA 工具，用户级 pip 安装，不进入仓库/requirements/运行时）。
- **后端语义变化：无**。所有写路径仍走同一 manager RPC；`same_origin.py`、`api.py`、`uploads.py` 零改动。
- 临时产物全部在 `<worktree>/runs/`（gitignore 覆盖），未进 Git；QA 种子库使用独立副本的 dowse.exe 与独立 Umi 端口，**生产 runtime/manager/WebUI 全程未被触碰**。
- ROADMAP.md 位于生产工作区，按约定本轮未修改；建议 review 通过并部署后再勾选"移动端 UX 收尾"。

## 7. 分支与交付

- 分支：`mobile-ux`，基于 `origin/main` `e471f7387604e67db310551e694ebe003b4bb58b`
- 变更：9 files changed，+600 / −20（`git diff --stat origin/main...HEAD`，见 §1）
- **是否适合进入 review：是**
- **PHYSICAL_MOBILE_ACCEPTANCE = PARTIAL** —— 物理手机验收待本分支 review 通过、部署到 LAN 后由用户本人用物理手机完成（首页搜索 → 分类/筛选 → 详情 → 查看原件 → 下载 → 上传页调出文件/相册/拍照选择器 → 带图反馈）。上文的 Playwright 检查属于移动端 viewport / touch 模拟浏览器 QA，不等价于物理手机验收。
