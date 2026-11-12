# AM Stats — mesure d'audience auto-hébergée, sans cookie

Outil de statistiques de fréquentation conçu pour remplacer Google Analytics sur
**www.alertes-meteo.com** : léger, rapide, hébergé chez vous, sans cookie ni
bandeau de consentement.

| | AM Stats |
|---|---|
| Sonde JavaScript | **2 412 octets** (1 307 octets compressée gzip), chargée en `defer`, envoi par `navigator.sendBeacon` |
| Cookies / stockage sur l'appareil | **aucun** (sauf la case « ne pas me compter » que le visiteur coche lui-même) |
| Adresse IP | **jamais enregistrée** : elle sert seulement à calculer une empreinte du jour |
| Empreinte visiteur | HMAC-SHA256(date + IP + User-Agent, sel secret + sel aléatoire du jour), sel détruit le lendemain |
| Données | votre serveur uniquement (SQLite par défaut, MySQL/MariaDB possible) |
| Conservation | 750 jours par défaut (maximum CNIL : 25 mois), purge automatique |

---

## 1. Contenu du dossier

```
am-stats/
├── tracker.js              Sonde (version lisible, commentée)
├── tracker.min.js          Sonde minifiée — c'est elle qu'on intègre au site
├── collect.php             Réception des visites (point d'entrée de la sonde)
├── index.php               Tableau de bord (connexion par mot de passe)
├── api.php                 Données JSON du tableau de bord (réservé à l'administrateur)
├── setup.php               Assistant d'installation en ligne de commande
├── config.sample.php       Modèle de configuration → à copier en config.php
├── lib/                    Code PHP (base de données, collecte, statistiques, authentification)
├── assets/                 CSS, JS du tableau de bord, Chart.js 4.5.1 hébergé localement
├── data/                   Base SQLite (créée automatiquement)
├── wordpress/              Extension « must-use » pour insérer la sonde dans WordPress
├── .htaccess               Protections Apache / LiteSpeed
└── nginx.conf.example      Protections équivalentes pour Nginx
```

## 2. Prérequis

- PHP **7.4 minimum** (8.1 ou plus recommandé) avec PDO et `pdo_sqlite` (ou `pdo_mysql`).
- HTTPS sur le site (le tableau de bord refuse le HTTP simple par défaut).
- Facultatif : APCu (met en cache le sel du jour et le dictionnaire d'URL).

## 3. Installation pas à pas

### Étape 1 — Téléverser le dossier

Copiez le dossier `am-stats/` à la racine du site, à côté de `wp-config.php`.
Le tableau de bord sera alors accessible à l'adresse
`https://www.alertes-meteo.com/am-stats/`.

WordPress ne gêne pas : ses règles de réécriture laissent passer les dossiers réels.

### Étape 2 — Créer la configuration

**Avec un accès SSH (recommandé)** :

```bash
cd ~/public_html/am-stats
php setup.php           # crée config.php : domaines, sel secret de 256 bits, mot de passe haché
php setup.php check     # vérifie extensions, droits d'écriture, base, mode WAL
```

**Sans SSH** : copiez `config.sample.php` en `config.php` (avec le gestionnaire
de fichiers de l'hébergeur), puis ouvrez `https://www.alertes-meteo.com/am-stats/`.
Tant qu'aucun mot de passe n'est configuré, la page affiche un **assistant
d'installation** : vous choisissez votre mot de passe, et il affiche les lignes
`admin_password_hash` et `secret_salt` à coller dans `config.php`. Il n'écrit
rien sur le serveur.

Vérifiez ensuite dans `config.php` :

- `allowed_hosts` → `['www.alertes-meteo.com', 'alertes-meteo.com']`
- `site_url` → `https://www.alertes-meteo.com`
- `excluded_ips` → l'IP de votre box, si vous voulez exclure vos visites côté serveur

### Étape 3 — Droits d'écriture

PHP doit pouvoir écrire dans `data/` : SQLite y crée `stats.sqlite`, plus les
fichiers `-wal` et `-shm`. Sur la plupart des hébergements mutualisés, c'est déjà
le cas. Sinon :

```bash
chmod 750 data   # ou 770 selon l'hébergeur
```

**Conseil sécurité** : placez la base **hors du dossier public** en modifiant
`sqlite_path`, par exemple `dirname(__DIR__, 2) . '/am-stats-data/stats.sqlite'`.

### Étape 4 — Vérifier les protections

Ces adresses ne doivent **rien** afficher (erreur 403 ou 404, ou page blanche) :

- `https://www.alertes-meteo.com/am-stats/config.php`
- `https://www.alertes-meteo.com/am-stats/data/stats.sqlite`
- `https://www.alertes-meteo.com/am-stats/lib/Stats.php`
- `https://www.alertes-meteo.com/am-stats/setup.php`

Sous **Nginx**, les fichiers `.htaccess` sont ignorés : ajoutez le contenu de
`nginx.conf.example` au bloc `server` du site.

### Étape 5 — Intégrer la sonde au site

**Option A — WordPress, recommandée** : copiez `wordpress/am-stats-loader.php`
dans `wp-content/mu-plugins/` (créez le dossier s'il n'existe pas). L'extension
est active immédiatement. Elle :

- insère la sonde en `defer` sur toutes les pages publiques ;
- **ne compte pas** les administrateurs, éditeurs et auteurs connectés, ni les aperçus et les pages 404 ;
- empêche WP Rocket, LiteSpeed Cache, Autoptimize et Cloudflare Rocket Loader de regrouper ou de retarder la sonde.

**Option B — balise manuelle** (thème Avada : *Options › Avancé › Code Fields*,
champ « Space before &lt;/head&gt; », ou tout autre site HTML) :

```html
<script defer src="https://www.alertes-meteo.com/am-stats/tracker.min.js"></script>
```

Options disponibles :

| Attribut | Effet |
|---|---|
| `data-endpoint="https://…/collect.php"` | URL de réception (déduite automatiquement de l'emplacement du script) |
| `data-respect-dnt="true"` | ignore les visiteurs ayant activé *Do Not Track* ou *Global Privacy Control* |
| `data-spa="true"` | suit aussi les changements d'URL des applications monopage |
| `data-allow-local="true"` | autorise `localhost` (tests) |

> Si une extension d'optimisation regroupe malgré tout les scripts, la sonde ne
> retrouve plus son adresse. Excluez `am-stats` de la concaténation et du
> « Delay JS », ou déclarez l'adresse avant la sonde :
> `<script>window.AM_STATS={endpoint:'/am-stats/collect.php'}</script>`.

### Étape 6 — Contrôler la réception

Ouvrez votre site en navigation privée, puis les outils de développement
(F12 › Réseau) et filtrez sur `collect`. Vous devez voir une requête
`collect.php` de type *ping* (ou *beacon*), qui renvoie **204** avec l'en-tête
`X-AM-Stats: ok-entry`. La visite apparaît aussitôt dans « Visiteurs actifs ».

| `X-AM-Stats` | Signification |
|---|---|
| `ok-entry` / `ok` | enregistrée (première page de la visite / page suivante) |
| `bot` | robot ou outil automatisé, ignoré |
| `excluded-path`, `excluded-ip` | exclusion volontaire (config.php) |
| `rate-limited` | plus de 30 pages par minute pour ce visiteur (anti-abus) |
| `origin` (403) | envoi depuis un domaine absent de `allowed_hosts` |
| `url` (400) | l'URL de la page n'appartient pas à `allowed_hosts` |
| `secret-salt-missing` (503) | `secret_salt` vide ou trop court dans config.php |

### Étape 7 — Ne pas compter vos propres visites

Dans le pied du tableau de bord, cochez **« Ne pas comptabiliser mes propres
visites depuis ce navigateur »**. Cela fonctionne si le tableau de bord est sur
le même domaine que le site (`www.alertes-meteo.com`). À faire une fois par
navigateur ; vous pouvez compléter avec `excluded_ips`.

---

## 4. Cloudflare, proxy, CDN

Derrière Cloudflare, toutes les requêtes arrivent avec une IP Cloudflare : tous
vos visiteurs seraient fusionnés. Indiquez dans `config.php` :

```php
'trusted_ip_header' => 'HTTP_CF_CONNECTING_IP',
```

N'activez ce réglage **que** si le site est réellement derrière ce proxy :
sinon, n'importe qui pourrait falsifier l'en-tête et gonfler les compteurs.

## 5. Utiliser MySQL / MariaDB plutôt que SQLite

```php
'db' => [
    'driver' => 'mysql',
    'table_prefix' => 'am_',
    'mysql' => ['host' => 'localhost', 'port' => 3306, 'dbname' => 'am_stats',
                'user' => 'am_stats', 'password' => '••••••'],
],
```

Les tables `am_pageviews`, `am_paths`, `am_salts` et `am_logins` et leurs index
sont créés automatiquement à la première visite. Vous pouvez réutiliser la base
de WordPress grâce au préfixe. Nécessite MySQL 5.7+ ou MariaDB 10.3+.

**Quel moteur choisir ?** Sur notre banc d'essai (§ 9), SQLite est plus rapide
en lecture comme en écriture, et sans aucune administration. MySQL est utile si
plusieurs serveurs web partagent la même base.

---

## 6. RGPD : ce qui est collecté, et ce qui ne l'est pas

| Collecté | Jamais collecté |
|---|---|
| Chemin de la page (`/vigilance-meteo-france/`), **sans** paramètres d'URL | Adresse IP (seulement utilisée en mémoire pour l'empreinte) |
| Domaine de provenance (`google.fr` → « Google ») | URL complète du référent, termes de recherche |
| `utm_source` / `ref` / `source` éventuel | Cookies, identifiant persistant, localStorage |
| Type d'appareil (mobile / tablette / ordinateur) | Résolution exacte, langue, système, géolocalisation |
| Empreinte anonyme du jour (64 bits) | Suivi d'un jour sur l'autre ou entre sites |
| Date et heure | Données transmises à un tiers |

**Pourquoi l'empreinte est anonyme** : elle combine la date, l'IP et le
User-Agent avec un sel secret **et** un sel aléatoire propre à chaque jour. Le
sel du jour est **détruit** dès la première visite du lendemain. Même avec un
accès complet au serveur, il devient alors impossible de retrouver à qui
correspond une empreinte passée. En IPv6, seul le préfixe /64 est utilisé.

**Cadre CNIL** : l'outil est conçu pour remplir les conditions d'exemption de
consentement des traceurs de mesure d'audience (lignes directrices et
recommandation de la CNIL du 17 septembre 2020) :

- finalité strictement limitée à la mesure d'audience du site, pour votre seul compte ;
- statistiques anonymes uniquement ;
- aucun croisement avec d'autres traitements, aucune transmission à des tiers ;
- conservation de 25 mois au maximum ;
- information des visiteurs et possibilité de s'opposer.

**Deux actions restent à votre charge** (ce guide ne constitue pas un avis juridique) :

1. **Informer** dans la politique de confidentialité. Texte proposé :

   > *Mesure d'audience.* Ce site utilise AM Stats, un outil de statistiques
   > hébergé sur nos propres serveurs, sans cookie. Il enregistre la page
   > consultée, le site d'où vous venez (nom de domaine uniquement), le type
   > d'appareil (mobile, tablette, ordinateur) et une empreinte anonyme calculée
   > à partir de la date, de votre adresse IP et de votre navigateur. Cette
   > empreinte est renouvelée chaque jour et ne peut plus être reliée à vous le
   > lendemain ; votre adresse IP n'est jamais conservée. Ces données servent
   > exclusivement à produire des statistiques de fréquentation, ne sont
   > transmises à aucun tiers et sont supprimées au bout de 25 mois au plus.
   > Vous pouvez vous opposer à cette mesure ci-dessous.

2. **Permettre l'opposition** : collez ce bloc sur la même page.

   ```html
   <p><label><input type="checkbox" id="am-optout"> Ne pas comptabiliser mes visites dans les statistiques d'audience</label></p>
   <script>
   (function () {
     var c = document.getElementById('am-optout'); if (!c) return;
     try { c.checked = localStorage.getItem('am_stats_ignore') === '1'; } catch (e) { c.disabled = true; return; }
     c.addEventListener('change', function () {
       try { c.checked ? localStorage.setItem('am_stats_ignore', '1') : localStorage.removeItem('am_stats_ignore'); } catch (e) {}
     });
   })();
   </script>
   ```

---

## 7. Définitions et limites (à connaître pour vos bilans)

- **Visiteur unique** : une empreinte distincte sur une journée. Sur une période
  de plusieurs jours, le chiffre est la **somme des visiteurs uniques
  quotidiens** : un lecteur venu 3 jours sur 7 compte 3 fois. C'est le prix de
  l'anonymat (Plausible fonctionne de la même façon). Il sera donc supérieur au
  « nombre d'utilisateurs » de Google Analytics sur une période longue.
- **Visite** : suite de pages vues sans interruption de plus de 30 minutes
  (`visit_timeout`). La source d'une visite est celle de sa première page.
- **Accès direct / favoris** : pas de référent, ou référent interne au site.
- **Comparaison** : chaque indicateur est comparé à la période précédente de
  même durée. Quand la période inclut aujourd'hui, la comparaison s'arrête
  **à la même heure**, pour ne pas confronter une journée entamée à une journée complète.
- **Type d'appareil** : déterminé par la largeur d'écran en pixels CSS. Sur
  écran tactile, c'est le plus petit côté qui compte, pour neutraliser
  l'orientation. Moins de 600 px = mobile ; de 600 à 1 099 px sur écran
  tactile = tablette ; au-delà = ordinateur.
- **Sous-comptage** : certains bloqueurs de publicité bloquent les sondes
  d'audience, y compris auto-hébergées. Les visiteurs sans JavaScript ne sont
  pas comptés.
- **Robots** : double filtrage. Le navigateur s'abstient s'il est automatisé
  (`navigator.webdriver`, Headless, Puppeteer, Playwright…) ; le serveur ignore
  plus de 60 signatures de robots (Googlebot, Bingbot, GPTBot, ClaudeBot,
  aperçus de liens Facebook et WhatsApp, outils de supervision, curl, python…).

## 8. Sécurité

- Mot de passe haché en **Argon2id**, ou bcrypt si Argon2 n'est pas disponible.
- **Anti-force brute** : 5 échecs en 15 minutes entraînent un blocage de
  15 minutes, même avec le bon mot de passe. Les tentatives sont repérées par un
  HMAC de l'IP, jamais par l'IP en clair.
- Session durcie : cookie `HttpOnly`, `Secure`, `SameSite=Strict`, identifiant
  régénéré à la connexion, expiration après 2 h d'inactivité ou 12 h au total.
- Jeton **CSRF** sur tous les formulaires.
- **CSP stricte** (`script-src 'self'`, aucun script inline) ; Chart.js est
  hébergé localement, sans CDN.
- `X-Frame-Options: DENY`, `noindex`, `no-store` sur le tableau de bord et l'API.
- Collecte : vérification `Origin`/`Referer`, liste blanche de domaines, charge
  utile limitée à 4 Ko, 30 pages par minute au maximum par visiteur.
- Requêtes SQL **préparées** partout ; aucun fichier de `lib/` n'est exécutable
  directement.

## 9. Performances mesurées

Banc d'essai : 1 185 487 pages vues **synthétiques** sur 61 jours (dont un pic
d'alerte simulé), PHP 8.3.6, 4 cœurs, SQLite 3.45.1 / MariaDB 10.11 (réglages
par défaut). Médiane de 5 exécutions de l'appel complet du tableau de bord.

| Période | SQLite | MariaDB |
|---|---:|---:|
| Aujourd'hui | 21 ms | 31 ms |
| Hier | 19 ms | 35 ms |
| 7 jours (119 086 pages vues) | 104 ms | 225 ms |
| 30 jours (636 024 pages vues) | 505 ms | 1 693 ms |
| 61 jours (1 185 487 pages vues) | 647 ms | 2 358 ms |

| Enregistrement d'une page vue | SQLite | MariaDB |
|---|---:|---:|
| Latence médiane (base de 1,19 M lignes) | 0,06 ms | 0,86 ms |
| Débit sur un seul processus PHP | 5 446 /s | 1 079 /s |
| Espace disque (table + index) | 189 Mo | 359 Mo |

Soit environ 160 octets par page vue, index compris, grâce à un schéma entièrement
numérique (date `AAAAMMJJ`, empreinte 64 bits, appareil sur 1 octet) et à un
dictionnaire d'URL. Une première version « texte » occupait 478 Mo pour le même
volume.

## 10. Maintenance

- **Purge automatique** : environ une requête sur 500 déclenche, après l'envoi
  de la réponse, la suppression des données plus anciennes que `retention_days`
  et des verrous de connexion expirés. Aucune tâche cron n'est nécessaire.
- **Sauvegarde SQLite** : copiez `stats.sqlite` **avec** les fichiers
  `stats.sqlite-wal` et `stats.sqlite-shm` s'ils existent, ou utilisez
  `sqlite3 stats.sqlite ".backup sauvegarde.sqlite"`.
- **Changer le mot de passe** : `php setup.php password`, ou videz
  `admin_password_hash` pour revoir l'assistant.
- **Changer le sel secret** : sans danger ; seuls les visiteurs uniques de la
  journée en cours repartent de zéro.
