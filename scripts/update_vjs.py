#!/usr/bin/env python3
"""Generate data/vigilance-jours-suivants.json for the WordPress SVG module.

Strategy:
1) Try the official Météo-France Vigilance website and open the "Prochains jours" view.
2) Detect the 5 interactive cards (J+2, J+3, J+4, J+5, J+6/J+7).
3) Hover the department shapes and read the visible tooltip. This yields the exact
   department, phenomenon and probability without copying map images.
4) If the official DOM is not exploitable, an optional reference-page fallback can
   be used. It is disabled only if VJS_ALLOW_REFERENCE_FALLBACK=0.

The output contains data only. The WordPress plugin draws its own local SVG map.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
DEPARTMENTS_FILE = ROOT / "data" / "departments.json"
OUTPUT_FILE = ROOT / "data" / "vigilance-jours-suivants.json"

OFFICIAL_URL = os.getenv("VJS_MF_URL", "https://vigilance.meteofrance.fr/fr")
REFERENCE_URL = os.getenv("VJS_REFERENCE_URL", "https://meteo-npdc.fr/vigilance-jours-suivants")
ALLOW_REFERENCE = os.getenv("VJS_ALLOW_REFERENCE_FALLBACK", "1") not in {"0", "false", "False", "no"}
RETRIES = max(1, min(4, int(os.getenv("VJS_RETRIES", "2"))))

CARD_LABELS = ["J+2", "J+3", "J+4", "J+5", "J+6 et J+7"]
PHENOMENA = {
    "vent": ["vent", "vent violent"],
    "pluie_inondation": ["pluie-inondation", "pluie inondation", "pluie_inondation", "pluie/inondation"],
    "orages": ["orages", "orage"],
    "neige_verglas": ["neige-verglas", "neige verglas", "neige"],
    "canicule": ["canicule"],
    "grand_froid": ["grand froid"],
    "vagues_submersion": ["vagues-submersion", "vagues submersion", "vague-submersion"],
}
LEVEL_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
LEVEL_LABEL = {
    "none": "Quasi nulle",
    "low": "Faible (≤ 30%)",
    "medium": "Moyenne (> 30 ≤ 70%)",
    "high": "Élevée (> 70%)",
}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def clean_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm(s: str) -> str:
    return strip_accents(clean_space(s)).lower().replace("_", "-")


def parse_level(text: str) -> str:
    t = norm(text)
    if "elevee" in t or "> 70" in t or ">70" in t:
        return "high"
    if "moyenne" in t or ("> 30" in t and "70" in t):
        return "medium"
    if "faible" in t or "<= 30" in t or "≤ 30" in text or "30%" in t:
        return "low"
    return "none"


def parse_phenomena(text: str, default_level: str | None = None) -> list[dict[str, str]]:
    """Pair each phenomenon with the probability text nearest after its label."""
    t = norm(text)
    hits: list[tuple[int, str, int]] = []
    for slug, aliases in PHENOMENA.items():
        best: tuple[int, int] | None = None
        for alias in aliases:
            a = norm(alias)
            idx = t.find(a)
            if idx >= 0 and (best is None or idx < best[0]):
                best = (idx, len(a))
        if best is not None:
            hits.append((best[0], slug, best[1]))
    hits.sort(key=lambda x: x[0])

    found: list[dict[str, str]] = []
    for i, (idx, slug, alias_len) in enumerate(hits):
        next_idx = hits[i + 1][0] if i + 1 < len(hits) else min(len(t), idx + alias_len + 130)
        segment = t[idx:next_idx]
        level = parse_level(segment)
        if level == "none" and default_level:
            level = default_level
        found.append({"phenomenon": slug, "level": level})
    return found


def parse_tooltip(text: str, known_names: dict[str, str]) -> tuple[str, dict[str, Any]] | None:
    raw_text = text or ""
    text = clean_space(raw_text)
    m = re.search(r"(.{1,90}?)\s*\((2A|2B|\d{2})\)", text, re.I)
    if not m:
        return None
    code = m.group(2).upper()
    if code not in known_names:
        return None
    name = clean_space(m.group(1)).split(" · ")[-1].strip(" -–—:") or known_names[code]
    # Prefer official/canonical department name to avoid tooltip prefixes.
    if len(name) > 45 or any(x in norm(name) for x in ("probabilite", "vigilance", "jours suivants")):
        name = known_names[code]
    level = parse_level(text)
    phenomena = parse_phenomena(raw_text, level if level != "none" else None)
    if phenomena:
        max_level = max((p["level"] for p in phenomena), key=lambda x: LEVEL_RANK.get(x, 0))
    else:
        max_level = level
    return code, {"name": known_names.get(code, name), "probability": max_level, "phenomena": phenomena}


def merge_department(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if not old:
        return new
    out = copy.deepcopy(old)
    if LEVEL_RANK.get(new.get("probability", "none"), 0) > LEVEL_RANK.get(out.get("probability", "none"), 0):
        out["probability"] = new["probability"]
    seen = {(p.get("phenomenon"), p.get("level")) for p in out.get("phenomena", [])}
    for p in new.get("phenomena", []):
        key = (p.get("phenomenon"), p.get("level"))
        if key not in seen:
            out.setdefault("phenomena", []).append(p)
            seen.add(key)
    return out


def empty_departments(names: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {c: {"name": n, "probability": "none", "phenomena": []} for c, n in names.items()}


def risk_summary_from_departments(deps: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, str]] = []
    for info in deps.values():
        for p in info.get("phenomena", []):
            level = p.get("level", "none")
            if level == "none":
                continue
            key = (p.get("phenomenon", ""), level)
            if key in seen:
                continue
            seen.add(key)
            items.append({"phenomenon": key[0], "level": key[1]})
    order_ph = {k: i for i, k in enumerate(PHENOMENA.keys())}
    items.sort(key=lambda x: (-LEVEL_RANK.get(x["level"], 0), order_ph.get(x["phenomenon"], 99)))
    return items


def parse_bulletin_and_summaries(body_text: str) -> tuple[dict[str, str], dict[str, str]]:
    txt = clean_space(body_text)
    bulletin = {"issued_at": "", "updated_at": ""}
    m = re.search(r"Bulletin\s+émis\s+le\s+(.+?)(?:\s*[·•]\s*Mise\s+à\s+jour\s+locale\s*:\s*(.+?))?(?:\.|J\+2)", txt, re.I)
    if m:
        bulletin["issued_at"] = clean_space(m.group(1)).rstrip(" .")
        if m.group(2):
            bulletin["updated_at"] = clean_space(m.group(2)).rstrip(" .")
    if not bulletin["issued_at"]:
        m = re.search(r"Diffusion\s*:\s*(.+?)(?:J\+2|$)", txt, re.I)
        if m:
            bulletin["issued_at"] = clean_space(m.group(1)).rstrip(" .")

    summaries = {"j2_j3": "", "j4_j7": ""}
    m = re.search(r"J\+2\s+et\s+J\+3\s+(.+?)\s+J\+2\s+(?!et)", txt, re.I)
    if m:
        summaries["j2_j3"] = clean_space(m.group(1))
    m = re.search(r"De\s+J\+4\s+à\s+J\+7\s+(.+?)\s+J\+4\s+", txt, re.I)
    if m:
        summaries["j4_j7"] = clean_space(m.group(1))
    return bulletin, summaries


async def try_open_next_days(page) -> None:
    # The official site can expose the view as a tab/button depending on the current frontend.
    candidates = [
        page.get_by_role("link", name=re.compile(r"prochains\s+jours", re.I)),
        page.get_by_role("button", name=re.compile(r"prochains\s+jours", re.I)),
        page.get_by_text(re.compile(r"^\s*prochains\s+jours\s*$", re.I)),
    ]
    for loc in candidates:
        try:
            if await loc.count():
                await loc.first.click(timeout=3000, force=True)
                await page.wait_for_timeout(1200)
                if await page.get_by_text(re.compile(r"J\+2")).count():
                    return
        except Exception:
            pass


async def mark_card(page, label: str, token: str) -> bool:
    """Mark the smallest ancestor containing the label and a map-like SVG."""
    return bool(await page.evaluate(
        """({label, token}) => {
          const directText = el => Array.from(el.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ').replace(/\\s+/g,' ').trim();
          const all = Array.from(document.querySelectorAll('body *'));
          const isLabel = txt => {
            txt=(txt||'').replace(/\\s+/g,' ').trim();
            if(label==='J+6 et J+7') return /^J\\+6\\s*(?:et|&|[/])\\s*J\\+7(?:\\s|$)/i.test(txt);
            return txt===label || txt.startsWith(label+' ');
          };
          let best=null, bestMap=null, bestScore=1e12;
          for (const el of all) {
            const own=directText(el), tx=(el.textContent||'').replace(/\\s+/g,' ').trim();
            if (!(isLabel(own) || isLabel(tx))) continue;
            let a=el;
            for(let depth=0; a && depth<9; depth++,a=a.parentElement){
              const svgs=Array.from(a.querySelectorAll('svg'));
              const mapSvg=svgs.find(s=>Array.from(s.querySelectorAll('path')).filter(p=>{try{const b=p.getBBox();return b.width*b.height>0.02}catch(e){return false}}).length>=40);
              if(!mapSvg) continue;
              const textLen=(a.textContent||'').length;
              const score=textLen+depth*50+svgs.length*1000;
              if(score<bestScore){best=a;bestMap=mapSvg;bestScore=score;}
              break;
            }
          }
          if(!best || !bestMap) return false;
          best.setAttribute('data-vjs-scrape-card',token);
          bestMap.setAttribute('data-vjs-scrape-map',token);
          return true;
        }""",
        {"label": label, "token": token},
    ))


async def visible_tooltip_text(page) -> str:
    return await page.evaluate(
        """() => {
          const codeRe=/\\((?:2A|2B|0[1-9]|[1-8][0-9]|9[0-5])\\)/i;
          const riskRe=/(quasi\\s+nulle|faible|moyenne|élevée|elevee|canicule|orage|pluie|vent|neige|froid|vague)/i;
          const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&+s.opacity!==0};
          const els=Array.from(document.querySelectorAll('body *')).filter(e=>visible(e));
          let best='',score=-1;
          for(const e of els){
            const t=(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
            if(t.length<4||t.length>500||!codeRe.test(t)||!riskRe.test(t)) continue;
            let sc=0;
            const c=(e.className&&String(e.className))||'';
            const role=e.getAttribute&&e.getAttribute('role');
            const pos=getComputedStyle(e).position;
            if(/tooltip|popover|info/i.test(c)) sc+=50;
            if(role==='tooltip') sc+=50;
            if(pos==='fixed'||pos==='absolute') sc+=15;
            sc+=Math.max(0,20-Math.floor(t.length/25));
            if(sc>score){score=sc;best=t;}
          }
          return best;
        }"""
    )


async def scrape_card(page, label: str, names: dict[str, str], token: str) -> dict[str, Any]:
    if not await mark_card(page, label, token):
        raise RuntimeError(f"Carte {label} introuvable")
    card_loc = page.locator(f'[data-vjs-scrape-card="{token}"]').first
    card_text = clean_space(await card_loc.inner_text())
    # Date title after label, if available.
    date = ""
    if label != "J+6 et J+7":
        m = re.search(re.escape(label) + r"\s+([A-Za-zÀ-ÿ.\-]+\s+\d{1,2}/\d{1,2}/\d{4})", card_text)
        if m:
            date = clean_space(m.group(1))

    svg = page.locator(f'svg[data-vjs-scrape-map="{token}"]').first
    paths = svg.locator("path")
    npaths = await paths.count()
    if npaths < 40:
        raise RuntimeError(f"Carte {label}: seulement {npaths} chemins SVG")

    deps = empty_departments(names)
    found: dict[str, dict[str, Any]] = {}
    # Hovering all paths is deliberate: it is independent of undocumented path IDs.
    for i in range(min(npaths, 260)):
        p = paths.nth(i)
        try:
            box = await p.bounding_box()
            if not box or box["width"] * box["height"] < 2:
                continue
            await p.hover(timeout=1200, force=True)
            await page.wait_for_timeout(18)
            tt = await visible_tooltip_text(page)
            parsed = parse_tooltip(tt, names) if tt else None
            if parsed:
                code, info = parsed
                found[code] = merge_department(found.get(code), info)
                # Once all department codes have been seen, no need to hover decorative paths.
                if len(found) >= len(names):
                    break
        except Exception:
            continue

    # Some frontends expose the code directly on the path but not all hover tooltips for quasi-null zones.
    # We keep unseen departments at quasi-null; risky departments must be represented by a tooltip.
    for code, info in found.items():
        deps[code] = info

    risky_count = sum(1 for x in deps.values() if x.get("probability") != "none")
    summary = risk_summary_from_departments(deps)
    # If no department tooltip was obtained, the source is not usable.
    if len(found) < 80:
        raise RuntimeError(f"Carte {label}: infobulles départementales insuffisantes ({len(found)})")

    # A card can legitimately have zero risky departments.
    return {
        "label": label,
        "date": date,
        "none": risky_count == 0,
        "departments": deps,
        "risk_summary": summary,
        "_scrape": {"tooltip_departments": len(found), "risky_departments": risky_count},
    }


async def scrape_site(url: str, names: dict[str, str], mode: str) -> dict[str, Any]:
    async with async_playwright() as pw:
        chromium_path = os.getenv("VJS_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
        launch_kwargs = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-sandbox"]}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="fr-FR",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36 Alertes-Meteo-VJS/2.0",
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1800)
        if mode == "official":
            await try_open_next_days(page)
        try:
            await page.get_by_text(re.compile(r"J\+2")).first.wait_for(timeout=12000)
        except PlaywrightTimeoutError:
            raise RuntimeError("La vue J+2 n'est pas visible")

        body_text = await page.locator("body").inner_text()
        bulletin, summaries = parse_bulletin_and_summaries(body_text)

        cards = []
        for i, label in enumerate(CARD_LABELS):
            cards.append(await scrape_card(page, label, names, f"c{i}"))

        await browser.close()

    # remove diagnostics from published cards after validation
    diag = {c["label"]: c.pop("_scrape") for c in cards}
    return {
        "schema_version": 2,
        "status": "ok",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "provider": "Météo-France" if mode == "official" else "Source de secours (données annoncées Météo-France)",
            "product": "Prévision des phénomènes dangereux",
            "retrieval_mode": mode,
            "source_url": url,
        },
        "bulletin": bulletin,
        "summaries": summaries,
        "cards": cards,
        "validation": {"cards": len(cards), "details": diag},
    }


def validate(data: dict[str, Any], names: dict[str, str]) -> None:
    cards = data.get("cards") or []
    if [c.get("label") for c in cards] != CARD_LABELS:
        raise ValueError("Les 5 échéances attendues ne sont pas présentes dans le bon ordre")
    for card in cards:
        deps = card.get("departments") or {}
        missing = [c for c in names if c not in deps]
        if missing:
            raise ValueError(f"{card['label']}: départements manquants: {missing[:8]}")
        for code, info in deps.items():
            if info.get("probability") not in LEVEL_RANK:
                raise ValueError(f"{card['label']} {code}: niveau invalide")
            for p in info.get("phenomena", []):
                if p.get("phenomenon") not in PHENOMENA or p.get("level") not in LEVEL_RANK:
                    raise ValueError(f"{card['label']} {code}: phénomène invalide")


def canonical_hash(data: dict[str, Any]) -> str:
    d = copy.deepcopy(data)
    d.pop("generated_at", None)
    # diagnostics may fluctuate in path count without meteorological change
    d.pop("validation", None)
    raw = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_if_changed(data: dict[str, Any], output: Path) -> bool:
    old = None
    if output.exists():
        try:
            old = json.loads(output.read_text(encoding="utf-8"))
        except Exception:
            old = None
    if old and canonical_hash(old) == canonical_hash(data):
        # preserve the previous generated_at so GitHub does not commit every scheduled check
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(output)
    return True


async def main_async(args) -> int:
    names = json.loads(DEPARTMENTS_FILE.read_text(encoding="utf-8"))
    errors = []
    sources = []
    if args.source:
        sources.append((args.source, args.mode))
    else:
        sources.append((OFFICIAL_URL, "official"))
        if ALLOW_REFERENCE:
            sources.append((REFERENCE_URL, "reference_fallback"))

    data = None
    for url, mode in sources:
        for attempt in range(1, RETRIES + 1):
            try:
                print(f"[VJS] Source {mode} — tentative {attempt}/{RETRIES}: {url}")
                candidate = await scrape_site(url, names, mode)
                validate(candidate, names)
                data = candidate
                break
            except Exception as exc:
                msg = f"{mode} tentative {attempt}/{RETRIES}: {type(exc).__name__}: {exc}"
                print(f"[VJS] Échec: {msg}", file=sys.stderr)
                errors.append(msg)
                if attempt < RETRIES:
                    await asyncio.sleep(4)
        if data is not None:
            break

    if data is None:
        print("[VJS] Aucune source exploitable. Le fichier existant est conservé.", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        return 2

    if errors:
        data["source"]["previous_attempt_errors"] = errors
    changed = write_if_changed(data, Path(args.output))
    print("[VJS] Données modifiées." if changed else "[VJS] Aucun changement météo détecté.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="URL à scraper (test ou source personnalisée)")
    ap.add_argument("--mode", default="custom", help="Nom du mode pour les métadonnées")
    ap.add_argument("--output", default=str(OUTPUT_FILE))
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
