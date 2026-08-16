# Guide d'installation — HARMONIE KNMI

Ce paquet contient deux fichiers ZIP :

- `harmonie-knmi-repository.zip` pour votre dépôt GitHub ;
- `harmonie-knmi-widget.zip` pour votre site WordPress.

La chaîne reste directe : **API officielle KNMI → votre dépôt GitHub → votre
site WordPress OVH**. Open-Meteo n'intervient nulle part.

## 1. Mettre le programme dans GitHub

1. Décompressez `harmonie-knmi-repository.zip` sur votre ordinateur.
2. Ouvrez https://github.com/alertesmeteo-hub/harmonie-knmi
3. Cliquez sur **Add file**, puis **Upload files**.
4. Dans le dossier décompressé, sélectionnez **tout ce qui se trouve à
   l'intérieur** (notamment `.github`, `config`, `data`, `scripts`, `tests`,
   `wordpress` et les fichiers de la racine). Ne déposez pas le dossier parent
   lui-même.
5. Glissez cette sélection sur la page GitHub.
6. En bas, conservez le message proposé puis cliquez sur
   **Commit changes**.

Si GitHub indique que `README.md` existe déjà, acceptez son remplacement par la
version du paquet.

## 2. Autoriser la mise à jour du JSON

1. Dans le dépôt, ouvrez **Settings**.
2. À gauche, ouvrez **Actions → General**.
3. Descendez jusqu'à **Workflow permissions**.
4. Cochez **Read and write permissions**.
5. Cliquez sur **Save**.

Cette autorisation sert uniquement à enregistrer automatiquement le nouveau
fichier `data/harmonie.json` dans votre propre dépôt.

## 3. Lancer le premier téléchargement

1. Ouvrez l'onglet **Actions** du dépôt.
2. À gauche, choisissez **Mise à jour HARMONIE**.
3. Cliquez sur **Run workflow**, puis encore **Run workflow**.
4. Actualisez la page et ouvrez l'exécution en cours.

Le premier passage peut être assez long, car une archive HARMONIE Europe est
volumineuse. Il est terminé quand une coche verte apparaît. Vérifiez ensuite
que `data/harmonie.json` contient en haut :

```json
"status": "ok"
```

Le traitement se relancera automatiquement chaque heure. Il utilise la clé
anonyme officielle du KNMI, valable jusqu'au 1er août 2027 : aucun compte KNMI
n'est nécessaire pour cette première installation.

## 4. Installer le tableau dans WordPress

1. Dans WordPress, ouvrez **Extensions → Ajouter une extension**.
2. Cliquez sur **Téléverser une extension**.
3. Choisissez `harmonie-knmi-widget.zip` sans le décompresser.
4. Cliquez sur **Installer maintenant**, puis **Activer**.
5. Ouvrez ou créez une page WordPress.
6. Ajoutez un bloc **Code court / Shortcode** contenant :

```text
[harmonie_table ville="Dunkerque" heures="48"]
```

Publiez la page. Le tableau se mettra ensuite à jour automatiquement, sans cron
Python sur OVH.

## 5. Autres villes et options

Exemples :

```text
[harmonie_table ville="Calais" heures="24"]
[harmonie_table ville="Lille" heures="36" titre="Prévisions détaillées"]
```

Villes déjà configurées : Dunkerque, Calais, Boulogne-sur-Mer, Le Touquet,
Lille, Roubaix, Douai, Arras, Valenciennes, Maubeuge, Cambrai et Abbeville.

Pour ajouter une ville, modifiez `config/locations.json` sur GitHub avec son nom,
sa latitude et sa longitude, puis relancez le workflow.

## En cas de blocage

- **Croix rouge dans Actions** : ouvrez l'étape rouge et copiez tout son message
  d'erreur pour le diagnostic.
- **Erreur d'autorisation lors du `git push`** : refaites l'étape 2 et vérifiez
  **Read and write permissions**.
- **« Les données ne sont pas encore disponibles » dans WordPress** : le premier
  workflow n'est pas encore terminé ou `data/harmonie.json` n'a pas le statut
  `ok`.
- **Ville inconnue** : utilisez exactement un nom présent dans
  `config/locations.json`.
- **Tableau ancien** : GitHub conserve la dernière prévision correcte et le
  module affiche une alerte après huit heures sans mise à jour.

Adresse JSON utilisée par le module :

```text
https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/main/data/harmonie.json
```
