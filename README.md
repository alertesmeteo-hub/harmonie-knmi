# HARMONIE KNMI → WordPress

Ce dépôt publie un tableau de prévisions horaires issu **directement du modèle
HARMONIE-AROME du KNMI**, sans Open-Meteo ni autre fournisseur météo
intermédiaire.

Le serveur mutualisé OVH ne pouvant pas joindre l'API du KNMI, une action
GitHub télécharge et décode le dernier run. Elle publie ensuite un petit fichier
JSON que l'extension WordPress lit depuis OVH.

## Contenu

- `.github/workflows/update-harmonie.yml` : mise à jour automatique chaque heure ;
- `scripts/update_harmonie.py` : téléchargement KNMI et extraction GRIB1 avec
  ecCodes ;
- `config/locations.json` : villes et durée de prévision ;
- `data/harmonie.json` : données publiées pour WordPress ;
- `wordpress/harmonie-knmi-widget/` : extension WordPress ;
- `tests/create_sample_archive.py` : générateur GRIB1 pour tester le pipeline.

## Mise en route sur GitHub

1. Copier tout le contenu de ce dépôt dans
   `alertesmeteo-hub/harmonie-knmi`, y compris le dossier `.github`.
2. Ouvrir **Settings → Actions → General**.
3. Dans **Workflow permissions**, choisir **Read and write permissions**, puis
   enregistrer.
4. Ouvrir **Actions → Mise à jour HARMONIE → Run workflow**.
5. Attendre la coche verte. Le fichier `data/harmonie.json` passe alors de
   `waiting_for_first_update` à `ok`.

La planification GitHub relance ensuite le traitement à la minute 27 de chaque
heure. Si le KNMI n'a pas encore publié de nouveau run, le script s'arrête sans
retélécharger l'archive.

## Clé KNMI

Le projet fonctionne immédiatement avec la clé anonyme officielle publiée par
le KNMI, valable jusqu'au **1er août 2027**. Aucun compte KNMI n'est donc requis
pour démarrer.

Pour une utilisation durable, il est conseillé de demander gratuitement une
clé enregistrée au KNMI, puis de l'ajouter dans GitHub sous
**Settings → Secrets and variables → Actions → New repository secret** :

- nom : `KNMI_API_KEY` ;
- valeur : la clé personnelle reçue du KNMI.

Le secret remplace automatiquement la clé anonyme et ne doit jamais être ajouté
à un fichier du dépôt.

## Installation WordPress

Installer le ZIP `harmonie-knmi-widget.zip` dans
**Extensions → Ajouter une extension → Téléverser une extension**, puis activer
**Tableau HARMONIE KNMI**.

Ajouter ensuite ce shortcode dans une page ou un bloc Shortcode :

```text
[harmonie_table ville="Dunkerque" heures="48"]
```

Autres exemples :

```text
[harmonie_table ville="Calais" heures="24"]
[harmonie_table ville="Lille" heures="36" titre="Météo détaillée à Lille"]
```

Villes incluses : Dunkerque, Calais, Boulogne-sur-Mer, Le Touquet, Lille,
Roubaix, Douai, Arras, Valenciennes, Maubeuge, Cambrai et Abbeville.

L'adresse JSON est préconfigurée pour ce dépôt :

```text
https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/main/data/harmonie.json
```

Elle peut être changée dans **Réglages → HARMONIE KNMI**.

## Ajouter ou modifier une ville

Éditer `config/locations.json` en conservant un identifiant `slug` unique :

```json
{
  "slug": "saint-omer",
  "name": "Saint-Omer",
  "latitude": 50.7483,
  "longitude": 2.2609
}
```

La valeur `forecast_hours` accepte de 1 à 60 heures. Après le changement,
lancer manuellement le workflow avec **Run workflow**.

## Données affichées

Le JSON contient, pour chaque échéance : température, point de rosée,
humidité, pluie horaire, nébulosité totale/basse/moyenne/haute, vent moyen,
direction, rafales, pression et visibilité. Les composantes de vent de la grille
tournée sont remises dans le repère géographique.

## Test local facultatif

Python 3.11 ou plus récent est recommandé :

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tests/create_sample_archive.py /tmp/sample_harmonie.tar
.venv/bin/python scripts/update_harmonie.py \
  --archive /tmp/sample_harmonie.tar \
  --config config/locations.json \
  --output /tmp/harmonie-test.json
```

## Source et attribution

- Jeu de données : `harmonie_arome_cy43_p3`, version `1.0` ;
- producteur : KNMI / UWC-West ;
- format source : GRIB1 ;
- licence des données : Creative Commons Attribution 4.0.

Documentation officielle :

- https://dataplatform.knmi.nl/dataset/harmonie-arome-cy43-p3-1-0
- https://developer.dataplatform.knmi.nl/open-data-api
- https://english.knmidata.nl/open-data/harmonie
