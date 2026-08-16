=== Tableau HARMONIE KNMI ===
Contributors: alertesmeteo-hub
Tags: meteo, harmonie, arome, knmi, previsions
Requires at least: 5.8
Requires PHP: 7.4
Stable tag: 1.0.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Affiche un tableau horaire du modèle HARMONIE-AROME officiel du KNMI.

== Description ==

L'extension lit le JSON produit par le dépôt GitHub alertesmeteo-hub/harmonie-knmi.
Elle n'utilise pas Open-Meteo. Les données sont mises en cache pendant 15 minutes
et la dernière version correcte reste disponible si GitHub répond temporairement
en erreur.

== Installation ==

1. Installer et activer l'extension.
2. Ajouter le shortcode `[harmonie_table ville="Dunkerque" heures="48"]`.
3. Si nécessaire, modifier l'adresse JSON dans Réglages > HARMONIE KNMI.

== Changelog ==

= 1.0.0 =
* Première version : tableau responsive, cache et alerte de données anciennes.
