"""Actual Chromium HTTP flows at desktop/phone sizes in an isolated fixture.

Browser plugin not available. Uses installed Chrome through Playwright; no
browser install or production configuration. Artifacts: runs/large-document.
Requires P05_DOWSE_EXE and LARGE_DOC_UMI_EXE, copied before use.
"""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from document_test_support import DocumentFixture, make_pdf, make_docx, make_xlsx
from jty_materials.common import Config, atomic_write
from jty_materials.document_extract import PageOcr
from jty_materials.manager import request
from jty_materials.project_documents import Documents
from jty_materials.setup import copy_runtime
from webui import app


def main():
    fixture=DocumentFixture();fixture.setUp()
    output=Path(__file__).resolve().parents[1]/'runs/large-document/browser-qa'
    output.mkdir(parents=True,exist_ok=True)
    fixture.process(make_pdf(fixture.base/'招标文件.pdf'))
    fixture.process(make_docx(fixture.base/'历史投标文件.docx'))
    fixture.process(make_xlsx(fixture.base/'工程量清单.xlsx'))
    fid=fixture.add(make_pdf(fixture.base/'部分页面.pdf',('native','scan')))
    rid,entry=fixture.begin(fid);partial=fixture.complete(rid,entry,PageOcr)
    assert partial['status']=='partial',partial
    broken=fixture.base/'损坏文件.pdf';broken.write_bytes(b'%PDF-malformed')
    bad=fixture.add(broken)
    scanned=fixture.add(make_pdf(fixture.base/'大型招标文件.pdf',('native',)*300))
    (fixture.base/'bin').mkdir()
    shutil.copy2(os.environ['P05_DOWSE_EXE'],fixture.base/'bin/dowse.exe')
    raw=dict(fixture.c.raw,dowse_exe='bin/dowse.exe')
    atomic_write(fixture.c.path,json.dumps(raw));c=Config(fixture.c.path)
    log=(output/'manager.log').open('ab')
    child=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('manage.py')),'--config',str(c.path)],
        stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    def rpc(action,payload=None):return request(c,action,payload,start=False,timeout=120)
    server=None
    evidence=[]
    try:
        until=time.monotonic()+20
        while time.monotonic()<until:
            if rpc('status').get('running'):break
            time.sleep(.1)
        else:raise RuntimeError('fixture manager did not start')
        rpc('project.document.extract',dict(project_id=fixture.pid,project_file_id=bad))
        until=time.monotonic()+20
        while time.monotonic()<until:
            if next(d for d in Documents(c).status(fixture.pid) if d['project_file_id']==bad)['status']=='failed':break
            time.sleep(.1)
        handler=type('DocumentQAHandler',(app.Handler,),dict(app=app.AppContext(c)))
        server=app.ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        # Flow: project detail -> extraction -> live status -> complete ->
        # project search -> file/page/section/sheet result and original link.
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            for width,height in [(1365,900),(390,844)]:
                context=browser.new_context(viewport=dict(width=width,height=height),is_mobile=width==390,has_touch=width==390)
                page=context.new_page();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
                def shot(name):
                    assert page.title()
                    assert page.locator('body').inner_text().strip()
                    assert not page.locator('body').filter(has_text='Traceback (most recent call last)').count()
                    sizes=page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
                    assert sizes['scroll']<=sizes['width'],(name,sizes)
                    target=output/f'{width}-{name}.png'
                    page.screenshot(path=str(target),full_page=True)
                    evidence.append(dict(viewport=f'{width}x{height}',state=name,url=page.url,title=page.title(),overflow=False,screenshot=str(target)))
                page.goto(origin+f'/project/{fixture.pid}')
                page.get_by_text('文档内容：部分内容未识别',exact=True).wait_for()
                page.get_by_text('文档内容：内容提取失败',exact=True).wait_for()
                shot('detail')
                row=page.locator('.project-row').filter(has_text='大型招标文件.pdf')
                row.get_by_role('button',name='提取内容' if width==1365 else '重新整理',exact=True).click()
                page.wait_for_url(origin+f'/project/{fixture.pid}')
                state=page.locator(f'[data-document-file="{scanned}"]')
                state.filter(has_text='正在整理内容').wait_for(timeout=60000)
                shot('processing')
                assert not c.maintenance_file.exists()
                response=context.request.get(origin+f'/project-file/{fixture.pid}/{scanned}')
                assert response.status==200
                page.goto(origin+'/browse')
                assert page.locator('body').inner_text().strip()
                page.goto(origin+f'/project/{fixture.pid}')
                state=page.locator(f'[data-document-file="{scanned}"]')
                state.filter(has_text='已可搜索').wait_for(timeout=180000)
                shot('complete')
                page.get_by_role('link',name='搜索项目文件',exact=True).click()
                for term,name,expected in [('项目经理','pdf-result','第 1 页'),('DOCX_SECTION_SENTINEL','docx-result','（一）项目经理'),('SPARSE_ROW_SENTINEL','xlsx-result','第 280–280 行')]:
                    page.get_by_role('searchbox').fill(term)
                    page.get_by_role('button',name='搜索',exact=True).click()
                    page.get_by_text(expected,exact=False).first.wait_for()
                    if name=='pdf-result':
                        assert page.locator('a[href$="#page=1"]').count()>0
                    shot(name)
                assert not errors,errors
                context.close()
            browser.close()
        atomic_write(output/'summary.json',json.dumps(dict(status='PASS',browser='installed Chrome via Playwright',
            browser_plugin='not available',origin=origin,console_errors=[],evidence=evidence,config=str(c.path)),ensure_ascii=False,indent=2))
        print(str(output/'summary.json'),flush=True)
    finally:
        if server:server.shutdown();server.server_close()
        if child.poll() is None:
            try:rpc('shutdown')
            finally:
                try:child.wait(timeout=45)
                except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
        log.close();fixture.doCleanups()


if __name__=='__main__':main()
