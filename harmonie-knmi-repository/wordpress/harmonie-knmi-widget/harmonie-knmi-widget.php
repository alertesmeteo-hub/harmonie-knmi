<?php
/**
 * Plugin Name: Tableau HARMONIE KNMI
 * Plugin URI: https://github.com/alertesmeteo-hub/harmonie-knmi
 * Description: Tableau de prévisions horaires HARMONIE-AROME officielles du KNMI, alimenté par le JSON du dépôt GitHub.
 * Version: 1.0.0
 * Author: Alertes Météo Hub
 * Requires at least: 5.8
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 */

if (!defined('ABSPATH')) {
    exit;
}

define('HKW_VERSION', '1.0.0');
define('HKW_OPTION_URL', 'hkw_data_url');
define(
    'HKW_DEFAULT_DATA_URL',
    'https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/main/data/harmonie.json'
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
}

function hkw_register_settings() {
    register_setting(
        'hkw_settings',
        HKW_OPTION_URL,
        array(
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => HKW_DEFAULT_DATA_URL,
        )
    );

    add_settings_section(
        'hkw_main_section',
        'Source des données',
        '__return_false',
        'harmonie-knmi'
    );

    add_settings_field(
        'hkw_data_url_field',
        'Adresse du fichier JSON',
        'hkw_render_url_field',
        'harmonie-knmi',
        'hkw_main_section'
    );
}

function hkw_render_url_field() {
    $value = get_option(HKW_OPTION_URL, HKW_DEFAULT_DATA_URL);
    printf(
        '<input type="url" class="regular-text code" name="%1$s" value="%2$s" autocomplete="off">',
        esc_attr(HKW_OPTION_URL),
        esc_attr($value)
    );
    echo '<p class="description">Conservez l’adresse proposée pour le dépôt alertesmeteo-hub/harmonie-knmi.</p>';
}

function hkw_add_settings_page() {
    add_options_page(
        'Tableau HARMONIE KNMI',
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
        <h1>Tableau HARMONIE KNMI</h1>
        <form action="options.php" method="post">
            <?php
            settings_fields('hkw_settings');
            do_settings_sections('harmonie-knmi');
            submit_button();
            ?>
        </form>
        <h2>Exemples de shortcodes</h2>
        <p><code>[harmonie_table ville="Dunkerque" heures="48"]</code></p>
        <p><code>[harmonie_table ville="Calais" heures="24" titre="Prévisions pour Calais"]</code></p>
        <p>Le cache WordPress est renouvelé automatiquement toutes les 15 minutes.</p>
    </div>
    <?php
}

function hkw_data_url() {
    $url = get_option(HKW_OPTION_URL, HKW_DEFAULT_DATA_URL);
    return apply_filters('hkw_data_url', $url);
}

function hkw_get_data() {
    $url = hkw_data_url();
    if (!$url) {
        return new WP_Error('hkw_missing_url', 'L’adresse du fichier HARMONIE est absente.');
    }

    $cache_key = 'hkw_' . md5($url);
    $cached = get_transient($cache_key);
    if (is_array($cached)) {
        return $cached;
    }

    $response = wp_safe_remote_get(
        $url,
        array(
            'timeout' => 20,
            'redirection' => 3,
            'headers' => array(
                'Accept' => 'application/json',
                'User-Agent' => 'WordPress Tableau HARMONIE KNMI/' . HKW_VERSION,
            ),
        )
    );

    if (is_wp_error($response)) {
        return hkw_last_good_or_error($response);
    }

    $status = wp_remote_retrieve_response_code($response);
    if ($status !== 200) {
        return hkw_last_good_or_error(
            new WP_Error('hkw_http_error', 'Le fichier HARMONIE répond avec le code HTTP ' . $status . '.')
        );
    }

    $decoded = json_decode(wp_remote_retrieve_body($response), true);
    if (!is_array($decoded) || ($decoded['status'] ?? '') !== 'ok') {
        return hkw_last_good_or_error(
            new WP_Error('hkw_invalid_json', 'Les données HARMONIE ne sont pas encore disponibles ou sont invalides.')
        );
    }

    set_transient($cache_key, $decoded, 15 * MINUTE_IN_SECONDS);
    update_option('hkw_last_good_data', $decoded, false);
    return $decoded;
}

function hkw_last_good_or_error($error) {
    $last_good = get_option('hkw_last_good_data');
    if (is_array($last_good) && ($last_good['status'] ?? '') === 'ok') {
        return $last_good;
    }
    return $error;
}

function hkw_find_location($locations, $requested) {
    $slug = sanitize_title($requested);
    if (isset($locations[$slug])) {
        return array($slug, $locations[$slug]);
    }
    foreach ($locations as $key => $location) {
        if (sanitize_title($location['name'] ?? '') === $slug) {
            return array($key, $location);
        }
    }
    return array(null, null);
}

function hkw_float($value) {
    return is_numeric($value) ? (float) $value : null;
}

function hkw_number($value, $decimals = 0) {
    $number = hkw_float($value);
    if ($number === null) {
        return '—';
    }
    return number_format_i18n($number, $decimals);
}

function hkw_temperature_class($value) {
    $temperature = hkw_float($value);
    if ($temperature === null) {
        return '';
    }
    if ($temperature >= 35) {
        return 'hkw-temp-extreme';
    }
    if ($temperature >= 30) {
        return 'hkw-temp-hot';
    }
    if ($temperature >= 22) {
        return 'hkw-temp-warm';
    }
    if ($temperature >= 12) {
        return 'hkw-temp-mild';
    }
    if ($temperature >= 4) {
        return 'hkw-temp-cool';
    }
    return 'hkw-temp-cold';
}

function hkw_gust_class($value) {
    $gust = hkw_float($value);
    if ($gust === null) {
        return '';
    }
    if ($gust >= 80) {
        return 'hkw-gust-danger';
    }
    if ($gust >= 60) {
        return 'hkw-gust-strong';
    }
    if ($gust >= 40) {
        return 'hkw-gust-moderate';
    }
    return '';
}

function hkw_parse_time($iso) {
    try {
        return (new DateTimeImmutable($iso))->setTimezone(wp_timezone());
    } catch (Exception $exception) {
        return null;
    }
}

function hkw_human_time($iso, $format) {
    $date = hkw_parse_time($iso);
    return $date ? wp_date($format, $date->getTimestamp(), wp_timezone()) : '—';
}

function hkw_is_stale($iso, $hours = 8) {
    $timestamp = strtotime((string) $iso);
    if (!$timestamp) {
        return true;
    }
    return $timestamp < time() - absint($hours) * HOUR_IN_SECONDS;
}

function hkw_render_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'ville' => 'Dunkerque',
            'heures' => '48',
            'titre' => '',
        ),
        $atts,
        'harmonie_table'
    );

    $hours = max(1, min(60, absint($atts['heures'])));
    $data = hkw_get_data();
    if (is_wp_error($data)) {
        return '<div class="hkw-message hkw-error">' . esc_html($data->get_error_message()) . '</div>';
    }

    $locations = $data['locations'] ?? array();
    list($location_slug, $location) = hkw_find_location($locations, $atts['ville']);
    if (!$location_slug || !is_array($location)) {
        $available = array_map(
            static function ($item) {
                return $item['name'] ?? '';
            },
            $locations
        );
        return '<div class="hkw-message hkw-error">Ville inconnue. Villes disponibles : ' .
            esc_html(implode(', ', array_filter($available))) . '.</div>';
    }

    $forecast = array_values($location['forecast'] ?? array());
    $now = time() - HOUR_IN_SECONDS;
    $forecast = array_values(
        array_filter(
            $forecast,
            static function ($item) use ($now) {
                $timestamp = strtotime($item['time'] ?? '');
                return $timestamp && $timestamp >= $now;
            }
        )
    );
    $forecast = array_slice($forecast, 0, $hours);
    if (!$forecast) {
        return '<div class="hkw-message hkw-error">Aucune échéance HARMONIE disponible pour cette ville.</div>';
    }

    wp_enqueue_style('hkw-table');
    $title = trim($atts['titre']);
    if ($title === '') {
        $title = 'Prévisions HARMONIE — ' . ($location['name'] ?? $atts['ville']);
    }

    $model = $data['model'] ?? array();
    $run_time = hkw_human_time($model['run_time'] ?? '', 'd/m/Y à H\hi');
    $generated = hkw_human_time($data['generated_at'] ?? '', 'd/m/Y à H\hi');
    $is_stale = hkw_is_stale($data['generated_at'] ?? '');

    ob_start();
    ?>
    <section class="hkw-card" data-city="<?php echo esc_attr($location_slug); ?>">
        <header class="hkw-header">
            <div>
                <p class="hkw-kicker">MODÈLE HAUTE RÉSOLUTION • ÉCHÉANCES HORAIRES</p>
                <h2><?php echo esc_html($title); ?></h2>
                <p class="hkw-meta">
                    Run du <?php echo esc_html($run_time); ?> (heure du site)
                    <span aria-hidden="true">•</span>
                    résolution <?php echo esc_html(hkw_number($model['resolution_km'] ?? null, 1)); ?> km
                </p>
            </div>
            <div class="hkw-badge">HARMONIE<br><strong>AROME</strong></div>
        </header>

        <?php if ($is_stale) : ?>
            <p class="hkw-stale" role="status">
                Attention : la dernière mise à jour disponible a plus de 8 heures.
            </p>
        <?php endif; ?>

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
                <tbody>
                <?php
                $previous_day = null;
                foreach ($forecast as $item) :
                    $date = hkw_parse_time($item['time'] ?? '');
                    if (!$date) {
                        continue;
                    }
                    $day_key = $date->format('Y-m-d');
                    $new_day = $day_key !== $previous_day;
                    $previous_day = $day_key;
                    $condition = $item['condition'] ?? array();
                    $rain = hkw_float($item['precipitation_mm'] ?? null);
                    $direction = trim((string) ($item['wind_direction'] ?? ''));
                    $direction_deg = hkw_float($item['wind_direction_deg'] ?? null);
                    ?>
                    <tr class="<?php echo $new_day ? 'hkw-new-day' : ''; ?>">
                        <th scope="row" data-label="Date">
                            <?php echo esc_html(wp_date('D d/m', $date->getTimestamp(), wp_timezone())); ?>
                        </th>
                        <td data-label="Heure" class="hkw-hour">
                            <?php echo esc_html(wp_date('H\hi', $date->getTimestamp(), wp_timezone())); ?>
                        </td>
                        <td data-label="Temps" class="hkw-condition">
                            <span class="hkw-icon" aria-hidden="true"><?php echo esc_html($condition['icon'] ?? '•'); ?></span>
                            <span><?php echo esc_html($condition['label'] ?? 'Indéterminé'); ?></span>
                        </td>
                        <td data-label="Température" class="hkw-temperature <?php echo esc_attr(hkw_temperature_class($item['temperature_c'] ?? null)); ?>">
                            <?php echo esc_html(hkw_number($item['temperature_c'] ?? null, 1)); ?>&nbsp;°C
                        </td>
                        <td data-label="Humidité">
                            <?php echo esc_html(hkw_number($item['humidity_pct'] ?? null)); ?>&nbsp;%
                        </td>
                        <td data-label="Pluie" class="<?php echo ($rain !== null && $rain >= 0.1) ? 'hkw-rain' : ''; ?>">
                            <?php echo esc_html(hkw_number($item['precipitation_mm'] ?? null, 1)); ?>&nbsp;mm
                        </td>
                        <td data-label="Nuages">
                            <?php echo esc_html(hkw_number($item['cloud_cover_pct'] ?? null)); ?>&nbsp;%
                        </td>
                        <td data-label="Vent">
                            <strong><?php echo esc_html(hkw_number($item['wind_speed_kmh'] ?? null)); ?></strong>&nbsp;km/h
                            <?php if ($direction !== '') : ?>
                                <span class="hkw-direction" title="<?php echo esc_attr($direction_deg !== null ? $direction_deg . '°' : ''); ?>">
                                    <?php echo esc_html($direction); ?>
                                </span>
                            <?php endif; ?>
                        </td>
                        <td data-label="Rafales" class="<?php echo esc_attr(hkw_gust_class($item['wind_gust_kmh'] ?? null)); ?>">
                            <strong><?php echo esc_html(hkw_number($item['wind_gust_kmh'] ?? null)); ?></strong>&nbsp;km/h
                        </td>
                        <td data-label="Pression">
                            <?php echo esc_html(hkw_number($item['pressure_hpa'] ?? null)); ?>&nbsp;hPa
                        </td>
                    </tr>
                <?php endforeach; ?>
                </tbody>
            </table>
        </div>

        <footer class="hkw-footer">
            <span>Mise à jour du tableau : <?php echo esc_html($generated); ?></span>
            <span>
                Données directes :
                <a href="https://dataplatform.knmi.nl/dataset/harmonie-arome-cy43-p3-1-0" target="_blank" rel="noopener noreferrer">KNMI HARMONIE-AROME Cy43</a>
                • CC BY 4.0
            </span>
        </footer>
    </section>
    <?php
    return ob_get_clean();
}
