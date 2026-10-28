# Project Document Intelligence — v0.8 Phase 1 Review Fix Report

分支 `project-document-intelligence-v08`。本轮只处理 4 个 merge-gate blocker
及 §7 回归门，未新增任何范围外功能。

## Blocker 判定

```text
BLOCKER_1_MULTI_SUBJECT_DEDUP      = PASS
BLOCKER_2_USCC_VALIDATION          = PASS
BLOCKER_3_STALE_AUTOLINK           = PASS
BLOCKER_4_TRANSIENT_CLASSIFICATION = PASS

NEW_TESTS     = PASS   23/23（11 原有 + 10 review-fix + 1 guards 门 + 归并）
REGRESSION    = PASS   documents×5 / project×4 / zero_fill×3 / upload_repair /
                        mobile / feedback / p05 / provenance / reextract /
                        v01 / v02 / v03 / batch（短路径 D:\b8，MAX_PATH 约束）全绿
PY_COMPILE    = PASS
REAL_FIXTURE  = PASS   cached acceptance：complete / 53 张全部复用 / 0 次引擎
                        调用 / 6.8s / 图片 USCC 与"营业执照"仍可搜 /
                        autounlinked=0 / 无吞候选（见下节逐项说明）

PRODUCTION_TOUCHED = NO
MERGED_TO_MAIN     = NO
DEPLOYED           = NO

MERGE_READY = YES（就本轮 4 个 blocker 与 §7 门而言）
```

## HEAD 记录（三段式）

```text
BASE（本轮起点）            = d4fd4c3（Phase 1 功能 af2733c + 报告提交）
REVIEW_FIX_FUNCTIONAL_HEAD  = a284a1a  fix(v0.8): resolve the four merge-gate review blockers
REPORT_COMMIT               = c948931（本报告提交，自引用说明见 git log）
BRANCH_TIP_AT_REPORT_PUSH   = REPORT_COMMIT（报告为分支最后一个提交后立即 push）
```

## BLOCKER 1 — 多主体 dedup（discovery.candidate_identity）

强度阶梯实现：

```text
1) 确定性主体身份： type + uscc_valid 通过的 USCC / 证件编号 / 人员身份证号（脱敏键）
2) 主体名称：      type + org_norm(subject_name)（复用 fields.org_norm，非裸字符串）
3) 主体不可靠时：  type + source unit identity —— 跨来源永不静默合并
```

测试：`test_different_subjects_same_type_without_strong_id_kept`（A/B 公司 ISO
均无 USCC/证号/名称 → 2 条候选）；`test_same_subject_normalized_name_still_dedupes`
（不同 label 前缀同名 → org_norm 合一）；`test_strong_uscc_identity_dedup_across_pictures`
（同 USCC 重复出现 → 仍去重）。

## BLOCKER 2 — USCC 校验（复用 normalize.uscc_valid）

`extract_usccs` 现在只返回通过 `uscc_valid`（canonical_credit + GB 32100-2015
校验位）的码。未实现第二套规则，无重复算法。无效 18 位串留在 evidence，但
`subject_uscc=None` → 不升高 confidence、不参与 exact、不触发 autolink。

测试：`test_uscc_checksum_gate`（`…KQ09` 收 / `…KQ08` 拒且 e2e 后
subject_uscc 为空）；`test_person_id_number_is_never_an_org_uscc`
（`142601199001011234` 不成为组织 USCC，同图内真 USCC 正常胜出）。

## BLOCKER 3 — 陈旧 autolink 撤销（discovery.retire_unsupported_autolinks）

规则实现：

- 仅撤销 `added_by='project-intelligence'` **且** note 可解析且
  `method='project_document_exact_match'` 的引用；手工链接与 provenance
  不可判定的引用永不触碰。
- 保留判定为 **project 级**：任一文档的 active exact candidate 仍支持该
  Material 即保留（跨文档共享安全）。
- 撤销写 `status='removed'`（note 记录 `…_no_longer_supported` 与原始
  provenance）并审计 `project.material.autounlink /
  project_document_exact_match_no_longer_supported`。
- 证据回归时 `_link_exact` 恢复 active 并写入**新的** exact_match provenance
  note（修复了 Phase 1 恢复时 note 置空的缺陷），可再次安全撤销。

测试×4：证据消失→自动 removed+审计；手工链接永不被撤；双文档共享时单文档
失据不撤；removed 后证据回归→恢复 active+provenance 正确+第二次 autolink 审计。

## BLOCKER 4 — transient 收紧（discovery.transient_evidence）

```text
强 marker 单独命中： 信用中国 / 公共信用信息报告 / 失败=失信被执行人查询 /
                     裁判文书网查询 / 国家企业信用信息公示系统 / 政府查询域名
组合规则：           查询时间 + 查询结果 同时出现
不再单独成立：       报告编号 / 生成时间 / 严重违法 / 查询结果（单出）
```

测试：即时查询（信用中国 或 时间+结果对）→ transient；正式评级报告
（报告编号+生成时间+AAA）→ 非 transient（credit_report 不在 reusable 集 →
unknown，符合"policy 无法确定则 unknown"）。

## §7 — 迁移守门回归（新增 V08MigrationGuardTests）

初始化（v1→v0.8 全链）后断言触发器齐全：v0.5 项目族（project/project_file/
project_material 的 no_replace/no_delete/controlled_INSERT/UPDATE +
两个证据表的 active_* + identity/path 触发器）、v0.6/v0.8 文档族（五表 ×
INSERT/UPDATE/DELETE controlled_* + document_source/identity/unit_binding）、
audit_log append-only 三件套。全部在位。

## 真实 83MiB fixture 回归（cached，生产零触碰）

```text
REAL_DOCX_IMAGES = 53   OCR_CACHE_REUSED = 53/53（0 次引擎调用，6.8s，complete）
IMAGE_FTS         = PASS（营业执照 8 命中 / 中保源 35 / 图片内 USCC 6）
invalid candidate swallowing = NO（无强 ID 候选不再被合并，见下）
```

候选数 10 → 10，构成变化全部由本轮修复解释：

| 变化 | 原因 |
|---|---|
| iso_cert 3 → 1 | 三张珠峰电缆体系证书共享 valid USCC，按 BLOCKER 1 A 层（type+USCC）合并为一条主体线索；三张图片证据与 FTS 不变 |
| business_license 2 → 1 | 商务表格文字行（老执照号引用）与扫描件图片同属 valid USCC 主体，正确合并；Phase 1 误存两条 |
| financial_statement 1 → 3 | **BLOCKER 1 救回**：此前无强 ID 全部退化为同一 identity 只留 1 条；现按名称/来源恢复为 3（闻达事务所 / 中保源 / 未识别主体） |
| person_id_card 1 → 2 | 同上救回：两张无名人员证件按 source identity 保留 |
| audit_report / award_notice / tax_payment_cert | 不变 |

主体仍含中保源（USCC 高置信）与珠峰电缆；exact_matches=0、autounlinked=0
（素材库无同主体 USCC，与 Phase 1 结论一致）。

## 既有能力保持

AUTO_EXTRACTION / SUMMARY_UI / 图片发现·OCR·FTS·锚点 / 候选发现 / 多主体 /
EXACT 匹配 / NEW_CANDIDATE / 无单公司假设 —— 由未改动的 11 项 Phase 1 测试
与全量回归持续覆盖，全部 PASS。

## STOP

功能提交 `a284a1a` + 本报告提交推送至 `origin/project-document-intelligence-v08`
后停止。未合 main、未部署。等待第二次独立 code review。
