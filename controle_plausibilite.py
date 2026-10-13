#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contrôle de plausibilité des relevés horaires de température (paquets DPPaquetObs V2 de Météo-France).

Un capteur défaillant publie parfois des valeurs absurdes : 0,0 °C en plein après-midi, puis -8,4 °C pour 22 °C la même heure
(La Chiappa, 12-13 octobre 2026). Sans contrôle, elles deviennent des records, la « minimale » en tête d'un classement ou un point
isolé sur une carte. Ce module est partagé par les scripts qui lisent ces paquets : records de température, classements,
températures en direct et températures de la veille.

Seuils réglés sur 24 h de paquets (47 700 lignes) : hors capteur défaillant, l'écart tx - tn d'une même heure ne dépasse pas 9,2 °C,
le saut de la température instantanée d'une heure à la suivante 9,1 °C, et une extrême reste à moins de 3,4 °C des températures
instantanées qui l'encadrent. Les seuils laissent une marge large ; ils n'ont pas été éprouvés par temps d'orage ou de foehn, où des
écarts réels plus grands existent : une extrême plausible mais isolée peut alors être écartée (la température instantanée la remplace).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Sequence, Tuple

QC_VERSION = 1  # à incrémenter quand les règles changent : les états qui mémorisent des extrêmes doivent alors être reconstruits
QC_BOUNDS_C = (-60.0, 60.0)
QC_TOLERANCE_C = 0.5  # la température instantanée peut sortir de [tn, tx] de 0,5 °C au plus (arrondis, mesures décalées dans l'heure)
QC_RANGE_MAX_C = 15.0  # écart maximal tx - tn au sein d'une même heure
QC_STEP_MAX_C = 15.0  # saut maximal de la température instantanée d'une heure à la suivante
QC_SUPPORT_C = 6.0  # une extrême reste à moins de 6 °C de l'une des températures instantanées (heure précédente, heure) qui l'encadrent
QC_MAX_LOGGED = 30  # relevés écartés détaillés dans le journal d'un passage
QC_MAX_LISTED = 100  # stations écartées listées dans le fichier publié


def check_hour(t: Optional[float], tx: Optional[float], tn: Optional[float],
               prev_t: Optional[float] = None) -> Tuple[Optional[float], Optional[float], Optional[float], List[str]]:
    """Contrôle de plausibilité d'une heure : (t, tx, tn) retenus et motifs de rejet (liste vide si tout est plausible).

    Une ligne incohérente est rejetée en entier (bornes, tx < tn, étendue, t hors de [tn, tx], saut de t depuis `prev_t`, la
    température retenue à l'heure précédente). Avec `prev_t`, une extrême qu'aucune des deux températures instantanées qui l'encadrent
    n'étaye (à plus de QC_SUPPORT_C de t et de `prev_t`) est écartée seule : la température instantanée la remplace alors, comme quand
    l'extrême est absente. Sans `prev_t`, seul le contrôle interne de la ligne s'applique : une seule température ne permet pas de juger
    un réchauffement ou un refroidissement rapide (tn de 3,7 °C pour t de 11,0 °C à la fin d'une nuit d'inversion, par exemple)."""
    low, high = QC_BOUNDS_C
    reasons: List[str] = []
    bounded: List[Optional[float]] = []
    for name, v in (("t", t), ("tx", tx), ("tn", tn)):
        if v is not None and not low <= v <= high:
            reasons.append(f"{name} hors bornes ({v:g} °C)")
            v = None
        bounded.append(v)
    t, tx, tn = bounded
    if tx is not None and tn is not None:
        if tx < tn - QC_TOLERANCE_C:
            return None, None, None, reasons + [f"tx {tx:g} °C inférieure à tn {tn:g} °C"]
        if tx - tn > QC_RANGE_MAX_C:
            return None, None, None, reasons + [f"étendue tx - tn de {tx - tn:.1f} °C dans l'heure"]
    if t is not None and ((tn is not None and t < tn - QC_TOLERANCE_C) or (tx is not None and t > tx + QC_TOLERANCE_C)):
        return None, None, None, reasons + [f"t {t:g} °C hors de [tn, tx]"]
    if t is not None and prev_t is not None and abs(t - prev_t) > QC_STEP_MAX_C:
        return None, None, None, reasons + [f"saut de t de {t - prev_t:+.1f} °C en une heure"]
    if t is not None and prev_t is not None:
        if tn is not None and tn < min(t, prev_t) - QC_SUPPORT_C:
            reasons.append(f"tn {tn:g} °C sans appui (t voisines : {min(t, prev_t):g} °C)")
            tn = None
        if tx is not None and tx > max(t, prev_t) + QC_SUPPORT_C:
            reasons.append(f"tx {tx:g} °C sans appui (t voisines : {max(t, prev_t):g} °C)")
            tx = None
    return t, tx, tn, reasons


def check_series(serie: Sequence[Tuple[datetime, Optional[float], Optional[float], Optional[float]]]
                 ) -> List[Tuple[Optional[float], Optional[float], Optional[float], List[str]]]:
    """Contrôle la chronologie d'une station : [(heure, t, tx, tn)] triée par heure -> pour chaque ligne (t, tx, tn, motifs) retenus.

    La température de référence d'une heure est celle de l'heure précédente exacte (une heure avant) quand elle a été retenue ;
    une heure manquante ou une ligne rejetée interrompt la chaîne (la ligne suivante n'est alors contrôlée que sur elle-même)."""
    out: List[Tuple[Optional[float], Optional[float], Optional[float], List[str]]] = []
    prev_dt: Optional[datetime] = None
    prev_t: Optional[float] = None
    for dt, t, tx, tn in serie:
        ref = prev_t if prev_dt is not None and dt - prev_dt == timedelta(hours=1) else None
        t2, tx2, tn2, reasons = check_hour(t, tx, tn, ref)
        out.append((t2, tx2, tn2, reasons))
        prev_dt, prev_t = dt, t2
    return out


def controler_station(sid: str, serie: Sequence[Tuple[datetime, Optional[float], Optional[float], Optional[float]]],
                      ecartes: List[dict]) -> List[Tuple[Optional[float], Optional[float], Optional[float]]]:
    """`check_series` pour la station `sid` : renvoie [(t, tx, tn)] retenus et complète `ecartes` ({id, hour, reasons, raw}) pour
    chaque relevé écarté ou corrigé."""
    retenus = []
    for (dt, t0, tx0, tn0), (t, tx, tn, motifs) in zip(serie, check_series(serie)):
        if motifs:
            heure = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            ecartes.append({"id": sid, "hour": heure, "reasons": motifs, "raw": [t0, tx0, tn0]})
        retenus.append((t, tx, tn))
    return retenus


def quality_summary(ecartes: Sequence[dict]) -> dict:
    """Bilan publié dans les fichiers de sortie (clé quality_control) : version des règles, relevés et stations écartés ou corrigés."""
    return {"version": QC_VERSION, "rejected_rows": len(ecartes), "rejected_stations": sorted({e["id"] for e in ecartes})[:QC_MAX_LISTED]}


def log_rejections(ecartes: Sequence[dict], noms: Mapping[str, Any]) -> None:
    """Journal d'un passage : un relevé écarté par ligne (les QC_MAX_LOGGED premiers) et le total. `noms` : {id: nom} ou {id: {"name": nom}}."""
    for e in ecartes[:QC_MAX_LOGGED]:
        t, tx, tn = e["raw"]
        nom = noms.get(e["id"]) or ""
        nom = nom.get("name") if isinstance(nom, dict) else nom
        print(f"[QC] {e['id']} {nom or ''} {e['hour']} : {' ; '.join(e['reasons'])} (t={t} tx={tx} tn={tn})")
    if len(ecartes) > QC_MAX_LOGGED:
        print(f"[QC] … et {len(ecartes) - QC_MAX_LOGGED} autres relevés écartés")
    print("Contrôle de plausibilité :", len(ecartes), "relevés écartés ou corrigés sur", len({e["id"] for e in ecartes}), "stations")
