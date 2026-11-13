<?php
/**
 * Plugin Name: AM Stats – sonde d'audience
 * Description: Insère la sonde AM Stats (sans cookie, conforme RGPD) sur les pages publiques. Les utilisateurs connectés pouvant éditer le site ne sont pas comptés.
 * Version: 1.0.0
 * Requires at least: 6.3
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 *
 * Installation : copier ce fichier dans wp-content/mu-plugins/ (créer le dossier si besoin).
 * Il est alors actif automatiquement, sans passer par la page des extensions.
 */

if (!defined('ABSPATH')) {
    exit;
}

// Dossier où AM Stats est installé, relatif à la racine du site.
if (!defined('AM_STATS_PATH')) {
    define('AM_STATS_PATH', '/am-stats/');
}

add_action('wp_enqueue_scripts', static function (): void {
    // Pas de comptage des administrateurs, éditeurs et auteurs connectés.
    if (is_user_logged_in() && current_user_can('edit_posts')) {
        return;
    }
    // Ni des aperçus, du personnalisateur, des flux et des pages d'erreur 404.
    if (is_preview() || is_customize_preview() || is_feed() || is_404()) {
        return;
    }

    wp_enqueue_script(
        'am-stats',
        home_url(AM_STATS_PATH . 'tracker.min.js'),
        [],
        '1.0.0',
        ['strategy' => 'defer', 'in_footer' => false]
    );
}, 1);

// Les extensions d'optimisation (WP Rocket, LiteSpeed Cache, Autoptimize…) ne doivent
// ni regrouper ni retarder la sonde : on la marque explicitement.
add_filter('script_loader_tag', static function (string $tag, string $handle): string {
    if ($handle !== 'am-stats') {
        return $tag;
    }
    return str_replace(' src=', ' nowprocket data-no-optimize="1" data-no-minify="1" data-no-defer="1" data-cfasync="false" src=', $tag);
}, 10, 2);
