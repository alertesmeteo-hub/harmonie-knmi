#!/usr/bin/env python3
"""Génère les fichiers JSON utilisés par la carte Crues Vigicrues.

Sources :
- Vigicrues InfoVigiCru.geojson (tronçons + vigilance)
- Hub'Eau Hydrométrie v2 (référentiel + observations temps réel)
- HydroPortail / Vigicrues : seuils historiques publics de hauteur (cache progressif)
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, datetime as dt, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

UA = "Alertes-Meteo-Crues/1.0.3 (+https://alertes-meteo.com/)"
VIGI_GEO = "https://www.vigicrues.gouv.fr/services/1/InfoVigiCru.geojson"
HUB_STATIONS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/referentiel/stations"
HUB_OBS = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr"
HYDRO_THRESH = "https://www.hydro.eaufrance.fr/stationhydro/{code}/seuils"

S = requests.Session(); S.headers.update({"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})

def get(url, params=None, timeout=45, tries=4):
    err=None
    for n in range(tries):
        try:
            r=S.get(url, params=params, timeout=timeout)
            r.raise_for_status(); return r
        except Exception as e:
            err=e; time.sleep(min(2**n,8))
    raise RuntimeError(f"GET {url}: {err}")

def load_json(path: Path, default):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(path: Path, obj, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":") if compact else None, indent=None if compact else 2), encoding="utf-8")

def fetch_troncons():
    return get(VIGI_GEO).json()

def fetch_stations():
    raw=get(HUB_STATIONS, params={"format":"json","size":10000}).json()
    rows=[]
    for s in raw.get("data",[]):
        if s.get("en_service") is False: continue
        lat=s.get("latitude_station"); lon=s.get("longitude_station")
        if lat is None or lon is None: continue
        rows.append({
            "code":str(s.get("code_station") or ""), "name":s.get("libelle_station") or "",
            "site_name":s.get("libelle_site") or "", "river":s.get("libelle_cours_eau") or "",
            "commune":s.get("libelle_commune") or "", "department_code":str(s.get("code_departement") or ""),
            "department_name":s.get("libelle_departement") or "", "lat":float(lat), "lon":float(lon),
            "opened":s.get("date_ouverture_station") or "", "type":s.get("type_station") or "",
        })
    return rows

def fetch_recent_observations(hours=6):
    start=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=hours)).replace(microsecond=0).isoformat().replace('+00:00','Z')
    params={"grandeur_hydro":"H","date_debut_obs":start,"sort":"desc","size":20000,
            "fields":"code_station,date_obs,resultat_obs"}
    url=HUB_OBS; out={}; pages=0
    while url and pages<12:
        raw=get(url, params=params if pages==0 else None, timeout=60).json(); pages+=1
        for o in raw.get("data",[]):
            code=o.get("code_station"); val=o.get("resultat_obs"); date=o.get("date_obs")
            if not code or val is None: continue
            old=out.get(code)
            if old is None or str(date)>str(old.get("date")):
                out[code]={"value_mm":float(val),"date":date}
        url=raw.get("next"); params=None
    return out

def parse_threshold_page(code: str):
    try: html=get(HYDRO_THRESH.format(code=code), timeout=20, tries=2).text
    except Exception: return None
    soup=BeautifulSoup(html,"html.parser")
    values=[]
    # Chaque h2 d'événement regroupe sa description + ses valeurs jusqu'au h2 suivant.
    for h in soup.find_all(["h2","h3"]):
        title=" ".join(h.stripped_strings)
        if "crue" not in title.lower(): continue
        chunks=[]
        node=h.next_sibling
        while node is not None:
            if getattr(node,"name",None) in ("h2","h3"): break
            if hasattr(node,"get_text"): chunks.append(node.get_text(" ",strip=True))
            else: chunks.append(str(node))
            node=node.next_sibling
        text=" ".join(chunks)
        if "Valeur historique" not in text or "Valeurs de seuil de hauteur" not in text: continue
        # Les valeurs HydroPortail sont en mm. On prend les cellules numériques plausibles.
        for table in h.find_all_next("table", limit=3):
            if table.find_previous(["h2","h3"]) != h: break
            head=table.get_text(" ",strip=True).lower()
            if "valeur en hauteur" not in head: continue
            for tr in table.find_all("tr"):
                tds=[td.get_text(" ",strip=True) for td in tr.find_all("td")]
                if len(tds)>=2:
                    raw=tds[1].replace(" ","").replace(",",".")
                    if re.fullmatch(r"-?\d+(?:\.\d+)?",raw):
                        v=float(raw)
                        if 0 < v < 100000: values.append(v)
    # Fallback texte si le DOM change légèrement.
    if not values:
        text=soup.get_text(" ",strip=True)
        if "Valeur historique" in text:
            for m in re.finditer(r"Valeur en hauteur\s+Tolérance.*?\b(\d{2,5})\b",text,re.I):
                values.append(float(m.group(1)))
    return min(values) if values else None

def update_threshold_cache(stations, cache, bootstrap=False, max_refresh=280):
    codes=[s["code"] for s in stations if s.get("code") and 41<=s["lat"]<=52 and -6<=s["lon"]<=10.5]
    if bootstrap:
        todo=codes
    else:
        day=dt.datetime.now(dt.timezone.utc).timetuple().tm_yday
        missing=[c for c in codes if c not in cache]
        rotating=[c for c in codes if (sum(map(ord,c)) % 14)==(day%14)]
        todo=(missing[:max_refresh//2]+rotating)[:max_refresh]
    if not todo: return cache
    print(f"Seuils historiques: {len(todo)} station(s) à contrôler", flush=True)
    def worker(c): return c, parse_threshold_page(c)
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for i,(c,v) in enumerate(ex.map(worker,todo),1):
            cache[c]={"threshold_mm":v,"checked_at":dt.datetime.now(dt.timezone.utc).isoformat()} if v else {"threshold_mm":None,"checked_at":dt.datetime.now(dt.timezone.utc).isoformat()}
            if i%50==0: print(f"  {i}/{len(todo)}", flush=True)
    return cache

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--out",default="data"); ap.add_argument("--bootstrap-thresholds",action="store_true"); ap.add_argument("--skip-thresholds",action="store_true"); args=ap.parse_args()
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    print("Téléchargement Vigicrues…", flush=True); troncons=fetch_troncons()
    print("Téléchargement Hub'Eau stations…", flush=True); stations=fetch_stations()
    print(f"Stations actives: {len(stations)}", flush=True)
    print("Téléchargement observations récentes…", flush=True); obs=fetch_recent_observations()
    thresholds=load_json(out/"historical_thresholds.json",{})
    if not args.skip_thresholds:
        thresholds=update_threshold_cache(stations,thresholds,args.bootstrap_thresholds)
        write_json(out/"historical_thresholds.json",thresholds)
    station_by={s["code"]:s for s in stations}
    alerts=[]; live={}
    for code,o in obs.items():
        th=(thresholds.get(code) or {}).get("threshold_mm")
        status="normal"; label="Niveau normal"
        if th and th>0:
            ratio=o["value_mm"]/th
            if ratio>=1:
                status="above"; label="Au-dessus du seuil historique"
            elif ratio>=0.90:
                status="near"; label="Proche du seuil historique"
            if status!="normal":
                s=station_by.get(code,{})
                alerts.append({"code":code,"name":s.get("name",code),"river":s.get("river",""),"status":status,"value_mm":o["value_mm"],"threshold_mm":th,"ratio":round(ratio,3),"date":o["date"]})
        live[code]={"value_mm":o["value_mm"],"date":o["date"],"threshold_mm":th,"status":status,"status_label":label}
    alerts.sort(key=lambda a:(0 if a["status"]=="above" else 1,-a["ratio"],a["name"]))
    write_json(out/"troncons.geojson",troncons,compact=True)
    write_json(out/"stations.json",{"generated_at":now,"count":len(stations),"stations":stations},compact=True)
    write_json(out/"live.json",{"generated_at":now,"observations":live,"alerts":alerts[:120]},compact=True)
    write_json(out/"meta.json",{"generated_at":now,"troncon_count":len(troncons.get("features",[])),"station_count":len(stations),"observation_count":len(obs),"alert_count":len(alerts),"source_mode":"github"})
    print(f"OK: {len(troncons.get('features',[]))} tronçons, {len(stations)} stations, {len(obs)} obs récentes, {len(alerts)} alertes", flush=True)

if __name__=="__main__":
    try: main()
    except Exception as e:
        print(f"ERREUR: {e}",file=sys.stderr); raise
