# 数据模型

资料库由两部分组成：磁盘上的**原件树**（`repository/materials/`）与 SQLite **事实库**（`repository/db/materials.db`）。二者在同一事务语义下登记，检索投影与全文索引都是可重建的派生物。

## 原件树布局

```
repository/
├─ db/materials.db
└─ materials/MAT-*/
   ├─ .versions/            # 全部历史版本原件（只追加，不覆盖）
   │  └─ vN/
   │     ├─ original.pdf    # 原件（或原始图片），登记 SHA256
   │     ├─ ocr/            # 逐页原始 OCR 与 JSONL（不可覆盖）
   │     ├─ nfkc.txt        # NFKC 检索投影（可重建）
   │     └─ ...
   └─ current.search.txt    # 当前版本检索投影（原子发布，可重建）
```

- `current.search.txt` 等投影只是视图；SQLite 中的文件映射保证投影永远指向不可覆盖的历史版本。
- 磁盘文件哈希与登记不一致时，查询/展示立即拒绝并要求体检。

## 事实库核心表（schema v1）

| 表 | 作用 |
|---|---|
| `schema_meta` / `setting` | schema 版本与运行设置 |
| `material_type` | 封闭的材料类型树（证书、合同业绩、人员、专利/软著、财务、纳税、荣誉等） |
| `org` / `person` | 单位与人员实体（带归一化名列索引） |
| `material` | 材料主记录：类型、标题、业务状态、核验状态、适用年份等 |
| `material_file` | 逐文件登记：角色（original/ocr/nfkc…）、版本、SHA256、字节大小 |
| `material_attr` | 材料级属性键值（含日期型属性索引） |
| `certificate_detail` / `performance_detail` | 证书与合同业绩结构化明细 |
| `material_person` / `person_certificate_detail` | 人员-材料关联与人员证书明细 |
| `patent_detail` / `patent_legal_status` / `software_copyright_detail` | 知识产权明细 |
| `audit_report_detail` | 审计报告明细 |
| `extraction_run` / `extracted_fact` | OCR/抽取运行与候选事实（永远“待核验”） |
| `verification_event` | 人工核验事件（绑定证据文件与 SHA256） |
| `audit_log` | 追加式审计台账 |
| `service_state` | 服务运行状态 |

后续前向迁移新增：

- v0.2：`org` 单位列 + `power_license_item`（承装修试明细）
- v0.3：`construction_qual_item`（建筑业资质明细）；证书范围与台账证据复用
- v0.4：`material_relation`（材料关联）与前向兼容的业务词汇扩展（旧证据保持不可变）

## 事实分级语义

1. **已核验事实（verified）**：仅来自人工核验事件——对照当前原件填写，绑定 `evidence_file_id` + `evidence_sha256`。这是对外唯一承诺可信的层级。
2. **候选事实（candidates）**：OCR/规则自动抽取（`business.py`、`enrichment.py`），永远是“待核验”；只入 `extracted_fact`，绝不写成已核验。
3. **再抽取与溯源**（`reextract.py`、`provenance.py`）：为未变化的当前原件追加 OCR 派生、比对历史人工记录，只产生新的待核验候选与机器比对记录，**永远不会生成人工核验**。
4. **换版失效**：`replace` 产生新版本后，既有事实需重新人工核验；未复核时不返回旧事实为已核验事实。

## 字段词汇

`jty_materials/fields.py` 定义封闭业务词汇：证书类型（营业执照、安全生产许可证、承装修试、建筑业资质、ISO、信用报告、纳税、荣誉等）、业绩类型（合同/中标通知书/验收/业主评价）、人员类型（身份证、职称、注册类、特种作业、安全考核、社保、学历等）、财务类型（审计报告/财务报表），以及各类明细表的合法字段集合。施工类字段（项目名称、电压等级、合同金额等）是明细表的固定列，不是通用属性。

## 保护机制

- SQLite 触发器兜底拦截绕过应用的越权写入（含对核验事件的伪造）。
- 全部业务写入进入 `audit_log` 追加台账。
- `verification_event` 只能由人工核验流程产生；没有任何 MCP 工具能写任何表。
- 备份（`backup.py`）覆盖 repository + 数据库并带逐文件哈希清单；索引与运行态可重建、不入备份。
