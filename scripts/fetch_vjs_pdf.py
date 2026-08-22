#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Récupération du PDF « Prévision des phénomènes dangereux » de Météo-France.

Le fichier est servi par
    https://rwg.meteofrance.com/internet2018client/2.0/report
        ?domain=france&report_type=vigilance&report_subtype=jours suivants&token=<JWT>

Le jeton est un JWT émis par le site pour la session en cours : il ne peut pas
être codé en dur, et le site est protégé par un dispositif anti-bot qui rejette
les clients HTTP nus. On ouvre donc la page officielle dans un vrai navigateur,
on déclenche l'ouverture du bulletin, et on récupère la réponse PDF.

Durée typique : quelques secondes. Aucun survol de carte, aucune lecture du DOM.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

VIGILANCE_URL = "https://vigilance.meteofrance.fr/fr"
REPORT_PATTERN = re.compile(r"/report\?.*report_type=vigilance", re.I)
SUBTYPE_PATTERN = re.compile(r"report_subtype=jours(?:%20|\+|\s)*suivants", re.I)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def _looks_like_target(url: str) -> bool:
    return bool(REPORT_PATTERN.search(url) and SUBTYPE_PATTERN.search(url))


def fetch(destination: Path, timeout_ms: int = 60000, headless: bool = True) -> Path:
    captured: dict[str, bytes] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 960},
            accept_downloads=True,
        )
        page = context.new_page()

        def on_response(response):
            if captured or not _looks_like_target(response.url):
                return
            try:
                body = response.body()
            except Exception:
                return
            if body[:4] == b"%PDF":
                captured["url"] = response.url.encode()
                captured["body"] = body

        page.on("response", on_response)

        page.goto(VIGILANCE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(4000)
        _dismiss_cookie_banner(page)

        # 1) le lien « Prochains jours » déclenche lui-même la requête
        for selector in (
            "a:has-text('Prochains jours')",
            "text=/prochains\\s+jours/i",
            "[href*='report_subtype']",
        ):
            if captured:
                break
            try:
                element = page.locator(selector).first
                if element.count() == 0:
                    continue
                element.click(timeout=5000)
                page.wait_for_timeout(5000)
            except Exception:
                continue

        # 2) à défaut, on lit l'URL dans le DOM et on la demande avec la session du navigateur
        if not captured:
            href = page.evaluate(
                """() => {
                    const a = [...document.querySelectorAll('a[href]')]
                        .find(x => /report_subtype=jours/i.test(x.href));
                    return a ? a.href : null;
                }"""
            )
            if href:
                response = context.request.get(href, timeout=timeout_ms)
                body = response.body()
                if body[:4] == b"%PDF":
                    captured["url"] = href.encode()
                    captured["body"] = body

        browser.close()

    if not captured:
        raise RuntimeError(
            "PDF « jours suivants » non récupéré : ni la navigation ni le lien direct "
            "n'ont produit de réponse PDF."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(captured["body"])
    return destination


def _dismiss_cookie_banner(page):
    """Refuse les cookies non essentiels si la bannière est présente."""
    for selector in (
        "button:has-text('Tout refuser')",
        "button:has-text('Refuser')",
        "#tarteaucitronAllDenied2",
    ):
        try:
            element = page.locator(selector).first
            if element.count():
                element.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except (PlaywrightTimeout, Exception):
            continue


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("build/prochains-jours.pdf"))
    parser.add_argument("--timeout", type=int, default=60000)
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args(argv)

    path = fetch(args.out, timeout_ms=args.timeout, headless=not args.headful)
    print("PDF récupéré : %s (%d octets)" % (path, path.stat().st_size), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
