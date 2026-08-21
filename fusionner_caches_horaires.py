#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json
from datetime import timedelta
from pathlib import Path
from generer_pluie_meteofrance import iso, parse_iso, utcnow

VERSION="2.3.4"

def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"hours": {}}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--parts', default='parts')
    ap.add_argument('--existing', default='cache_pluie_horaire.json')
    ap.add_argument('--output', default='cache_pluie_horaire.json')
    args=ap.parse_args()
    merged={}
    existing=Path(args.existing)
    if existing.exists():
        for h,b in (load(existing).get('hours') or {}).items():
            merged.setdefault(h,{}).update(b or {})
    files=sorted(Path(args.parts).rglob('*.json'))
    print('Parties trouvées :', len(files))
    for p in files:
        d=load(p)
        for h,b in (d.get('hours') or {}).items():
            merged.setdefault(h,{}).update(b or {})
    dts=[parse_iso(k) for k in merged]
    dts=[x for x in dts if x is not None]
    latest=max(dts) if dts else utcnow().replace(minute=0,second=0,microsecond=0)
    cutoff=latest-timedelta(hours=79)
    keep={k:v for k,v in merged.items() if (parse_iso(k) is not None and cutoff <= parse_iso(k) <= latest)}
    out={
        'schema_version':1,
        'module_version':VERSION,
        'generated_at':iso(utcnow()),
        'latest_hour':iso(latest),
        'hours':dict(sorted(keep.items())),
    }
    Path(args.output).write_text(json.dumps(out,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print('Cache fusionné :', len(keep), 'heures |', sum(len(v) for v in keep.values()), 'valeurs')
    if len(keep) < 66:
        print('[WARN] profondeur globale < 66 h ; certaines stations 72 h peuvent rester incomplètes')
    return 0 if keep else 2
if __name__=='__main__':
    raise SystemExit(main())
