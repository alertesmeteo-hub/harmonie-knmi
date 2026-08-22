#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extraction du bulletin Météo-France « Prévision des phénomènes dangereux »
(vigilance des jours suivants, J+2 à J+7) depuis le PDF officiel.

Le PDF est produit par Météo-France et diffusé sous licence Etalab 2.0.
Ce module ne fait qu'en lire le contenu ; il n'invente aucune donnée.

Structure réelle du bulletin (cf. page 3 du PDF) :
  * J+2 et J+3      -> granularité DÉPARTEMENTALE (carte colorée)
  * J+4 et J+5      -> granularité par QUART DE LA FRANCE
  * J+6 et J+7      -> granularité NATIONALE, aucune indication géographique

Aucune de ces granularités n'est extrapolée vers une autre : ce que
Météo-France ne publie pas n'est pas publié ici.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf
from PIL import Image

SCHEMA_VERSION = 3
PARIS = timezone(timedelta(hours=2))  # recalculé plus bas d'après le libellé du bulletin

LEVEL_ORDER = ["none", "low", "medium", "high"]

# Libellés de probabilité tels qu'ils apparaissent dans la légende du PDF.
LEVEL_PATTERNS = [
    ("none", r"quasi\s*nulle"),
    ("low", r"faible"),
    ("medium", r"moyenne"),
    ("high", r"[ée]lev[ée]e"),
]

PHENOMENON_SLUGS = {
    "vent": "vent",
    "pluieinondation": "pluie_inondation",
    "orages": "orages",
    "neigeverglas": "neige_verglas",
    "canicule": "canicule",
    "grandfroid": "grand_froid",
    "vaguessubmersion": "vagues_submersion",
}

MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12,
}


class ParseError(RuntimeError):
    """Le PDF ne correspond pas à la structure attendue : on ne publie rien."""


# --------------------------------------------------------------------------
# utilitaires
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def _slugify_phenomenon(label: str) -> str | None:
    key = re.sub(r"[^a-z]", "", _norm(label))
    return PHENOMENON_SLUGS.get(key)


def _rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in rgb[:3])


def _hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _rect_center(rect):
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def _contains(outer, x, y, pad=0.0):
    return (outer[0] - pad) <= x <= (outer[2] + pad) and (outer[1] - pad) <= y <= (outer[3] + pad)


# --------------------------------------------------------------------------
# lecture des éléments du PDF
# --------------------------------------------------------------------------

def _spans(page):
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].replace("﻿", "").strip()
                if text:
                    out.append({"text": text, "bbox": tuple(span["bbox"]), "size": span["size"]})
    return out


def _legend_frame(page):
    """Le cadre de légende : encadré non rempli, dans la moitié gauche de la page."""
    frames = [d["rect"] for d in page.get_drawings()
              if not d.get("fill") and d.get("color")
              and (d["rect"].x1 - d["rect"].x0) > 200
              and d["rect"].x0 < page.rect.width * 0.5]
    if not frames:
        return None
    frame = max(frames, key=lambda r: (r.x1 - r.x0) * (r.y1 - r.y0))
    return (frame.x0, frame.y0, frame.x1, frame.y1)


def _images(doc, page):
    """Occurrences d'images de la page, dédoublonnées, avec l'empreinte du contenu."""
    out = []
    seen = set()
    for info in page.get_images(full=True):
        xref = info[0]
        try:
            raw = doc.extract_image(xref)
        except Exception:
            continue
        digest = hashlib.sha1(raw["image"]).hexdigest()[:16]
        for rect in page.get_image_rects(xref):
            # Météo-France superpose plusieurs XObjects identiques au même endroit :
            # on ne garde qu'une occurrence par (contenu, position).
            key = (digest, round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "xref": xref,
                "hash": digest,
                "rect": tuple(rect),
                "w": info[2],
                "h": info[3],
                "smask": raw.get("smask"),
            })
    return out


def _load_image_rgba(doc, xref, smask_xref):
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n == 4:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if smask_xref:
        mask = pymupdf.Pixmap(doc, smask_xref)
        pix = pymupdf.Pixmap(pix)
        pix.set_alpha(mask.samples)
    mode = "RGBA" if pix.alpha else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples).convert("RGBA")


# --------------------------------------------------------------------------
# légendes (auto-descriptives : rien n'est codé en dur)
# --------------------------------------------------------------------------

def _legend_spans(page):
    frame = _legend_frame(page)
    spans = _spans(page)
    if not frame:
        return spans
    return [s for s in spans if _contains(frame, *_rect_center(s["bbox"]))]


def _phenomenon_order(page):
    """Ordre d'affichage des phénomènes tel que Météo-France le présente."""
    entries = []
    for span in _legend_spans(page):
        slug = _slugify_phenomenon(span["text"])
        if slug:
            entries.append((span["bbox"][1], slug))
    ordered = []
    for _, slug in sorted(entries):
        if slug not in ordered:
            ordered.append(slug)
    return ordered


def _read_phenomenon_legend(doc, page):
    """Associe l'empreinte de chaque pictogramme au nom du phénomène affiché à côté."""
    labels = []
    for span in _legend_spans(page):
        slug = _slugify_phenomenon(span["text"])
        if slug:
            labels.append((slug, span["bbox"]))
    if not labels:
        raise ParseError("légende des phénomènes introuvable")

    icons = [im for im in _images(doc, page) if 12 <= im["w"] <= 60 and 10 <= im["h"] <= 40]
    if not icons:
        raise ParseError("aucun pictogramme détecté")
    mapping = _match_icons_to_labels(icons, labels, max_gap=40.0)
    if not mapping:
        raise ParseError("impossible d'associer les pictogrammes aux libellés")
    return mapping


def _match_icons_to_labels(icons, labels, max_gap=40.0, y_tolerance=12.0):
    """Chaque entrée de légende = un pictogramme placé juste à gauche de son libellé."""
    mapping = {}
    for value, bbox in labels:
        cy = (bbox[1] + bbox[3]) / 2.0
        candidates = [
            im for im in icons
            if im["rect"][2] <= bbox[0] + 1.0
            and (bbox[0] - im["rect"][2]) < max_gap
            and abs(_rect_center(im["rect"])[1] - cy) < y_tolerance
        ]
        if candidates:
            best = min(candidates, key=lambda im: bbox[0] - im["rect"][2])
            mapping[best["hash"]] = value
    return mapping


def _read_level_legend_colours(page):
    """Couleurs de probabilité lues sur les pastilles vectorielles de la légende."""
    labels = []
    for span in _legend_spans(page):
        norm = _norm(span["text"])
        for level, pattern in LEVEL_PATTERNS:
            if re.match(pattern, norm):
                labels.append((level, span["bbox"], span["text"]))
                break
    swatches = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        w, h = rect.x1 - rect.x0, rect.y1 - rect.y0
        if 8 <= w <= 40 and 6 <= h <= 26 and drawing.get("fill"):
            swatches.append((rect, drawing["fill"]))

    colours, texts = {}, {}
    for level, bbox, raw in labels:
        texts[level] = raw
        cy = (bbox[1] + bbox[3]) / 2.0
        # la pastille est collée à gauche du libellé ; « quasi nulle » n'en a pas (contour vide)
        near = [(r, f) for r, f in swatches
                if abs((r.y0 + r.y1) / 2.0 - cy) < 9
                and r.x1 <= bbox[0] + 1.0
                and (bbox[0] - r.x1) < 25.0]
        if near:
            rect, fill = max(near, key=lambda rf: (rf[0].x1 - rf[0].x0) * (rf[0].y1 - rf[0].y0))
            colours[level] = _rgb_to_hex(fill)
    return colours, texts


def _read_level_legend_icons(doc, page):
    """Sur la page J+4→J+7 la probabilité est portée par un triangle : hash -> niveau."""
    labels = []
    for span in _spans(page):
        norm = _norm(span["text"])
        for level, pattern in LEVEL_PATTERNS:
            if re.match(pattern, norm):
                labels.append((level, span["bbox"]))
                break
    icons = [im for im in _images(doc, page) if 20 <= im["w"] <= 60 and 20 <= im["h"] <= 60]
    if not icons:
        return {}
    return _match_icons_to_labels(icons, labels, max_gap=40.0, y_tolerance=14.0)


# --------------------------------------------------------------------------
# en-tête, dates, synthèses
# --------------------------------------------------------------------------

def _read_issued_at(page):
    text = page.get_text()
    m = re.search(r"[ÉE]mise le\s+\w+\s+(\d{2})/(\d{2})/(\d{4})\s+à\s+(\d{1,2})h(\d{2})", text)
    if not m:
        raise ParseError("date d'émission introuvable")
    day, month, year, hour, minute = (int(g) for g in m.groups())
    label = re.search(r"[ÉE]mise le[^\n]*", text).group(0).strip()
    naive = datetime(year, month, day, hour, minute)
    offset = 2 if 3 <= month <= 10 else 1  # heure de Paris, approximation sûre hors nuit de bascule
    return naive.replace(tzinfo=timezone(timedelta(hours=offset))), label


def _read_day_headers(page, expected):
    """Retourne [(x_centre, libellé_jour, date_iso, 'J+n'), ...] triés de gauche à droite."""
    found = []
    for span in _spans(page):
        m = re.search(r"(?:(\w+)\s+)?(\d{2})/(\d{2})/(\d{4})", span["text"])
        if not m or "mise le" in span["text"].lower():
            continue
        if span["bbox"][1] > 130:
            continue
        found.append(span)
    # les libellés « (J+n) » peuvent être sur une seconde ligne
    tags = [s for s in _spans(page) if re.fullmatch(r"\(J\+\d(?: et J\+\d)?\)", s["text"]) and s["bbox"][1] < 135]
    inline = [s for s in found if re.search(r"\(J\+\d", s["text"])]

    cards = []
    if inline:
        for span in inline:
            label = re.search(r"\(J\+(\d)(?: et J\+(\d))?\)", span["text"])
            title = re.sub(r"\s*\(J\+.*\)\s*$", "", span["text"]).strip()
            cards.append({
                "x": _rect_center(span["bbox"])[0],
                "date_label": title,
                "label": "J+%s" % label.group(1) + (" et J+%s" % label.group(2) if label.group(2) else ""),
            })
    else:
        for tag in tags:
            tx = _rect_center(tag["bbox"])[0]
            above = [s for s in found if abs(_rect_center(s["bbox"])[0] - tx) < 90
                     and s["bbox"][1] < tag["bbox"][1]]
            title = " ".join(s["text"] for s in sorted(above, key=lambda s: s["bbox"][1])).strip()
            m = re.search(r"\(J\+(\d)(?: et J\+(\d))?\)", tag["text"])
            cards.append({
                "x": tx,
                "date_label": title,
                "label": "J+%s" % m.group(1) + (" et J+%s" % m.group(2) if m.group(2) else ""),
            })
    cards.sort(key=lambda c: c["x"])
    if len(cards) != expected:
        raise ParseError("attendu %d échéances sur la page, %d trouvée(s)" % (expected, len(cards)))
    return cards


def _read_summary(page):
    """Le texte de synthèse est dans le cadre de droite."""
    frames = [d["rect"] for d in page.get_drawings()
              if not d.get("fill") and d.get("color") and (d["rect"].x1 - d["rect"].x0) > 150
              and d["rect"].x0 > page.rect.width * 0.6]
    if not frames:
        return ""
    frame = max(frames, key=lambda r: (r.x1 - r.x0) * (r.y1 - r.y0))
    inside = [s for s in _spans(page)
              if _contains((frame.x0, frame.y0, frame.x1, frame.y1), *_rect_center(s["bbox"]))
              and s["size"] < 11]
    inside.sort(key=lambda s: (round(s["bbox"][1], 1), s["bbox"][0]))
    return re.sub(r"\s+", " ", " ".join(s["text"] for s in inside)).strip()


# --------------------------------------------------------------------------
# cartes départementales (J+2 / J+3)
# --------------------------------------------------------------------------

def _map_placements(images, min_w=200):
    """Les grandes images placées côte à côte sont les fonds de carte."""
    maps = [im for im in images if im["w"] >= min_w and im["h"] >= min_w * 0.8]
    maps.sort(key=lambda im: im["rect"][0])
    return maps


def _sample_departments(img, calibration, colours):
    """Niveau de chaque département par vote majoritaire sur ses pixels."""
    width, height = img.size
    pixels = img.load()
    palette = {level: _hex_to_rgb(value) for level, value in colours.items() if level != "none"}
    out = {}
    for code, points in calibration["samples"].items():
        votes = Counter()
        for nx, ny in points:
            x = min(width - 1, max(0, int(round(nx * width))))
            y = min(height - 1, max(0, int(round(ny * height))))
            r, g, b, a = pixels[x, y]
            if a < 60:
                votes["none"] += 1
                continue
            best, dist = None, None
            for level, ref in palette.items():
                d = (r - ref[0]) ** 2 + (g - ref[1]) ** 2 + (b - ref[2]) ** 2
                if dist is None or d < dist:
                    best, dist = level, d
            # pixels trop éloignés de la palette = trait de frontière : on les ignore
            if dist is not None and dist <= 2500:
                votes[best] += 1
        out[code] = votes.most_common(1)[0][0] if votes else "none"
    return out


def _map_norm_to_svg(nx, ny, calibration):
    """Coordonnées normalisées de la carte PDF -> repère du SVG départemental."""
    (a, b, c), (d, e, f) = calibration["affine"]
    width, height = calibration["map_size"]
    px, py = nx * width, ny * height
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ParseError("calibration cartographique dégénérée")
    ox, oy = px - c, py - f
    return (e * ox - b * oy) / det, (-d * ox + a * oy) / det


def _clusters(levels, adjacency):
    """Composantes connexes de départements partageant le même niveau (hors 'none')."""
    seen, groups = set(), []
    for code, level in levels.items():
        if level == "none" or code in seen:
            continue
        queue, group = deque([code]), []
        seen.add(code)
        while queue:
            cur = queue.popleft()
            group.append(cur)
            for nb in adjacency.get(cur, []):
                if nb not in seen and levels.get(nb) == level:
                    seen.add(nb)
                    queue.append(nb)
        groups.append((level, group))
    return groups


def _nearest_department(nx, ny, calibration):
    best, dist = None, None
    for code, points in calibration["samples"].items():
        for px, py in points:
            d = (px - nx) ** 2 + (py - ny) ** 2
            if dist is None or d < dist:
                best, dist = code, d
    return best, (dist or 0) ** 0.5


def _build_department_card(doc, page, header, map_image, images, phen_by_hash, colours, calibration):
    img = _load_image_rgba(doc, map_image["xref"], map_image["smask"])
    levels = _sample_departments(img, calibration, colours)

    rect = map_image["rect"]
    span_x = rect[2] - rect[0]
    span_y = rect[3] - rect[1]

    markers = []
    for im in images:
        slug = phen_by_hash.get(im["hash"])
        if not slug:
            continue
        cx, cy = _rect_center(im["rect"])
        if not _contains(rect, cx, cy):
            continue
        nx = (cx - rect[0]) / span_x
        ny = (cy - rect[1]) / span_y
        sx, sy = _map_norm_to_svg(nx, ny, calibration)
        markers.append({
            "phenomenon": slug,
            "x": round(nx, 5),
            "y": round(ny, 5),
            "svg_x": round(sx, 4),
            "svg_y": round(sy, 4),
        })

    # Le pictogramme désigne le phénomène prépondérant d'une zone, pas d'un département :
    # on le propage à la zone de couleur homogène qui le contient.
    groups = _clusters(levels, calibration["adjacency"])
    phen_by_dept = {code: [] for code in levels}
    for marker in markers:
        code, distance = _nearest_department(marker["x"], marker["y"], calibration)
        if code is None:
            continue
        target = next((g for lv, g in groups if code in g), None)
        if target is None:
            # Pictogramme posé sur un département resté « quasi nulle » (débordement du
            # symbole) : on rattache le phénomène à la zone colorée voisine la plus proche.
            neighbours = [nb for nb in calibration["adjacency"].get(code, [])
                          if levels.get(nb, "none") != "none"]
            if not neighbours:
                continue
            target = next((g for lv, g in groups if neighbours[0] in g), None)
            if target is None:
                continue
        for dept in target:
            if marker["phenomenon"] not in phen_by_dept[dept]:
                phen_by_dept[dept].append(marker["phenomenon"])

    for marker in markers:
        code, _ = _nearest_department(marker["x"], marker["y"], calibration)
        marker["level"] = levels.get(code, "none")

    departments = {}
    for code in sorted(levels):
        departments[code] = {
            "name": calibration["names"][code],
            "level": levels[code],
            "phenomena": phen_by_dept.get(code, []),
        }

    highest = max((LEVEL_ORDER.index(v["level"]) for v in departments.values()), default=0)
    return {
        "label": header["label"],
        "date_label": header["date_label"],
        "granularity": "department",
        "max_level": LEVEL_ORDER[highest],
        "departments": departments,
        "markers": markers,
    }


# --------------------------------------------------------------------------
# cartes par quart et carte nationale (J+4 → J+7)
# --------------------------------------------------------------------------

def _pair_symbols(images, rect, level_by_hash, phen_by_hash):
    """Sur ces cartes, un triangle (probabilité) surmonte un pictogramme (phénomène)."""
    triangles, icons = [], []
    for im in images:
        cx, cy = _rect_center(im["rect"])
        if not _contains(rect, cx, cy, pad=2.0):
            continue
        if im["hash"] in level_by_hash:
            triangles.append((cx, cy, im))
        elif im["hash"] in phen_by_hash:
            icons.append((cx, cy, im))

    pairs = []
    used = set()
    for cx, cy, tri in triangles:
        best, dist = None, None
        for i, (ix, iy, icon) in enumerate(icons):
            if i in used or iy <= cy:
                continue
            d = abs(ix - cx) * 2 + (iy - cy)
            if d < 40 and (dist is None or d < dist):
                best, dist = i, d
        phenomenon = None
        if best is not None:
            used.add(best)
            phenomenon = phen_by_hash[icons[best][2]["hash"]]
        pairs.append({
            "level": level_by_hash[tri["hash"]],
            "phenomenon": phenomenon,
            "x": (cx - rect[0]) / (rect[2] - rect[0]),
            "y": (cy - rect[1]) / (rect[3] - rect[1]),
        })
    return pairs


def _build_quadrant_card(header, rect, images, level_by_hash, phen_by_hash):
    zones = {z: {"level": "none", "phenomena": []} for z in ("NO", "NE", "SO", "SE")}
    for symbol in _pair_symbols(images, rect, level_by_hash, phen_by_hash):
        zone = ("N" if symbol["y"] < 0.5 else "S") + ("O" if symbol["x"] < 0.5 else "E")
        entry = zones[zone]
        if LEVEL_ORDER.index(symbol["level"]) > LEVEL_ORDER.index(entry["level"]):
            entry["level"] = symbol["level"]
        if symbol["phenomenon"]:
            existing = next((p for p in entry["phenomena"] if p["phenomenon"] == symbol["phenomenon"]), None)
            if existing is None:
                entry["phenomena"].append({"phenomenon": symbol["phenomenon"], "level": symbol["level"]})
            elif LEVEL_ORDER.index(symbol["level"]) > LEVEL_ORDER.index(existing["level"]):
                existing["level"] = symbol["level"]
    highest = max(LEVEL_ORDER.index(z["level"]) for z in zones.values())
    return {
        "label": header["label"],
        "date_label": header["date_label"],
        "granularity": "quadrant",
        "max_level": LEVEL_ORDER[highest],
        "zones": zones,
    }


def _build_national_card(header, rect, images, level_by_hash, phen_by_hash):
    phenomena = []
    level = "none"
    for symbol in _pair_symbols(images, rect, level_by_hash, phen_by_hash):
        if LEVEL_ORDER.index(symbol["level"]) > LEVEL_ORDER.index(level):
            level = symbol["level"]
        if symbol["phenomenon"]:
            phenomena.append({"phenomenon": symbol["phenomenon"], "level": symbol["level"]})
    return {
        "label": header["label"],
        "date_label": header["date_label"],
        "granularity": "national",
        "max_level": level,
        "national": {"level": level, "phenomena": phenomena},
    }


# --------------------------------------------------------------------------
# point d'entrée
# --------------------------------------------------------------------------

def parse(pdf_path: Path, calibration_path: Path) -> dict:
    calibration = json.loads(Path(calibration_path).read_text(encoding="utf-8"))
    doc = pymupdf.open(str(pdf_path))
    if doc.page_count < 2:
        raise ParseError("PDF de %d page(s), 3 attendues" % doc.page_count)

    page_a, page_b = doc[0], doc[1]

    issued_at, issued_label = _read_issued_at(page_a)
    phen_by_hash = _read_phenomenon_legend(doc, page_a)
    phen_by_hash.update(_read_phenomenon_legend(doc, page_b))
    phenomenon_order = _phenomenon_order(page_a)
    for slug in _phenomenon_order(page_b):
        if slug not in phenomenon_order:
            phenomenon_order.append(slug)
    colours, level_texts = _read_level_legend_colours(page_a)
    for level in ("low", "medium", "high"):
        if level not in colours:
            raise ParseError("couleur de probabilité « %s » absente de la légende" % level)
    colours.setdefault("none", "#ffffff")
    level_by_hash = _read_level_legend_icons(doc, page_b)
    if not level_by_hash:
        raise ParseError("symboles de probabilité de la page J+4→J+7 non identifiés")

    images_a = _images(doc, page_a)
    images_b = _images(doc, page_b)

    headers_a = _read_day_headers(page_a, 2)
    maps_a = _map_placements(images_a, min_w=300)
    if len(maps_a) != 2:
        raise ParseError("attendu 2 cartes départementales, %d trouvée(s)" % len(maps_a))

    cards = []
    for header, map_image in zip(headers_a, maps_a):
        cards.append(_build_department_card(doc, page_a, header, map_image,
                                            images_a, phen_by_hash, colours, calibration))

    headers_b = _read_day_headers(page_b, 3)
    frames_b = sorted(
        [d["rect"] for d in page_b.get_drawings()
         if d.get("fill") == (1.0, 1.0, 1.0) and 120 < (d["rect"].x1 - d["rect"].x0) < 260
         and (d["rect"].y1 - d["rect"].y0) > 120],
        key=lambda r: r.x0,
    )
    if len(frames_b) != 3:
        raise ParseError("attendu 3 cadres de carte page 2, %d trouvé(s)" % len(frames_b))

    for index, (header, frame) in enumerate(zip(headers_b, frames_b)):
        rect = (frame.x0, frame.y0, frame.x1, frame.y1)
        if index < 2:
            cards.append(_build_quadrant_card(header, rect, images_b, level_by_hash, phen_by_hash))
        else:
            cards.append(_build_national_card(header, rect, images_b, level_by_hash, phen_by_hash))

    now = datetime.now(timezone.utc).astimezone(issued_at.tzinfo)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "generated_at": now.replace(microsecond=0).isoformat(),
        "source": {
            "provider": "Météo-France",
            "product": "Prévision des phénomènes dangereux",
            "licence": "Licence Ouverte / Open Licence (Etalab 2.0)",
            "page": "https://vigilance.meteofrance.fr/fr",
            "retrieval_mode": "pdf",
        },
        "bulletin": {
            "issued_at": issued_at.replace(microsecond=0).isoformat(),
            "issued_label": issued_label,
        },
        "legend": {
            "levels": [
                {"key": key, "label": level_texts.get(key, key), "colour": colours.get(key, "#ffffff")}
                for key in LEVEL_ORDER
            ],
            "phenomena": phenomenon_order,
        },
        "summaries": {
            "j2_j3": _read_summary(page_a),
            "j4_j7": _read_summary(page_b),
        },
        "cards": cards,
    }


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--calibration", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data" / "geo" / "france-map-calibration.json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    data = parse(args.pdf, args.calibration)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
