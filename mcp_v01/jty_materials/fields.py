"""Closed business vocabulary; construction category keys are not generic attrs."""
import datetime as dt
import re
from .common import normalize

CERTIFICATES = {'business_license':'business_license', 'safety_prod_license':'safety_production',
                'power_facility_license':'power_facility','construction_qual':'construction_qualification','iso_cert':'iso'}
CERTIFICATES.update(credit_report='credit_report',tax_credit='tax_credit_a',tax_credit_a='tax_credit_a',
                    tax_payment_cert='other',honor_cert='other',enterprise_status='other')
PERFORMANCE_TYPES={'contract','award_notice','acceptance_doc','owner_evaluation'}
PERSON_TYPES={'person_id_card':'id_card','person_title_cert':'title','person_registered_cert':'registered',
              'person_special_op':'special_operation','person_safety_cert':'safety_assessment',
              'person_social_security':'social_security','person_education':'education','person_document':'other'}
FINANCE_TYPES={'audit_report','financial_statement'}
# Reuse existing typed columns. Values in these columns still require human review.
DETAIL_FIELDS={
 'performance_detail':{'project_name':('project_name','text'),'client':('client_name_text','text'),
  'contract_no':('contract_no','text'),'contract_date':('contract_date','date'),
  'contract_amount':('contract_amount_cents','amount'),'project_type':('project_type','text'),
  'voltage_level_kv':('voltage_level_kv','int'),'location':('location','text'),
  'start_date':('start_date','date'),'completion_date':('completion_date','date'),'acceptance_date':('acceptance_date','date')},
 'person_certificate_detail':{f:(f,'date' if f.endswith('_date') or f in ('valid_from','valid_until') else 'text')
   for f in ['certificate_no','specialty','grade','issuing_authority','issue_date','valid_from','valid_until']},
 'patent_detail':{'patent_name':('patent_name','text'),'patent_no':('patent_no','text'),'patent_type':('patent_type','text'),
   'patentee':('patentee_text','text'),'application_date':('application_date','date'),'grant_date':('grant_date','date')},
 'software_copyright_detail':{'software_name':('software_name','text'),'registration_no':('registration_no','text'),
   'owner':('owner_text','text'),'registration_date':('registration_date','date'),'software_version':('version','text')},
 'audit_report_detail':{'fiscal_year':('fiscal_year','year'),'report_type':('report_type','text'),
   'audit_firm':('accounting_firm','text'),'audit_opinion':('audit_opinion','text'),'report_date':('report_date','date'),
   'revenue':('revenue_cents','amount'),'net_profit':('net_profit_cents','signed_amount'),
   'total_assets':('total_assets_cents','amount'),'total_liabilities':('total_liabilities_cents','amount')}}
TYPE_TABLE={**{t:'performance_detail' for t in PERFORMANCE_TYPES},**{t:'person_certificate_detail' for t in PERSON_TYPES},
            'patent':'patent_detail','software_copyright':'software_copyright_detail',**{t:'audit_report_detail' for t in FINANCE_TYPES}}
ORG_FIELDS = {'enterprise_name':('org','name','text'),
              'unified_social_credit_code':('org','unified_social_credit_code','credit'),
              'established_on':('org','established_on','date')}
GRADE_FIELDS = {'install_grade':'install','repair_grade':'repair','test_grade':'test'}
FIELDS = dict(ORG_FIELDS, valid_until=('certificate_detail','valid_until','date'),
              valid_from=('certificate_detail','valid_from','date'),
              long_term_valid=('certificate_detail','long_term_valid','bool'),
              certificate_no=('certificate_detail','certificate_no','text'),
              issuing_authority=('certificate_detail','issuing_authority','text'),
              scope=('certificate_detail','scope','text'),
              standard=('certificate_detail','standard','text'),
              issue_date=('certificate_detail','issue_date','date'),
              contract_amount=('performance_detail','contract_amount_cents','amount'))
FIELDS.update({f:('power_license_item','grade','grade') for f in GRADE_FIELDS})
ALLOWED = {'business_license':set(ORG_FIELDS)|{'valid_from','valid_until','long_term_valid'},
           'safety_prod_license':{'certificate_no','issuing_authority','valid_until','valid_from','long_term_valid'},
           'power_facility_license':{'certificate_no','issuing_authority','valid_from','valid_until','long_term_valid','scope'}|set(GRADE_FIELDS),
           'contract':{'contract_amount'}}
ALLOWED['construction_qual']={'certificate_no','issuing_authority','valid_from','valid_until','long_term_valid','issue_date'}
ALLOWED['iso_cert']={'certificate_no','issuing_authority','valid_from','valid_until','long_term_valid','issue_date','standard','scope'}
for typ,table in TYPE_TABLE.items():ALLOWED[typ]=set(DETAIL_FIELDS[table])
for typ in ['credit_report','tax_credit','tax_credit_a','tax_payment_cert','honor_cert','enterprise_status']:
    ALLOWED[typ]={'certificate_no','issuing_authority','issue_date','valid_from','valid_until','long_term_valid','scope','title','year','rating','authority_source'}
ALLOWED['other']={'title'}
FIELDS.update(title=('certificate_detail','title','text'),year=('certificate_detail','tax_year','year'),
              rating=('certificate_detail','rating','text'),authority_source=('certificate_detail','authority_source','text'))
for table,fields in DETAIL_FIELDS.items():
    for field,(col,kind) in fields.items():FIELDS.setdefault(field,(table,col,kind))

QUAL_PREFIX='qualification:'
def qualification_name(field):
    if isinstance(field,str) and field.startswith(QUAL_PREFIX):
        name=field[len(QUAL_PREFIX):]
        if not name or name!=name.strip() or len(name)>120 or any(ord(c)<32 for c in name):
            raise ValueError('qualification name must be 1..120 trimmed printable characters')
        return name
    return None

def field_spec(field,material_type=None):
    table=TYPE_TABLE.get(material_type)
    if table and field in DETAIL_FIELDS[table]:
        col,kind=DETAIL_FIELDS[table][field];return table,col,kind
    return ('construction_qual_item','grade','text') if qualification_name(field) is not None else FIELDS.get(field)

def allowed_field(material_type,field):
    return field in ALLOWED.get(material_type,set()) or (material_type=='construction_qual' and qualification_name(field) is not None)

def resolve_type(kind, subtype=None, existing=None):
    if existing is not None:
        if existing not in ALLOWED:raise ValueError('unsupported existing material type')
        expected_kind='contract' if existing in PERFORMANCE_TYPES else 'certificate' if existing in CERTIFICATES else 'material'
        if kind is not None and kind!=expected_kind:raise ValueError('replacement kind cannot change material type')
        if subtype is not None and subtype!=existing:raise ValueError('replacement subtype cannot change material type')
        return expected_kind,existing,CERTIFICATES.get(existing)
    if kind=='contract' and subtype is None:return kind,'contract',None
    if kind=='certificate' and subtype in CERTIFICATES:return kind,subtype,CERTIFICATES[subtype]
    if kind=='material' and subtype in ALLOWED:return kind,subtype,CERTIFICATES.get(subtype)
    if kind=='contract' and subtype in PERFORMANCE_TYPES:return kind,subtype,None
    raise ValueError('certificate requires an explicit supported subtype; contract forbids subtype')

def org_norm(value):return re.sub(r'\s+','',normalize(value))

def validate_value(field,value):
    spec=field_spec(field)
    if spec is None:raise ValueError('unsupported verified field')
    kind=spec[2]
    if value is None:return None
    if kind=='date':
        if not isinstance(value,str) or dt.date.fromisoformat(value).isoformat()!=value:raise ValueError('canonical ISO date required')
    elif kind=='bool':
        if type(value) is not bool:raise ValueError('long_term_valid requires JSON boolean or null to withdraw')
        return int(value)
    elif kind in ('int','year','signed_amount'):
        if type(value) is not int or abs(value)>2**63-1 or (kind=='int' and value<0) or (kind=='year' and not 1900<=value<=2200):raise ValueError('invalid typed integer')
    elif kind in ('amount','grade'):
        if type(value) is not int or value<0 or value>2**63-1 or (kind=='grade' and not 1<=value<=5):raise ValueError('invalid integer amount/grade')
    elif not isinstance(value,str) or not value.strip() or value!=value.strip() or len(value)>4000:
        raise ValueError('nonempty trimmed text required, at most 4000 characters')
    if kind=='credit' and not re.fullmatch(r'[0-9A-Z]{18}',value):raise ValueError('credit code requires 18 uppercase alphanumeric characters')
    return value
