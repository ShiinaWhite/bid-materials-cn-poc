"""Local native-text extraction plus existing Umi for image-only pages.

Originals are never converted. Rendering/EXIF changes are memory-only inputs.
Raw per-page engine responses are retained; unreadable inputs are explicit.
"""
import base64,io,json,uuid,zipfile
from pathlib import Path
from xml.etree import ElementTree
from PIL import Image,ImageOps
import pymupdf
from .ocr import Ocr
from .common import atomic_write,digest,normalize

class PhaseOcr:
    def __init__(self,config):self.c=config;self.engine=None
    def start(self):pass
    def stop(self):pass  # borrowed per-file session; owner closes after the category
    def close(self):
        if self.engine is not None:
            try:self.engine.stop()
            finally:self.engine=None
    def recognize(self,data):
        with Image.open(io.BytesIO(data)) as image:
            orientation=image.getexif().get(274)
            if orientation in range(2,9):
                image=ImageOps.exif_transpose(image);buf=io.BytesIO();image.save(buf,format='PNG');data=buf.getvalue()
        if self.engine is None:
            engine=Ocr(self.c)
            try:engine.start()
            except Exception:
                engine.stop()
                raise
            self.engine=engine  # A failed startup must not poison all later files.
        try:
            response=self.engine.request('/api/ocr',dict(base64=base64.b64encode(data).decode('ascii'),options={
                'ocr.language':'简体中文','ocr.maxSideLen':1600,'tbpu.parser':'multi_para','data.format':'dict'}),timeout=120)
        except OSError:
            engine,self.engine=self.engine,None
            engine.stop()
            raise
        if not isinstance(response,dict) or response.get('code') not in (100,101):raise RuntimeError('Umi task failed: '+repr(response)[:600])
        blocks=response.get('data',[]) if isinstance(response.get('data'),list) else []
        if any(not isinstance(b,dict) or not isinstance(b.get('text'),str) for b in blocks):raise RuntimeError('malformed OCR blocks')
        return '\n'.join(b['text'] for b in blocks),dict(method='Umi-OCR',exif_orientation=orientation,input_sha256=__import__('hashlib').sha256(data).hexdigest(),response=response)
    def document(self,source):
        source=Path(source);before=digest(source);texts=[];records=[];errors=[];ext=source.suffix.lower()
        try:
            if ext=='.pdf':
                with pymupdf.open(source) as doc:
                    if doc.is_encrypted:raise ValueError('encrypted PDF requires a separately authorized password')
                    for i,page in enumerate(doc):
                        text=page.get_text('text')
                        if len(text.strip())>=80 and (not page.get_images() or len(text.strip())>=800):
                            record=dict(method='native PDF text',text=text)
                        else:
                            zoom=min(2,2000/max(page.rect.width,page.rect.height))
                            text,record=self.recognize(page.get_pixmap(matrix=pymupdf.Matrix(zoom,zoom),alpha=False).tobytes('png'))
                        texts.append(text);records.append(dict(page=i+1,**record))
            elif ext in ('.jpg','.jpeg','.png'):
                text,record=self.recognize(source.read_bytes());texts.append(text);records.append(dict(page=1,**record))
            elif ext=='.docx':
                with zipfile.ZipFile(source) as archive:
                    parts=[n for n in archive.namelist() if n.startswith('word/') and n.endswith('.xml') and ('document' in n or 'header' in n or 'footer' in n)]
                    text='\n'.join(''.join(node.itertext()) for name in parts for node in ElementTree.fromstring(archive.read(name)).iter() if node.tag.endswith('}t'))
                    if text.strip():texts.append(text);records.append(dict(method='DOCX native XML',text=text))
                    for name in archive.namelist():
                        if name.startswith('word/media/') and name.lower().endswith(('.png','.jpg','.jpeg')):
                            data=archive.read(name)
                            with Image.open(io.BytesIO(data)) as im:
                                if im.width<100 or im.height<100:continue
                            text,record=self.recognize(data);texts.append(text);records.append(dict(part=name,**record))
            elif ext=='.xlsx':
                import openpyxl
                book=openpyxl.load_workbook(source,read_only=True,data_only=True)
                try:
                    for sheet in book:
                        text='\n'.join('\t'.join('' if v is None else str(v) for v in row) for row in sheet.iter_rows(values_only=True))
                        texts.append(text);records.append(dict(method='XLSX cached values (no formula execution)',sheet=sheet.title,text=text))
                finally:book.close()
            elif ext=='.md':
                text=source.read_text(encoding='utf-8-sig');texts.append(text);records.append(dict(method='source text',text=text))
            else:raise ValueError('unsupported container; complete original retained for later extraction')
        except Exception as error:
            errors.append(type(error).__name__+': '+str(error)[:600])
        text='\n\n'.join(t for t in texts if t.strip());task=uuid.uuid4().hex
        raw=source.parent/'.ocr-raw'/task/(source.name+'.ocr.txt');jl=raw.with_suffix('.jsonl');search=source.with_name(source.name+'.search.txt')
        state='complete' if text.strip() and not errors else 'partial' if text.strip() else 'metadata_only'
        atomic_write(raw,text,immutable=True)
        atomic_write(jl,'\n'.join(json.dumps(r,ensure_ascii=False) for r in records+[dict(text_status=state,errors=errors)])+'\n',immutable=True)
        search_text=normalize(text) if text.strip() else '[METADATA ONLY: content not extracted]\n'+getattr(self,'source_label',source.name)
        atomic_write(search,normalize(search_text))
        if digest(source)!=before:raise ValueError('original changed during extraction')
        result=dict(original=str(source),raw=str(raw),jsonl=str(jl),search=str(search),task=task,state='success' if state=='complete' else state,
           pages=len(records),processed=len(records),raw_sha256=digest(raw),options=dict(text_status=state,errors=errors,
             native_or_existing_umi=True,original_sha256=before,ocr_derivative_only=True))
        self.c.log('phase.extraction',**result);return result
