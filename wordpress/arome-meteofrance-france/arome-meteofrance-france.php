<?php
/**
 * Plugin Name: AROME Météo-France France — Tableaux et cartes
 * Plugin URI: https://github.com/alertesmeteo-hub/arome-meteofrance
 * Description: Module unique de cartes interactives et de prévisions AROME de Météo-France pour la France métropolitaine et la Corse.
 * Version: 1.0.0
 * Author: Alertes Météo Hub
 * Requires at least: 5.8
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 */

if (!defined('ABSPATH')) {
    exit;
}

define('AMF_VERSION', '1.0.0');
define('AMF_OPTION_BASE_URL', 'amf_national_data_base_url');
define(
    'AMF_DEFAULT_BASE_URL',
    'https://raw.githubusercontent.com/alertesmeteo-hub/arome-meteofrance/data'
);

add_action('wp_enqueue_scripts', 'amf_register_assets');
add_action('admin_init', 'amf_register_settings');
add_action('admin_menu', 'amf_add_settings_page');
add_shortcode('arome_meteo', 'amf_render_shortcode');

function amf_register_assets() {
    wp_register_style(
        'hkw-table',
        plugin_dir_url(__FILE__) . 'assets/arome-meteo.css',
        array(),
        AMF_VERSION
    );
    wp_register_script(
        'hkw-table',
        plugin_dir_url(__FILE__) . 'assets/arome-meteo.js',
        array(),
        AMF_VERSION,
        true
    );
    wp_register_style(
        'hkw-map',
        plugin_dir_url(__FILE__) . 'assets/arome-map.css',
        array('hkw-table'),
        AMF_VERSION
    );
    wp_register_script(
        'hkw-map',
        plugin_dir_url(__FILE__) . 'assets/arome-map.js',
        array(),
        AMF_VERSION,
        true
    );
}

function amf_register_settings() {
    register_setting(
        'amf_settings',
        AMF_OPTION_BASE_URL,
        array(
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => AMF_DEFAULT_BASE_URL,
        )
    );

    add_settings_section(
        'amf_main_section',
        'Source des données nationales',
        '__return_false',
        'arome-meteofrance'
    );

    add_settings_field(
        'amf_data_base_url_field',
        'Adresse du dossier de données',
        'amf_render_url_field',
        'arome-meteofrance',
        'amf_main_section'
    );
}

function amf_render_url_field() {
    $value = get_option(AMF_OPTION_BASE_URL, AMF_DEFAULT_BASE_URL);
    printf(
        '<input type="url" class="regular-text code" name="%1$s" value="%2$s" autocomplete="off">',
        esc_attr(AMF_OPTION_BASE_URL),
        esc_attr($value)
    );
    echo '<p class="description">Conservez l’adresse proposée : elle pointe vers la branche nationale « data » du dépôt.</p>';
}

function amf_add_settings_page() {
    add_options_page(
        'Tableau AROME Météo-France France',
        'AROME Météo-France',
        'manage_options',
        'arome-meteofrance',
        'amf_render_settings_page'
    );
}

function amf_render_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    ?>
    <div class="wrap">
        <h1>AROME Météo-France France</h1>
        <form action="options.php" method="post">
            <?php
            settings_fields('amf_settings');
            do_settings_sections('arome-meteofrance');
            submit_button();
            ?>
        </form>
        <p><strong>Version du module : <?php echo esc_html(AMF_VERSION); ?></strong></p>
        <h2>Shortcode unique</h2>
        <p><code>[arome_meteo]</code> : cartes interactives, prévisions générales, orages, neige et graphiques.</p>
        <p><code>[arome_meteo code="75056" departement="75" ville="Paris" heures="48"]</code></p>
        <p><code>[arome_meteo code="66136" departement="66" ville="Perpignan" selecteur="non"]</code> : une seule ville, sans recherche.</p>
        <p>Le visiteur peut ensuite rechercher n’importe quelle commune ou saisir un code postal.</p>
    </div>
    <?php
}

function amf_base_url() {
    $url = get_option(AMF_OPTION_BASE_URL, AMF_DEFAULT_BASE_URL);
    return untrailingslashit(apply_filters('amf_national_data_base_url', $url));
}

function amf_department_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^(?:\d{2}|2A|2B)$/', $code) ? $code : '66';
}

function amf_commune_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^[0-9A-Z]{5}$/', $code) ? $code : '66136';
}

function amf_unique_identifier() {
    if (function_exists('wp_unique_id')) {
        return wp_unique_id('hkw-city-');
    }
    return 'hkw-city-' . wp_rand(1000, 999999);
}

function amf_map_variable($value) {
    $variable = strtolower(trim(sanitize_key((string) $value)));
    $allowed = array(
        'temperature',
        'temperature_ressentie',
        'point_rosee',
        'humidex',
        'pluie_1h',
        'pluie_cumul',
        'neige',
        'neige_au_sol',
        'equivalent_eau_neige',
        'graupel',
        'vent',
        'rafales',
        'pression',
        'pression_surface',
        'nebulosite',
        'nuages_bas',
        'nuages_moyens',
        'nuages_eleves',
        'humidite',
        'mucape',
        'reflectivite',
        'altitude',
    );
    return in_array($variable, $allowed, true) ? $variable : 'temperature';
}

function amf_render_map_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'variable' => 'temperature',
            'hauteur' => '700',
            'titre' => 'Cartes AROME France',
            'animation' => 'oui',
        ),
        $atts,
        'arome_meteo'
    );

    $variable = amf_map_variable($atts['variable']);
    $height = max(440, min(900, absint($atts['hauteur'])));
    $title = trim(sanitize_text_field($atts['titre']));
    if ($title === '') {
        $title = 'Cartes AROME France';
    }
    $animation_value = strtolower(trim(sanitize_text_field($atts['animation'])));
    $animation = !in_array($animation_value, array('non', '0', 'false', 'off'), true);
    $map_id = function_exists('wp_unique_id')
        ? wp_unique_id('hkw-map-')
        : 'hkw-map-' . wp_rand(1000, 999999);

    wp_enqueue_style('hkw-map');
    wp_enqueue_script('hkw-map');

    ob_start();
    ?>
    <section
        id="<?php echo esc_attr($map_id); ?>"
        class="hkw-card hkm-card"
        data-hkm-app
        data-base-url="<?php echo esc_url(amf_base_url()); ?>"
        data-variable="<?php echo esc_attr($variable); ?>"
        data-timezone="<?php echo esc_attr(wp_timezone_string()); ?>"
        data-animation="<?php echo $animation ? '1' : '0'; ?>"
        data-module-version="<?php echo esc_attr(AMF_VERSION); ?>"
        style="--hkm-height: <?php echo esc_attr($height); ?>px"
    >
        <header class="hkw-header hkm-header">
            <div>
                <p class="hkw-kicker">MODÈLE HAUTE RÉSOLUTION • ÉCHÉANCES HORAIRES</p>
                <h2><?php echo esc_html($title); ?></h2>
                <p class="hkw-meta" data-hkm-run>Chargement du dernier run AROME…</p>
            </div>
            <div class="hkw-badge">AROME<br><strong>1,3 km</strong></div>
        </header>

        <div class="hkm-toolbar">
            <div class="hkm-field hkm-layer-picker">
                <span>Paramètre</span>
                <button
                    type="button"
                    class="hkm-layer-trigger"
                    data-hkm-menu-toggle
                    aria-expanded="false"
                    aria-controls="<?php echo esc_attr($map_id . '-layers'); ?>"
                >
                    <span data-hkm-current-layer>Température à 2 m</span>
                    <span class="hkm-layer-chevron" aria-hidden="true">⌄</span>
                </button>
            </div>
            <div class="hkm-time-controls" aria-label="Navigation dans les échéances">
                <button type="button" data-hkm-previous title="Échéance précédente" aria-label="Échéance précédente">◀</button>
                <button type="button" data-hkm-play title="Lancer l’animation" aria-label="Lancer l’animation">▶</button>
                <button type="button" data-hkm-next title="Échéance suivante" aria-label="Échéance suivante">▶</button>
            </div>
            <div class="hkm-validity">
                <span>Prévision valable</span>
                <strong data-hkm-validity>—</strong>
                <small data-hkm-lead>—</small>
            </div>
        </div>

        <div
            id="<?php echo esc_attr($map_id . '-layers'); ?>"
            class="hkm-layer-menu"
            data-hkm-layer-menu
            hidden
        >
            <div class="hkm-layer-menu-head">
                <div>
                    <strong>Choisir une carte AROME</strong>
                    <small>Uniquement les paramètres disponibles dans la production Météo-France</small>
                </div>
                <button type="button" data-hkm-menu-close aria-label="Réduire le menu">×</button>
            </div>
            <div class="hkm-layer-grid" data-hkm-layer-grid></div>
        </div>

        <p class="hkw-stale" data-hkm-stale role="status" hidden>
            Attention : la dernière production disponible a plus de 8 heures.
        </p>

        <div class="hkm-viewport" data-hkm-viewport role="img" aria-label="Carte météo AROME interactive">
            <div class="hkm-scene" data-hkm-scene>
                <canvas class="hkm-weather-canvas" data-hkm-weather aria-hidden="true"></canvas>
                <canvas class="hkm-vector-canvas" data-hkm-vectors aria-hidden="true"></canvas>
            </div>
            <canvas class="hkm-label-canvas" data-hkm-labels aria-hidden="true"></canvas>
            <div class="hkm-probe" data-hkm-probe hidden>
                <strong data-hkm-probe-value>—</strong>
                <span data-hkm-probe-label>Valeur AROME</span>
            </div>
            <div class="hkm-map-titlebar">
                <strong data-hkm-map-title>Carte AROME</strong>
                <span data-hkm-map-run>Run AROME —</span>
            </div>
            <div class="hkm-map-date" data-hkm-map-date>Échéance —</div>
            <div class="hkm-map-buttons" aria-label="Commandes de zoom">
                <span class="hkm-zoom-level" data-hkm-zoom-level>100 %</span>
                <button type="button" data-hkm-zoom-in title="Agrandir" aria-label="Agrandir">+</button>
                <button type="button" data-hkm-zoom-out title="Réduire" aria-label="Réduire">−</button>
                <button type="button" data-hkm-reset title="Recentrer" aria-label="Recentrer">⌂</button>
                <button type="button" data-hkm-fullscreen title="Plein écran" aria-label="Plein écran">⛶</button>
            </div>
            <div class="hkm-legend" data-hkm-legend aria-label="Légende de la carte"></div>
            <a class="hkm-map-brand" href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">
                www.alertes-meteo.com • Module v<?php echo esc_html(AMF_VERSION); ?>
            </a>
            <div class="hkm-loading" data-hkm-loading role="status">Chargement de la carte…</div>
            <div class="hkm-error" data-hkm-error role="alert" hidden></div>
        </div>

        <div class="hkm-timeline">
            <input data-hkm-slider type="range" min="0" max="0" value="0" step="1" aria-label="Échéance de prévision">
            <div class="hkm-timeline-labels"><span>Run</span><span>Échéance maximale</span></div>
        </div>

        <footer class="hkw-footer">
            <span data-hkm-generated>Mise à jour en cours de lecture…</span>
            <span>
                Données météo directes :
                <a href="https://www.data.gouv.fr/datasets/paquets-arome-resolution-0-01deg" target="_blank" rel="noopener noreferrer">AROME 0,01° — Météo-France</a>
                • <a href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">www.alertes-meteo.com</a>
                • Module cartes v<?php echo esc_html(AMF_VERSION); ?>
            </span>
        </footer>

        <noscript>
            <p class="hkw-message hkw-error">JavaScript doit être activé pour afficher les cartes.</p>
        </noscript>
    </section>
    <?php
    return ob_get_clean();
}

function amf_render_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'ville' => 'Perpignan',
            'code' => '66136',
            'departement' => '66',
            'heures' => '48',
            'titre' => '',
            'selecteur' => 'oui',
        ),
        $atts,
        'arome_meteo'
    );

    $hours = max(1, min(48, absint($atts['heures'])));
    $city_name = sanitize_text_field($atts['ville']);
    if ($city_name === '') {
        $city_name = 'Perpignan';
    }
    $city_code = amf_commune_code($atts['code']);
    $department = amf_department_code($atts['departement']);
    $title_prefix = trim(sanitize_text_field($atts['titre']));
    if ($title_prefix === '') {
        $title_prefix = 'Prévisions AROME';
    }
    $selector_value = strtolower(trim(sanitize_text_field($atts['selecteur'])));
    $show_selector = !in_array($selector_value, array('non', '0', 'false', 'off'), true);

    $input_id = amf_unique_identifier();
    $results_id = $input_id . '-results';
    $status_id = $input_id . '-status';

    wp_enqueue_style('hkw-table');
    wp_enqueue_script('hkw-table');
    wp_enqueue_style('hkw-map');
    wp_enqueue_script('hkw-map');

    ob_start();
    ?>
    <section
        class="hkw-card hkw-national"
        data-hkw-app
        data-base-url="<?php echo esc_url(amf_base_url()); ?>"
        data-default-code="<?php echo esc_attr($city_code); ?>"
        data-default-department="<?php echo esc_attr($department); ?>"
        data-default-name="<?php echo esc_attr($city_name); ?>"
        data-hours="<?php echo esc_attr($hours); ?>"
        data-timezone="<?php echo esc_attr(wp_timezone_string()); ?>"
        data-title-prefix="<?php echo esc_attr($title_prefix); ?>"
        data-selector="<?php echo $show_selector ? '1' : '0'; ?>"
    >
        <header class="hkw-header">
            <div>
                <p class="hkw-kicker">MODÈLE HAUTE RÉSOLUTION • FRANCE MÉTROPOLITAINE</p>
                <h2 data-hkw-title><?php echo esc_html($title_prefix . ' — ' . $city_name); ?></h2>
                <p class="hkw-city-altitude" data-hkw-altitude>Altitude de <?php echo esc_html($city_name); ?> : chargement…</p>
                <p class="hkw-meta" data-hkw-meta>Chargement du dernier run AROME…</p>
            </div>
            <div class="hkw-badge">AROME<br><strong>1,3 km</strong></div>
        </header>

        <div class="hkw-toolbar" <?php if (!$show_selector) : ?>hidden<?php endif; ?>>
            <div class="hkw-search">
                <label for="<?php echo esc_attr($input_id); ?>">Choisissez votre commune</label>
                <div class="hkw-search-control">
                    <span class="hkw-search-icon" aria-hidden="true">⌕</span>
                    <input
                        id="<?php echo esc_attr($input_id); ?>"
                        class="hkw-city-input"
                        type="search"
                        value="<?php echo esc_attr($city_name); ?>"
                        placeholder="Nom de commune ou code postal"
                        autocomplete="off"
                        spellcheck="false"
                        role="combobox"
                        aria-autocomplete="list"
                        aria-expanded="false"
                        aria-controls="<?php echo esc_attr($results_id); ?>"
                        aria-describedby="<?php echo esc_attr($status_id); ?>"
                    >
                </div>
                <button type="button" class="hkw-locate-button" data-hkw-locate>📍 Détecter ma ville</button>
                <div
                    id="<?php echo esc_attr($results_id); ?>"
                    class="hkw-search-results"
                    role="listbox"
                    hidden
                ></div>
                <p
                    id="<?php echo esc_attr($status_id); ?>"
                    class="hkw-search-status"
                    role="status"
                    aria-live="polite"
                >Saisissez au moins deux lettres ou un code postal.</p>
            </div>
            <div class="hkw-coverage">
                <strong>34 746 communes</strong>
                <span>Métropole et Corse</span>
            </div>
        </div>

        <p class="hkw-stale" data-hkw-stale role="status" hidden>
            Attention : la dernière mise à jour disponible a plus de 8 heures.
        </p>

        <div class="hkw-tabs" role="tablist" aria-label="Type de prévision AROME">
            <button
                type="button"
                class="hkw-tab hkw-tab-map is-active"
                role="tab"
                aria-selected="true"
                data-hkw-tab="map"
            >🗺️ Cartes météo</button>
            <button
                type="button"
                class="hkw-tab"
                role="tab"
                aria-selected="false"
                data-hkw-tab="general"
            >🌤️ Prévisions générales</button>
            <button
                type="button"
                class="hkw-tab hkw-tab-storm"
                role="tab"
                aria-selected="false"
                data-hkw-tab="storms"
            >⛈️ Prévisions orages</button>
            <button
                type="button"
                class="hkw-tab hkw-tab-snow"
                role="tab"
                aria-selected="false"
                data-hkw-tab="snow"
            >❄️ Risque de neige</button>
        </div>

        <div class="hkw-panel hkw-map-panel" data-hkw-panel="map">
            <?php
            echo amf_render_map_shortcode(
                array(
                    'variable' => 'temperature',
                    'hauteur' => '760',
                    'titre' => 'Cartes AROME France — résolution 1,3 km',
                    'animation' => 'oui',
                )
            );
            ?>
        </div>

        <div class="hkw-panel" data-hkw-panel="general" hidden>
            <div class="hkw-table-wrap hkw-general-wrap" role="region" aria-label="Prévisions horaires générales" tabindex="0">
                <table class="hkw-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Temps</th>
                            <th scope="col">T°</th>
                            <th scope="col">Hum.</th>
                            <th scope="col">Pluie</th>
                            <th scope="col">Nuages</th>
                            <th scope="col">Vent</th>
                            <th scope="col">Rafales</th>
                            <th scope="col">Pression</th>
                        </tr>
                    </thead>
                    <tbody data-hkw-body-general>
                        <tr>
                            <td colspan="10" class="hkw-loading">Chargement des prévisions…</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <section class="hkw-charts" data-hkw-charts aria-label="Diagrammes AROME">
                <article class="hkw-chart-card">
                    <h3 data-hkw-chart-title-temperature>Diagramme températures (°C)</h3>
                    <div class="hkw-chart" data-hkw-chart-temperature></div>
                </article>
                <article class="hkw-chart-card">
                    <h3 data-hkw-chart-title-pressure>Diagramme pression ramenée au niveau de la mer (hPa)</h3>
                    <div class="hkw-chart" data-hkw-chart-pressure></div>
                </article>
                <article class="hkw-chart-card">
                    <h3 data-hkw-chart-title-rain>Diagramme précipitations (mm)</h3>
                    <p class="hkw-chart-total" data-hkw-rain-total>Précipitations cumulées : —</p>
                    <div class="hkw-chart" data-hkw-chart-rain></div>
                </article>
                <article class="hkw-chart-card">
                    <h3 data-hkw-chart-title-wind>Diagramme rafales et vent moyen</h3>
                    <div class="hkw-chart" data-hkw-chart-wind></div>
                </article>
            </section>
        </div>

        <div class="hkw-panel" data-hkw-panel="storms" hidden>
            <p class="hkw-storm-summary" data-hkw-storm-summary>
                Diagnostic convectif AROME 0,01° : chargement…
            </p>
            <div class="hkw-top-scroll" data-hkw-top-scroll="storms" aria-label="Navigation horizontale du tableau orages" hidden><div></div></div>
            <div class="hkw-table-wrap hkw-storm-wrap" data-hkw-scroll-wrap="storms" role="region" aria-label="Prévisions horaires d'orages" tabindex="0">
                <table class="hkw-table hkw-storm-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Risque orage</th>
                            <th scope="col">MUCAPE</th>
                            <th scope="col">LCL estimé</th>
                            <th scope="col">Foudre</th>
                            <th scope="col">Grêle</th>
                            <th scope="col">Pluie conv.</th>
                            <th scope="col">Graupel</th>
                            <th scope="col">Pluie 1 h</th>
                            <th scope="col">Rafales</th>
                            <th scope="col">Type</th>
                            <th scope="col">Détails</th>
                        </tr>
                    </thead>
                    <tbody data-hkw-body-storms>
                        <tr>
                            <td colspan="13" class="hkw-loading">Chargement du diagnostic orageux…</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <p class="hkw-storm-note">
                <strong>Lecture expert :</strong> la MUCAPE et la réflectivité maximale sont des sorties directes AROME. Le risque, la foudre, la grêle et le type d’orage sont des diagnostics dérivés clairement signalés ; aucune valeur indisponible n’est inventée.
            </p>
        </div>

        <div class="hkw-panel" data-hkw-panel="snow" hidden>
            <p class="hkw-snow-summary" data-hkw-snow-summary>
                Diagnostic neige AROME 0,01° : chargement…
            </p>
            <div class="hkw-top-scroll" data-hkw-top-scroll="snow" aria-label="Navigation horizontale du tableau neige" hidden><div></div></div>
            <div class="hkw-table-wrap hkw-snow-wrap" data-hkw-scroll-wrap="snow" role="region" aria-label="Risque horaire de neige" tabindex="0">
                <table class="hkw-table hkw-snow-table">
                    <thead>
                        <tr>
                            <th scope="col">Date</th>
                            <th scope="col">Heure</th>
                            <th scope="col">Risque neige</th>
                            <th scope="col">Phase</th>
                            <th scope="col">Neige 1 h</th>
                            <th scope="col">Neige 3 h</th>
                            <th scope="col">Neige 6 h</th>
                            <th scope="col">Tenue</th>
                            <th scope="col">Pres. hPa</th>
                            <th scope="col">Hum.</th>
                            <th scope="col">Vent moy. / raf.</th>
                            <th scope="col">Cumul neige fraîche</th>
                            <th scope="col">Détails</th>
                        </tr>
                    </thead>
                    <tbody data-hkw-body-snow>
                        <tr>
                            <td colspan="13" class="hkw-loading">Chargement du risque de neige…</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <p class="hkw-snow-note">
                <strong>Lecture neige :</strong> les cumuls de neige sont des sorties directes AROME. La neige fraîche et la tenue sont estimées à partir du cumul en eau, de la température à 2 m et de l’altitude du point de grille.
            </p>
        </div>

        <footer class="hkw-footer">
            <span data-hkw-generated>Mise à jour en cours de lecture…</span>
            <span>
                Données météo directes :
                <a href="https://www.data.gouv.fr/datasets/paquets-arome-resolution-0-01deg" target="_blank" rel="noopener noreferrer">AROME 0,01° — Météo-France</a>
                • Recherche des communes :
                <a href="https://geo.api.gouv.fr/decoupage-administratif/communes" target="_blank" rel="noopener noreferrer">API officielle française</a>
                • <a href="https://www.alertes-meteo.com/" target="_blank" rel="noopener noreferrer">www.alertes-meteo.com</a>
            </span>
            <span class="hkw-plugin-version">Module AROME v<?php echo esc_html(AMF_VERSION); ?></span>
        </footer>

        <noscript>
            <p class="hkw-message hkw-error">JavaScript doit être activé pour rechercher une commune.</p>
        </noscript>
    </section>
    <?php
    return ob_get_clean();
}
