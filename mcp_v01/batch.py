"""Offline batch prototype; no batch or write MCP tools."""
import argparse,json,sys
from pathlib import Path
from jty_materials.common import Config
from jty_materials import batch

def main():
 p=argparse.ArgumentParser();p.add_argument('--config',default=str(Path(__file__).resolve().parents[1]/'config/materials.json'))
 sub=p.add_subparsers(dest='action',required=True)
 x=sub.add_parser('inventory');x.add_argument('--source-root',required=True);x.add_argument('--output',required=True);x.add_argument('--limit',type=int,default=50)
 x=sub.add_parser('create');x.add_argument('--inventory',required=True);x.add_argument('--plan',required=True)
 for name in ('execute','validate','status','close'):
  x=sub.add_parser(name);x.add_argument('--batch-id',required=True)
  if name=='close':x.add_argument('--accept-partial',action='store_true')
 a=p.parse_args();c=Config(a.config)
 if a.action=='inventory':
  if Path(a.output).resolve().is_relative_to(Path(a.source_root).resolve()):raise ValueError('inventory output must be outside source directory')
  result=batch.inventory(c,a.source_root,a.limit);batch.write(a.output,result,immutable=True);return result
 if a.action=='create':return batch.create(c,a.inventory,a.plan)
 return {'execute':batch.execute,'validate':batch.validate_items,'status':batch.status,'close':batch.close}[a.action](c,a.batch_id,**({'accept_partial':a.accept_partial} if a.action=='close' else {}))

if __name__=='__main__':
 sys.stdout.reconfigure(encoding='utf-8')
 try:print(json.dumps(main(),ensure_ascii=False,indent=2))
 except Exception as e:print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);raise SystemExit(1)
