"""Offline entry point. No write operations are exposed as MCP tools."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jty_materials.common import Config, atomic_write
from jty_materials.manager import request
from jty_materials.fields import ALLOWED


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(project / "config/materials.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--repository", default=str(project / "repository"))
    init.add_argument("--runtime-target", default=str(project / "local/runtime"))
    init.add_argument("--umi", default=str(project / "runtime/umi-ocr/Umi-OCR_Rapid_v2.1.5/Umi-OCR.exe"))
    init.add_argument("--dowse", default=str(project / "runtime/dowse/dowse.exe"))
    init.add_argument("--fixture-only", action="store_true")
    watch = commands.add_parser("watch"); watch.add_argument("action", choices=["start", "stop", "status"])
    commands.add_parser("index"); commands.add_parser("health"); commands.add_parser("shutdown"); commands.add_parser("backup");commands.add_parser('migrate-v02');commands.add_parser('migrate-v03');commands.add_parser('migrate-v04')
    add = commands.add_parser("add"); add.add_argument("--relative-root", required=True)
    for name in ("import", "replace"):
        p = commands.add_parser(name); p.add_argument("--source", required=True); p.add_argument("--actor", required=True)
        p.add_argument('--subtype',choices=sorted(ALLOWED))
        if name == "import":
            p.add_argument("--kind", choices=["certificate", "contract",'material'], required=True); p.add_argument("--title", required=True)
        else:
            p.add_argument("--material-id", type=int, required=True)
    review = commands.add_parser("review"); review.add_argument("--record", required=True)
    candidate = commands.add_parser('candidates');candidate.add_argument('--record',required=True)
    reextract = commands.add_parser('reextract');reextract.add_argument('--record',required=True)
    restore = commands.add_parser("restore"); restore.add_argument("--backup", required=True); restore.add_argument("--target", required=True); restore.add_argument("--runtime-root")
    inspect = commands.add_parser("get"); inspect.add_argument("--material-id", type=int, required=True); inspect.add_argument("--text", action="store_true")
    search = commands.add_parser("search")
    from jty_materials.mcp import SCHEMAS
    for field,spec in SCHEMAS['search_materials'].items():
        options={'default':None}
        if spec['type']=='integer':options['type']=int
        elif spec['type']=='boolean':
            if field=='eligible_only':options['action']='store_true'
            else:options['choices']=['true','false']
        if 'enum' in spec:options['choices']=spec['enum']
        search.add_argument('--'+field.replace('_','-'),**options)
    commands.add_parser("doctor"); commands.add_parser("client-config")
    args = parser.parse_args()
    if args.command == "init":
        from jty_materials.setup import initialize
        return initialize(args.config, umi_exe=args.umi, dowse_exe=args.dowse,
                          runtime_target=args.runtime_target, repository=args.repository, fixture_only=args.fixture_only)
    if args.command == "restore":
        from jty_materials.backup import restore_backup
        result = restore_backup(args.backup, args.target, args.runtime_root)
        request(Config(result["config"]), "index")
        result["index_rebuild_required"] = False
        return result
    config = Config(args.config)
    if args.command == "watch":
        return request(config, "status" if args.action == "status" else "watch_"+args.action, start=args.action != "status")
    if args.command in ("index", "health", "backup", "shutdown",'migrate-v02','migrate-v03','migrate-v04'):
        if args.command=='migrate-v02':raise ValueError('v0.3 code requires explicit migrate-v03; use the v0.2 snapshot for an exact v0.2 migration')
        return request(config, args.command, start=args.command != "shutdown")
    if args.command == "add":
        return request(config, "add", {"relative_root": args.relative_root})
    if args.command in ("import", "replace"):
        if args.command=='import':
            from jty_materials.fields import resolve_type
            resolve_type(args.kind,args.subtype)  # reject bad combinations before contacting the manager
        payload = {"source": str(Path(args.source).resolve()), "actor": args.actor}
        if args.subtype is not None:payload['subtype']=args.subtype
        if args.command == "import":
            payload.update(kind=args.kind, title=args.title)
        else:
            payload["material_id"] = args.material_id
        return request(config, args.command, payload)
    if args.command == "review":
        return request(config, "review", json.loads(Path(args.record).read_text(encoding="utf-8")))
    if args.command=='candidates':
        return request(config,'candidates',json.loads(Path(args.record).read_text(encoding='utf-8')))
    if args.command=='reextract':
        return request(config,'reextract',json.loads(Path(args.record).read_text(encoding='utf-8')))
    if args.command == "client-config":
        server = {"command": sys.executable, "args": ["-B", str(Path(__file__).with_name("serve.py")), "--config", str(config.path)]}
        text = "[mcp_servers.jty_materials]\ncommand = " + json.dumps(server["command"]) + "\nargs = " + json.dumps(server["args"]) + "\nenabled_tools = [\"search_materials\", \"get_material\", \"get_material_text\"]\nstartup_timeout_sec = 30\ntool_timeout_sec = 120\n"
        target = config.base / "codex-mcp.toml"
        atomic_write(target, text)
        return {"codex_config_snippet": str(target), "server": server}
    from jty_materials.query import Query
    query = Query(config)
    if args.command == "get":
        return (query.get_material_text if args.text else query.get_material)(args.material_id)
    if args.command == "search":
        values={field:getattr(args,field) for field in SCHEMAS['search_materials'] if getattr(args,field) is not None}
        if 'long_term_valid' in values:values['long_term_valid']=values['long_term_valid']=='true'
        return query.search_materials(**values)
    if args.command == "doctor":
        from jty_materials.store import Store
        from jty_materials.common import safe_path, digest
        from jty_materials.rpc import Rpc
        import os
        with Store(config, readonly=True) as store:
            integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = [list(r) for r in store.db.execute("PRAGMA foreign_key_check")]
            missing = [r["relative_path"] for r in store.db.execute("SELECT relative_path,sha256 FROM material_file")
                       if not safe_path(config.repository_root, r["relative_path"]).is_file() or digest(safe_path(config.repository_root, r["relative_path"])) != r["sha256"]]
            projections = [r["relative_path"] for r in store.db.execute("SELECT relative_path,sha256 FROM search_projection")
                           if not safe_path(config.repository_root,r["relative_path"]).is_file() or digest(safe_path(config.repository_root,r["relative_path"])) != r["sha256"]]
        with Rpc([str(config.dowse_exe), "mcp"], env=dict(os.environ, DOWSE_INDEX_DIR=str(config.index_dir))) as rpc:
            status = rpc.tool("index_status", {})
        return {"integrity": integrity, "foreign_key_errors": foreign_keys,
                "file_errors": missing, "projection_errors": projections, "index": status,
                "manager": request(config, "status", start=False), "maintenance": config.maintenance_file.exists()}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
