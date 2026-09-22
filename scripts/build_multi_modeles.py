#!/usr/bin/env python3
"""Construit une frise de cumul moyen Occitanie-PACA depuis AROME, GFS et GEFS."""
from __future__ import annotations
import argparse, json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import requests

RAW="https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/observations/"
AROME="https://raw.githubusercontent.com/alertesmeteo-hub/arome-meteofrance/data/"
DEPS=("04","05","06","09","11","12","13","30","31","32","34","46","48","65","66","81","82","83","84")
S=requests.Session(); S.headers["User-Agent"]="alertes-meteo-multi-modeles/1.0"
def get(url):
 r=S.get(url,timeout=90);r.raise_for_status();return r.json()
def mean(values):
 vals=[float(v) for v in values if isinstance(v,(int,float))]
 return round(sum(vals)/len(vals),2) if vals else None
def arome():
 idx=get(AROME+"index.json")
 with ThreadPoolExecutor(max_workers=5) as pool: files=list(pool.map(lambda d:get(AROME+"departements/"+d+".json"),DEPS))
 grouped={}
 for data in files:
  cols=data.get("columns",{}).get("values",[]); pos=cols.index("precipitation_mm")
  for row in data.get("forecast",[]):
   grouped.setdefault(row[0],[]).extend(v[pos] for v in row[1:] if len(v)>pos)
 total=0; points=[]
 for utc in sorted(grouped):
  inc=mean(grouped[utc]); total+=inc or 0; points.append({"valid_utc":utc,"total_mm":round(total,1)})
 return {"id":"AROME","label":"AROME 1,3 km","status":"ok","run_utc":idx["model"]["run_time"],"points":points}
def gfs():
 idx=get(RAW+"gfs/index.json"); total=0; points=[]
 for frame in idx["frames"]:
  d=get(RAW+"gfs/"+frame["file"]); grid=d["grid"]; rain=d.get("fields",{}).get("precipitation_mm")
  if not rain: continue
  vals=[]
  for i,lat in enumerate(grid["latitudes"]):
   if not 42<=lat<=45.5: continue
   for j,lon in enumerate(grid["longitudes"]):
    if -1.5<=lon<=8 and rain[i][j] is not None: vals.append(rain[i][j])
  total+=mean(vals) or 0; points.append({"valid_utc":frame["valid_utc"],"total_mm":round(total,1)})
 return {"id":"GFS","label":"GFS 0,25°","status":"ok","run_utc":idx["run_utc"],"points":points}
def gefs():
 manifest=get(RAW+"gefs-occitanie/index.json"); run=manifest["runs"][0]; idx=get(RAW+"gefs-occitanie/"+run["index"]); points=[]
 for item in idx["frames"]:
  d=get(RAW+"gefs-occitanie/runs/"+run["run_id"]+"/"+item["file"]); x=d["regional_summary_cumulative"]
  points.append({"valid_utc":item["valid_utc"],"total_mm":x["median_mm"],"p10_mm":x["p10_mm"],"p90_mm":x["p90_mm"]})
 return {"id":"GEFS","label":"GEFS médiane (31 membres)","status":"ok","run_utc":run["run_utc"],"band":True,"points":points}
def main():
 p=argparse.ArgumentParser();p.add_argument("--output",default="build/multi-modeles/index.json");args=p.parse_args(); series=[]; unavailable=[]
 for name,fn in (("AROME",arome),("GFS",gfs),("GEFS",gefs)):
  try: series.append(fn())
  except Exception as exc: unavailable.append(name+" ("+str(exc)+")")
 payload={"status":"ok","schema_version":1,"module_version":"1.0.0","area":{"codes":["76","93"],"name":"Occitanie + Provence-Alpes-Côte d’Azur"},"generated_at":datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"),"series":series,"unavailable":unavailable+["HARMONIE (pas de flux régional méditerranéen actualisé)"]}
 out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
 if not series: raise SystemExit("Aucune série disponible")
if __name__=="__main__": main()
