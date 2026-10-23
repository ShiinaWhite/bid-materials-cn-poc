"""Synthetic large-file smoke. No production paths/config; no committed binaries.

Run: python -B mcp_v01/large_document_smoke.py
"""
import ctypes
import io
import json
from pathlib import Path
import random
import time

import pymupdf
from PIL import Image

from document_test_support import DocumentFixture, make_docx, make_xlsx
from jty_materials.common import atomic_write
from jty_materials.document_extract import PageOcr
from jty_materials.project_documents import Documents
from jty_materials.store import Store
from jty_materials.doctor import check_documents


def illustrated_pdf(path, pages):
    """Native text plus unique synthetic drawings: OCR must stay at zero."""
    with pymupdf.open() as document:
        for i in range(pages):
            page=document.new_page()
            for line in range(20):
                page.insert_text((35,40+line*18), f'PAGE {i+1} item {line+1} LARGE_DOCUMENT_NATIVE_SENTINEL project manager quality payment safety 10kV', fontsize=9)
            page.insert_text((35,430),'项目经理 工期 质量标准 付款方式 安全生产许可证',fontname='china-s',fontsize=12)
            image=Image.frombytes('RGB',(400,300),random.Random(i).randbytes(400*300*3))
            output=io.BytesIO();image.save(output,format='JPEG',quality=75)
            page.insert_image(pymupdf.Rect(35,460,550,810),stream=output.getvalue())
        document.save(path,deflate=True)
    return path


def peak_memory():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(k,ctypes.c_size_t) for k in
            ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    kernel=ctypes.WinDLL('kernel32');psapi=ctypes.WinDLL('psapi')
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    counters=Counters();counters.cb=ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):return None
    return counters.PeakWorkingSetSize


def size_with_wal(path):
    return sum(p.stat().st_size for p in (path,Path(str(path)+'-wal')) if p.exists())


def main():
    fixture=DocumentFixture();fixture.setUp()
    records=[]
    try:
        root=fixture.base
        for name,generator in [
            ('native-100.pdf',lambda p:illustrated_pdf(p,100)),
            ('native-300.pdf',lambda p:illustrated_pdf(p,300)),
            ('large.docx',lambda p:make_docx(p,sections=500)),
            ('large.xlsx',lambda p:make_xlsx(p,rows=10000,sheets=4)),
        ]:
            source=generator(root/name)
            fid=fixture.add(source)
            db_before=size_with_wal(fixture.c.db_path)
            index_before=size_with_wal(fixture.c.document_index)
            started=time.perf_counter()
            rid,entry=fixture.begin(fid)
            result=fixture.complete(rid,entry,PageOcr)
            elapsed=time.perf_counter()-started
            assert result['status']=='complete',result
            with Store(fixture.c,readonly=True) as s:
                rows=s.db.execute('''SELECT page_status,count(*) FROM project_document_unit WHERE document_id=? GROUP BY page_status''',(entry['document_id'],)).fetchall()
                raw,norm=s.db.execute('''SELECT coalesce(sum(length(CAST(text_raw AS BLOB))),0),coalesce(sum(length(CAST(text_norm AS BLOB))),0)
                    FROM project_document_unit WHERE document_id=?''',(entry['document_id'],)).fetchone()
                assert check_documents(fixture.c,s.db)['healthy']
            record=dict(file=name,original_bytes=source.stat().st_size,units=result['unit_count'],methods=dict(rows),
                raw_bytes=raw,normalized_bytes=norm,sqlite_growth_bytes=size_with_wal(fixture.c.db_path)-db_before,
                index_growth_bytes=size_with_wal(fixture.c.document_index)-index_before,elapsed_seconds=round(elapsed,3),
                peak_process_working_set_bytes=peak_memory(),peak_scope='whole smoke process including fixture generation')
            records.append(record)
            print(json.dumps(record),flush=True)
        result=dict(status='PASS',records=records,config=str(fixture.c.path),source_root=str(root),production_touched=False)
        out=Path(__file__).resolve().parents[1]/'runs/large-document/smoke.json'
        atomic_write(out,json.dumps(result,ensure_ascii=False,indent=2))
        print(str(out),flush=True)
    finally:
        fixture.doCleanups()


if __name__=='__main__':
    main()
