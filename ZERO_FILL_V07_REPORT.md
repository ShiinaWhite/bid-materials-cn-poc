# ZERO_FILL_V07_REPORT.md — v0.7 Zero-Fill 产品重构交付报告

## 头部标识

```text
REVIEW_BASE_HEAD    = 123be8d3c6f54043bb4fb0a55ecd2d7a2ac875e3 (review 起点)
FUNCTIONAL_FIX_HEAD = d93cb30 (review 修补轮的功能修复提交)
REPORT_COMMIT       = self（本报告提交自身；最终 HEAD 以 review 时 branch HEAD 为准）

历史说明：主实现 = e2e929e（+2af8274 清理、+报告 commit），BASE = c627510（origin/main）。
BRANCH      = zero-fill-product-reset
WORKTREE    = D:\workSpace\bmc-zero-fill-v07

WEBUI_VERSION    = 0.7.0
SCHEMA_VERSION   = jty-mcp-0.7.0
```

## 版本内容一览

新增模块：`providers.py`（VisionProvider/LLMProvider 抽象 + OpenCode Go 实现）、`normalize.py`（canonical normalization + GB 32100-2015 统一社会信用代码校验）、`classify.py`（确定性 OCR 信号 + 双信号合成）、`vision.py`（Vision 直看原件、按页渲染、schema 校验）、`consensus.py`（字段级共识、machine verify 硬条件、异常消歧、other→typed 受控重分类）、`titling.py`（确定性命名）、`schema_v07.py`（显式 migration）。

改写入口：零填写上传（无类别/名称/经办人必填）、导入后自动双引擎、异常优先详情页、项目中心自动建档/自动入桶/USCC 精确自动链接。

## 交付门禁

```text
ZERO_FILL_AUDIT = PASS  （ZERO_FILL_AUDIT.md，34 个 WebUI 输入点全覆盖）

TOTAL_EXISTING_INPUTS_FOUND = 34（另有 4 项边界确认：CLI/只读搜索等）
REMOVED            = 6   （经办人×3、确认说明、隐藏假身份"本机用户"×2 的表单形态、重新识别弹窗）
AUTO_DERIVED       = 7   （上传类别、上传名称、项目四字段、项目文件桶、Feedback 身份等）
AUTO_IDENTITY      = 8   （全部写入口统一 local_webui）
EXCEPTION_ONLY     = 4   （异常消歧的字段选择/值输入、分类异常确认）
ADVANCED_OVERRIDE  = 5   （高级选项的类别/名称、手动建项、修正信息、引用备注）
KEEP_INTENT        = 4   （搜索、反馈内容、意图按钮、文件选择）

NORMAL_MATERIAL_TEXT_INPUTS         = 0 (PASS)
NORMAL_MATERIAL_REQUIRED_SELECTS    = 0 (PASS)
NORMAL_MATERIAL_MANUAL_REVIEW       = 0 (PASS)  # 共识成功时 requires_human_review=False

DUAL_ENGINE                 = PASS
OCR_INDEPENDENT             = PASS   # Umi 链与 Vision 链分别绑定同一 original SHA，互不引用
VISION_INDEPENDENT          = PASS   # Vision 输入为原件页像素（PDF 按页内存渲染），绝不取 OCR 派生物
CONSENSUS_ENGINE            = PASS   # 字段级 canonical 相等 + 9 项硬条件 + 触发器级写入护栏

MACHINE_VERIFIED_SEMANTICS  = PASS   # verification_event.verification_method='dual_engine_consensus'
                                     # verified_by='machine:vision:opencode-go:deepseek-v4.1-flash'
                                     # 未授权机器写（无上下文 SQL）被 guard 拒绝（测试验证）

AUTO_CLASSIFICATION = PASS   # 真实链路：合成执照 → business_license（OCR 头 + Vision 一致）
AUTO_TITLE          = PASS   # <企业名称> 营业执照（确定性 builder，仅 filename_fallback/auto 来源可改写）

ORIGINAL_FIRST_UI = PASS     # 原件大图/PDF 框架为主体，识别信息折叠
NORMAL_HIDDEN     = PASS     # 正常态：下载/更新原件/⋯ 三控件；无按钮墙
EXCEPTION_FIRST   = PASS     # ⚠ 横幅置顶；冲突卡内 [使用OCR][使用AI][查看原件][手动修改]，无经办人/说明

PROJECT_ZERO_FILL = PARTIAL
  # 已实现：招标文件自动建档（确定性抽取 项目名称/招标编号/招标人/标段 → 精确匹配加入或自动新建）、
  #         项目文件自动入桶（首匹配关键词规则）、USCC 精确唯一命中自动引用（点击留审计）
  # 未实现（deferred）：LLM 辅助字段抽取、多候选自动链接、Zero-Fill Inbox 统一入口

REAL_UMI_INTEGRATION     = PASS  # test_zero_fill_real + 回归中的真实 Umi 集成测试
REAL_VISION_INTEGRATION  = PASS  # OpenCode Go / deepseek-v4.1-flash 直读原件图像
                                 # （用户提供 key，经环境变量注入；接口实测见下）

FULL_REGRESSION = 251 tests, 0 代码失败
  # 回归 runs/large-document/regression-20261024-163613：1 error + 1 fail 均为
  # LARGE_DOC_UMI_EXE 未导出的环境性阻塞；补环境变量后单独复跑 PASS（93.8s）。
  # 1 skip = test_zero_fill_real（无 OPENCODE_GO_API_KEY 时跳过；带 key 已单独 PASS）。

DESKTOP_QA = PASS  # 1365×900：上传/列表/已核验详情/异常详情/分类异常/项目新建，无横向溢出，无按钮墙
MOBILE_QA  = PASS  # 390×844：七页无横向溢出，移动卡片/筛选正常
           # ⚠ 如实说明：本会话 IAB 截图面持续超时，视觉结论基于 DOM/ARIA 结构断言、
           # 布局类 CSS 断言（测试）与溢出测量；未做像素级目检。

PUBLIC_FIXTURE_REAL_DATA_HITS = 0   # 全部合成（合成测试示例建筑工程有限公司 / 91JTY…X013 / JTYZB-…）

PRODUCTION_TOUCHED = NO
MERGED_TO_MAIN     = NO
DEPLOYED           = NO
```

## FINAL_REVIEW_FIXES（review 修补轮，REVIEW_BASE=123be8d… → FUNCTIONAL_FIX=d93cb30）

```text
REQUIRES_HUMAN_REVIEW_EXCEPTION_ACCOUNTING = PASS  # manager.review_required() 纯函数 + 用例：
                                                   # policy_skipped-only=False；single_chain/validator_failed/
                                                   # ambiguous/human_conflicts/dispute/classification/vision失败=True
POLICY_SKIPPED_DISTINGUISHED = PASS  # scope（iso_cert 合法字段）共识进入 policy_skipped，不强制人工
FINAL_DISK_SHA_RECHECK = PASS        # BEGIN IMMEDIATE 临界区内：disk SHA == DB SHA == 识别时 SHA == version 绑定
TOCTOU_MACHINE_VERIFY_GUARD = PASS   # 识别中途篡改原件 bytes → 整次共识拒绝；0 machine_verified 标记、
                                     # 0 dual_engine_consensus 事件、org 列未写（无部分写入）
OVERSIZED_IMAGE_MIME = PASS          # 超大 PNG 传输副本重编码 JPEG 且声明 image/jpeg（magic bytes 断言）
PILLOW_FALLBACK = PASS               # sys.modules 屏蔽 PIL → 原始 bytes 原样透传、mime 与内容一致；
                                     # 生产部署本就依赖 Pillow（缩略图/OCR 链），本轮仍做了缺依赖退化
PROJECT_IDENTITY_REQUIRED_FOR_AUTO_CREATE = PASS  # 仅文件名、正文无身份 → insufficient_project_identity，
                                                  # 不建项目；正文有项目名称 → 正常建档
VISION_ONLY_TYPED_CLASSIFICATION = BLOCKED   # 已移除 vision_only 强绑定通路
VISION_ONLY_BECOMES_EXCEPTION = PASS         # OCR 无信号 + Vision 0.99 → other + conflict +
                                             # suggested type 保留在 classification.vision_signal/basis
MACHINE_DIRECT_SQL_GUARD = PASS              # 无 machine 上下文的普通 Store 连接 UPDATE org/certificate_detail
                                             # 受保护事实列 → SQLite 触发器拒绝，数据不变

MIGRATION_BACKUP_NAME = FIXED        # schema-before-jty-mcp-0.7.0-*.db（此前误写 v06）
PROJECT_BUCKET_CONTENT_CLASSIFY = FIXED  # project.file.add 现以文件名 + 有界原生文本分类（txt/md/docx/pdf
                                         # native 前页，无 OCR），文案同步改为"根据文件名及可识别内容"

TARGETED_TESTS   = test_zero_fill_review 20/20 PASS + test_zero_fill 回归 42/42 PASS
                 + 受影响既有套件（project_manager/service/webui、upload_repair、p05 Origin/RealManager）
                   57 tests：1 个环境性错误（P05_DOWSE_EXE 未导出）补环境后单独复跑 PASS
PY_COMPILE       = PASS
GIT_DIFF_CHECK   = PASS

FULL_REGRESSION = NOT_RERUN（沿用 123be8d 前的 251 tests / 0 代码失败结果；本轮仅改
                  共识分桶/mime/分类/项目 intake，均被上述 targeted tests 覆盖）
REAL_UMI        = NOT_RERUN（沿用此前 PASS）
REAL_VISION     = NOT_RERUN（沿用此前 PASS；本轮未触碰 provider 传输语义以外的行为）
BROWSER_QA      = NOT_RERUN（沿用此前 DOM/溢出 PASS；本轮未改页面结构，仅一句文案）

PRODUCTION_TOUCHED = NO
MERGED_TO_MAIN     = NO
DEPLOYED           = NO
```

## OpenCode Go 实际接口（实测，非记忆）

```text
base      = https://opencode.ai/zen/go/v1 （本机 provider_config 实配）
api       = OpenAI chat-completions 兼容 POST /chat/completions
认证      = Authorization: Bearer <key>；key 仅来自 OPENCODE_GO_API_KEY 环境变量
            或 git-ignored 部署配置；绝不入库/审计/日志/报告/fixture
必带头    = x-opencode-session（每次提取运行一个稳定 UUID）+ 自定义 User-Agent
            （通用 UA 被 Cloudflare 1010 拦截）
vision    = deepseek-v4.1-flash 支持 image_url data URL（实测读出合成执照文字）
响应      = 含 reasoning_content；content 为最终答案；max_tokens 需覆盖思考消耗
/models   = 网关 403（不可用）；模型名以配置为准
存储记录  = provider/model/purpose/status/latency/response_hash（events.jsonl），
          不记录 key、不落完整图片、不落完整响应
```

## machine_verified 硬条件（全部满足才自动核验）

同一 current original（file_id+SHA 双重复核）、双链独立、canonical 完全相等、字段在策略白名单
（scope/audit_opinion/title/qualification:* 排除；>200 字符排除）、确定性 validator PASS
（信用代码必须过 GB 32100-2015 校验位）、证据可绑定回原件、事务内复核 is_current、
不与已有人工事实冲突（人工优先，冲突只产生异常）。替换原件即清空全部机器标记，
共识对新原件重新推导；人工事实永不被静默覆盖。

## 数据升级

- 显式 migration `jty-mcp-0.6.0 → jty-mcp-0.7.0`（backup → 事务 → integrity/FK 校验），已在
  真实 0.6.0 库（main 工作树生成）上验证：数据保留、105 个统一事实触发器重建、9 个视图完整。
- extracted_fact 增列 verification/verified_at/verification_json；verification_event 重建
  （新增方法 `dual_engine_consensus`）；guard 同时承认人工上下文与机器上下文，其余照旧拒绝。
- 顺带修复历史怪癖：certificate_detail.standard 在 v0.6.1 存在 v02+v03 双触发器重复记录
  verification_event，统一重建后每列单一触发器。
- 生产库未迁移、未部署（等待 review）。

## 残余必须人工输入项（预期内）

| 输入 | 判据 |
|---|---|
| 选择/拍摄文件 | 用户意图本体 |
| 异常消歧点选（OCR/AI/手动值） | 机器无法消歧（B）；点选即审计 |
| 高级菜单：指定类别/自定义名称/修正信息/新增资质项 | 显式覆盖（A），默认隐藏 |
| 手动创建项目表单（高级） | 无招标文件时的显式意图（A） |
| 搜索关键词、反馈内容 | 真实意图（A，搜索只读） |
| CLI `--actor` | 维护接口（C） |

## 已知风险 / Deferred

1. **视觉字形混淆**：deepseek-v4.1-flash 对信用代码类字段偶发误读（实测 1 次被判格式无效）。
   系统行为符合规范：拒收→单链→不核验→留待完善；实测重跑全字段通过。分辨率上限已提至 2200px。
2. **重分类范围**：仅支持 `other → 任意类型`（无人工事实、无项目引用时）；证书类型间的转换
   因 certificate_kind 不可变而引导重新上传（诚实降级，未造危险通路）。
3. **项目自动建档**：确定性正则抽取，未见字段进入 EXCEPTION_ONLY（不猜）；LLM 辅助抽取 deferred。
4. **自动链接**：仅 USCC 精确匹配且唯一命中；多候选跳过并标记 needs_review。
5. **Provider 运维**：key 过期时行为为诚实降级（vision_deferred + requires_human_review=True），
   上传永不失败；更换 provider 仅需实现同类接口。
6. **测试卫生**：API 级测试 mock 了 job 执行，避免夹具拉起真实 manager；集成测试一律
   copy_runtime 隔离（曾因指向生产运行时被生产 dowse watch 阻塞索引，已修正）。
7. **浏览器截图**：本会话 IAB 截图面超时，QA 以 DOM/ARIA + 溢出测量 + CSS 断言替代像素目检。

## 验收命令

```bash
cd D:\workSpace\bmc-zero-fill-v07\mcp_v01
python -B -m unittest test_zero_fill            # 42 tests（无外部依赖）
set LARGE_DOC_UMI_EXE=... & set P05_DOWSE_EXE=... & set OPENCODE_GO_API_KEY=...
python -B -m unittest test_zero_fill_real       # 真实 Umi+Vision 端到端
python -B run_project_center_tests.py           # 全量回归
```
