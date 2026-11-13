<?php
/**
 * AM Stats — configuration.
 *
 * 1. Copiez ce fichier en « config.php » (ou lancez « php setup.php » qui le fait pour vous).
 * 2. Renseignez secret_salt et admin_password_hash (setup.php les génère).
 * 3. Ne versionnez JAMAIS config.php : il contient vos secrets.
 */

declare(strict_types=1);

return [
    // Domaines autorisés à envoyer des statistiques (sans https://, sans slash).
    // Un sous-domaine doit être listé explicitement.
    'allowed_hosts' => ['www.alertes-meteo.com', 'alertes-meteo.com'],

    // Adresse du site : sert à rendre cliquables les pages du classement.
    'site_url' => 'https://www.alertes-meteo.com',

    // Fuseau horaire des statistiques (jours et heures du tableau de bord).
    'timezone' => 'Europe/Paris',

    'db' => [
        // 'sqlite' (aucune installation, idéal jusqu'à plusieurs centaines de milliers de pages vues/jour)
        // ou 'mysql' (MySQL ≥ 5.7 / MariaDB ≥ 10.3).
        'driver' => 'sqlite',

        // Fichier SQLite. Idéalement HORS du dossier public, par ex. dirname(__DIR__, 2) . '/am-stats-data/stats.sqlite'.
        // Le dossier doit être accessible en écriture par PHP.
        'sqlite_path' => __DIR__ . '/data/stats.sqlite',

        // Préfixe des tables (pratique pour partager la base MySQL de WordPress).
        'table_prefix' => 'am_',

        'mysql' => [
            'host'     => 'localhost',
            'port'     => 3306,
            'socket'   => '',
            'dbname'   => 'am_stats',
            'user'     => 'am_stats',
            'password' => '',
        ],
    ],

    // Sel secret permanent (64 caractères hexadécimaux). Généré par setup.php.
    // Le changer remet simplement à zéro les visiteurs uniques de la journée en cours.
    'secret_salt' => '',

    // Hash du mot de passe du tableau de bord (password_hash). Généré par setup.php
    // ou par l'assistant affiché sur la page de connexion tant qu'il est vide.
    'admin_password_hash' => '',

    // Refuser l'accès au tableau de bord en HTTP simple (recommandé : true).
    'require_https' => true,

    // Durées de session du tableau de bord (secondes) : inactivité et durée maximale.
    'session_idle'     => 7200,
    'session_absolute' => 43200,

    // En-tête contenant l'IP réelle du visiteur SI le site est derrière un proxy de confiance :
    // Cloudflare → 'HTTP_CF_CONNECTING_IP' ; proxy/CDN classique → 'HTTP_X_FORWARDED_FOR'.
    // Laisser null sans proxy (sinon n'importe qui pourrait falsifier l'en-tête).
    'trusted_ip_header' => null,

    // IP ou plages CIDR à ne pas comptabiliser (votre box, vos bureaux…). Elles ne sont pas stockées.
    'excluded_ips' => [
        // '203.0.113.42',
        // '2001:db8:1234::/48',
    ],

    // Débuts de chemins à ignorer.
    'excluded_paths' => ['/wp-admin', '/wp-login.php', '/am-stats'],

    // Inactivité (secondes) au-delà de laquelle une nouvelle visite commence (sources de trafic).
    'visit_timeout' => 1800,

    // Durée de conservation des données (jours). La CNIL fixe un maximum de 25 mois (≈ 760 jours).
    'retention_days' => 750,

    // Anti-abus : nombre maximal de pages vues par visiteur et par minute.
    'rate_limit' => 30,
];
