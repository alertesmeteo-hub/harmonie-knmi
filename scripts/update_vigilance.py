#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://public-api.meteofrance.fr/public/DPVigilance/v1"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"
OUT = Path(os.getenv("VIGILANCE_OUTPUT", "data/vigilance.json"))
APPLICATION_ID = os.getenv("METEOFRANCE_APPLICATION_ID", "").strip()
API_KEY = os.getenv("METEOFRANCE_API_KEY", "").strip()
LEGACY_TOKEN = os.getenv("METEOFRANCE_TOKEN", "").strip()
USER_AGENT = "alertes-meteo-vigilance/1.2.1"
RETRY_CODES = {429, 500, 502, 503, 504}


def retry_delay(headers: Any, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return max(1.0, min(60.0, float(retry_after)))
        except (TypeError, ValueError):
            try:
                dt = parsedate_to_datetime(retry_after)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                seconds = (dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                return max(1.0, min(60.0, seconds))
            except Exception:
                pass
    return min(8.0, 2.0 ** attempt)


def normalize_application_id(value: str) -> str:
    """Accepte l'APPLICATION_ID seul, `Basic ...` ou même la commande curl copiée du portail."""
    value = value.strip().strip('"').strip("'")
    if not value:
        raise RuntimeError("Secret METEOFRANCE_APPLICATION_ID absent")

    # Si l'utilisateur a collé toute la commande curl du portail, extraire la valeur après Basic.
    match = re.search(r"Authorization\s*:\s*Basic\s+([^\s\"']+)", value, flags=re.IGNORECASE)
    if match:
        value = match.group(1).strip()
    elif value.lower().startswith("basic "):
        value = value[6:].strip()

    # Les tokens/API keys JWT commencent typiquement par eyJ... et contiennent deux points.
    # Ils ne doivent PAS être utilisés comme APPLICATION_ID.
    if value.startswith("eyJ") and value.count(".") >= 2:
        raise RuntimeError(
            "METEOFRANCE_APPLICATION_ID ressemble à un token/API Key JWT (eyJ...). "
            "Ce n'est pas l'APPLICATION_ID. Dans le portail Météo-France, choisissez OAuth2 puis "
            "copiez uniquement la chaîne située après 'Authorization: Basic' dans la commande curl. "
            "Si vous souhaitez utiliser une API Key, enregistrez-la plutôt dans METEOFRANCE_API_KEY."
        )

    if any(ch.isspace() for ch in value):
        raise RuntimeError(
            "METEOFRANCE_APPLICATION_ID contient des espaces. Copiez uniquement la chaîne après "
            "'Authorization: Basic' dans la commande curl du portail Météo-France."
        )
    return value


def response_excerpt(raw: bytes, limit: int = 350) -> str:
    text = raw.decode("utf-8", errors="replace").strip().replace("\r", " ").replace("\n", " ")
    return text[:limit] if text else "<réponse vide>"


def request_oauth_token(attempts: int = 3) -> str:
    """Obtenir un jeton OAuth2 temporaire à chaque exécution via l'APPLICATION_ID."""
    app_id = normalize_application_id(APPLICATION_ID)
    body = urlencode({"grant_type": "client_credentials"}).encode("ascii")
    headers = {
        "Authorization": f"Basic {app_id}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    for attempt in range(attempts):
        req = Request(TOKEN_URL, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=35) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "inconnu")
                try:
                    data = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "Le serveur d'authentification Météo-France n'a pas renvoyé du JSON. "
                        f"HTTP {getattr(response, 'status', 200)}, Content-Type={content_type}, "
                        f"début de réponse={response_excerpt(raw)!r}. "
                        "Vérifiez que METEOFRANCE_APPLICATION_ID est bien la chaîne située après "
                        "'Authorization: Basic' dans le curl OAuth2 du portail, et non le token généré."
                    ) from exc

                if not isinstance(data, dict):
                    raise RuntimeError("Réponse d'authentification Météo-France inattendue (objet JSON attendu)")
                token = str(data.get("access_token", "")).strip()
                if not token:
                    err = data.get("error_description") or data.get("error") or "access_token absent"
                    raise RuntimeError(f"Météo-France a refusé la génération du token OAuth2: {err}")
                expires = data.get("expires_in")
                print(f"Jeton OAuth2 Météo-France obtenu{f' (validité {expires}s)' if expires else ''}.")
                return token
        except HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            detail = response_excerpt(raw)
            if exc.code not in RETRY_CODES or attempt == attempts - 1:
                if exc.code in {400, 401, 403}:
                    raise RuntimeError(
                        f"Météo-France HTTP {exc.code} lors de la génération du token OAuth2. "
                        f"Réponse={detail!r}. Vérifiez METEOFRANCE_APPLICATION_ID : il faut copier "
                        "uniquement la chaîne après 'Authorization: Basic' dans le curl OAuth2."
                    ) from exc
                raise RuntimeError(
                    f"Météo-France HTTP {exc.code} lors de la génération du token; réponse={detail!r}"
                ) from exc
            delay = retry_delay(exc.headers, attempt)
            print(f"HTTP {exc.code} pour le token; nouvel essai dans {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
        except URLError as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Erreur réseau lors de la génération du token OAuth2: {exc.reason}") from exc
            time.sleep(min(8.0, 2.0 ** attempt))

    raise RuntimeError("Impossible d'obtenir le token OAuth2 Météo-France")


def resolve_token() -> tuple[str, str]:
    # Pour un automatisme GitHub, l'APPLICATION_ID est le mode recommandé ici :
    # il permet de renouveler automatiquement le jeton OAuth2 (env. 1 h).
    if APPLICATION_ID:
        try:
            return request_oauth_token(), "oauth2"
        except RuntimeError as exc:
            if API_KEY:
                print(f"AVERTISSEMENT OAuth2: {exc}", file=sys.stderr)
                print("Bascule sur METEOFRANCE_API_KEY.", file=sys.stderr)
                return API_KEY, "api_key"
            raise
    if API_KEY:
        print("Authentification Météo-France par API Key.")
        return API_KEY, "api_key"
    if LEGACY_TOKEN:
        print(
            "ATTENTION: METEOFRANCE_TOKEN est utilisé directement. "
            "Un token généré par le portail peut expirer (souvent ~1 h).",
            file=sys.stderr,
        )
        return LEGACY_TOKEN, "legacy_token"
    raise RuntimeError(
        "Aucun secret Météo-France configuré. Ajoutez METEOFRANCE_APPLICATION_ID "
        "(recommandé) ou METEOFRANCE_API_KEY dans GitHub Actions."
    )


def get_json(path: str, token: str, auth_mode: str, optional: bool = False, attempts: int = 4) -> dict[str, Any] | None:
    url = f"{BASE}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json,*/*",
        "User-Agent": USER_AGENT,
    }

    last_error: Exception | None = None
    for attempt in range(attempts):
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=35) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
                if optional and status == 204:
                    return None
                try:
                    data = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"Réponse JSON invalide pour {path}") from exc
                if not isinstance(data, dict):
                    raise RuntimeError(f"Réponse inattendue pour {path}: objet JSON attendu")
                return data
        except HTTPError as exc:
            if optional and exc.code in {204, 404}:
                return None
            last_error = exc
            if exc.code == 401:
                if auth_mode == "legacy_token":
                    raise RuntimeError(
                        f"Météo-France HTTP 401 pour {path}: le METEOFRANCE_TOKEN est invalide ou expiré. "
                        "Utilisez METEOFRANCE_APPLICATION_ID pour renouveler automatiquement le token."
                    ) from exc
                if auth_mode == "api_key":
                    raise RuntimeError(
                        f"Météo-France HTTP 401 pour {path}: METEOFRANCE_API_KEY invalide ou expirée."
                    ) from exc
                raise RuntimeError(
                    f"Météo-France HTTP 401 pour {path} malgré un token OAuth2 fraîchement généré. "
                    "Vérifiez l'abonnement à l'API Bulletin Vigilance dans le portail Météo-France."
                ) from exc
            if exc.code == 403:
                raise RuntimeError(
                    f"Météo-France HTTP 403 pour {path}: abonnement à l'API Bulletin Vigilance absent "
                    "ou droits insuffisants."
                ) from exc
            if exc.code not in RETRY_CODES or attempt == attempts - 1:
                raise RuntimeError(f"Météo-France HTTP {exc.code} pour {path}") from exc
            delay = retry_delay(exc.headers, attempt)
            print(f"HTTP {exc.code} pour {path}; nouvel essai dans {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
        except URLError as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise RuntimeError(f"Erreur réseau pour {path}: {exc.reason}") from exc
            delay = min(8.0, 2.0 ** attempt)
            print(f"Erreur réseau pour {path}; nouvel essai dans {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)

    raise RuntimeError(f"Échec de téléchargement pour {path}: {last_error}")


def product_datetime(obj: dict[str, Any] | None) -> str | None:
    if not isinstance(obj, dict):
        return None
    meta = obj.get("meta")
    product = obj.get("product")
    if isinstance(meta, dict) and meta.get("product_datetime"):
        return str(meta["product_datetime"])
    if isinstance(product, dict) and product.get("update_time"):
        return str(product["update_time"])
    return None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def same_product_datetime(a: str | None, b: str | None) -> bool:
    da, db = parse_utc(a), parse_utc(b)
    if da is not None and db is not None:
        return da == db
    return a == b


def validate_carte(carte: dict[str, Any]) -> None:
    product = carte.get("product")
    if not isinstance(product, dict):
        raise RuntimeError("Produit carte invalide: clé product absente")

    periods = product.get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError("Produit carte invalide: aucune période")

    terms = {
        str(period.get("echeance", "")).upper()
        for period in periods
        if isinstance(period, dict)
    }
    if "J" not in terms:
        raise RuntimeError("Produit carte invalide: échéance J absente")

    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError("Produit carte invalide: période non objet")
        timelaps = period.get("timelaps")
        if not isinstance(timelaps, dict):
            raise RuntimeError(f"Produit carte invalide: timelaps absent pour {period.get('echeance')}")
        domains = timelaps.get("domain_ids")
        if not isinstance(domains, list) or not domains:
            raise RuntimeError(f"Produit carte invalide: domaines absents pour {period.get('echeance')}")


def validate_textes(textes: dict[str, Any] | None) -> None:
    if textes is None:
        return
    product = textes.get("product")
    if not isinstance(product, dict):
        raise RuntimeError("Produit textes invalide: clé product absente")
    blocks = product.get("text_bloc_items")
    if blocks is not None and not isinstance(blocks, list):
        raise RuntimeError("Produit textes invalide: text_bloc_items doit être un tableau")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    token, auth_mode = resolve_token()

    carte = get_json("cartevigilance/encours", token, auth_mode)
    if carte is None:
        raise RuntimeError("Météo-France n'a renvoyé aucune carte de vigilance")
    validate_carte(carte)

    try:
        textes = get_json("textesvigilance/encours", token, auth_mode, optional=True)
        validate_textes(textes)
    except Exception as exc:
        print(f"Avertissement textesvigilance: {exc}", file=sys.stderr)
        textes = None

    carte_dt = product_datetime(carte)
    textes_dt = product_datetime(textes)
    if textes is not None and carte_dt and textes_dt and not same_product_datetime(carte_dt, textes_dt):
        print(f"Textes ignorés car non concordants ({textes_dt} != {carte_dt})", file=sys.stderr)
        textes = None

    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            if old.get("carte") == carte and old.get("textes") == textes:
                print("Vigilance inchangée : aucun fichier modifié.")
                return 0
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Météo-France – DPVigilance",
        "source_api": BASE,
        "carte": carte,
        "textes": textes,
    }
    atomic_write(
        OUT,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    periods = [p.get("echeance") for p in carte["product"]["periods"] if isinstance(p, dict)]
    print(f"Écrit {OUT} – produit {carte_dt or 'date inconnue'} – échéances {periods}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        raise SystemExit(1)
