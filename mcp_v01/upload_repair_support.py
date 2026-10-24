"""Synthetic, readable license; no production original is used as a fixture."""
from PIL import Image, ImageDraw, ImageFont

NAME = '合成测试示例建筑工程有限公司'
# synthetic fixture only: TEST is a test marker, never a production registry prefix.
# The current validator enforces [0-9A-Z]{18}; it does not compute a check digit.
CREDIT = 'TEST0000012345678Y'
TEXT = f'''营业执照
统一社会信用代码
{CREDIT}
名称
{NAME}
法定代表人 测试人员
成立日期 2001年02月03日
住所 测试地址
登记机关
2031年12月19日'''
EXPECTED = dict(enterprise_name=NAME, unified_social_credit_code=CREDIT, established_on='2001-02-03')


def make_license(path):
    image = Image.new('RGB', (1700, 1550), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 56)
    for i, line in enumerate(TEXT.splitlines()):
        draw.text((95, 65 + i*130), line, font=font, fill='black')
    image.save(path)
    return path
import json, os, re, socket, uuid
from pathlib import Path
from jty_materials.common import Config, atomic_write
from jty_materials.setup import copy_runtime
from jty_materials.store import Store


def real_fixture(label='real'):
    base = Path(__file__).resolve().parents[1]/'runs/upload-repair'/(label+'-'+uuid.uuid4().hex[:6])
    umi = Path(os.environ['LARGE_DOC_UMI_EXE'])
    dowse = Path(os.environ['P05_DOWSE_EXE'])
    assert umi.is_file() and dowse.is_file(), 'Real Umi/dowse required; no skip'
    version=json.loads((umi.parent/'UmiOCR-data/about.json').read_text(encoding='utf-8'))['version']
    assert [version[k] for k in ('major','minor','patch')]==[2,1,5], version
    url = copy_runtime(umi, dowse, base/'rt')
    # Keep the OCR listener out of Windows' ephemeral client-port range while
    # Chromium and the HTTP harness open many loopback connections.
    for _ in range(100):
        port=18000+int(uuid.uuid4().hex[:4],16)%5000
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port))
            except OSError:continue
            break
    else:raise RuntimeError('no free isolated OCR port')
    data=base/'rt/umi/UmiOCR-data'
    atomic_write(data/'.settings',re.sub(r'(?m)^server.port=.*$',f'server.port={port}',(data/'.settings').read_text(encoding='utf-8')))
    pre=json.loads((data/'.pre_settings').read_text(encoding='utf-8'));pre['server_port']=port
    atomic_write(data/'.pre_settings',json.dumps(pre));url=f'http://127.0.0.1:{port}'
    raw = dict(repository_root='repo', db_path='repo/db/materials.db', state_dir='state',logs_dir='logs',
        backup_dir='backups',index_dir='index/dowse-index',umi_exe='rt/umi/Umi-OCR.exe',dowse_exe='rt/dowse/dowse.exe',
        umi_url=url,fixture_only=True)
    atomic_write(base/'config.json',json.dumps(raw))
    c = Config(base/'config.json');c.materials.mkdir(parents=True);c.logs_dir.mkdir(parents=True)
    Store.initialize(c)
    return base,c
