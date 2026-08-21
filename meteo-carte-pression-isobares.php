<?php
/**
 * Plugin Name: Alertes-Météo.com – Cartes Réel & GFS France / Europe
 * Description: Observations réelles de pression + cartes GFS 0,25° (pression/isobares, température 2 m, pluie, vent/rafales) pour la France et l'Europe.
 * Version: 1.3.8
 * Author: Alertes-Météo.com
 * Author URI: https://alertes-meteo.com/
 * License: GPL-2.0-or-later
 * Text Domain: am-carte-pression-isobares
 */

if (!defined('ABSPATH')) {
    exit;
}

final class AM_Carte_Pression_Isobares
{
    public const VERSION = '1.3.8';
    private const FRANCE_JSON_URL = 'https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/observations/classements_temperature.json';
    private const GFS_INDEX_URL = 'https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/observations/gfs/index.json';
    private const AWC_METAR_URL = 'https://aviationweather.gov/api/data/metar';
    private const AWC_METAR_CACHE_URL = 'https://aviationweather.gov/data/cache/metars.cache.csv.gz';
    private const EUROPE_CACHE_KEY = 'am_pr_europe_metar_v138';
    private const EUROPE_STALE_KEY = 'am_pr_europe_metar_stale_v138';

    private static $instance = null;
    private $assets_enqueued = false;

    public static function instance()
    {
        if (null === self::$instance) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function __construct()
    {
        add_shortcode('am_carte_pression', array($this, 'render_shortcode'));
        add_shortcode('carte_pression_isobares', array($this, 'render_shortcode'));
        add_shortcode('am_pression_isobares', array($this, 'render_shortcode'));
        add_action('rest_api_init', array($this, 'register_rest_routes'));
    }

    public function register_rest_routes()
    {
        register_rest_route(
            'am-pression/v1',
            '/europe',
            array(
                'methods'             => WP_REST_Server::READABLE,
                'callback'            => array($this, 'rest_europe'),
                'permission_callback' => '__return_true',
            )
        );
    }

    public function rest_europe()
    {
        $payload = $this->build_europe_payload();
        if (is_wp_error($payload)) {
            return $payload;
        }
        $response = rest_ensure_response($payload);
        $response->header('Cache-Control', 'public, max-age=300, stale-while-revalidate=600');
        return $response;
    }

    private function build_europe_payload()
    {
        $cached = get_transient(self::EUROPE_CACHE_KEY);
        if (is_array($cached) && !empty($cached['hourly'])) {
            return $cached;
        }

        $stations = array();
        $errors = array();

        /*
         * Source principale : cache METAR mondial AWC, mis à jour chaque minute.
         * Une seule requête remplace les 6 requêtes bbox, ce qui évite les 429/502
         * rencontrés en mode Réel ↔ GFS et respecte la recommandation officielle AWC
         * d'utiliser le cache pour les gros jeux de données.
         */
        $response = wp_remote_get(
            self::AWC_METAR_CACHE_URL,
            array(
                'timeout'     => 35,
                'redirection' => 2,
                'headers'     => array(
                    'Accept'     => 'text/csv,application/gzip,application/octet-stream,*/*;q=0.2',
                    'User-Agent' => 'Alertes-Meteo-Pression-Europe/' . self::VERSION . ' (+https://alertes-meteo.com/)',
                ),
            )
        );

        if (!is_wp_error($response)) {
            $status = (int) wp_remote_retrieve_response_code($response);
            if ($status >= 200 && $status < 300) {
                $cache_rows = $this->parse_metar_cache_csv(wp_remote_retrieve_body($response));
                foreach ($cache_rows as $normalized) {
                    $id = $normalized['id'];
                    if (!isset($stations[$id]) || $normalized['_obs_ts'] > $stations[$id]['_obs_ts']) {
                        $stations[$id] = $normalized;
                    }
                }
            } else {
                $errors[] = 'Cache METAR AWC HTTP ' . $status;
            }
        } else {
            $errors[] = 'Cache METAR AWC : ' . $response->get_error_message();
        }

        /* Secours : API décodée, uniquement si le cache mondial n'a rien donné. */
        if (empty($stations)) {
            $boxes = array(
                '-20,52,10,75',
                '10,52,40,75',
                '-20,34,10,52',
                '10,34,40,52',
            );
            foreach ($boxes as $bbox) {
                $url = add_query_arg(
                    array(
                        'bbox'           => $bbox,
                        'format'         => 'json',
                        'hoursBeforeNow' => 2,
                    ),
                    self::AWC_METAR_URL
                );
                $api_response = wp_remote_get(
                    $url,
                    array(
                        'timeout'     => 22,
                        'redirection' => 2,
                        'headers'     => array(
                            'Accept'     => 'application/json',
                            'User-Agent' => 'Alertes-Meteo-Pression-Europe/' . self::VERSION . ' (+https://alertes-meteo.com/)',
                        ),
                    )
                );
                if (is_wp_error($api_response)) {
                    $errors[] = $api_response->get_error_message();
                    continue;
                }
                $status = (int) wp_remote_retrieve_response_code($api_response);
                if (204 === $status) {
                    continue;
                }
                if ($status < 200 || $status >= 300) {
                    $errors[] = 'AviationWeather HTTP ' . $status;
                    continue;
                }
                $rows = json_decode(wp_remote_retrieve_body($api_response), true);
                if (!is_array($rows)) {
                    $errors[] = 'Réponse METAR JSON invalide';
                    continue;
                }
                foreach ($rows as $row) {
                    if (!is_array($row)) {
                        continue;
                    }
                    $normalized = $this->normalize_metar_row($row);
                    if (!$normalized) {
                        continue;
                    }
                    $id = $normalized['id'];
                    if (!isset($stations[$id]) || $normalized['_obs_ts'] > $stations[$id]['_obs_ts']) {
                        $stations[$id] = $normalized;
                    }
                }
            }
        }

        if (empty($stations)) {
            $stale = get_transient(self::EUROPE_STALE_KEY);
            if (is_array($stale) && !empty($stale['hourly'])) {
                $stale['stale'] = true;
                $stale['warning'] = 'La source METAR européenne est temporairement indisponible : dernières données en cache affichées.';
                return $stale;
            }
            return new WP_Error(
                'am_pr_europe_unavailable',
                'Impossible de récupérer les observations METAR européennes.' . (!empty($errors) ? ' ' . implode(' | ', array_unique($errors)) : ''),
                array('status' => 502)
            );
        }

        $rows = array_values($stations);
        usort($rows, function ($a, $b) {
            if ($a['lat'] === $b['lat']) {
                return $a['lon'] <=> $b['lon'];
            }
            return $b['lat'] <=> $a['lat'];
        });

        $latest = 0;
        foreach ($rows as &$row) {
            $latest = max($latest, (int) $row['_obs_ts']);
            unset($row['_obs_ts']);
        }
        unset($row);
        if (!$latest) {
            $latest = time();
        }
        $latest_iso = gmdate('c', $latest);

        $payload = array(
            'status'                => 'ok',
            'schema_version'        => 2,
            'module_version'        => self::VERSION,
            'area'                  => 'europe',
            'generated_at'          => gmdate('c'),
            'latest_observation_at' => $latest_iso,
            'source'                => array(
                'name'      => 'NOAA / Aviation Weather Center — cache METAR mondial',
                'product'   => 'metars.cache.csv.gz',
                'parameter' => 'SLP, sinon altimeter/QNH',
            ),
            'hourly' => array(
                array(
                    'utc'         => $latest_iso,
                    'local'       => $latest_iso,
                    'local_label' => 'Dernières observations METAR européennes',
                    'stations'    => count($rows),
                    'rows'        => $rows,
                ),
            ),
        );

        set_transient(self::EUROPE_CACHE_KEY, $payload, 10 * MINUTE_IN_SECONDS);
        set_transient(self::EUROPE_STALE_KEY, $payload, 6 * HOUR_IN_SECONDS);
        return $payload;
    }

    private function parse_metar_cache_csv($body)
    {
        if (!is_string($body) || '' === $body) {
            return array();
        }
        if (strlen($body) >= 2 && ord($body[0]) === 0x1f && ord($body[1]) === 0x8b && function_exists('gzdecode')) {
            $decoded = @gzdecode($body);
            if (is_string($decoded) && '' !== $decoded) {
                $body = $decoded;
            }
        }

        $lines = preg_split('/\r\n|\n|\r/', $body);
        $header = null;
        $header_index = -1;
        foreach ($lines as $i => $line) {
            if (false !== stripos($line, 'station_id') && false !== stripos($line, 'observation_time') && false !== stripos($line, 'latitude')) {
                $header = str_getcsv($line);
                $header_index = $i;
                break;
            }
        }
        if (!is_array($header) || $header_index < 0) {
            return array();
        }

        $index = array();
        foreach ($header as $i => $name) {
            $key = strtolower(trim((string) $name));
            if (!isset($index[$key])) {
                $index[$key] = $i;
            }
        }
        $get = static function ($row, $name) use ($index) {
            $key = strtolower($name);
            return isset($index[$key], $row[$index[$key]]) ? $row[$index[$key]] : null;
        };

        $out = array();
        $now = time();
        for ($i = $header_index + 1, $n = count($lines); $i < $n; $i++) {
            $line = trim((string) $lines[$i]);
            if ('' === $line || '#' === $line[0]) {
                continue;
            }
            $row = str_getcsv($line);
            if (count($row) < 5) {
                continue;
            }
            $lat = $this->number($get($row, 'latitude'));
            $lon = $this->number($get($row, 'longitude'));
            if (null === $lat || null === $lon || $lat < 30 || $lat > 75 || $lon < -35 || $lon > 50) {
                continue;
            }
            $obs_ts = $this->observation_timestamp($get($row, 'observation_time'));
            if ($obs_ts < $now - 4 * HOUR_IN_SECONDS || $obs_ts > $now + HOUR_IN_SECONDS) {
                continue;
            }
            $pressure = $this->pressure_hpa($get($row, 'sea_level_pressure_mb'));
            $pressure_source = 'SLP METAR';
            if (null === $pressure) {
                $pressure = $this->pressure_hpa($get($row, 'altim_in_hg'));
                $pressure_source = 'Altimètre / QNH METAR';
            }
            if (null === $pressure) {
                continue;
            }
            $id = sanitize_text_field((string) $get($row, 'station_id'));
            if ('' === $id) {
                continue;
            }
            $temp = $this->number($get($row, 'temp_c'));
            $dew = $this->number($get($row, 'dewpoint_c'));
            $elev = $this->number($get($row, 'elevation_m'));
            $out[] = array(
                'id'              => $id,
                'icao_id'         => $id,
                'name'            => $id,
                'lat'             => round($lat, 5),
                'lon'             => round($lon, 5),
                'altitude_m'      => null === $elev ? null : round($elev, 0),
                'pressure_msl'    => round($pressure, 1),
                'pressure_source' => $pressure_source,
                'temperature'     => null === $temp ? null : round($temp, 1),
                'dew_point'       => null === $dew ? null : round($dew, 1),
                'obs_time_utc'    => gmdate('c', $obs_ts),
                'source_network'  => 'METAR / Aviation Weather Center',
                '_obs_ts'         => $obs_ts,
            );
        }
        return $out;
    }

    private function normalize_metar_row($row)
    {
        $lat = $this->number($row['lat'] ?? null);
        $lon = $this->number($row['lon'] ?? null);
        if (null === $lat || null === $lon || $lat < 30 || $lat > 75 || $lon < -35 || $lon > 50) {
            return null;
        }

        $pressure = $this->pressure_hpa($row['slp'] ?? null);
        $pressure_source = 'SLP METAR';
        if (null === $pressure) {
            $pressure = $this->pressure_hpa($row['altim'] ?? null);
            $pressure_source = 'Altimètre / QNH METAR';
        }
        if (null === $pressure) {
            return null;
        }

        $id = sanitize_text_field((string) ($row['icaoId'] ?? $row['stationId'] ?? ''));
        if ('' === $id) {
            return null;
        }
        $name = sanitize_text_field((string) ($row['name'] ?? $id));
        $obs_ts = $this->observation_timestamp($row['obsTime'] ?? ($row['reportTime'] ?? null));
        $temp = $this->number($row['temp'] ?? null);
        $dew = $this->number($row['dewp'] ?? null);
        $elev = $this->number($row['elev'] ?? null);

        return array(
            'id'              => $id,
            'icao_id'         => $id,
            'name'            => $name,
            'lat'             => round($lat, 5),
            'lon'             => round($lon, 5),
            'altitude_m'      => null === $elev ? null : round($elev, 0),
            'pressure_msl'    => round($pressure, 1),
            'pressure_source' => $pressure_source,
            'temperature'     => null === $temp ? null : round($temp, 1),
            'dew_point'       => null === $dew ? null : round($dew, 1),
            'obs_time_utc'    => gmdate('c', $obs_ts),
            'source_network'  => 'METAR / Aviation Weather Center',
            '_obs_ts'         => $obs_ts,
        );
    }

    private function observation_timestamp($value)
    {
        if (is_numeric($value)) {
            $ts = (int) $value;
            if ($ts > 1000000000 && $ts < 5000000000) {
                return $ts;
            }
        }
        if (is_string($value) && '' !== trim($value)) {
            $ts = strtotime($value);
            if (false !== $ts) {
                return $ts;
            }
        }
        return time();
    }

    private function pressure_hpa($value)
    {
        $n = $this->number($value);
        if (null === $n) {
            return null;
        }
        if ($n > 2000) {
            $n /= 100.0;
        } elseif ($n > 20 && $n < 40) {
            $n *= 33.8638866667; // inHg -> hPa
        }
        if ($n < 850 || $n > 1100) {
            return null;
        }
        return $n;
    }

    private function number($value)
    {
        if (null === $value || '' === $value || !is_numeric($value)) {
            return null;
        }
        $n = (float) $value;
        return is_finite($n) ? $n : null;
    }

    private function enqueue_assets()
    {
        if ($this->assets_enqueued) {
            return;
        }
        if (!wp_style_is('leaflet', 'registered')) {
            wp_register_style('leaflet', plugin_dir_url(__FILE__) . 'assets/vendor/leaflet/leaflet.css', array(), '1.9.4');
        }
        if (!wp_script_is('leaflet', 'registered')) {
            wp_register_script('leaflet', plugin_dir_url(__FILE__) . 'assets/vendor/leaflet/leaflet.js', array(), '1.9.4', true);
        }
        wp_enqueue_style('leaflet');
        wp_enqueue_script('leaflet');
        wp_enqueue_style('am-carte-pression-isobares', plugin_dir_url(__FILE__) . 'assets/carte-pression-isobares.css', array('leaflet'), self::VERSION);
        wp_enqueue_script('am-carte-pression-isobares', plugin_dir_url(__FILE__) . 'assets/carte-pression-isobares.js', array('leaflet'), self::VERSION, true);
        $this->assets_enqueued = true;
    }

    public function render_shortcode($atts)
    {
        $atts = shortcode_atts(
            array(
                'titre'      => 'Cartes météo – Réel & GFS France / Europe',
                'hauteur'    => 650,
                'densite'    => 'tres-lisible',
                'intervalle' => 2,
                'zone'       => 'france',
                'mode'       => 'reel',
                'variable'   => 'pression',
            ),
            $atts,
            'am_carte_pression'
        );

        $height = max(440, min(900, absint($atts['hauteur'])));
        $allowed_densities = array('tres-lisible', 'lisible', 'dense', 'toutes');
        $density = in_array($atts['densite'], $allowed_densities, true) ? $atts['densite'] : 'tres-lisible';
        $interval = (int) $atts['intervalle'];
        if (!in_array($interval, array(2, 4, 5), true)) {
            $interval = 2;
        }
        $zone = strtolower((string) $atts['zone']);
        if (!in_array($zone, array('france', 'europe'), true)) {
            $zone = 'france';
        }
        $mode = strtolower((string) $atts['mode']);
        if (!in_array($mode, array('reel', 'gfs', 'compare'), true)) {
            $mode = 'reel';
        }
        $variable = strtolower((string) $atts['variable']);
        if (!in_array($variable, array('pression', 'temperature', 'pluie', 'vent'), true)) {
            $variable = 'pression';
        }

        $france_json_url = apply_filters('am_carte_pression_json_url', self::FRANCE_JSON_URL);
        $gfs_index_url = apply_filters('am_carte_gfs_index_url', self::GFS_INDEX_URL);
        $europe_json_url = rest_url('am-pression/v1/europe');
        $instance_id = function_exists('wp_unique_id') ? wp_unique_id('am-pr-') : 'am-pr-' . wp_rand(1000, 999999);
        $this->enqueue_assets();

        ob_start();
        ?>
        <section
            id="<?php echo esc_attr($instance_id); ?>"
            class="am-pr"
            data-json-url="<?php echo esc_url($france_json_url); ?>"
            data-europe-url="<?php echo esc_url($europe_json_url); ?>"
            data-gfs-index-url="<?php echo esc_url($gfs_index_url); ?>"
            data-map-height="<?php echo esc_attr($height); ?>"
            data-default-density="<?php echo esc_attr($density); ?>"
            data-default-interval="<?php echo esc_attr($interval); ?>"
            data-default-area="<?php echo esc_attr($zone); ?>"
            data-default-mode="<?php echo esc_attr($mode); ?>"
            data-default-variable="<?php echo esc_attr($variable); ?>"
        >
            <header class="am-pr__header">
                <div>
                    <div class="am-pr__eyebrow js-eyebrow">OBSERVATIONS MÉTÉO-FRANCE</div>
                    <h2>🗺️ <?php echo esc_html($atts['titre']); ?></h2>
                    <p class="am-pr__subtitle js-subtitle">Pression observée au niveau de la mer et isobares lissées</p>
                </div>
                <div class="am-pr__headright">
                    <a href="https://www.alertes-meteo.com/" target="_blank" rel="noopener">www.alertes-meteo.com</a>
                    <strong class="am-pr__date js-date">—</strong>
                    <button type="button" class="am-pr__refresh js-refresh">↻ Actualiser</button>
                </div>
            </header>

            <div class="am-pr__tabs" role="tablist" aria-label="Type de carte">
                <button type="button" class="am-pr__tab js-mode-tab" data-mode="reel">● Réel</button>
                <button type="button" class="am-pr__tab js-mode-tab" data-mode="gfs">GFS</button>
                <button type="button" class="am-pr__tab js-mode-tab" data-mode="compare">Réel ↔ GFS</button>
            </div>

            <div class="am-pr__summary">
                <div class="am-pr__metric"><span class="js-min-label">Minimum</span><strong class="js-min">—</strong></div>
                <div class="am-pr__metric"><span class="js-max-label">Maximum</span><strong class="js-max">—</strong></div>
                <div class="am-pr__metric"><span class="js-count-label">Points disponibles</span><strong class="js-count">—</strong></div>
                <div class="am-pr__metric"><span class="js-range-label">Amplitude</span><strong class="js-range">—</strong></div>
            </div>

            <div class="am-pr__controls">
                <label class="js-area-wrap">
                    Zone
                    <select class="js-area">
                        <option value="france">🇫🇷 France</option>
                        <option value="europe">🇪🇺 Europe</option>
                    </select>
                </label>
                <label class="js-variable-wrap">
                    Variable
                    <select class="js-variable">
                        <option value="pression">Pression / isobares</option>
                        <option value="temperature">Température 2 m</option>
                        <option value="pluie">Précipitations</option>
                        <option value="vent">Vent 10 m + rafales</option>
                    </select>
                </label>
                <label class="am-pr__period-label js-period-wrap">
                    <span class="js-period-title">Heure d’observation</span>
                    <select class="js-period"></select>
                </label>
                <label class="js-pressure-filter-wrap">
                    Pression
                    <select class="js-filter">
                        <option value="all">Toutes les valeurs</option>
                        <option value="lt:1000">Moins de 1000 hPa</option>
                        <option value="between:1000:1010">1000 à 1009 hPa</option>
                        <option value="between:1010:1020">1010 à 1019 hPa</option>
                        <option value="between:1020:1030">1020 à 1029 hPa</option>
                        <option value="ge:1030">1030 hPa et plus</option>
                    </select>
                </label>
                <label class="js-isobars-wrap">
                    Isobares
                    <select class="js-isobars">
                        <option value="on">Afficher</option>
                        <option value="off">Masquer</option>
                    </select>
                </label>
                <label class="js-interval-wrap">
                    Intervalle
                    <select class="js-interval">
                        <option value="2">2 hPa</option>
                        <option value="4">4 hPa</option>
                        <option value="5">5 hPa</option>
                    </select>
                </label>
                <label class="js-density-wrap">
                    Densité valeurs
                    <select class="js-density">
                        <option value="tres-lisible">Très lisible</option>
                        <option value="lisible">Lisible</option>
                        <option value="dense">Dense</option>
                        <option value="toutes">Toutes</option>
                    </select>
                </label>
                <label class="js-altitude-wrap">
                    Altitude stations
                    <select class="js-altitude">
                        <option value="all">Toutes les altitudes</option>
                        <option value="lt:500">Moins de 500 m</option>
                        <option value="between:500:1000">500 à 999 m</option>
                        <option value="between:1000:1500">1 000 à 1 499 m</option>
                        <option value="ge:1500">1 500 m et plus</option>
                    </select>
                </label>
                <label class="js-search-wrap">
                    Rechercher une station
                    <input type="search" class="js-search" placeholder="Nom, ICAO ou département…" />
                </label>
                <button type="button" class="am-pr__reset js-reset">⌂ Recentrer</button>
            </div>

            <div class="am-pr__notice js-notice" hidden></div>
            <div class="am-pr__mapwrap">
                <div class="am-pr__map js-map" aria-label="Carte météo interactive Réel et GFS France Europe"></div>
                <div class="am-pr__loading js-loading">Chargement…</div>
                <div class="am-pr__legend js-legend"></div>
            </div>

            <div class="am-pr__timeline js-timeline" hidden>
                <div class="am-pr__timeline-head">
                    <strong>Prévisions GFS jusqu’à J+15</strong>
                    <span class="js-timeline-label">—</span>
                </div>
                <div class="am-pr__timeline-player js-timeline-player">
                    <button type="button" class="js-prev" aria-label="Échéance précédente">◀</button>
                    <button type="button" class="js-play" aria-label="Lecture animation">▶ Lecture</button>
                    <button type="button" class="js-next" aria-label="Échéance suivante">▶</button>
                    <label class="am-pr__speed-label">
                        Vitesse
                        <select class="js-speed" aria-label="Vitesse de l’animation GFS">
                            <option value="650">Très rapide</option>
                            <option value="1000">Rapide</option>
                            <option value="1600" selected>Normale</option>
                            <option value="2600">Lente</option>
                            <option value="4000">Très lente</option>
                        </select>
                    </label>
                    <input type="range" class="js-timeline-range" min="0" max="0" step="1" value="0" aria-label="Échéance GFS" />
                </div>
                <div class="am-pr__risk-title">Indice automatique de risque sur 15 jours <small>— cliquez sur un jour pour voir ses isobares</small></div>
                <div class="am-pr__riskstrip js-riskstrip" aria-label="Risques tempête et fortes pluies sur 15 jours"></div>
                <div class="am-pr__risk-note">Faible / Modéré / Fort — indicateur GFS automatique, ne remplace pas une vigilance officielle.</div>
            </div>

            <div class="am-pr__explain js-explain">
                En mode Réel, la carte affiche les observations de surface. En mode GFS, les champs proviennent du modèle GFS 0,25° de la NOAA jusqu’à J+15, avec minimum et maximum sur l’ensemble des 15 jours. Le mode Comparaison superpose les isobares observées et GFS.
            </div>

            <footer class="am-pr__footer">
                <span>Réel France : Météo-France – DPPaquetObs V2</span>
                <span>Réel Europe : METAR – NOAA / Aviation Weather Center</span>
                <span>Prévision : NOAA/NCEP GFS 0,25°</span>
                <span>Variables GFS : PRMSL, TMP 2 m, APCP, U/V 10 m, GUST</span>
                <span>Fond : © OpenStreetMap</span>
                <span>Module <strong>v<?php echo esc_html(self::VERSION); ?></strong></span>
            </footer>
        </section>
        <?php
        return ob_get_clean();
    }
}

AM_Carte_Pression_Isobares::instance();
