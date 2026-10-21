-- =====================================================================
--  投标素材库 — SQLite 事实层 schema
--  ---------------------------------------------------------------------
--  版本   : 1
--  目标   : 已核验结构化事实层（不承担全文检索）
--  原则   : Evidence / Extraction / Fact 三层严格分离；
--           自动提取值永远只是候选，不得静默进入事实层。
--
--  已在本机 SQLite 3.50.4 上逐项验证的能力（见 design/_probe.py）：
--    STRICT 表 / 生成列 + 唯一索引 / 部分唯一索引 / 日期 CHECK /
--    跨字段 CHECK / 触发器 / 视图 / VACUUM INTO / 外键级联 / 窗口函数
--
--  使用：
--    sqlite3 prototype.db < schema.sql
--    PRAGMA foreign_keys = ON;   -- 每个连接都要开，不持久
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- ⚠️ 命名纪律（已实测踩坑）
-- SQLite 的内建关键字在"裸名"位置上优先于列名解析，会把列值静默替换成
-- 关键字的值。实测：视图里 `SELECT cd AS current_date FROM t` 之后
-- `SELECT current_date FROM v` 返回的是【今天的日期】而不是 cd 的值。
--
-- 因此本 schema 严禁把下列名字用作列名或列别名：
--   current_date / current_time / current_timestamp
-- 已经统一改名为 fact_value_date / fact_value_text / fact_value_int /
-- fact_value_amount。新增字段请遵守：不要用 SQLite 关键字作为标识符；
-- 拿不准就加前缀（fact_ / mat_ / src_）。
--
-- 同理避免：index / table / where / group / order / key / values / select /
-- from / to / end / begin / default / check / references / primary / unique /
-- not / null / exists / case / when / then / else / in / is / like / between /
-- and / or / distinct / all / as / asc / desc / limit / offset / join / on /
-- using / union / having / cast / collate / into / trigger / view / temp /
-- without / rowid / strict / generated / always / over / window / filter /
-- range / rows / current / recursive / with
-- （多数是"非保留关键字"，能当标识符用，但混用时极易出错，一律避开。）
-- ---------------------------------------------------------------------

-- ---------------------------------------------------------------------
-- 0. 元信息
-- ---------------------------------------------------------------------

CREATE TABLE schema_meta (
    meta_key    TEXT PRIMARY KEY,
    meta_value  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

INSERT INTO schema_meta(meta_key, meta_value, updated_at) VALUES
    ('schema_version',  '1',            strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('schema_name',     'jty_bid_materials', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('created_at',      strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('code_prefix',     'MAT',          strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- 通用设置。注意：repository_root 不在这里 —— 它属于配置文件，
-- 这样 DB 文件本身与挂载位置解耦，整库可搬到任何盘。
CREATE TABLE setting (
    setting_key   TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    note          TEXT
) STRICT;

-- =====================================================================
-- 1. 词典层（可扩展的业务分类）
-- =====================================================================

-- 材料类型：用 lookup 表而不是 CHECK 枚举 —— 业务分类会持续扩展，
-- 且需要中文显示名、排序、层级；CHECK 枚举每次扩展都要改 DDL。
CREATE TABLE material_type (
    type_id     INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,           -- 稳定机器名，代码里引用它
    name_zh     TEXT NOT NULL,
    parent_id   INTEGER REFERENCES material_type(type_id),
    sort_order  INTEGER NOT NULL DEFAULT 100,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    notes       TEXT
) STRICT;

CREATE INDEX ix_material_type_parent ON material_type(parent_id);

INSERT INTO material_type(type_id, code, name_zh, parent_id, sort_order) VALUES
  ( 1,'company_basic',        '公司基础资料',           NULL,  10),
  ( 2,'qualification',        '企业资质',               NULL,  20),
  ( 3,'finance',              '财务',                   NULL,  30),
  ( 4,'tax_credit',           '纳税信用',               NULL,  40),
  ( 5,'performance',          '项目业绩',               NULL,  50),
  ( 6,'personnel',            '人员',                   NULL,  60),
  ( 7,'ip',                   '知识产权',               NULL,  70),
  ( 8,'honor_credit',         '荣誉 / 信用 / 企业状态', NULL,  80),

  (11,'business_license',     '营业执照',                  1, 110),
  (12,'company_charter',      '公司章程',                  1, 120),
  (13,'legal_rep_id',         '法定代表人身份证明',        1, 130),
  (14,'basic_account',        '基本户开户许可',            1, 140),

  (21,'construction_qual',    '建筑业企业资质',            2, 210),
  (22,'safety_prod_license',  '安全生产许可证',            2, 220),
  (23,'power_facility_license','承装（修、试）电力设施许可证',2, 230),
  (24,'iso_cert',             '体系认证证书',              2, 240),
  (25,'credit_report',        '信用报告 / 无行贿证明',     2, 250),

  (31,'audit_report',         '审计报告',                  3, 310),
  (32,'financial_statement',  '财务报表',                  3, 320),

  (41,'tax_credit_a',         '纳税信用 A 级证明',         4, 410),
  (42,'tax_payment_cert',     '完税证明',                  4, 420),

  (51,'contract',             '合同',                      5, 510),
  (52,'award_notice',         '中标通知书',                5, 520),
  (53,'acceptance_doc',       '验收材料',                  5, 530),
  (54,'owner_evaluation',     '业主评价 / 履约证明',       5, 540),

  (61,'person_id_card',       '身份证明',                  6, 610),
  (62,'person_title_cert',    '职称证书',                  6, 620),
  (63,'person_registered_cert','注册证书',                 6, 630),
  (64,'person_special_op',    '特种作业操作证',            6, 640),
  (65,'person_safety_cert',   '安全生产考核合格证',        6, 650),
  (66,'person_social_security','社保材料',                 6, 660),
  (67,'person_education',     '学历证明',                  6, 670),

  (71,'patent',               '专利',                      7, 710),
  (72,'software_copyright',   '软件著作权',                7, 720),

  (81,'honor_cert',           '荣誉证书',                  8, 810),
  (82,'enterprise_status',    '企业状态证明',              8, 820),
  (89,'other',                '其他',                   NULL, 899);

-- 组织（本公司 / 客户 / 供应商 / 集团内其他公司）
-- 发证机关、会计师事务所这类"只作为文本出现的机构"不进本表，避免过度规范化。
CREATE TABLE org (
    org_id      INTEGER PRIMARY KEY,
    org_code    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,                  -- NFKC + 去空白，用于匹配
    is_self     INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0,1)),
    unified_social_credit_code TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','inactive','unknown')),
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE INDEX ix_org_name_norm ON org(name_norm);
CREATE UNIQUE INDEX ix_org_self ON org(is_self) WHERE is_self = 1;  -- 只能有一个"本公司"

-- 人员
CREATE TABLE person (
    person_id   INTEGER PRIMARY KEY,
    person_code TEXT NOT NULL DEFAULT '',        -- PER-0001，插入后由触发器补
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    id_card_no  TEXT,                            -- 敏感；可只存后 6 位 + 标记，见 README
    employee_no TEXT,
    department  TEXT,
    job_title   TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','resigned','unknown')),
    hired_on    TEXT CHECK (hired_on IS NULL OR (length(hired_on)=10 AND date(hired_on)=hired_on)),
    left_on     TEXT CHECK (left_on  IS NULL OR (length(left_on )=10 AND date(left_on )=left_on )),
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (left_on IS NULL OR hired_on IS NULL OR left_on >= hired_on)
) STRICT;

CREATE UNIQUE INDEX ix_person_code ON person(person_code) WHERE person_code <> '';
CREATE INDEX ix_person_name_norm ON person(name_norm);

CREATE TRIGGER trg_person_code AFTER INSERT ON person
WHEN NEW.person_code = ''
BEGIN
    UPDATE person SET person_code = printf('PER-%04d', NEW.person_id)
    WHERE person_id = NEW.person_id;
END;

-- =====================================================================
-- 2. 材料主体
-- =====================================================================

-- 逻辑材料。material_id 是内部代理主键；material_code 是稳定对外标识。
-- 二者一经分配永不改变 —— 文件改名/移动、OCR 重跑、元数据修改、
-- dowse 重建索引、导入导出，都不影响它们。
CREATE TABLE material (
    material_id         INTEGER PRIMARY KEY,
    material_code       TEXT NOT NULL DEFAULT '',   -- MAT-000123，插入后由触发器补

    title               TEXT NOT NULL,
    material_type_id    INTEGER NOT NULL REFERENCES material_type(type_id),
    subject_org_id      INTEGER REFERENCES org(org_id),   -- 本材料描述的主体单位

    applicable_year     INTEGER CHECK (applicable_year IS NULL OR applicable_year BETWEEN 1990 AND 2100),

    -- 业务状态 与 核验状态 严格分开，不合成一个 status
    business_status     TEXT NOT NULL DEFAULT 'active'
                        CHECK (business_status IN ('draft','active','expired','superseded','archived')),
    verification_status TEXT NOT NULL DEFAULT 'unverified'
                        CHECK (verification_status IN ('unverified','ai_extracted','human_verified','disputed')),

    confidentiality     TEXT NOT NULL DEFAULT 'internal'
                        CHECK (confidentiality IN ('public','internal','confidential')),

    -- 核验汇总（由 material_fact 变更触发维护；可用下方 rebuild SQL 全量重算）
    verified_at         TEXT,
    verified_by         TEXT,

    source_note         TEXT,        -- 来源说明（哪里拿到的、谁提供的）
    tags_text           TEXT,        -- 逗号分隔的自由标签，仅供人看；结构化用途请用 material_tag
    notes               TEXT,

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    archived_at         TEXT
) STRICT;

CREATE UNIQUE INDEX ix_material_code ON material(material_code) WHERE material_code <> '';
CREATE INDEX ix_material_type   ON material(material_type_id);
CREATE INDEX ix_material_suborg ON material(subject_org_id);
CREATE INDEX ix_material_year   ON material(applicable_year);
CREATE INDEX ix_material_bstmt  ON material(business_status);
CREATE INDEX ix_material_vstmt  ON material(verification_status);
CREATE INDEX ix_material_title  ON material(title);

-- 自动分配对外编号。material_id 由 SQLite 分配后立刻派生，永不改动。
CREATE TRIGGER trg_material_code AFTER INSERT ON material
WHEN NEW.material_code = ''
BEGIN
    UPDATE material SET material_code = printf('MAT-%06d', NEW.material_id)
    WHERE material_id = NEW.material_id;
END;

-- 自由标签（多对多，用于跨越 material_type 的横切分组，如"2026年某某项目投标用"）
CREATE TABLE tag (
    tag_id      INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT,
    notes       TEXT
) STRICT;

CREATE TABLE material_tag (
    material_id INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tag(tag_id) ON DELETE CASCADE,
    tagged_at   TEXT NOT NULL,
    PRIMARY KEY (material_id, tag_id)
) STRICT;

-- =====================================================================
-- 3. 文件版本层（一份逻辑材料可以有多个文件版本）
-- =====================================================================

-- NOTE: 删除 material_file 的行不会删除磁盘文件（证据层归文件系统管）。
CREATE TABLE material_file (
    file_id       INTEGER PRIMARY KEY,
    material_id   INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,

    -- role 是"闭集技术枚举"，代码里 switch 它 —— 用 CHECK 而不是 lookup 表。
    -- （与 material_type 相反：后者是"开集业务分类"，用 lookup 表。）
    role          TEXT NOT NULL CHECK (role IN (
                      'original',        -- 原件 PDF/Word/Excel/图片
                      'ocr_raw_text',    -- Umi-OCR 原始输出，永久保留、不得覆盖
                      'ocr_raw_jsonl',   -- Umi-OCR 带坐标/置信度输出
                      'search_text',     -- NFKC 归一后的可搜索版本（dowse 索引对象）
                      'preview',         -- 缩略图 / 首页预览
                      'converted',       -- 格式转换产物
                      'attachment',      -- 附件材料
                      'other')),

    -- 路径规则：只存相对路径，正斜杠，不含盘符与仓库根。
    -- 整库搬迁时只改配置文件里的 repository_root，DB 不动。
    relative_path TEXT NOT NULL
                  CHECK (relative_path <> ''
                         AND instr(relative_path, char(92)) = 0      -- 不含反斜杠
                         AND substr(relative_path,1,1) <> '/'),      -- 不以 / 开头
    -- 大小写/分隔符归一键，用于把 dowse 返回的绝对路径反查回本表
    rel_path_key  TEXT GENERATED ALWAYS AS (lower(replace(relative_path, char(92), '/'))) VIRTUAL,

    filename      TEXT NOT NULL,
    extension     TEXT NOT NULL DEFAULT '' CHECK (extension = lower(extension)),

    sha256        TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 = lower(sha256)),
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_fs      TEXT,                       -- 文件系统 mtime（ISO8601 秒）
    imported_at   TEXT NOT NULL,

    is_current    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    source_type   TEXT NOT NULL DEFAULT 'scan'
                  CHECK (source_type IN ('scan','digital','download','generated','converted','external')),
    origin_path   TEXT,                       -- 导入前的原始位置（证据来源，可含盘符）
    notes         TEXT
) STRICT;

-- 同一 SHA256 允许出现多次（同一文件可作为多份材料的证据），但要有索引便于查重
CREATE INDEX ix_file_sha        ON material_file(sha256);
CREATE INDEX ix_file_material   ON material_file(material_id, role);
CREATE UNIQUE INDEX ix_file_relpath ON material_file(rel_path_key);
-- 每个 material + role 只能有一个 current（历史版本保留，is_current=0）
CREATE UNIQUE INDEX ix_file_one_current ON material_file(material_id, role) WHERE is_current = 1;

-- =====================================================================
-- 4. 基础通用扩展（强类型关系的补充：少量非常规字段）
-- =====================================================================

-- 只放"没有专门列、且不需要被范围查询"的长尾字段。
-- 需要比较/排序/校验的字段一律进强类型表或 material 主表。
CREATE TABLE material_attr (
    attr_id       INTEGER PRIMARY KEY,
    material_id   INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    attr_key      TEXT NOT NULL,
    value_type    TEXT NOT NULL CHECK (value_type IN ('text','date','int','amount','bool')),
    value_text    TEXT,
    value_date    TEXT CHECK (value_date IS NULL OR (length(value_date)=10 AND date(value_date)=value_date)),
    value_int     INTEGER,
    value_amount_cents INTEGER,
    note          TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE (material_id, attr_key),
    -- 恰好一个类型列被填充
    CHECK ( (value_text IS NOT NULL)
          + (value_date IS NOT NULL)
          + (value_int  IS NOT NULL)
          + (value_amount_cents IS NOT NULL) = 1 )
) STRICT;

CREATE INDEX ix_attr_key  ON material_attr(attr_key);
CREATE INDEX ix_attr_date ON material_attr(attr_key, value_date);

-- =====================================================================
-- 5. 分类别强类型明细表（§9 §10 §11 §12 §13）
-- =====================================================================

-- 5.1 证书类（营业执照 / 安许 / 承装修试 / 建筑业资质 / 纳税信用A级 / 体系认证 …）
CREATE TABLE certificate_detail (
    material_id        INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    certificate_kind   TEXT NOT NULL CHECK (certificate_kind IN (
                           'business_license','safety_production','power_facility',
                           'construction_qualification','iso','tax_credit_a',
                           'credit_report','company_charter','other')),
    certificate_no     TEXT,
    issuing_authority  TEXT,
    issue_date         TEXT CHECK (issue_date IS NULL OR (length(issue_date)=10 AND date(issue_date)=issue_date)),
    valid_from         TEXT CHECK (valid_from IS NULL OR (length(valid_from)=10 AND date(valid_from)=valid_from)),
    valid_until        TEXT CHECK (valid_until IS NULL OR (length(valid_until)=10 AND date(valid_until)=valid_until)),
    long_term_valid    INTEGER NOT NULL DEFAULT 0 CHECK (long_term_valid IN (0,1)),
    grade              TEXT,       -- 等级：三级 / 二级 / 一级 …
    scope              TEXT,       -- 许可范围 / 经营范围
    holder_org_id      INTEGER REFERENCES org(org_id),
    notes              TEXT,
    CHECK (long_term_valid = 1 OR valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from),
    CHECK (long_term_valid = 0 OR valid_until IS NULL)          -- 长期有效不应再填到期日
) STRICT;

CREATE INDEX ix_cert_no     ON certificate_detail(certificate_no);
CREATE INDEX ix_cert_until  ON certificate_detail(valid_until);
CREATE INDEX ix_cert_kind   ON certificate_detail(certificate_kind, valid_until);

-- 5.2 合同 / 业绩（合同 / 中标通知书 / 验收材料 共用）
CREATE TABLE performance_detail (
    material_id        INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    project_code       TEXT,       -- 预留：同一项目的多份材料可填同一 code 便于归组
    project_name       TEXT,
    client_org_id      INTEGER REFERENCES org(org_id),
    client_name_text   TEXT,       -- 客户未建档时直接存文本
    contract_no        TEXT,
    contract_date      TEXT CHECK (contract_date IS NULL OR (length(contract_date)=10 AND date(contract_date)=contract_date)),

    -- 金额：整数"分"，绝不用 REAL
    contract_amount_cents INTEGER CHECK (contract_amount_cents IS NULL OR contract_amount_cents >= 0),
    currency           TEXT NOT NULL DEFAULT 'CNY' CHECK (length(currency) = 3),

    project_type       TEXT,       -- 变电 / 线路 / 配电 / 改造 …
    -- 电压等级存数值 kV，便于 >= / BETWEEN；"35kV" 只是显示格式
    voltage_level_kv   INTEGER CHECK (voltage_level_kv IS NULL OR voltage_level_kv > 0),
    location           TEXT,

    start_date         TEXT CHECK (start_date      IS NULL OR (length(start_date     )=10 AND date(start_date     )=start_date     )),
    completion_date    TEXT CHECK (completion_date IS NULL OR (length(completion_date)=10 AND date(completion_date)=completion_date)),
    acceptance_date    TEXT CHECK (acceptance_date IS NULL OR (length(acceptance_date)=10 AND date(acceptance_date)=acceptance_date)),

    performance_status TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (performance_status IN ('ongoing','completed','accepted','terminated','unknown')),
    notes              TEXT,
    CHECK (completion_date IS NULL OR start_date IS NULL OR completion_date >= start_date)
) STRICT;

CREATE INDEX ix_perf_amount  ON performance_detail(contract_amount_cents);
CREATE INDEX ix_perf_kv      ON performance_detail(voltage_level_kv);
CREATE INDEX ix_perf_client  ON performance_detail(client_org_id);
CREATE INDEX ix_perf_accept  ON performance_detail(acceptance_date);

-- 5.3 人员 ↔ 材料 多对多（一份材料可关联多人；一人可有多份材料）
CREATE TABLE material_person (
    material_id   INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    person_id     INTEGER NOT NULL REFERENCES person(person_id)     ON DELETE CASCADE,
    role_on_material TEXT NOT NULL DEFAULT 'holder'
                  CHECK (role_on_material IN ('holder','project_manager','tech_lead','safety_officer','other')),
    is_primary    INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    note          TEXT,
    PRIMARY KEY (material_id, person_id, role_on_material)
) STRICT;

CREATE INDEX ix_mp_person ON material_person(person_id, role_on_material);
CREATE UNIQUE INDEX ix_mp_one_primary ON material_person(material_id) WHERE is_primary = 1;

-- 5.4 人员证书明细（证书字段；持有人通过 material_person 关联）
CREATE TABLE person_certificate_detail (
    material_id        INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    certificate_kind   TEXT NOT NULL CHECK (certificate_kind IN (
                           'id_card','title','registered','special_operation',
                           'safety_assessment','social_security','education','other')),
    certificate_no     TEXT,
    specialty          TEXT,       -- 准操项目 / 专业 / 注册专业
    issuing_authority  TEXT,
    issue_date         TEXT CHECK (issue_date  IS NULL OR (length(issue_date )=10 AND date(issue_date )=issue_date )),
    valid_from         TEXT CHECK (valid_from  IS NULL OR (length(valid_from )=10 AND date(valid_from )=valid_from )),
    valid_until        TEXT CHECK (valid_until IS NULL OR (length(valid_until)=10 AND date(valid_until)=valid_until)),
    long_term_valid    INTEGER NOT NULL DEFAULT 0 CHECK (long_term_valid IN (0,1)),
    review_date        TEXT CHECK (review_date IS NULL OR (length(review_date)=10 AND date(review_date)=review_date)),
    employer_org_id    INTEGER REFERENCES org(org_id),
    notes              TEXT,
    CHECK (long_term_valid = 1 OR valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from),
    CHECK (long_term_valid = 0 OR valid_until IS NULL)
) STRICT;

CREATE INDEX ix_pcert_kind  ON person_certificate_detail(certificate_kind, valid_until);
CREATE INDEX ix_pcert_no    ON person_certificate_detail(certificate_no);

-- 5.5 专利。注意：证书事实 与 当前法律状态 严格分开存储。
CREATE TABLE patent_detail (
    material_id      INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    patent_no        TEXT,
    patent_name      TEXT,
    patent_type      TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (patent_type IN ('invention','utility_model','design','unknown')),
    application_date TEXT CHECK (application_date IS NULL OR (length(application_date)=10 AND date(application_date)=application_date)),
    grant_date       TEXT CHECK (grant_date IS NULL OR (length(grant_date)=10 AND date(grant_date)=grant_date)),
    patentee_org_id  INTEGER REFERENCES org(org_id),
    patentee_text    TEXT,
    inventor_text    TEXT,
    certificate_no   TEXT,
    notes            TEXT,
    CHECK (grant_date IS NULL OR application_date IS NULL OR grant_date >= application_date)
) STRICT;

CREATE INDEX ix_patent_no ON patent_detail(patent_no);

-- 专利当前法律状态的核验记录（append-only；当前值 = 最新一条）
-- 证书只能证明"授权时"的状态，不能证明"现在还有效" —— 两者必须分开。
CREATE TABLE patent_legal_status (
    status_id     INTEGER PRIMARY KEY,
    material_id   INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    checked_at    TEXT NOT NULL,
    legal_status  TEXT NOT NULL CHECK (legal_status IN ('valid','expired','terminated','pledged','abandoned','unknown')),
    fee_paid_until TEXT CHECK (fee_paid_until IS NULL OR (length(fee_paid_until)=10 AND date(fee_paid_until)=fee_paid_until)),
    status_source TEXT NOT NULL,      -- 'cnipa_web' / 'agency_report' / 'official_letter' …
    source_ref    TEXT,               -- 查询单号 / URL / 文件路径
    checked_by    TEXT NOT NULL,
    evidence_file_id INTEGER REFERENCES material_file(file_id),
    note          TEXT
) STRICT;

CREATE INDEX ix_pls_material ON patent_legal_status(material_id, checked_at DESC);

-- 5.6 软件著作权
CREATE TABLE software_copyright_detail (
    material_id        INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    registration_no    TEXT,
    software_name      TEXT,
    version            TEXT,
    dev_completion_date TEXT CHECK (dev_completion_date IS NULL OR (length(dev_completion_date)=10 AND date(dev_completion_date)=dev_completion_date)),
    registration_date  TEXT CHECK (registration_date IS NULL OR (length(registration_date)=10 AND date(registration_date)=registration_date)),
    copyright_owner_org_id INTEGER REFERENCES org(org_id),
    owner_text         TEXT,
    notes              TEXT
) STRICT;

-- 5.7 审计报告 / 年度资料。第一阶段只需"按年度找到正确报告"，不建报表数字表。
CREATE TABLE audit_report_detail (
    material_id      INTEGER PRIMARY KEY REFERENCES material(material_id) ON DELETE CASCADE,
    fiscal_year      INTEGER NOT NULL CHECK (fiscal_year BETWEEN 1990 AND 2100),
    report_date      TEXT CHECK (report_date IS NULL OR (length(report_date)=10 AND date(report_date)=report_date)),
    accounting_firm  TEXT,
    report_no        TEXT,
    audit_opinion    TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (audit_opinion IN ('unqualified','qualified','adverse','disclaimer','unknown')),
    is_consolidated  INTEGER NOT NULL DEFAULT 0 CHECK (is_consolidated IN (0,1)),
    notes            TEXT
) STRICT;

CREATE INDEX ix_audit_year ON audit_report_detail(fiscal_year);

-- =====================================================================
-- 6. 提取层（OCR / AI 候选值）—— 永远不是事实
-- =====================================================================

-- 一次提取运行的元信息
CREATE TABLE extraction_run (
    run_id            INTEGER PRIMARY KEY,
    material_id       INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    source_file_id    INTEGER REFERENCES material_file(file_id),   -- 从哪份文件抽的
    extractor         TEXT NOT NULL,          -- 'umi-ocr' / 'rule-regex-cn-id' / 'llm-xxx' …
    extractor_version TEXT,
    params_json       TEXT,                   -- 引擎参数快照（如 extractionMode/maxSideLen）
    run_at            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'succeeded'
                      CHECK (status IN ('running','succeeded','failed','partial')),
    error_text        TEXT,
    notes             TEXT
) STRICT;

CREATE INDEX ix_run_material ON extraction_run(material_id, run_at DESC);

-- 候选值。多条候选可以并存（同一字段不同 run、不同来源），互不覆盖。
CREATE TABLE extracted_fact (
    extracted_id     INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES extraction_run(run_id) ON DELETE CASCADE,
    material_id      INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    field_name       TEXT NOT NULL,
    value_type       TEXT NOT NULL CHECK (value_type IN ('text','date','int','amount','bool')),

    raw_value        TEXT,        -- 引擎原样输出，绝不改写
    normalized_value TEXT,        -- 归一化（NFKC / 去空格 / 日期标准化），仅供机器比较
    value_date       TEXT CHECK (value_date IS NULL OR (length(value_date)=10 AND date(value_date)=value_date)),
    value_amount_cents INTEGER,

    confidence       REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    page_no          INTEGER,
    bbox_json        TEXT,        -- 来自 .ocr.jsonl 的坐标
    evidence_span    TEXT,        -- 命中的上下文片段（短）
    review_status    TEXT NOT NULL DEFAULT 'pending'
                     CHECK (review_status IN ('pending','accepted','rejected','superseded')),
    reviewed_at      TEXT,
    reviewed_by      TEXT,
    review_note      TEXT
) STRICT;

CREATE INDEX ix_ef_material_field ON extracted_fact(material_id, field_name);
CREATE INDEX ix_ef_run            ON extracted_fact(run_id);
CREATE INDEX ix_ef_review         ON extracted_fact(review_status);
CREATE INDEX ix_ef_date           ON extracted_fact(field_name, value_date);

-- =====================================================================
-- 7. 核验 / 审计层
-- =====================================================================

-- 字段级变更与核验事件（append-only）。
-- "当前值"就在各强类型表里；本表回答"为什么变成这样、谁改的、依据是什么"。
CREATE TABLE verification_event (
    event_id        INTEGER PRIMARY KEY,
    material_id     INTEGER NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('set','verify','correct','dispute','retract','import')),
    old_value       TEXT,
    new_value       TEXT,
    evidence_file_id INTEGER REFERENCES material_file(file_id),
    evidence_external TEXT,          -- 外部来源（官网查询、函件编号、URL）
    source_extracted_id INTEGER REFERENCES extracted_fact(extracted_id),  -- 采纳了哪条候选
    verification_method TEXT NOT NULL CHECK (verification_method IN (
                          'manual_original',      -- 人工看原件
                          'manual_system',        -- 人工在系统里核对
                          'external_lookup',      -- 外部权威渠道查询
                          'import',               -- 批量导入
                          'ai_assisted_review',   -- AI 辅助、人工确认
                          'direct_edit')),        -- 绕过 MCP 直接改库（会被标记待复核）
    verified_by     TEXT NOT NULL,   -- 人员代号或 'system'
    reason          TEXT,
    occurred_at     TEXT NOT NULL
) STRICT;

CREATE INDEX ix_ve_material ON verification_event(material_id, occurred_at);
CREATE INDEX ix_ve_field    ON verification_event(material_id, field_name, occurred_at);
CREATE INDEX ix_ve_time     ON verification_event(occurred_at);

-- 事实变更历史视图（不额外建表，避免三写不一致）
CREATE VIEW v_fact_history AS
SELECT
    e.event_id,
    e.material_id,
    m.material_code,
    e.field_name,
    e.action,
    e.old_value,
    e.new_value,
    e.verification_method,
    e.verified_by,
    e.reason,
    e.occurred_at,
    e.evidence_file_id,
    f.relative_path AS evidence_rel_path
FROM verification_event e
JOIN material m ON m.material_id = e.material_id
LEFT JOIN material_file f ON f.file_id = e.evidence_file_id;

-- 通用技术审计日志：记录非事实类变更（材料/文件/导入/服务生命周期…）
CREATE TABLE audit_log (
    log_id       INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    actor        TEXT,               -- 'mcp' / 'user:xxx' / 'system'
    action       TEXT NOT NULL,      -- 'material.create' / 'file.import' / 'dowse.reindex' …
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('material','file','fact','extraction','org','person','tag','service','system')),
    entity_id    INTEGER,
    detail_json  TEXT,
    note         TEXT
) STRICT;

CREATE INDEX ix_audit_at     ON audit_log(at);
CREATE INDEX ix_audit_entity ON audit_log(entity_type, entity_id, at);

-- =====================================================================
-- 8. 后台服务状态（dowse 生命周期）
-- =====================================================================

-- 单行表（id=1）。持久化是为了崩溃后可诊断，不是权威状态源 ——
-- 权威状态永远是"dowse 进程实际在做什么"，启动时必须重新探测。
CREATE TABLE service_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    dowse_state       TEXT NOT NULL DEFAULT 'STOPPED'
                      CHECK (dowse_state IN ('STOPPED','WATCHING','REINDEXING','ERROR')),
    dowse_pid         INTEGER,
    watch_started_at  TEXT,
    last_health_at    TEXT,        -- 最近一次 canary 健康检查时间
    last_health_ok    INTEGER CHECK (last_health_ok IS NULL OR last_health_ok IN (0,1)),
    last_health_note  TEXT,
    last_index_at     TEXT,
    index_doc_count   INTEGER,
    updated_at        TEXT NOT NULL,
    note              TEXT
) STRICT;

INSERT INTO service_state(id, dowse_state, updated_at, note)
VALUES (1, 'STOPPED', strftime('%Y-%m-%dT%H:%M:%SZ','now'), '初始化');

-- =====================================================================
-- 9. 视图：统一事实投影 / 到期 / 材料状态汇总
-- =====================================================================

-- 把散落在各强类型表里的"当前事实"投影成统一形状，
-- 这样校验/对比/导出不必写一长串 UNION。只读，不引入第二份真值。
CREATE VIEW v_current_facts AS
    -- 证书类
    SELECT m.material_id, m.material_code, 'certificate_detail' AS src_table,
           'certificate_no' AS field_name, 'text' AS value_type,
           c.certificate_no AS value_text, NULL AS value_date, NULL AS value_int, NULL AS value_amount_cents
      FROM material m JOIN certificate_detail c USING (material_id)
     WHERE c.certificate_no IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'certificate_detail', 'valid_until', 'date',
           NULL, c.valid_until, NULL, NULL
      FROM material m JOIN certificate_detail c USING (material_id)
     WHERE c.valid_until IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'certificate_detail', 'issue_date', 'date',
           NULL, c.issue_date, NULL, NULL
      FROM material m JOIN certificate_detail c USING (material_id)
     WHERE c.issue_date IS NOT NULL
    -- 业绩类
    UNION ALL
    SELECT m.material_id, m.material_code, 'performance_detail', 'contract_no', 'text',
           p.contract_no, NULL, NULL, NULL
      FROM material m JOIN performance_detail p USING (material_id)
     WHERE p.contract_no IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'performance_detail', 'contract_amount', 'amount',
           NULL, NULL, NULL, p.contract_amount_cents
      FROM material m JOIN performance_detail p USING (material_id)
     WHERE p.contract_amount_cents IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'performance_detail', 'voltage_level_kv', 'int',
           NULL, NULL, p.voltage_level_kv, NULL
      FROM material m JOIN performance_detail p USING (material_id)
     WHERE p.voltage_level_kv IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'performance_detail', 'acceptance_date', 'date',
           NULL, p.acceptance_date, NULL, NULL
      FROM material m JOIN performance_detail p USING (material_id)
     WHERE p.acceptance_date IS NOT NULL
    -- 专利
    UNION ALL
    SELECT m.material_id, m.material_code, 'patent_detail', 'patent_no', 'text',
           t.patent_no, NULL, NULL, NULL
      FROM material m JOIN patent_detail t USING (material_id)
     WHERE t.patent_no IS NOT NULL
    -- 软著
    UNION ALL
    SELECT m.material_id, m.material_code, 'software_copyright_detail', 'registration_no', 'text',
           s.registration_no, NULL, NULL, NULL
      FROM material m JOIN software_copyright_detail s USING (material_id)
     WHERE s.registration_no IS NOT NULL
    -- 审计报告
    UNION ALL
    SELECT m.material_id, m.material_code, 'audit_report_detail', 'fiscal_year', 'int',
           NULL, NULL, a.fiscal_year, NULL
      FROM material m JOIN audit_report_detail a USING (material_id)
    UNION ALL
    SELECT m.material_id, m.material_code, 'audit_report_detail', 'audit_opinion', 'text',
           a.audit_opinion, NULL, NULL, NULL
      FROM material m JOIN audit_report_detail a USING (material_id)
    UNION ALL
    SELECT m.material_id, m.material_code, 'audit_report_detail', 'report_no', 'text',
           a.report_no, NULL, NULL, NULL
      FROM material m JOIN audit_report_detail a USING (material_id)
     WHERE a.report_no IS NOT NULL
    -- 人员证书
    UNION ALL
    SELECT m.material_id, m.material_code, 'person_certificate_detail', 'certificate_no', 'text',
           pc.certificate_no, NULL, NULL, NULL
      FROM material m JOIN person_certificate_detail pc USING (material_id)
     WHERE pc.certificate_no IS NOT NULL
    UNION ALL
    SELECT m.material_id, m.material_code, 'person_certificate_detail', 'valid_until', 'date',
           NULL, pc.valid_until, NULL, NULL
      FROM material m JOIN person_certificate_detail pc USING (material_id)
     WHERE pc.valid_until IS NOT NULL
    -- 通用扩展字段
    UNION ALL
    SELECT m.material_id, m.material_code, 'material_attr', a.attr_key, a.value_type,
           a.value_text, a.value_date, a.value_int, a.value_amount_cents
      FROM material m JOIN material_attr a USING (material_id);

-- 统一的"到期视图"：把证书、人员证书、专利年费集中成一个可查询面
CREATE VIEW v_expiring AS
    SELECT m.material_id, m.material_code, m.title,
           t.code AS material_type_code, t.name_zh AS material_type_name,
           'certificate' AS expiry_source,
           c.valid_until AS expires_on,
           c.certificate_no AS ref_no,
           NULL AS person_id, NULL AS person_name
      FROM material m
      JOIN material_type t ON t.type_id = m.material_type_id
      JOIN certificate_detail c USING (material_id)
     WHERE c.valid_until IS NOT NULL AND c.long_term_valid = 0
       AND m.business_status NOT IN ('archived','superseded')
    UNION ALL
    SELECT m.material_id, m.material_code, m.title,
           t.code, t.name_zh,
           'person_certificate',
           pc.valid_until,
           pc.certificate_no,
           p.person_id, p.name
      FROM material m
      JOIN material_type t ON t.type_id = m.material_type_id
      JOIN person_certificate_detail pc USING (material_id)
      LEFT JOIN material_person mp ON mp.material_id = m.material_id AND mp.role_on_material = 'holder'
      LEFT JOIN person p ON p.person_id = mp.person_id
     WHERE pc.valid_until IS NOT NULL AND pc.long_term_valid = 0
       AND m.business_status NOT IN ('archived','superseded');

-- 专利当前法律状态（每份专利取最新一次核验）
CREATE VIEW v_patent_current_status AS
SELECT d.material_id, d.patent_no, d.patent_name,
       s.checked_at, s.legal_status, s.status_source, s.checked_by, s.fee_paid_until
FROM patent_detail d
LEFT JOIN patent_legal_status s
       ON s.status_id = (
            SELECT status_id FROM patent_legal_status x
             WHERE x.material_id = d.material_id
             ORDER BY x.checked_at DESC, x.status_id DESC LIMIT 1);

-- 材料核验汇总（读 material 的缓存列；下方 rebuild SQL 可全量重算）
CREATE VIEW v_material_overview AS
SELECT m.material_id, m.material_code, m.title,
       t.code AS material_type_code, t.name_zh AS material_type_name,
       m.business_status, m.verification_status,
       m.applicable_year, m.confidentiality,
       (SELECT count(*) FROM material_file f WHERE f.material_id = m.material_id) AS file_count,
       (SELECT count(*) FROM extracted_fact e WHERE e.material_id = m.material_id AND e.review_status = 'pending') AS pending_candidates,
       (SELECT count(*) FROM verification_event v WHERE v.material_id = m.material_id) AS fact_event_count,
       m.created_at, m.updated_at
FROM material m JOIN material_type t ON t.type_id = m.material_type_id;

-- =====================================================================
-- 10. 触发器：时间戳、编号、状态汇总、绕过 MCP 的改动留痕
-- =====================================================================

-- 10.1 updated_at 自动维护（示例覆盖三张主要表；其余同构）
CREATE TRIGGER trg_material_touch AFTER UPDATE ON material
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE material SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
    WHERE material_id = NEW.material_id;
END;

CREATE TRIGGER trg_org_touch AFTER UPDATE ON org
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE org SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE org_id = NEW.org_id;
END;

-- 10.2 绕过 MCP 直接改证书编号时会留下痕迹（verification_method='direct_edit'）
--     目的：让"AI 不能静默覆盖已核验事实"这条原则在数据库层面也有兜底。
CREATE TRIGGER trg_cert_no_direct_edit AFTER UPDATE OF certificate_no ON certificate_detail
WHEN NEW.certificate_no IS NOT OLD.certificate_no
BEGIN
    INSERT INTO verification_event(
        material_id, field_name, action, old_value, new_value,
        verification_method, verified_by, reason, occurred_at)
    VALUES (
        NEW.material_id, 'certificate_no', 'correct', OLD.certificate_no, NEW.certificate_no,
        'direct_edit', 'unattributed',
        '数据库层检测到证书编号被直接修改（未经 API），请人工复核', 
        strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- 10.3 候选值被接受时，要求同时登记核验事件
--     这里不做强制（SQLite 触发器不能要求调用方参数），只提供一致性校验用视图，
--     见 §11 的 v_inconsistency_candidates。

-- =====================================================================
-- 11. 一致性校验视图（给巡检任务用）
-- =====================================================================

-- 候选值与当前事实不一致的字段（可能意味着事实已过期，或候选是错的）
CREATE VIEW v_inconsistency_candidates AS
SELECT e.material_id,
       m.material_code,
       e.field_name,
       e.extracted_id,
       e.raw_value              AS candidate_raw,
       e.normalized_value       AS candidate_norm,
       e.confidence,
       r.run_at,
       r.extractor,
       e.review_status,
       cf.value_text            AS fact_value_text,
       cf.value_date            AS fact_value_date,
       cf.value_int             AS fact_value_int,
       cf.value_amount_cents    AS fact_value_amount
FROM extracted_fact e
JOIN material m ON m.material_id = e.material_id
JOIN extraction_run r ON r.run_id = e.run_id
LEFT JOIN v_current_facts cf
       ON cf.material_id = e.material_id AND cf.field_name = e.field_name
WHERE e.review_status = 'pending'
  AND cf.field_name IS NOT NULL
  AND COALESCE(e.normalized_value,'') <> COALESCE(cf.value_text,
                                                  cf.value_date,
                                                  CAST(cf.value_int AS TEXT),
                                                  CAST(cf.value_amount_cents AS TEXT), '');

-- 缺少 search_text 或 missing ocr 的材料（巡检：OCR 流水线漏项）
CREATE VIEW v_missing_artifacts AS
SELECT m.material_id, m.material_code, m.title, t.code AS material_type_code,
       CASE WHEN NOT EXISTS (SELECT 1 FROM material_file f
                              WHERE f.material_id = m.material_id AND f.role = 'original'  AND f.is_current = 1)
            THEN 1 ELSE 0 END AS missing_original,
       CASE WHEN NOT EXISTS (SELECT 1 FROM material_file f
                              WHERE f.material_id = m.material_id AND f.role = 'ocr_raw_text' AND f.is_current = 1)
            THEN 1 ELSE 0 END AS missing_ocr_raw,
       CASE WHEN NOT EXISTS (SELECT 1 FROM material_file f
                              WHERE f.material_id = m.material_id AND f.role = 'search_text' AND f.is_current = 1)
            THEN 1 ELSE 0 END AS missing_search_text
FROM material m JOIN material_type t ON t.type_id = m.material_type_id
WHERE m.business_status <> 'archived';

-- 12.1 字段级"当前核验状态"：取每个 (material, field) 最新一次事件
--      不建冗余表，避免与强类型表双写不一致。
CREATE VIEW v_field_verification AS
SELECT e.material_id,
       e.field_name,
       e.action                 AS last_action,
       e.new_value              AS accepted_value,
       e.verification_method,
       e.verified_by,
       e.occurred_at            AS last_verified_at,
       e.evidence_file_id,
       e.source_extracted_id,
       e.reason
FROM verification_event e
WHERE e.event_id = (
        SELECT x.event_id FROM verification_event x
         WHERE x.material_id = e.material_id AND x.field_name = e.field_name
         ORDER BY x.occurred_at DESC, x.event_id DESC LIMIT 1);

-- 12.2 核验进度（给"哪些材料还没核"这类报表用）
CREATE VIEW v_verification_progress AS
SELECT m.material_id,
       m.material_code,
       m.title,
       t.code AS material_type_code,
       t.name_zh AS material_type_name,
       m.business_status,
       m.verification_status                                    AS material_level_status,
       (SELECT count(*) FROM extracted_fact e WHERE e.material_id = m.material_id)                       AS candidate_total,
       (SELECT count(*) FROM extracted_fact e WHERE e.material_id = m.material_id AND e.review_status='pending') AS candidate_pending,
       (SELECT count(*) FROM v_field_verification v WHERE v.material_id = m.material_id)                 AS fields_with_verification_event,
       (SELECT count(*) FROM verification_event v WHERE v.material_id = m.material_id)                   AS event_total
FROM material m JOIN material_type t ON t.type_id = m.material_type_id;

-- =====================================================================
-- 12. 运维 SQL（不是 schema 的一部分，放在这里备查）
-- =====================================================================
-- 见 README「事实层落地方式」一节：
--   * 当前值只存在一处 —— 各强类型表 / material 主表 / material_attr，
--     不另建 material_fact 冗余表（避免双写不一致）。
--   * 字段级核验状态由 v_field_verification 从 verification_event 派生。
--   * 材料级 verification_status 是"人工编辑判断"，不是聚合缓存，
--     维护方式见 README。
