"""Real HTTP/Chromium QA. Browser plugin not available; installed Playwright.
All screenshots, Chrome profiles and runtime copies remain in this worktree.
"""
import json, os, threading, time
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright
from jty_materials.common import atomic_write
from jty_materials.manager import request
from jty_materials.query import Query
from jty_materials.store import Store
from webui import app
from upload_repair_support import EXPECTED, NAME, make_license, real_fixture


def main():
    base,c=real_fixture('qa')
    handler=type('UploadRepairQA',(app.Handler,),dict(app=app.AppContext(c)))
    server=app.ThreadingHTTPServer(('127.0.0.1',0),handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}'
    evidence=[];errors=[]
    try:
        with sync_playwright() as p:
            for width,height in [(1365,900),(390,844)]:
                context=p.chromium.launch_persistent_context(str(base/f'profile-{width}'),
                    executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True,
                    viewport=dict(width=width,height=height),is_mobile=width==390,has_touch=width==390)
                page=context.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
                def shot(name):
                    assert page.title() and page.locator('body').inner_text().strip()
                    assert 'Traceback (most recent call last)' not in page.locator('body').inner_text()
                    sizes=page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
                    assert sizes['scroll']<=sizes['width'],(name,sizes)
                    path=base/f'{width}-{name}.png';page.screenshot(path=str(path),full_page=True)
                    evidence.append(dict(viewport=f'{width}x{height}',state=name,url=page.url,title=page.title(),overflow=False,screenshot=str(path)))
                path=make_license(base/f'营业执照-{width}.png')
                with Image.open(path) as image:
                    image.putpixel((0,0),(width%255,0,0));image.save(path)
                page.goto(origin+'/upload')
                assert not page.locator('[name=title]').get_attribute('required')
                page.locator('[name=subtype]').select_option('business_license')
                page.locator('.upload-form [name=actor]').fill('隔离QA测试')
                page.locator('.upload-form .file-input:not(.camera-input)').set_input_files(str(path))
                assert page.locator('[name=title]').input_value()==''
                shot('upload')
                page.get_by_role('button',name='开始上传',exact=True).click()
                page.locator('.upload-progress:not(.hidden)').wait_for()
                shot('progress')
                link=page.get_by_role('link',name='查看资料 →')
                page.locator('.upload-result a, .upload-result .err-box').first.wait_for(timeout=240000)
                assert not page.locator('.upload-result .err-box').count(),page.locator('.upload-result').inner_text()
                link.wait_for(timeout=240000);link.click()
                page.get_by_role('heading',name=NAME+' 营业执照',exact=True).wait_for()
                mid=int(page.url.rsplit('/',1)[-1])
                assert '有效期未记录' not in page.locator('body').inner_text()
                assert page.locator('.pending-tag').count()>=3
                for value in EXPECTED.values():assert value in page.locator('body').inner_text()
                shot('detail')
                page.get_by_role('button',name='完善资料',exact=True).click()
                dialog=page.locator('#review-dialog');dialog.wait_for(state='visible')
                dialog.locator('select').first.select_option('enterprise_name')
                dialog.get_by_role('button',name=NAME,exact=True).click()
                assert dialog.locator('[name=value]').input_value()==NAME
                dialog.get_by_text('查看识别依据',exact=True).click()
                assert dialog.get_by_role('link',name='查看当前原件').count()==1
                shot('review-dialog')
                dialog.get_by_role('button',name='取消',exact=True).click()
                with Store(c,readonly=True) as s:
                    previous_run=s.db.execute('select max(run_id) from extraction_run where material_id=?',(mid,)).fetchone()[0]
                    before=dict(materials=s.db.execute('select count(*) from material').fetchone()[0],
                        originals=[tuple(r) for r in s.db.execute("select file_id,version_id,sha256 from material_file where role='original'")],
                        events=[tuple(r) for r in s.db.execute('select * from verification_event')])
                page.once('dialog',lambda d:d.accept('隔离QA重新识别'))
                page.get_by_role('button',name='重新识别',exact=True).click()
                page.get_by_text('正在重新识别…',exact=True).wait_for()
                shot('reextract-progress')
                page.wait_for_function("!document.querySelector('[data-reextract]').disabled",timeout=240000)
                page.get_by_role('heading',name=NAME+' 营业执照',exact=True).wait_for()
                assert '重新识别失败' not in page.locator('body').inner_text()
                shot('reextract-done')
                with Store(c,readonly=True) as s:
                    latest_run=s.db.execute('select max(run_id) from extraction_run where material_id=?',(mid,)).fetchone()[0]
                    assert latest_run>previous_run
                    assert s.db.execute("select count(*) from extracted_fact where run_id=? and review_status='pending'",(latest_run,)).fetchone()[0]==3
                    after=dict(materials=s.db.execute('select count(*) from material').fetchone()[0],
                        originals=[tuple(r) for r in s.db.execute("select file_id,version_id,sha256 from material_file where role='original'")],
                        events=[tuple(r) for r in s.db.execute('select * from verification_event')])
                assert before==after,(before,after)
                assert Query(c).get_material(mid)['verified_facts']=={}
                response=context.request.post(origin+'/api/reextract',data=json.dumps(dict(material_id=mid,actor='QA',path='C:/untrusted')),
                    headers={'Content-Type':'application/json'})
                assert response.status==400
                response=context.request.post(origin+'/api/reextract',data='{}',headers={'Origin':'https://invalid.example'})
                assert response.status==403
                context.close()
        assert not errors,errors
        atomic_write(base/'summary.json',json.dumps(dict(status='PASS',browser_plugin='not available',origin=origin,
            console_errors=errors,flow='upload -> real Umi -> pending fields -> review evidence -> reextract same original',evidence=evidence,config=str(c.path)),ensure_ascii=False,indent=2))
        print(str(base/'summary.json'),flush=True)
    finally:
        server.shutdown();server.server_close();request(c,'shutdown',start=False)


if __name__=='__main__':main()
