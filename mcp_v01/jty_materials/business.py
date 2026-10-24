"""Small closed business extractors: all output is pending, never verified."""
import datetime as dt,json,re
from decimal import Decimal,InvalidOperation
from .common import normalize
from .fields import ALLOWED,PERSON_TYPES,PERFORMANCE_TYPES,FINANCE_TYPES,field_spec,validate_value,allowed_field

def _license_values(raw):
    """Match normalized labels, but return spans from the immutable OCR text."""
    chars=[]; offsets=[]
    for pos,char in enumerate(raw):
        for normalized in normalize(char):
            chars.append(normalized);offsets.append(pos)
    text=''.join(chars)
    def evidence(match):return raw[offsets[match.start()]:offsets[match.end()-1]+1]
    # A line label, not a mention of a name in the scope/footer text. Stop at
    # adjacent license labels when OCR puts multiple columns on one line.
    stop=r'(?:类型|法定代表人|注册资本|成立日期|住所|经营范围|登记机关)'
    for m in re.finditer(r'(?:^|\n)[ \t]*(?:企业[ \t]*)?名[ \t]*称[ \t]*[:：]?[ \t]*\n?[ \t]*([^\n]{2,160})',text):
        value=re.split(stop,m[1])[0]
        value=re.sub(r'\s+','',value).strip(':：')
        if (re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9()·]{4,120}',value)
                and re.search(r'(?:公司|企业|合作社|事务所|中心|厂|商行)$',value)
                and not any(s in value for s in ('营业执照','登记机关','公示系统','网址'))):
            yield 'enterprise_name',value,evidence(m)
    label=r'统\s*一\s*社\s*会\s*信\s*用\s*代\s*码'
    for m in re.finditer(label+r'\s*[:：]?\s*((?:[0-9A-Z][ \t]*){18})(?![ \t]*[0-9A-Z])',text):
        yield 'unified_social_credit_code',re.sub(r'\s+','',m[1]),evidence(m)
    label=r'成\s*立\s*日\s*期'
    for m in re.finditer(label+r'\s*[:：]?\s*(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?',text):
        try:value=dt.date(*map(int,m.groups())).isoformat()
        except ValueError:continue
        yield 'established_on',value,evidence(m)


def extract_candidates(text,typ,context=None):
    raw_text=text;context=context or {}
    text=normalize(text);lines=text.splitlines();seen=set()
    def emit(field,value,raw):
        if not allowed_field(typ,field):return
        try:validate_value(field,value)
        except (ValueError,TypeError):return
        key=(field,str(value))
        if key in seen:return
        seen.add(key);kind=field_spec(field,typ)[2]
        vt='int' if kind in ('int','year','grade') else 'amount' if kind in ('amount','signed_amount') else 'text' if kind=='credit' else kind
        yield field,vt,raw,str(value)
    def labeled(field,labels,max_length=200):
        for label in labels:
            for match in re.finditer(label+r'\s*[:：]?\s*([^\n]{2,'+str(max_length)+'})',text):
                value=match[1].strip()
                if value and not re.fullmatch(r'[\d\s/.-]+',value):yield from emit(field,value,match[0])
    for line in lines:
        for y,m,d in re.findall(r'(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})',line):
            try:date=dt.date(int(y),int(m),int(d)).isoformat()
            except ValueError:continue
            if any(w in line for w in ('有效期','有效期限')) and ('至' in line or '有效期:' in line or '有效期：' in line):
                # For a range, its final date is the expiry, never the start.
                dates=re.findall(r'(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})',line)
                try:expiry=dt.date(*map(int,dates[-1])).isoformat()
                except ValueError:continue
                yield from emit('valid_until',expiry,line)
            elif '发证日期' in line or '颁发日期' in line:yield from emit('issue_date',date,line)
            elif '登记日期' in line:yield from emit('registration_date',date,line)
    if typ=='business_license':
        for field,value,evidence in _license_values(raw_text):yield from emit(field,value,evidence)
    elif typ in PERFORMANCE_TYPES:
        if context.get('project_name'):yield from emit('project_name',context['project_name'],'source directory hint (unverified): '+context['project_name'])
        yield from labeled('client',[r'发包人(?:名称)?',r'甲方(?:名称)?',r'建设单位'])
        yield from labeled('contract_no',[r'合同编号',r'合同号'])
        for match in re.finditer(r'(?<![\d.])(\d{1,3})\s*[kKｋ][vVＶ](?![aA])',text+'\n'+context.get('source_relative_path','')):
            yield from emit('voltage_level_kv',int(match[1]),match[0])
        for label in ['签约合同价','合同金额','合同总价','签约合同价格']:
            for m in re.finditer(label+r'[^\d\n]{0,25}(\d[\d,]*(?:\.\d{1,2})?)\s*(万元|元)?',text):
                if not m[2]:continue
                try:value=int(Decimal(m[1].replace(',',''))*(1000000 if m[2]=='万元' else 100))
                except InvalidOperation:continue
                yield from emit('contract_amount',value,m[0])
        # Retain the original explicit currency amount rule for all entrances.
        for m in re.finditer(r'[¥￥]\s*([\d,]+\.\d{2})',text):
            yield from emit('contract_amount',int(Decimal(m[1].replace(',',''))*100),m[0])
        yield from labeled('location',[r'工程地点',r'项目地点',r'建设地点'])
        for label,field in [('开工日期','start_date'),('竣工日期','completion_date'),('签订日期','contract_date')]:
            for m in re.finditer(label+r'[^\d\n]{0,10}(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})',text):
                try:yield from emit(field,dt.date(*map(int,m.groups())).isoformat(),m[0])
                except ValueError:pass
    elif typ in PERSON_TYPES:
        if typ!='person_id_card':yield from labeled('certificate_no',[r'证书编号',r'证书号码',r'注册编号'],80)
        yield from labeled('specialty',[r'专业类别',r'注册专业',r'专业',r'准操项目'],80)
        yield from labeled('grade',[r'资格等级',r'技术等级',r'职称级别'],40)
        yield from labeled('issuing_authority',[r'发证机关',r'发证机构'],100)
        # Do not parse identity numbers, bank accounts, dates of birth or every personal field.
        if typ=='person_id_card':
            for result in []:yield result
    elif typ=='patent':
        for m in re.finditer(r'(?:ZL\s*)?20\d{10}[.．]\s*[\dXx]',text+'\n'+context.get('source_relative_path','')):
            yield from emit('patent_no',re.sub(r'\s+','',m[0]).replace('．','.'),m[0])
        yield from labeled('patent_name',[r'实用新型名称',r'发明名称',r'外观设计名称',r'专利名称'])
        yield from labeled('patentee',[r'专利权人',r'申请人'])
        if '实用新型' in text:yield from emit('patent_type','utility_model','OCR contains 实用新型; no current legal-status inference')
        elif '发明专利' in text:yield from emit('patent_type','invention','OCR contains 发明专利; no current legal-status inference')
        for m in re.finditer(r'授权公告日[^\d]{0,12}(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})',text):
            try:yield from emit('grant_date',dt.date(*map(int,m.groups())).isoformat(),m[0])
            except ValueError:pass
    elif typ=='software_copyright':
        yield from labeled('software_name',[r'软件名称'])
        yield from labeled('registration_no',[r'登记号',r'登记编号'],80)
        yield from labeled('owner',[r'著作权人'])
    elif typ in FINANCE_TYPES:
        year=context.get('fiscal_year_hint')
        if year:yield from emit('fiscal_year',year,'source path fiscal-year hint (unverified): '+context['source_relative_path'])
        for m in re.finditer(r'(20\d{2})年度(?:财务报表|财务报告)',text):yield from emit('fiscal_year',int(m[1]),m[0])
        yield from emit('report_type','audit_report' if '审计报告' in text else 'financial_statement','document wording, unverified classification')
        for m in re.finditer(r'[\u4e00-\u9fff]{3,24}会计师事务所(?:\([^\n]{1,20}\))?',text):yield from emit('audit_firm',m[0],m[0])
        unit=100 if re.search(r'(?:金额)?单位\s*[:：]\s*(?:人民币)?元',text) else 1000000 if re.search(r'(?:金额)?单位\s*[:：]\s*(?:人民币)?万元',text) else None
        if unit:
            for label,field in [('营业收入','revenue'),('净利润','net_profit'),('资产总计','total_assets'),('负债合计','total_liabilities')]:
                # One row, one explicit number only. Comparative columns remain manual_review.
                for m in re.finditer(label+r'[^\d\n-]{0,12}(-?\d[\d,]*\.\d{2})\s*(?=\n|$)',text):
                    try:yield from emit(field,int(Decimal(m[1].replace(',',''))*unit),m[0])
                    except InvalidOperation:pass
    elif typ in ('tax_credit','tax_credit_a'):
        for m in re.finditer(r'(20\d{2})\s*(?:年度|年)',text+'\n'+context.get('source_relative_path','')):yield from emit('year',int(m[1]),m[0])
        for m in re.finditer(r'(?:纳税信用.{0,12}|评价结果.{0,12}|信用级别.{0,8})([ABCDM])\s*级?',text):yield from emit('rating',m[1],m[0])
        yield from labeled('authority_source',[r'主管税务机关',r'主管税务局',r'出具机关'])
    elif typ in ('credit_report','honor_cert','enterprise_status'):
        yield from emit('title',context.get('source_title','投标证明材料'), 'source filename hint, not verified title')
        yield from labeled('issuing_authority',[r'颁发单位',r'发证单位',r'发证机关'])
        for m in re.finditer(r'(20\d{2})年\d{1,2}月',text):yield from emit('year',int(m[1]),m[0])
        for m in re.finditer(r'(AAA|AA|A)\s*级',text):yield from emit('rating',m[1],m[0])
    elif typ=='other':
        yield from emit('title',context.get('source_title','投标证明材料'),'source filename hint, not verified title')

def business_metadata(db,mid,material_type,original_ids):
    candidates={}
    for r in db.execute('''SELECT e.*,r.source_file_id FROM extracted_fact e JOIN extraction_run r USING(run_id)
              WHERE e.material_id=? AND e.review_status IN ('pending','accepted') ORDER BY e.extracted_id''',(mid,)):
        if r['source_file_id'] not in original_ids:continue
        if not allowed_field(material_type,r['field_name']):continue
        spec=field_spec(r['field_name'],material_type);value=r['normalized_value']
        if spec[2] in ('int','year','amount','signed_amount','grade'):
            try:value=int(value)
            except (TypeError,ValueError):continue
        elif spec[2]=='bool':
            if value not in ('0','1'):continue
            value=value=='1'
        candidates.setdefault(r['field_name'],[]).append(dict(value=value,status='candidate',extracted_id=r['extracted_id'],evidence_file_id=r['source_file_id'],evidence=r['evidence_span']))
    people=[dict(r) for r in db.execute('''SELECT p.person_id,p.person_code,p.name,p.job_title,p.identity_status,mp.role_on_material,mp.relation_status,mp.evidence_file_id
      FROM material_person mp JOIN person p USING(person_id) WHERE mp.material_id=?''',(mid,)) if r['evidence_file_id'] in original_ids]
    relations=[dict(r) for r in db.execute('SELECT * FROM material_relation WHERE material_id=? OR related_material_id=?',(mid,mid))]
    source=None
    for row in db.execute("SELECT detail_json FROM audit_log WHERE action='material.source_context' AND entity_id=? ORDER BY log_id DESC",(mid,)):
        if json.loads(row[0]).get('original_file_id') in original_ids:
            source=row;break
    profile=None
    for record in db.execute("SELECT detail_json FROM audit_log WHERE action='material.document_profile' AND entity_id=? ORDER BY log_id DESC",(mid,)):
        item=json.loads(record[0])
        if item['original_file_id'] in original_ids:profile=item;break
    ambiguities={f:[c['value'] for c in values] for f,values in candidates.items() if len({str(c['value']) for c in values})>1}
    return dict(candidate_fields=candidates,candidate_ambiguities=ambiguities,persons=people,relations=relations,source_context=json.loads(source[0]) if source else None,document_profile=profile,
      financial_metric_policy='CNY cents; candidate periods/units and comparative columns require review. Versions/pages can repeat a year; never sum across material records.' if material_type in FINANCE_TYPES else None,
      legal_status={'value':'unknown','basis':'no independently verified current legal status'} if material_type=='patent' else None)
