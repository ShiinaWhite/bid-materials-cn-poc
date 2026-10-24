# ZERO_FILL_AUDIT.md — v0.7 Zero-Fill 产品重构 · 全系统人工输入审计

- BASE_HEAD: `c627510a93d829eb32866be11f216d2180b27aa5` (origin/main)
- BRANCH: `zero-fill-product-reset`
- WORKTREE: `D:\workSpace\bmc-zero-fill-v07`
- 审计范围: 整个 WebUI（页面模板、客户端 JS、JSON API、multipart 表单、manager RPC 写入口）+ CLI 边界确认
- 分类词表: `REMOVE` / `AUTO_DERIVE` / `AUTO_IDENTITY` / `EXCEPTION_ONLY` / `ADVANCED_OVERRIDE` / `KEEP_INTENT`

判定问题（规范 §2）：**这个值为什么必须由用户填写？** 只有 A（真实业务意图）/ B（机器无法消歧）/ C（法律上必须本人声明）允许保留人工输入。

---

## 0. 结论摘要

| 统计 | 数量 |
|---|---|
| 扫描到的人工输入点（含 API 写入口的隐含输入） | 34 |
| REMOVE（纯仪式性，直接删除） | 6 |
| AUTO_DERIVE（系统已知/可识别，改为自动） | 7 |
| AUTO_IDENTITY（身份类，统一系统身份 `local_webui`） | 8 |
| EXCEPTION_ONLY（仅异常处理路径出现） | 4 |
| ADVANCED_OVERRIDE（高级/手动修正入口，默认隐藏） | 5 |
| KEEP_INTENT（真实用户意图，保留） | 4 |

普通营业执照上传路径（规范 §44 KPI）改造后：`TEXT_INPUT_COUNT=0`、`REQUIRED_SELECT_COUNT=0`、`MANUAL_CLASSIFICATION_COUNT=0`、`MANUAL_TITLE_INPUT_COUNT=0`、`MANUAL_REVIEW_COUNT=0`、`MANUAL_ACTOR_INPUT_COUNT=0`、`MANUAL_CONFIRM_NOTE_COUNT=0`。

---

## 1. Material 上传页 `/upload`（新建，`pages.upload_page` import 模式）

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 1 | `<input type="file" class="file-input">` 选择文件 | pages.py:654 | 必填 | KEEP_INTENT | 保留——文件本身即用户意图（非"填写"） |
| 2 | 拍照上传 `<input capture>`（手机） | pages.py:613 | 可选 | KEEP_INTENT | 保留，同一上传通道 |
| 3 | `<select name="subtype" required>` 资料类别（必选） | pages.py:657 | 必选下拉，26 个类别 | AUTO_DERIVE | 正常入口删除。上传后 OCR 证据 + Vision 分类 + 确定性信号自动分类；不一致→异常。原下拉移入默认隐藏的「高级选项」（ADVANCED_OVERRIDE） |
| 4 | `<input name="title">` 资料名称（可选） | pages.py:659 | 可选 | AUTO_DERIVE | 删除。deterministic title builder 依据已核验/高可信字段命名（§19）；可在高级选项覆盖 |
| 5 | `<input name="actor" required>` 经办人（必填） | pages.py:660 | 必填，localStorage 记忆 | AUTO_IDENTITY | 删除。WebUI 统一诚实系统身份 `local_webui`（§20 ActorProvider）；CLI 维护接口保留 `--actor` |

## 2. Material 更新页 `/upload?material=N`（replace 模式）

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 6 | `<input type="file">` 新原件 | pages.py:637 | 必填 | KEEP_INTENT | 保留 |
| 7 | 拍照上传 | 同上 | 可选 | KEEP_INTENT | 保留 |
| 8 | `<input name="actor" required>` 经办人 | pages.py:640 | 必填 | AUTO_IDENTITY | 删除 → `local_webui` |

## 3. Material 详情页 `/material/N`（`pages.material_page` + `static/app.js`）

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 9 | `data-reextract` 重新识别按钮的 `prompt('请填写经办人…')` | app.js:284 | 弹窗强制输入经办人 | AUTO_IDENTITY | 删除弹窗。点击即以 `local_webui` 提交；点击本身就是 audit event |
| 10 | 完善资料对话框 `<select>` 信息项选择 | app.js:332 | 普通路径入口（data-review 按钮） | EXCEPTION_ONLY | 正常路径消失（§22）：OCR==Vision 且 validator PASS 的字段直接 machine_verified，不再等人工点确认。字段选择仅出现在异常处理与高级「修正信息」中 |
| 11 | 完善资料对话框 值输入框（date/number/text/bool…） | app.js:263 `valueInput` | 普通路径必填 | EXCEPTION_ONLY | 仅异常消歧（使用 OCR / 使用 AI / 手动修改）与高级修正中出现 |
| 12 | 新增资质项名称 `<input>`（customName） | app.js:343 | 对话框内 | ADVANCED_OVERRIDE | 保留在高级修正入口（资质项名称机器无法枚举，属真实意图 B） |
| 13 | 识别候选 chip 点击填入 | app.js:367 | 参考 | KEEP_INTENT | 保留，并升级为异常 UX 的「使用 OCR / 使用 AI」选择（选择即 audit） |
| 14 | 对话框 `<input>` 经办人 | app.js:347 | 必填 | REMOVE | 删除（§14 禁止伪装人工身份；§20 系统身份） |
| 15 | 对话框 `<input>` 确认说明（必填，例如"与资料内容一致"） | app.js:348 | 必填 | REMOVE | 删除（§21 仪式性输入）。系统生成结构化 reason：`dual_engine_consensus` / `human_selected_ocr_candidate` / `human_selected_vision_candidate` / `human_corrected_conflict` / `manual_correction_from_advanced_menu` |
| 16 | 「完善资料」详情页按钮 | pages.py:477 | 普通按钮 | EXCEPTION_ONLY | 正常状态移入「⋯」高级菜单（修正信息）；有异常时突出「处理 N 项异常」 |

## 4. JSON API 写入口

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 17 | `POST /api/upload` query `actor` | api.py:52,61 | 必填校验 | AUTO_IDENTITY | 移除必填校验；服务端固定 `local_webui` |
| 18 | `POST /api/upload` query `subtype`（import 必填） | api.py:53,63 | 必填校验 | AUTO_DERIVE | 改为可选：缺省走自动分类 |
| 19 | `POST /api/upload` query `title` | api.py:54 | 可选 | AUTO_DERIVE | 改为可选；缺省自动命名 |
| 20 | `POST /api/reextract` body `actor` | api.py:81 | 必填 | AUTO_IDENTITY | 移除；`local_webui` |
| 21 | `POST /api/review` body `actor` | review.py:98 | 必填 | REMOVE | 默认 `local_webui`（WebUI 入口不再收集） |
| 22 | `POST /api/review` body `reason` | review.py:99 | 必填 | REMOVE | 结构化 reason 由入口自动生成 |
| 23 | `POST /api/feedback` multipart `actor` | render.py:179 + app.js:571 | 可选输入框 | AUTO_IDENTITY | 删除输入框；不收集（反馈页显示保持诚实留空） |

## 5. Project Center（`webui/projects.py`）

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 24 | 新建/编辑项目 4 字段 `name/tender_no/tenderer/lot_name` | projects.py:68-70 | name 必填 | AUTO_DERIVE + ADVANCED_OVERRIDE | 正常路径=丢入招标文件自动建档（抽取项目名称/招标编号/招标人/标段 → 匹配已有项目 → 加入或新建，§31）。手动表单保留为「手动创建（高级）」 |
| 25 | 新建/编辑项目 hidden `actor="本机用户"` | projects.py:73 | 隐藏假身份 | AUTO_IDENTITY | 统一 `local_webui`（"本机用户"是伪装人名，§14） |
| 26 | 项目文件上传 `<input type="file">` | projects.py:202 | 必填 | KEEP_INTENT | 保留 |
| 27 | 项目文件上传 `<select name="category">` 分类（必选） | projects.py:206 | 必选 6 桶 | AUTO_DERIVE | 正常入口删除；系统按内容自动入桶（§32），冲突→异常；原下拉保留在数据模型与高级选项 |
| 28 | 项目文件上传 `actor_field()` 经办人 | projects.py:27-28,206 | 必填 | AUTO_IDENTITY | 删除 → `local_webui` |
| 29 | 归档/恢复项目 `actor_field()` 经办人 | projects.py:149 | 必填输入框 | REMOVE | "归档这个项目"按钮即完整意图（§4）；经办人输入框删除 → `local_webui` |
| 30 | 引用/取消引用素材 hidden `actor="本机用户"` | projects.py:124,137 | 隐藏假身份 | AUTO_IDENTITY | → `local_webui`；按钮点击即意图 |
| 31 | 引用备注 `note`（payload 可选） | jty_materials/projects.py:94 | 可选 | ADVANCED_OVERRIDE | 正常路径不出现；数据模型保留 |
| 32 | 项目列表/详情搜索框 `q` + 状态 `<select>` | projects.py:51-56,130,175 | 筛选 | KEEP_INTENT | 保留——搜索是真实意图且只读（GET） |
| 33 | 项目文档 提取/重试/重新整理 按钮 | projects.py:113 | 无输入 | KEEP_INTENT | 保留（意图按钮，无填写） |

## 6. 全局 / 其他

| # | 输入点 | 位置 | 现状 | 分类 | v0.7 处理 |
|---|---|---|---|---|---|
| 34 | 反馈对话框 `textarea content` | render.py:172 | 必填 | KEEP_INTENT | 保留——用户表达新信息是真实意图（A 类） |
| 35 | 首页/分类页搜索框 | pages.py:167,352 | 只读检索 | KEEP_INTENT | 保留 |
| 36 | 浏览页筛选 `person`/`validity`/`order` | pages.py:339-358 | 只读筛选 | KEEP_INTENT | 保留 |
| 37 | 维护页深度体检按钮 | pages.py:748 | 无输入 | KEEP_INTENT | 保留 |
| 38 | CLI `--actor`（import/reextract/review） | cli.py:33,43 | 维护接口 | KEEP_INTENT | 保留（CLI 是维护接口，§20）；WebUI 不受影响 |

> 注：`#37/#38` 为边界确认项，不计入上方统计表的 34 个 WebUI 输入点。

---

## 7. 后端写入口语义（v0.7 新增/变更）

| 写入口 | 变更 |
|---|---|
| `workflow.import_material` | `kind/subtype/title` 可全空 → OCR 确定性分类 + Vision 分类（可用时）→ 自动类型；标题由 deterministic builder 生成；随后自动进入 Dual-Engine 共识 |
| 新 manager action `vision_extract` | Vision 直看当前原件页（PDF 只渲染需要的页，内存/tmp→发送→删除），schema 校验后落为候选；失败不阻塞入库（§40） |
| 新 manager action `dual_engine_process` | 对当前原件跑 Vision（如 OCR 候选缺失则先补 OCR），字段级共识，满足 §15 全部硬条件才 machine_verified |
| 新 manager action `exception.resolve` | 异常字段消歧（使用 OCR / 使用 AI / 手动修改），结构化 reason，点击即 audit |
| 新 manager action `material.reclassify` | 仅当分类异常且无人工核验事实/无项目引用时允许（分类异常的唯一安全出口） |
| 新 manager action `project.auto_intake` | 招标文件 → 抽取项目字段 → 匹配/新建项目 → 自动入桶；确定性抽取优先，LLM 仅辅助且可缺席 |
| `project.file.add` 后置 | 同一项目文件若与既有 Material 高置信匹配（如信用代码完全一致且唯一命中）→ 自动 link 并 audit（§33） |
| `store.verify` | 不变（仍是唯一人工核验通道）；WebUI 调用侧不再收集 actor/reason，reason 由入口结构化生成 |
| migration `jty-mcp-0.7.0` | extracted_fact 增列 `verification/verified_at/verification_json`；verification_event 增 `dual_engine_consensus` 方法（表重建）；受控触发器增加机器写入上下文（machine_write）。backup → 事务 → FK 校验；仅测试库验证 |

## 8. 不改动项（有意保留）

- 原件不可变、Material≠Project、LLM 不得直写 SQLite、搜索双底座（dowse + 独立项目 FTS）——全部边界保持（§57）。
- Umi OCR 保留全部职责（§41）；不引入向量库/RAG/聊天 UI（§42、§52）；不做本地 LLM（§53）。
- 已存在 `human_verified` 事实优先级最高，冲突只产生异常，永不静默覆盖（§16）。
- CLI 维护接口的显式 `--actor`（§20）。

## 9. 残余必须人工输入项（预期，改造后）

| 输入 | 原因（§2 判据） |
|---|---|
| 选择文件 / 拍照 | 用户意图本体 |
| 反馈内容 textarea | 用户表达新信息（A） |
| 搜索关键词 | 用户意图（A，只读） |
| 异常消歧的选择（OCR/AI/手动值） | 机器无法消歧（B），点选即 audit |
| 高级菜单：手动分类/改名/修正字段/新增资质项 | 显式覆盖意图（A），默认隐藏 |
| 手动创建项目表单（高级） | 无招标文件时的显式意图（A） |
| CLI `--actor` | 维护接口身份声明（C） |
