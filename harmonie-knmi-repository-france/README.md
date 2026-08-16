# HARMONIE KNMI France → WordPress

Ce dépôt publie des prévisions horaires issues **directement du modèle
HARMONIE-AROME Cy43 du KNMI**, sans Open-Meteo ni fournisseur météo
intermédiaire. La version nationale couvre les **34 746 communes de France
métropolitaine et de Corse**.

Le serveur mutualisé OVH ne pouvant pas joindre l'API du KNMI, GitHub Actions
télécharge et décode le dernier run. Les résultats sont découpés par
département sur une branche `data`. Le widget WordPress ne charge ainsi que le
département de la commune choisie.

## Fonctionnement

1. Le KNMI publie une archive HARMONIE Europe N55.
2. GitHub Actions télécharge l'archive et décode les échéances H+0 à H+48.
3. Les 34 746 communes sont rattachées à 15 870 points de grille uniques.
4. Les fichiers départementaux sont publiés sur la branche `data`, remplacée à
   chaque run pour éviter l'accumulation d'un historique volumineux.
5. Dans WordPress, le visiteur recherche une commune ou un code postal sans
   recharger la page.

## Fichiers importants

- `.github/workflows/update-harmonie.yml` : mise à jour automatique horaire ;
- `scripts/update_harmonie_france.py` : production nationale compacte ;
- `scripts/update_harmonie.py` : acquisition KNMI et fonctions GRIB communes ;
- `scripts/build_commune_catalog.py` : maintenance du référentiel communal ;
- `config/communes-france.json` : catalogue compact des communes et points ;
- `wordpress/harmonie-knmi-widget/` : extension WordPress nationale ;
- `tests/create_sample_archive.py` : générateur d'archive GRIB de test.

## Première mise en route sur GitHub

1. Copier tout le contenu dans `alertesmeteo-hub/harmonie-knmi`, y compris
   `.github`, `config`, `scripts` et `wordpress`.
2. Ouvrir **Settings → Actions → General**.
3. Dans **Workflow permissions**, sélectionner **Read and write permissions**.
4. Ouvrir **Actions → Mise à jour HARMONIE France → Run workflow**.
5. Attendre la coche verte. Une première exécution nationale peut durer
   plusieurs minutes.
6. Dans le sélecteur de branche du dépôt, choisir `data` et vérifier la présence
   de `index.json` et du dossier `departements`.

Les passages suivants s'arrêtent rapidement si le run KNMI est déjà publié.
La branche `data` est volontairement remplacée à chaque mise à jour.

## Clé KNMI

Le projet peut démarrer avec la clé anonyme officielle publiée par le KNMI,
valable jusqu'au **1er août 2027**. Son quota est partagé et peut parfois
produire une réponse `HTTP 429` ; le script applique alors une attente et des
tentatives automatiques.

Pour une exploitation durable, ajouter une clé KNMI personnelle gratuite dans
**Settings → Secrets and variables → Actions** :

- nom : `KNMI_API_KEY` ;
- valeur : la clé reçue du KNMI.

## Installation WordPress

Installer `harmonie-knmi-widget.zip` depuis **Extensions → Ajouter une
extension → Téléverser une extension**, puis activer l'extension.

Ajouter le shortcode suivant dans une page :

```text
[harmonie_table]
```

Le tableau s'ouvre sur Dunkerque. Le visiteur peut rechercher n'importe quelle
commune métropolitaine ou saisir un code postal.

Pour ouvrir directement une autre commune, préciser son code INSEE et son
département :

```text
[harmonie_table code="75056" departement="75" ville="Paris" heures="48"]
[harmonie_table code="2A004" departement="2A" ville="Ajaccio" heures="36"]
```

L'adresse nationale préconfigurée est :

```text
https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/data
```

Elle peut être modifiée dans **Réglages → HARMONIE KNMI**.

## Recherche des communes

L'autocomplétion utilise l'API officielle française de découpage
administratif. Elle sert uniquement à retrouver le nom, le code INSEE, le code
postal et le département. Toutes les valeurs météorologiques proviennent du
KNMI.

La couverture correspond à la France métropolitaine et à la Corse. Les
territoires d'outre-mer ne font pas partie du domaine européen HARMONIE P3.

## Format national

Chaque fichier `departements/XX.json` contient :

- la liste des communes du département ;
- la liste des points de grille uniques utilisés ;
- les échéances horaires et leurs valeurs compactes.

Plusieurs communes proches peuvent partager un même point HARMONIE, ce qui
réduit fortement le volume sans modifier la résolution du modèle (environ
5,5 km).

## Test local du décodeur

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tests/create_sample_archive.py /tmp/sample_harmonie.tar
.venv/bin/python scripts/update_harmonie.py \
  --archive /tmp/sample_harmonie.tar \
  --config config/locations.json \
  --output /tmp/harmonie-test.json \
  --force
```

La production nationale valide en plus la signature de la véritable grille
N55 ; son test complet nécessite donc une archive HARMONIE P3 réelle.

## Sources et attribution

- modèle : KNMI / UWC-West HARMONIE-AROME Cy43 P3 ;
- format : GRIB1, décodé avec ecCodes ;
- licence météo : Creative Commons Attribution 4.0 ;
- référentiel communal : API Découpage administratif de la République
  française.

Documentation :

- https://english.knmidata.nl/open-data/harmonie
- https://dataplatform.knmi.nl/dataset/harmonie-arome-cy43-p3-1-0
- https://geo.api.gouv.fr/decoupage-administratif/communes
