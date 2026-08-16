<?php
/**
 * Plugin Name: Tableau HARMONIE KNMI France
 * Plugin URI: https://github.com/alertesmeteo-hub/harmonie-knmi
 * Description: Recherche parmi toutes les communes de France métropolitaine et prévisions horaires HARMONIE-AROME officielles du KNMI.
 * Version: 2.0.0
 * Author: Alertes Météo Hub
 * Requires at least: 5.8
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 */

if (!defined('ABSPATH')) {
    exit;
}

define('HKW_VERSION', '2.0.0');
define('HKW_OPTION_BASE_URL', 'hkw_national_data_base_url');
define(
    'HKW_DEFAULT_BASE_URL',
    'https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/data'
);

add_action('wp_enqueue_scripts', 'hkw_register_assets');
add_action('admin_init', 'hkw_register_settings');
add_action('admin_menu', 'hkw_add_settings_page');
add_shortcode('harmonie_table', 'hkw_render_shortcode');

function hkw_register_assets() {
    wp_register_style(
        'hkw-table',
        plugin_dir_url(__FILE__) . 'assets/harmonie-knmi.css',
        array(),
        HKW_VERSION
    );
    wp_register_script(
        'hkw-table',
        plugin_dir_url(__FILE__) . 'assets/harmonie-knmi.js',
        array(),
        HKW_VERSION,
        true
    );
}

function hkw_register_settings() {
    register_setting(
        'hkw_settings',
        HKW_OPTION_BASE_URL,
        array(
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => HKW_DEFAULT_BASE_URL,
        )
    );

    add_settings_section(
        'hkw_main_section',
        'Source des données nationales',
        '__return_false',
        'harmonie-knmi'
    );

    add_settings_field(
        'hkw_data_base_url_field',
        'Adresse du dossier de données',
        'hkw_render_url_field',
        'harmonie-knmi',
        'hkw_main_section'
    );
}

function hkw_render_url_field() {
    $value = get_option(HKW_OPTION_BASE_URL, HKW_DEFAULT_BASE_URL);
    printf(
        '<input type="url" class="regular-text code" name="%1$s" value="%2$s" autocomplete="off">',
        esc_attr(HKW_OPTION_BASE_URL),
        esc_attr($value)
    );
    echo '<p class="description">Conservez l’adresse proposée : elle pointe vers la branche nationale « data » du dépôt.</p>';
}

function hkw_add_settings_page() {
    add_options_page(
        'Tableau HARMONIE KNMI France',
        'HARMONIE KNMI',
        'manage_options',
        'harmonie-knmi',
        'hkw_render_settings_page'
    );
}

function hkw_render_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    ?>
    <div class="wrap">
        <h1>Tableau HARMONIE KNMI France</h1>
        <form action="options.php" method="post">
            <?php
            settings_fields('hkw_settings');
            do_settings_sections('harmonie-knmi');
            submit_button();
            ?>
        </form>
        <h2>Shortcodes</h2>
        <p><code>[harmonie_table]</code> : recherche nationale, initialisée sur Dunkerque.</p>
        <p><code>[harmonie_table code="75056" departement="75" ville="Paris" heures="48"]</code></p>
        <p>Le visiteur peut ensuite rechercher n’importe quelle commune ou saisir un code postal.</p>
    </div>
    <?php
}

function hkw_base_url() {
    $url = get_option(HKW_OPTION_BASE_URL, HKW_DEFAULT_BASE_URL);
    return untrailingslashit(apply_filters('hkw_national_data_base_url', $url));
}

function hkw_department_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^(?:\d{2}|2A|2B)$/', $code) ? $code : '59';
}

function hkw_commune_code($value) {
    $code = strtoupper(trim((string) $value));
    return preg_match('/^[0-9A-Z]{5}$/', $code) ? $code : '59183';
}

function hkw_unique_identifier() {
    if (function_exists('wp_unique_id')) {
        return wp_unique_id('hkw-city-');
    }
    return 'hkw-city-' . wp_rand(1000, 999999);
}

function hkw_render_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'ville' => 'Dunkerque',
            'code' => '59183',
            'departement' => '59',
            'heures' => '48',
            'titre' => '',
        ),
        $atts,
        'harmonie_table'
    );

    $hours = max(1, min(48, absint($atts['heures'])));
    $city_name = sanitize_text_field($atts['ville']);
    if ($city_name === '') {
        $city_name = 'Dunkerque';
    }
    $city_code = hkw_commune_code($atts['code']);
    $department = hkw_department_code($atts['departement']);
    $title_prefix = trim(sanitize_text_field($atts['titre']));
    if ($title_prefix === '') {
        $title_prefix = 'Prévisions HARMONIE';
    }

    $input_id = hkw_unique_identifier();
    $results_id = $input_id . '-results';
    $status_id = $input_id . '-status';

    wp_enqueue_style('hkw-table');
    wp_enqueue_script('hkw-table');

    ob_start();
    ?>
    <section
        class="hkw-card hkw-national"
        data-hkw-app
        data-base-url="<?php echo esc_url(hkw_base_url()); ?>"
        data-default-code="<?php echo esc_attr($city_code); ?>"
        data-default-department="<?php echo esc_attr($department); ?>"
        data-default-name="<?php echo esc_attr($city_name); ?>"
        data-hours="<?php echo esc_attr($hours); ?>"
        data-timezone="<?php echo esc_attr(wp_timezone_string()); ?>"
        data-title-prefix="<?php echo esc_attr($title_prefix); ?>"
    >
        <header class="hkw-header">
            <div>
                <p class="hkw-kicker">MODÈLE HAUTE RÉSOLUTION • FRANCE MÉTROPOLITAINE</p>
                <h2 data-hkw-title><?php echo esc_html($title_prefix . ' — ' . $city_name); ?></h2>
                <p class="hkw-meta" data-hkw-meta>Chargement du dernier run HARMONIE…</p>
            </div>
            <div class="hkw-badge">HARMONIE<br><strong>AROME</strong></div>
        </header>

        <div class="hkw-toolbar">
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

        <div class="hkw-table-wrap" role="region" aria-label="Prévisions horaires" tabindex="0">
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
                <tbody data-hkw-body>
                    <tr>
                        <td colspan="10" class="hkw-loading">Chargement des prévisions…</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <footer class="hkw-footer">
            <span data-hkw-generated>Mise à jour en cours de lecture…</span>
            <span>
                Données météo directes :
                <a href="https://dataplatform.knmi.nl/dataset/harmonie-arome-cy43-p3-1-0" target="_blank" rel="noopener noreferrer">KNMI HARMONIE-AROME Cy43</a>
                • Recherche des communes :
                <a href="https://geo.api.gouv.fr/decoupage-administratif/communes" target="_blank" rel="noopener noreferrer">API officielle française</a>
            </span>
        </footer>

        <noscript>
            <p class="hkw-message hkw-error">JavaScript doit être activé pour rechercher une commune.</p>
        </noscript>
    </section>
    <?php
    return ob_get_clean();
}
