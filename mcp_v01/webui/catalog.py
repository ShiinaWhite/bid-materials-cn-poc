"""Business-facing Chinese labels for the closed phase-one vocabulary.

Only existing type codes / field names are labeled here; nothing new is
invented. Upload categories and detail-page field layouts both read this file.
"""

GROUPS = [
    {"key": "company", "label": "公司证照", "icon": "shield",
     "types": ["business_license", "safety_prod_license", "power_facility_license",
               "construction_qual", "iso_cert"]},
    {"key": "performance", "label": "合同与业绩", "icon": "doc",
     "types": ["contract", "award_notice", "acceptance_doc", "owner_evaluation"]},
    {"key": "person", "label": "人员资料", "icon": "person",
     "types": ["person_id_card", "person_education", "person_title_cert", "person_registered_cert",
               "person_special_op", "person_safety_cert", "person_social_security", "person_document"]},
    {"key": "ip", "label": "专利与软著", "icon": "bulb",
     "types": ["patent", "software_copyright"]},
    {"key": "finance", "label": "审计与财务", "icon": "chart",
     "types": ["audit_report", "financial_statement"]},
    {"key": "tax", "label": "纳税与税务", "icon": "stamp",
     "types": ["tax_credit", "tax_payment_cert"]},
    {"key": "credit", "label": "荣誉与信用", "icon": "medal",
     "types": ["honor_cert", "credit_report"]},
    {"key": "other", "label": "其他证明", "icon": "box",
     "types": ["other"]},
]

TYPE_LABELS = {
    "business_license": "营业执照", "safety_prod_license": "安全生产许可证",
    "power_facility_license": "承装(修/试)电力设施许可", "construction_qual": "建筑业企业资质",
    "iso_cert": "ISO体系认证", "contract": "施工合同", "award_notice": "中标通知书",
    "acceptance_doc": "验收材料", "owner_evaluation": "业主评价",
    "person_id_card": "身份证", "person_education": "学历证书", "person_title_cert": "职称证书",
    "person_registered_cert": "注册类证书", "person_special_op": "特种作业操作证",
    "person_safety_cert": "安全考核合格证", "person_social_security": "社保证明",
    "person_document": "其他人员证明", "patent": "专利", "software_copyright": "软件著作权",
    "audit_report": "审计报告", "financial_statement": "财务报表",
    "tax_credit": "纳税信用", "tax_payment_cert": "完税证明",
    "honor_cert": "荣誉证书", "credit_report": "信用报告", "other": "其他证明材料",
}

TYPE_GROUP = {t: g["key"] for g in GROUPS for t in g["types"]}
GROUP_LABEL = {g["key"]: g["label"] for g in GROUPS}

# Upload categories offered in the UI (kind='material' + subtype covers every
# supported type; 'contract' kind is equivalent to subtype contract).
UPLOAD_TYPES = [
    "business_license", "safety_prod_license", "power_facility_license", "construction_qual", "iso_cert",
    "contract", "award_notice", "acceptance_doc", "owner_evaluation",
    "person_id_card", "person_education", "person_title_cert", "person_registered_cert",
    "person_special_op", "person_safety_cert", "person_social_security", "person_document",
    "patent", "software_copyright", "audit_report", "financial_statement",
    "tax_credit", "tax_payment_cert", "honor_cert", "credit_report", "other",
]

FIELD_LABELS = {
    "enterprise_name": "企业名称", "unified_social_credit_code": "统一社会信用代码",
    "established_on": "成立日期", "certificate_no": "证书编号", "issuing_authority": "发证机关",
    "valid_from": "有效期起", "valid_until": "有效期至", "long_term_valid": "长期有效",
    "issue_date": "发证日期", "scope": "业务范围", "standard": "认证标准",
    "title": "材料名称", "year": "年度", "rating": "等级/评级", "authority_source": "出具机关",
    "project_name": "项目名称", "client": "发包人/建设单位", "contract_no": "合同编号",
    "contract_date": "签订日期", "contract_amount": "合同金额", "project_type": "工程类别",
    "voltage_level_kv": "电压等级(kV)", "location": "工程地点", "start_date": "开工日期",
    "completion_date": "竣工日期", "acceptance_date": "验收日期",
    "specialty": "专业/作业类别", "grade": "资格/等级",
    "patent_name": "专利名称", "patent_no": "专利号", "patent_type": "专利类型",
    "patentee": "专利权人", "application_date": "申请日期", "grant_date": "授权公告日",
    "software_name": "软件名称", "registration_no": "登记号", "owner": "著作权人",
    "registration_date": "登记日期", "software_version": "版本号",
    "fiscal_year": "审计年度", "report_type": "报告类型", "audit_firm": "会计师事务所",
    "audit_opinion": "审计意见", "report_date": "报告日期",
    "revenue": "营业收入", "net_profit": "净利润", "total_assets": "资产总计",
    "total_liabilities": "负债合计",
    "install_grade": "承装等级", "repair_grade": "承修等级", "test_grade": "承试等级",
}

# Field kinds drive both value display and the review form input.
FIELD_KIND = {
    "valid_from": "date", "valid_until": "date", "established_on": "date", "issue_date": "date",
    "contract_date": "date", "start_date": "date", "completion_date": "date", "acceptance_date": "date",
    "application_date": "date", "grant_date": "date", "registration_date": "date", "report_date": "date",
    "long_term_valid": "bool", "fiscal_year": "year", "year": "year",
    "voltage_level_kv": "int", "install_grade": "grade", "repair_grade": "grade", "test_grade": "grade",
    "contract_amount": "amount", "revenue": "amount", "net_profit": "signed_amount",
    "total_assets": "amount", "total_liabilities": "amount",
    "unified_social_credit_code": "credit",
}

FIELD_HINT = {
    "long_term_valid": "选择“长期有效”或留空撤回", "contract_amount": "以元为单位填写，系统按分保存",
    "revenue": "以元为单位填写，系统按分保存", "net_profit": "以元为单位填写，可为负数",
    "total_assets": "以元为单位填写，系统按分保存", "total_liabilities": "以元为单位填写，系统按分保存",
    "fiscal_year": "4 位年份，例如 2024", "voltage_level_kv": "整数 kV，例如 10",
}

# Material types for which an expiry/validity concept exists at all. For every
# other type "有效期" is NOT "not recorded yet" — the concept does not apply,
# so the UI never shows a validity row/column for them.
HAS_VALIDITY = {
    "business_license", "safety_prod_license", "power_facility_license",
    "construction_qual", "iso_cert", "tax_credit", "tax_payment_cert",
    "honor_cert", "credit_report", "enterprise_status",
    "person_education", "person_title_cert", "person_registered_cert",
    "person_special_op", "person_safety_cert", "person_social_security",
    "person_document",
}

# Per-group list columns, driven by what readers of that category care about.
# "human" fields below are only rendered when a value exists — no dash columns.
LIST_COLUMNS = {
    "default": ["name", "type", "validity", "status"],
    "company": ["name", "no", "validity", "status"],
    "performance": ["name", "type", "client", "amount_date", "status"],
    "person": ["name", "persons", "no", "validity", "status"],
    "finance": ["name", "year", "type", "status"],
    "tax": ["name", "year", "rating", "status"],
    "ip": ["name", "type", "status"],
    "credit": ["name", "type", "validity", "status"],
    "other": ["name", "type", "status"],
}

COLUMN_HEADERS = {
    "name": "名称", "type": "类别", "persons": "人员", "no": "编号",
    "validity": "有效期", "status": "状态", "client": "客户/发包方",
    "amount_date": "金额 / 日期", "year": "年度", "rating": "等级",
}

# User wording for "this material has no registered info items yet", per type.
EMPTY_INFO_HINTS = {
    "person_id_card": "暂未录入证件编号等详细信息。",
    "person_education": "暂未录入证书编号、专业、有效期等详细信息。",
    "person_title_cert": "暂未录入证书编号、专业、有效期等详细信息。",
    "person_registered_cert": "暂未录入证书编号、专业、有效期等详细信息。",
    "person_special_op": "暂未录入作业类别、证书编号、有效期等详细信息。",
    "person_safety_cert": "暂未录入证书编号、考核类别、有效期等详细信息。",
    "person_social_security": "暂未录入参保编号、缴费期间等详细信息。",
    "person_document": "暂未录入证书编号、有效期等详细信息。",
    "business_license": "暂未录入企业名称、统一社会信用代码、有效期等详细信息。",
    "safety_prod_license": "暂未录入证书编号、发证机关、有效期等详细信息。",
    "power_facility_license": "暂未录入证书编号、承装(修/试)等级、有效期等详细信息。",
    "construction_qual": "暂未录入证书编号、资质类别与等级、有效期等详细信息。",
    "iso_cert": "暂未录入证书编号、认证标准、有效期等详细信息。",
    "contract": "暂未录入项目名称、发包方、合同金额等详细信息。",
    "award_notice": "暂未录入项目名称、中标日期等详细信息。",
    "acceptance_doc": "暂未录入项目名称、验收日期等详细信息。",
    "owner_evaluation": "暂未录入项目名称、评价单位等详细信息。",
    "patent": "暂未录入专利号、专利权人、授权公告日等详细信息。",
    "software_copyright": "暂未录入登记号、著作权人等详细信息。",
    "audit_report": "暂未录入审计年度、财务数据等详细信息。",
    "financial_statement": "暂未录入报告年度、财务数据等详细信息。",
    "tax_credit": "暂未录入评价年度、信用等级等详细信息。",
    "tax_payment_cert": "暂未录入所属年度、出具机关等详细信息。",
    "honor_cert": "暂未录入颁发单位、获得年度等详细信息。",
    "credit_report": "暂未录入出具机构、评价年度等详细信息。",
    "other": "暂未录入这项资料的详细说明信息。",
}

# Per-type field display order on the detail page. Fields not listed here but
# carrying verified values are still shown; the review form uses the closed
# ALLOWED set from jty_materials.fields, never this display list.
TYPE_FIELDS = {
    "business_license": ["enterprise_name", "unified_social_credit_code", "established_on",
                         "valid_from", "valid_until", "long_term_valid"],
    "safety_prod_license": ["certificate_no", "issuing_authority", "valid_from", "valid_until", "long_term_valid"],
    "power_facility_license": ["certificate_no", "issuing_authority", "valid_from", "valid_until",
                               "install_grade", "repair_grade", "test_grade", "scope", "long_term_valid"],
    "construction_qual": ["certificate_no", "issuing_authority", "issue_date", "valid_from",
                          "valid_until", "long_term_valid"],
    "iso_cert": ["certificate_no", "standard", "scope", "issuing_authority", "issue_date",
                 "valid_from", "valid_until", "long_term_valid"],
    "contract": ["project_name", "client", "contract_no", "contract_amount", "contract_date",
                 "project_type", "voltage_level_kv", "location", "start_date", "completion_date"],
    "award_notice": ["project_name", "client", "contract_no", "contract_date", "voltage_level_kv"],
    "acceptance_doc": ["project_name", "client", "acceptance_date", "completion_date", "contract_no"],
    "owner_evaluation": ["project_name", "client", "contract_no"],
    "person_id_card": [],
    "person_education": ["certificate_no", "specialty", "grade", "issuing_authority",
                         "issue_date", "valid_from", "valid_until"],
    "person_title_cert": ["certificate_no", "specialty", "grade", "issuing_authority",
                          "issue_date", "valid_from", "valid_until"],
    "person_registered_cert": ["certificate_no", "specialty", "grade", "issuing_authority",
                               "issue_date", "valid_from", "valid_until"],
    "person_special_op": ["certificate_no", "specialty", "grade", "issuing_authority",
                          "issue_date", "valid_from", "valid_until"],
    "person_safety_cert": ["certificate_no", "specialty", "grade", "issuing_authority",
                           "issue_date", "valid_from", "valid_until"],
    "person_social_security": ["certificate_no", "specialty", "issuing_authority",
                               "issue_date", "valid_from", "valid_until"],
    "person_document": ["certificate_no", "specialty", "grade", "issuing_authority",
                        "issue_date", "valid_from", "valid_until"],
    "patent": ["patent_name", "patent_no", "patent_type", "patentee", "application_date", "grant_date"],
    "software_copyright": ["software_name", "registration_no", "owner", "registration_date", "software_version"],
    "audit_report": ["fiscal_year", "report_type", "audit_firm", "audit_opinion", "report_date",
                     "revenue", "net_profit", "total_assets", "total_liabilities"],
    "financial_statement": ["fiscal_year", "report_type", "report_date",
                            "revenue", "net_profit", "total_assets", "total_liabilities"],
    "tax_credit": ["year", "rating", "authority_source", "issue_date", "valid_from", "valid_until"],
    "tax_payment_cert": ["title", "year", "authority_source", "issue_date"],
    "honor_cert": ["title", "year", "rating", "issuing_authority", "issue_date"],
    "credit_report": ["title", "year", "issuing_authority", "issue_date"],
    "other": ["title"],
}

# Fields that make sense to show on list rows when present (verified values only).
LIST_VALIDITY_TYPES = {"business_license", "safety_prod_license", "power_facility_license",
                       "construction_qual", "iso_cert", "person_education", "person_title_cert",
                       "person_registered_cert", "person_special_op", "person_safety_cert",
                       "person_social_security", "person_document", "tax_credit"}

EXT_CONTENT_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".jsonl": "application/jsonl; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".aip": "application/octet-stream",
}

OPENABLE_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}
UPLOAD_EXTS = (".pdf", ".png", ".jpg", ".jpeg")
UPLOAD_FORMAT_ERROR = "普通上传仅支持 PDF / PNG / JPG / JPEG；DOCX / XLSX 暂不支持，.doc 不支持。"

STATUS_LABELS = {
    "unverified": ("在用", "chip-blue"),
    "human_verified": ("在用", "chip-blue"),
    "ai_extracted": ("在用", "chip-blue"),
    "disputed": ("在用", "chip-blue"),
    "active": ("在用", "chip-blue"),
    "draft": ("整理中", "chip-gray"),
    "expired": ("已停用", "chip-gray"),
    "superseded": ("已更新", "chip-gray"),
    "archived": ("已归档", "chip-gray"),
}

# User-facing wording for field-level fact states. verified and inherited both
# mean "the information has a reliable confirmation basis" for readers; the
# underlying distinction is untouched in data and maintenance views.
FACT_OK = ("已确认", "chip-green")
FACT_PENDING = ("待完善", "chip-amber")
FACT_UNSET = ("暂未记录", "chip-gray")

FACT_LEGEND = [
    ("chip-green", "已确认", "信息已有可靠确认依据"),
    ("chip-amber", "待完善", "系统识别到信息，建议使用前核对"),
    ("chip-gray", "暂未记录", "这项信息目前没有记录"),
]

# Detail-page reading blocks: business information grouped the way people read
# documents (basic info first, scope/figures after). Maintenance is NOT done
# here — it lives in the single "完善资料" entry.
TYPE_BLOCKS = {
    "business_license": [("基本信息", ["enterprise_name", "unified_social_credit_code",
                                       "established_on", "valid_from", "valid_until", "long_term_valid"])],
    "safety_prod_license": [("基本信息", ["certificate_no", "issuing_authority", "issue_date",
                                          "valid_from", "valid_until", "long_term_valid"])],
    "power_facility_license": [("基本信息", ["certificate_no", "issuing_authority", "issue_date",
                                            "valid_from", "valid_until", "long_term_valid"]),
                               ("承装(修/试)范围", ["install_grade", "repair_grade", "test_grade", "scope"])],
    "construction_qual": [("基本信息", ["certificate_no", "issuing_authority", "issue_date",
                                        "valid_from", "valid_until", "long_term_valid"])],
    "iso_cert": [("基本信息", ["certificate_no", "standard", "issuing_authority", "issue_date",
                              "valid_from", "valid_until", "long_term_valid"]),
                 ("认证范围", ["scope"])],
    "contract": [("项目信息", ["project_name", "client", "contract_no", "contract_date",
                               "contract_amount", "project_type", "voltage_level_kv",
                               "location", "start_date", "completion_date"])],
    "award_notice": [("中标信息", ["project_name", "client", "contract_no", "contract_date", "voltage_level_kv"])],
    "acceptance_doc": [("验收信息", ["project_name", "client", "contract_no", "completion_date", "acceptance_date"])],
    "owner_evaluation": [("评价信息", ["project_name", "client", "contract_no"])],
    "person_id_card": [("证件信息", [])],
    "person_education": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                       "issue_date", "valid_from", "valid_until"])],
    "person_title_cert": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                        "issue_date", "valid_from", "valid_until"])],
    "person_registered_cert": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                             "issue_date", "valid_from", "valid_until"])],
    "person_special_op": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                        "issue_date", "valid_from", "valid_until"])],
    "person_safety_cert": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                         "issue_date", "valid_from", "valid_until"])],
    "person_social_security": [("参保信息", ["certificate_no", "specialty", "issuing_authority",
                                             "issue_date", "valid_from", "valid_until"])],
    "person_document": [("证书信息", ["certificate_no", "specialty", "grade", "issuing_authority",
                                      "issue_date", "valid_from", "valid_until"])],
    "patent": [("专利信息", ["patent_name", "patent_no", "patent_type", "patentee",
                             "application_date", "grant_date"])],
    "software_copyright": [("软件著作权信息", ["software_name", "registration_no", "owner",
                                              "software_version", "registration_date"])],
    "audit_report": [("基本信息", ["fiscal_year", "report_type", "audit_firm", "audit_opinion", "report_date"]),
                     ("财务数据（人民币）", ["revenue", "net_profit", "total_assets", "total_liabilities"])],
    "financial_statement": [("基本信息", ["fiscal_year", "report_type", "report_date"]),
                            ("财务数据（人民币）", ["revenue", "net_profit", "total_assets", "total_liabilities"])],
    "tax_credit": [("纳税信用信息", ["year", "rating", "authority_source", "issue_date",
                                     "valid_from", "valid_until"])],
    "tax_payment_cert": [("基本信息", ["year", "title", "authority_source", "issue_date"])],
    "honor_cert": [("基本信息", ["title", "year", "rating", "issuing_authority", "issue_date"])],
    "credit_report": [("基本信息", ["title", "year", "issuing_authority", "issue_date"])],
    "other": [("基本信息", ["title"])],
}
