# 数据模型

资料库由磁盘上的**原件树**（全局素材 `repository/materials/`、项目文件 `repository/projects/`）与 SQLite **事实库**（`repository/db/materials.db`）。二者在同一事务语义下登记，检索投影与全文索引都是可重建的派生物。

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
- v0.5：`project`（投标工作容器）、`project_file`（项目专属原件）、`project_material`（项目引用全局素材的关系）。Project 不属于 `material_type`。

## 项目中心（schema jty-mcp-0.5.0）

```text
repository/
├─ materials/MAT-*/...
└─ projects/PRJ-000001/files/<project_file_id>.<extension>
```

- `project_id` / `project_code` 是不可变系统身份；路径不使用项目名称或上传文件名。基础信息可受控编辑，状态仅 `active` / `archived`，不能物理删除。
- `project_file` 有稳定 UUID、分类、原显示文件名、SHA256、大小与经办人；登记后全行不可修改、不可删除。本阶段不做项目原件换版。
- 分类仅为招标资料、投标成果、项目证明资料、项目过程资料、项目结果、其他。引用素材是独立关系，不能放入附件分类。
- 项目原件支持 PDF/PNG/JPG/JPEG/DOC/DOCX/XLS/XLSX/PPT/PPTX/TXT/MD/ZIP，单文件 200MB；只做容器签名初检、保存及哈希核验，不解析 Office、不解压 ZIP、不 OCR、不索引。容器签名检查不承诺鉴别 Office 子类型或验证文档结构完整性。
- 全局 Material 普通上传仍仅允许 PDF/PNG/JPG/JPEG。项目文件不创建 Material 或 material_file，不进入任何全局检索投影。
- `project_material` 对 `(project_id,material_id)` 唯一，`active → removed → active` 保持同一关系身份；每次变化保留审计。引用不改动素材或复制字节；素材详情反向展示所有 active 引用，含已归档项目。
- 原有 `audit_log` 扩展实体类型并将 `entity_id` 设为 SQLite STRICT `ANY`，保留旧整数 ID 与全部记录，同时支持项目文件 UUID；原有 append-only / 禁止 REPLACE 保护继续有效。

已有 0.4 库必须经明确的 `cli.py --config <隔离配置> migrate-v05` 升级；启动 WebUI 或打开 Store 不会迁移。迁移先使用 SQLite backup API 保存数据库，随后在 `BEGIN IMMEDIATE` 事务中升级，失败全部回滚，重试仍通过 manager 维护锁。升级后执行 doctor 检查数据库、外键、Material 与 Project 原件。

项目上传在 manager 操作锁及 maintenance 内写持久化意图记录，流式复制到 `projects/.staging/`，核对 SHA 与大小后无覆盖发布到最终 UUID 路径，事务登记文件与审计。提交前失败清理未登记文件；提交后失败保留已登记证据。中断由同一 manager recovery 按已提交 DB 状态核验、清理，更新 generation 后解除 maintenance。不要在存在未完成意图记录时单独清空 state。

doctor 分别报告 Material / Project 文件检查数与错误。现有 backup 在同一 manifest 中包含已登记项目文件，DB backup 包含三张项目表及引用；restore 后按原身份恢复，索引仍只针对 materials。

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
