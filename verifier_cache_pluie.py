#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2.6.0"


def parse_iso(s):
    try:
        d=datetime.fromisoformat(str(s).replace('Z','+00:00'))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: return None


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--file', default='cache_pluie_horaire.json')
    ap.add_argument('--min-depth', type=int, default=48)
    ap.add_argument('--require-48', action='store_true')
    ap.add_argument('--require-72', action='store_true')
    args=ap.parse_args()
    p=Path(args.file)
    if not p.exists():
        raise SystemExit(f'CACHE ABSENT : {p}')
    d=json.loads(p.read_text(encoding='utf-8'))
    hours=d.get('hours') or {}
    keys=[]
    for k in hours:
        dt=parse_iso(k)
        if dt: keys.append((dt,k))
    keys.sort()
    if not keys: raise SystemExit('CACHE VIDE')
    latest=keys[-1][0]
    valid=[(dt,k) for dt,k in keys if 0 <= (latest-dt).total_seconds() <= 79*3600]
    depth=int((latest-valid[0][0]).total_seconds()//3600)+1 if valid else 0
    counts48={}; counts72={}
    for dt,k in valid:
        age=int((latest-dt).total_seconds()//3600)
        for sid in (hours.get(k) or {}):
            if age < 48: counts48[sid]=counts48.get(sid,0)+1
            if age < 72: counts72[sid]=counts72.get(sid,0)+1
    c44=sum(v>=44 for v in counts48.values())
    c66=sum(v>=66 for v in counts72.values())
    print('Version cache :', d.get('module_version'))
    print('Début :', valid[0][1] if valid else '—')
    print('Fin   :', valid[-1][1] if valid else '—')
    print('Profondeur :', depth, 'h')
    print('Stations >=44/48 h :', c44)
    print('Stations >=66/72 h :', c66)
    if depth < args.min_depth:
        raise SystemExit(f'CACHE INSUFFISANT : {depth} h < {args.min_depth} h')
    if args.require_48 and c44 == 0:
        raise SystemExit('AUCUNE STATION 48H EXPLOITABLE')
    if args.require_72 and c66 == 0:
        raise SystemExit('AUCUNE STATION 72H EXPLOITABLE')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
