=== Tableau HARMONIE KNMI ===
Contributors: alertesmeteo-hub
Tags: meteo, harmonie, arome, knmi, previsions
Requires at least: 5.8
Requires PHP: 7.4
Stable tag: 2.0.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Recherche nationale et tableau horaire du modèle HARMONIE-AROME officiel du KNMI.

== Description ==

L'extension permet de rechercher les 34 746 communes de France métropolitaine
et de Corse par nom ou par code postal. Elle charge ensuite uniquement le
fichier du département choisi. Les prévisions proviennent directement du
modèle HARMONIE-AROME Cy43 du KNMI ; Open-Meteo n'est pas utilisé.

== Installation ==

1. Installer et activer l'extension.
2. Ajouter le shortcode `[harmonie_table]` dans une page WordPress.
3. Le visiteur saisit le nom d'une commune ou un code postal.
4. Si nécessaire, modifier l'adresse du dossier national dans Réglages > HARMONIE KNMI.

Exemple avec Paris comme commune affichée au chargement :
`[harmonie_table code="75056" departement="75" ville="Paris" heures="48"]`

== Changelog ==

= 2.0.0 =
* Recherche avec autocomplétion par commune ou code postal.
* Couverture des 34 746 communes de France métropolitaine et de Corse.
* Chargement rapide par département et partage des points de grille identiques.
* Sélection d'une nouvelle commune sans rechargement de la page.

= 1.0.0 =
* Première version : tableau responsive, cache et alerte de données anciennes.
