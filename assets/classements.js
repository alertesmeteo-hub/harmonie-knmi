(function () {
    'use strict';

    var CONFIG = window.AMCO_CONFIG || {};

    var DEPARTMENTS = {
        '01': ['Ain', 'Auvergne-Rhône-Alpes'], '02': ['Aisne', 'Hauts-de-France'],
        '03': ['Allier', 'Auvergne-Rhône-Alpes'], '04': ['Alpes-de-Haute-Provence', "Provence-Alpes-Côte d'Azur"],
        '05': ['Hautes-Alpes', "Provence-Alpes-Côte d'Azur"], '06': ['Alpes-Maritimes', "Provence-Alpes-Côte d'Azur"],
        '07': ['Ardèche', 'Auvergne-Rhône-Alpes'], '08': ['Ardennes', 'Grand Est'],
        '09': ['Ariège', 'Occitanie'], '10': ['Aube', 'Grand Est'], '11': ['Aude', 'Occitanie'],
        '12': ['Aveyron', 'Occitanie'], '13': ['Bouches-du-Rhône', "Provence-Alpes-Côte d'Azur"],
        '14': ['Calvados', 'Normandie'], '15': ['Cantal', 'Auvergne-Rhône-Alpes'],
        '16': ['Charente', 'Nouvelle-Aquitaine'], '17': ['Charente-Maritime', 'Nouvelle-Aquitaine'],
        '18': ['Cher', 'Centre-Val de Loire'], '19': ['Corrèze', 'Nouvelle-Aquitaine'],
        '2A': ['Corse-du-Sud', 'Corse'], '2B': ['Haute-Corse', 'Corse'],
        '21': ["Côte-d'Or", 'Bourgogne-Franche-Comté'], '22': ["Côtes-d'Armor", 'Bretagne'],
        '23': ['Creuse', 'Nouvelle-Aquitaine'], '24': ['Dordogne', 'Nouvelle-Aquitaine'],
        '25': ['Doubs', 'Bourgogne-Franche-Comté'], '26': ['Drôme', 'Auvergne-Rhône-Alpes'],
        '27': ['Eure', 'Normandie'], '28': ['Eure-et-Loir', 'Centre-Val de Loire'],
        '29': ['Finistère', 'Bretagne'], '30': ['Gard', 'Occitanie'], '31': ['Haute-Garonne', 'Occitanie'],
        '32': ['Gers', 'Occitanie'], '33': ['Gironde', 'Nouvelle-Aquitaine'], '34': ['Hérault', 'Occitanie'],
        '35': ['Ille-et-Vilaine', 'Bretagne'], '36': ['Indre', 'Centre-Val de Loire'],
        '37': ['Indre-et-Loire', 'Centre-Val de Loire'], '38': ['Isère', 'Auvergne-Rhône-Alpes'],
        '39': ['Jura', 'Bourgogne-Franche-Comté'], '40': ['Landes', 'Nouvelle-Aquitaine'],
        '41': ['Loir-et-Cher', 'Centre-Val de Loire'], '42': ['Loire', 'Auvergne-Rhône-Alpes'],
        '43': ['Haute-Loire', 'Auvergne-Rhône-Alpes'], '44': ['Loire-Atlantique', 'Pays de la Loire'],
        '45': ['Loiret', 'Centre-Val de Loire'], '46': ['Lot', 'Occitanie'],
        '47': ['Lot-et-Garonne', 'Nouvelle-Aquitaine'], '48': ['Lozère', 'Occitanie'],
        '49': ['Maine-et-Loire', 'Pays de la Loire'], '50': ['Manche', 'Normandie'],
        '51': ['Marne', 'Grand Est'], '52': ['Haute-Marne', 'Grand Est'],
        '53': ['Mayenne', 'Pays de la Loire'], '54': ['Meurthe-et-Moselle', 'Grand Est'],
        '55': ['Meuse', 'Grand Est'], '56': ['Morbihan', 'Bretagne'], '57': ['Moselle', 'Grand Est'],
        '58': ['Nièvre', 'Bourgogne-Franche-Comté'], '59': ['Nord', 'Hauts-de-France'],
        '60': ['Oise', 'Hauts-de-France'], '61': ['Orne', 'Normandie'],
        '62': ['Pas-de-Calais', 'Hauts-de-France'], '63': ['Puy-de-Dôme', 'Auvergne-Rhône-Alpes'],
        '64': ['Pyrénées-Atlantiques', 'Nouvelle-Aquitaine'], '65': ['Hautes-Pyrénées', 'Occitanie'],
        '66': ['Pyrénées-Orientales', 'Occitanie'], '67': ['Bas-Rhin', 'Grand Est'],
        '68': ['Haut-Rhin', 'Grand Est'], '69': ['Rhône', 'Auvergne-Rhône-Alpes'],
        '70': ['Haute-Saône', 'Bourgogne-Franche-Comté'], '71': ['Saône-et-Loire', 'Bourgogne-Franche-Comté'],
        '72': ['Sarthe', 'Pays de la Loire'], '73': ['Savoie', 'Auvergne-Rhône-Alpes'],
        '74': ['Haute-Savoie', 'Auvergne-Rhône-Alpes'], '75': ['Paris', 'Île-de-France'],
        '76': ['Seine-Maritime', 'Normandie'], '77': ['Seine-et-Marne', 'Île-de-France'],
        '78': ['Yvelines', 'Île-de-France'], '79': ['Deux-Sèvres', 'Nouvelle-Aquitaine'],
        '80': ['Somme', 'Hauts-de-France'], '81': ['Tarn', 'Occitanie'],
        '82': ['Tarn-et-Garonne', 'Occitanie'], '83': ['Var', "Provence-Alpes-Côte d'Azur"],
        '84': ['Vaucluse', "Provence-Alpes-Côte d'Azur"], '85': ['Vendée', 'Pays de la Loire'],
        '86': ['Vienne', 'Nouvelle-Aquitaine'], '87': ['Haute-Vienne', 'Nouvelle-Aquitaine'],
        '88': ['Vosges', 'Grand Est'], '89': ['Yonne', 'Bourgogne-Franche-Comté'],
        '90': ['Territoire de Belfort', 'Bourgogne-Franche-Comté'], '91': ['Essonne', 'Île-de-France'],
        '92': ['Hauts-de-Seine', 'Île-de-France'], '93': ['Seine-Saint-Denis', 'Île-de-France'],
        '94': ['Val-de-Marne', 'Île-de-France'], '95': ["Val-d'Oise", 'Île-de-France']
    };

    var PARAMETERS = [
        {
            group: 'Température / Ressenti',
            items: [
                p('temperature', 'Température actuelle', '°C', 1, ['temperature', 'temp', 't', 't2m', 'temperature_actuelle']),
                p('temp_change_1h', 'Evol. T° sur 1h', '°C', 1, ['temp_change_1h', 'temperature_change_1h', 'evolution_temperature_1h', 'variation_1h']),
                p('temp_change_24h', 'Evol. T° sur 24h', '°C', 1, ['temp_change_24h', 'temperature_change_24h', 'evolution_temperature_24h', 'variation_24h']),
                p('temp_min_24h', 'Température minimale (24h glissantes)', '°C', 1, ['temp_min_24h', 'tmin_24h', 'temperature_min_24h']),
                p('temp_max_24h', 'Température maximale (24h glissantes)', '°C', 1, ['temp_max_24h', 'tmax_24h', 'temperature_max_24h']),
                p('temp_min_clim_18', 'Température minimale (climatologique - 18h -> 18h UTC)', '°C', 1, ['temp_min_clim_18', 'tmin_clim', 'tn_clim', 'tmin_18_18']),
                p('temp_max_clim_06', 'Température maximale (climatologique - 06h -> 06h UTC)', '°C', 1, ['temp_max_clim_06', 'tmax_clim', 'tx_clim', 'tmax_06_06']),
                p('windchill', 'Windchill', '°C', 1, ['windchill', 'temperature_ressentie', 'feels_like', 'refroidissement_eolien']),
                p('humidex', 'Humidex', '', 1, ['humidex']),
                p('dew_point', 'Point de rosée', '°C', 1, ['dew_point', 'dewpoint', 'td', 'point_rosee'])
            ]
        },
        {
            group: 'Normales',
            items: [
                p('anomaly_tmax_clim', 'Écart à la T° max. moyenne (climato)', '°C', 1, ['anomaly_tmax_clim', 'ecart_tmax_moyenne_climato']),
                p('anomaly_tmin_clim', 'Écart à la T° min. moyenne (climato)', '°C', 1, ['anomaly_tmin_clim', 'ecart_tmin_moyenne_climato']),
                p('anomaly_tmax_24h', 'Écart à la T° max. moyenne (24h glissantes)', '°C', 1, ['anomaly_tmax_24h', 'ecart_tmax_moyenne_24h']),
                p('anomaly_tmin_24h', 'Écart à la T° min. moyenne (24h glissantes)', '°C', 1, ['anomaly_tmin_24h', 'ecart_tmin_moyenne_24h']),
                p('monthly_record_tmax_gap', 'Écart aux records mensuels T° max (climato)', '°C', 1, ['monthly_record_tmax_gap', 'ecart_record_mensuel_tmax']),
                p('monthly_record_tmin_gap', 'Écart aux records mensuels T° min (climato)', '°C', 1, ['monthly_record_tmin_gap', 'ecart_record_mensuel_tmin']),
                p('absolute_record_tmax_gap', 'Écart aux records absolus T° max (climato)', '°C', 1, ['absolute_record_tmax_gap', 'ecart_record_absolu_tmax']),
                p('absolute_record_tmin_gap', 'Écart aux records absolus T° min (climato)', '°C', 1, ['absolute_record_tmin_gap', 'ecart_record_absolu_tmin'])
            ]
        },
        {
            group: 'Vent',
            items: [
                p('wind_speed', 'Vent moyen', 'km/h', 0, ['wind_speed', 'wind', 'ff', 'vent_moyen', 'vitesse_vent']),
                p('wind_gust', 'Vent rafales', 'km/h', 0, ['wind_gust', 'gust', 'fx', 'rafale', 'rafales']),
                p('wind_gust_max_24h', 'Vent rafales max. sur 24h', 'km/h', 0, ['wind_gust_max_24h', 'gust_max_24h', 'fx_24h', 'rafale_max_24h'])
            ]
        },
        {
            group: 'Précipitations',
            items: [
                p('rain_1h', 'Pluie sur la dernière heure', 'mm', 1, ['rain_1h', 'rr1', 'precipitation_1h', 'pluie_1h']),
                p('rain_24h', 'Pluie sur les dernières 24 heures', 'mm', 1, ['rain_24h', 'rr24', 'precipitation_24h', 'pluie_24h']),
                p('rain_48h', 'Pluie sur les dernières 48 heures', 'mm', 1, ['rain_48h', 'rr48', 'precipitation_48h', 'pluie_48h']),
                p('rain_72h', 'Pluie sur les dernières 72 heures', 'mm', 1, ['rain_72h', 'rr72', 'precipitation_72h', 'pluie_72h'])
            ]
        },
        {
            group: 'Conditions atmosphériques',
            items: [
                p('pressure_msl', 'Pression au niveau de la mer', 'hPa', 1, ['pressure_msl', 'pressure', 'pmer', 'pression_mer', 'pression']),
                p('pressure_change_3h', 'Variation de pression sur 3h', 'hPa', 1, ['pressure_change_3h', 'variation_pression_3h']),
                p('pressure_change_12h', 'Variation de pression sur 12h', 'hPa', 1, ['pressure_change_12h', 'variation_pression_12h']),
                p('pressure_change_24h', 'Variation de pression sur 24h', 'hPa', 1, ['pressure_change_24h', 'variation_pression_24h']),
                p('humidity', 'Humidité relative', '%', 0, ['humidity', 'relative_humidity', 'u', 'humidite']),
                p('visibility', 'Visibilité', 'km', 1, ['visibility_km', 'visibility', 'vv', 'visibilite']),
                p('snow_depth', 'Hauteur de neige', 'cm', 0, ['snow_depth_cm', 'snow_depth', 'neige_hauteur', 'hauteur_neige']),
                p('sunshine_24h', 'Ensoleillement sur les dernières 24 heures', 'h', 1, ['sunshine_24h', 'sun_duration_24h', 'ensoleillement_24h'])
            ]
        }
    ];

    var PARAMETER_MAP = {};
    PARAMETERS.forEach(function (group) {
        group.items.forEach(function (parameter) {
            PARAMETER_MAP[parameter.key] = parameter;
        });
    });

    function p(key, label, unit, precision, aliases) {
        return { key: key, label: label, unit: unit, precision: precision, aliases: aliases };
    }

    function getPath(object, path) {
        if (!object || typeof object !== 'object') {
            return undefined;
        }

        var parts = String(path).split('.');
        var value = object;
        for (var index = 0; index < parts.length; index += 1) {
            if (value === null || typeof value !== 'object' || !(parts[index] in value)) {
                return undefined;
            }
            value = value[parts[index]];
        }
        return value;
    }

    function firstValue(sources, aliases) {
        for (var sourceIndex = 0; sourceIndex < sources.length; sourceIndex += 1) {
            var source = sources[sourceIndex];
            for (var aliasIndex = 0; aliasIndex < aliases.length; aliasIndex += 1) {
                var value = getPath(source, aliases[aliasIndex]);
                if (value !== undefined && value !== null && value !== '') {
                    return value;
                }
            }
        }
        return undefined;
    }

    function toNumber(value) {
        if (typeof value === 'number') {
            return Number.isFinite(value) ? value : null;
        }
        if (typeof value !== 'string') {
            return null;
        }
        var normalized = value.trim().replace(/\s/g, '').replace(',', '.').replace(/[^0-9+\-.]/g, '');
        if (!normalized || normalized === '-' || normalized === '+') {
            return null;
        }
        var parsed = Number(normalized);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function numericFromSources(sources, aliases) {
        return toNumber(firstValue(sources, aliases));
    }

    function cleanText(value, fallback) {
        if (value === undefined || value === null || value === '') {
            return fallback || '';
        }
        return String(value).replace(/\s+/g, ' ').trim();
    }

    function normalizeDepartmentCode(value) {
        var code = cleanText(value).toUpperCase().replace(/^FR[-_]?/, '');
        if (/^[1-9]$/.test(code)) {
            code = '0' + code;
        }
        if (/^20$/.test(code)) {
            return '2A/2B';
        }
        return code;
    }

    function normalizeNetwork(value) {
        var original = cleanText(value, 'Réseau inconnu');
        var normalized = original.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');

        if (normalized === 'mf' || normalized === 'principal' || normalized === 'secondaire' || normalized.indexOf('meteo-france') !== -1 || normalized.indexOf('meteo france') !== -1) {
            return { key: 'mf', label: 'Météo-France' };
        }
        if (normalized.indexOf('irm') !== -1) {
            return { key: 'irm', label: 'IRM' };
        }
        if (normalized.indexOf('mae') !== -1) {
            return { key: 'ic_mae', label: 'Infoclimat (MAE)' };
        }
        if (normalized === 'ic' || normalized === 'amateur' || normalized.indexOf('infoclimat') !== -1 || normalized.indexOf('static') !== -1) {
            return { key: 'ic_static', label: 'Infoclimat (StatIC)' };
        }

        return {
            key: 'other_' + normalized.replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
            label: original
        };
    }

    function normalizeUrl(value) {
        try {
            var url = new URL(cleanText(value));
            return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
        } catch (error) {
            return '';
        }
    }

    function asTimestamp(value) {
        if (typeof value === 'number') {
            return value > 100000000000 ? value : value * 1000;
        }
        var parsed = Date.parse(value);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function formatDate(value) {
        if (!value) {
            return '—';
        }
        var timestamp = asTimestamp(value);
        if (timestamp === null) {
            return cleanText(value, '—');
        }
        try {
            return new Intl.DateTimeFormat(CONFIG.locale || 'fr-FR', {
                day: '2-digit', month: '2-digit', year: 'numeric',
                hour: '2-digit', minute: '2-digit', hour12: false,
                timeZone: CONFIG.timeZone || 'Europe/Paris'
            }).format(new Date(timestamp)).replace(',', '');
        } catch (error) {
            return new Date(timestamp).toLocaleString('fr-FR');
        }
    }

    function formatTime(value) {
        if (!value) {
            return '';
        }
        var timestamp = asTimestamp(value);
        if (timestamp === null) {
            var match = cleanText(value).match(/(?:^|\s)([0-2]?\d[:h][0-5]\d)(?:\s|$)/);
            return match ? match[1].replace('h', ':') : '';
        }
        try {
            return new Intl.DateTimeFormat(CONFIG.locale || 'fr-FR', {
                hour: '2-digit', minute: '2-digit', hour12: false,
                timeZone: CONFIG.timeZone || 'Europe/Paris'
            }).format(new Date(timestamp));
        } catch (error) {
            return '';
        }
    }

    function extractStations(payload) {
        if (Array.isArray(payload)) {
            return payload;
        }
        if (!payload || typeof payload !== 'object') {
            return [];
        }

        var candidates = ['stations', 'observations', 'data', 'results', 'rows', 'items', 'records', 'classement', 'ranking', 'features'];
        for (var index = 0; index < candidates.length; index += 1) {
            var candidate = payload[candidates[index]];
            if (Array.isArray(candidate)) {
                if (candidates[index] === 'features') {
                    return candidate.map(function (feature) {
                        var properties = feature && feature.properties ? feature.properties : {};
                        if (feature && feature.geometry && Array.isArray(feature.geometry.coordinates)) {
                            properties = Object.assign({}, properties, {
                                longitude: feature.geometry.coordinates[0],
                                latitude: feature.geometry.coordinates[1]
                            });
                        }
                        return properties;
                    });
                }
                return candidate;
            }
            if (candidate && typeof candidate === 'object') {
                return Object.keys(candidate).map(function (key) {
                    var item = candidate[key];
                    return item && typeof item === 'object' ? Object.assign({ id: key }, item) : null;
                }).filter(Boolean);
            }
        }

        var ignored = ['generated_at', 'updated_at', 'timestamp', 'meta', 'metadata', 'version', 'sample'];
        var keys = Object.keys(payload).filter(function (key) { return ignored.indexOf(key) === -1; });
        if (keys.length && keys.every(function (key) { return payload[key] && typeof payload[key] === 'object'; })) {
            return keys.map(function (key) { return Object.assign({ id: key }, payload[key]); });
        }
        return [];
    }

    function identityText(value) {
        return cleanText(value).toLowerCase().normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .trim();
    }

    function hasStationIdentity(row) {
        row = row || {};
        var name = firstValue([row], ['name', 'station_name', 'station', 'nom', 'nom_usuel', 'libelle']);
        if (name) {
            return true;
        }
        var id = cleanText(firstValue([row], ['id', 'station_id', 'code', 'numer_sta', 'num_poste', 'station_code']));
        return /\d{5,}/.test(id);
    }

    function departmentMetadata(raw) {
        var sources = [raw || {}, raw && raw.values ? raw.values : {}];
        var code = normalizeDepartmentCode(firstValue(sources, [
            'department_code', 'departement_code', 'code_departement', 'dept_code', 'dept',
            'department.code', 'departement.code'
        ]));
        var name = cleanText(firstValue(sources, [
            'department_name', 'departement_nom', 'nom_departement', 'department.name',
            'departement.name', 'department', 'departement'
        ]));

        if (!code) {
            var stationId = cleanText(firstValue(sources, [
                'id', 'station_id', 'code', 'numer_sta', 'num_poste', 'station_code', 'geo_id_insee'
            ])).replace(/\D/g, '');
            if (stationId.length >= 2) {
                code = normalizeDepartmentCode(stationId.slice(0, 2));
            }
        }

        if (!code && name) {
            var match = name.match(/\((2A\/2B|2A|2B|\d{1,3})\)/i);
            if (match) {
                code = normalizeDepartmentCode(match[1]);
                name = name.replace(match[0], '').trim();
            }
        }
        if (/^[0-9]{1,3}$|^2[AB]$|^2A\/2B$/i.test(name)) {
            code = code || normalizeDepartmentCode(name);
            name = '';
        }

        return { code: code, name: name };
    }

    function createStationMerger() {
        var stations = [];
        var byId = new Map();
        var byName = new Map();

        function upsert(raw, values, observedAt, historyEntry, defaultNetwork) {
            raw = raw && typeof raw === 'object' ? raw : {};
            values = values && typeof values === 'object' ? values : {};
            var sources = [raw, raw.values && typeof raw.values === 'object' ? raw.values : {}];
            var id = cleanText(firstValue(sources, ['id', 'station_id', 'code', 'numer_sta', 'num_poste', 'station_code', 'geo_id_insee']));
            var name = cleanText(firstValue(sources, ['name', 'station_name', 'station', 'nom', 'nom_usuel', 'libelle', 'city', 'ville']), id || 'Station sans nom');
            var department = departmentMetadata(raw);
            var nameKey = identityText(name);
            var detailedNameKey = nameKey + '|' + identityText(department.code || department.name);
            var station = (id && byId.get(identityText(id))) || byName.get(detailedNameKey) || byName.get(nameKey);

            if (!station) {
                station = {
                    id: id || 'station-auto-' + (stations.length + 1),
                    name: name,
                    department_code: department.code,
                    department_name: department.name,
                    region: cleanText(firstValue(sources, ['region', 'region_name', 'nom_region', 'region.name'])),
                    network: cleanText(firstValue(sources, ['network', 'reseau', 'network_name', 'source', 'provider']), defaultNetwork || 'Réseau inconnu'),
                    quality: firstValue(sources, ['quality', 'quality_class', 'classe', 'classe_site']),
                    site_quality: firstValue(sources, ['site_quality', 'quality_by_parameter', 'qualite_site']) || {},
                    altitude: firstValue(sources, ['altitude', 'elevation', 'alt', 'altitude_m']),
                    url: firstValue(sources, ['url', 'station_url', 'link', 'lien']),
                    observed_at: observedAt || firstValue(sources, ['observed_at', 'obs_time_utc', 'observation_time', 'datetime', 'date', 'time', 'timestamp', 'validity_time']),
                    values: {},
                    history: []
                };
                stations.push(station);
            } else {
                station.department_code = station.department_code || department.code;
                station.department_name = station.department_name || department.name;
                station.region = station.region || cleanText(firstValue(sources, ['region', 'region_name', 'nom_region', 'region.name']));
                station.altitude = station.altitude || firstValue(sources, ['altitude', 'elevation', 'alt', 'altitude_m']);
                station.url = station.url || firstValue(sources, ['url', 'station_url', 'link', 'lien']);
                var incomingQuality = firstValue(sources, ['site_quality', 'quality_by_parameter', 'qualite_site']);
                if (incomingQuality && typeof incomingQuality === 'object') {
                    station.site_quality = Object.assign({}, station.site_quality || {}, incomingQuality);
                }
                if (station.network === 'Réseau inconnu') {
                    station.network = cleanText(firstValue(sources, ['network', 'reseau', 'network_name', 'source', 'provider']), defaultNetwork || station.network);
                }
            }

            Object.keys(values).forEach(function (key) {
                if (/_time$/.test(key)) {
                    if (values[key]) {
                        station.values[key] = values[key];
                    }
                    return;
                }
                var numeric = toNumber(values[key]);
                if (numeric !== null) {
                    station.values[key] = numeric;
                }
            });

            var candidateTime = asTimestamp(observedAt);
            var currentTime = asTimestamp(station.observed_at);
            if (candidateTime !== null && (currentTime === null || candidateTime > currentTime)) {
                station.observed_at = observedAt;
            }
            if (historyEntry && typeof historyEntry === 'object') {
                station.history.push(historyEntry);
            }

            if (id) {
                byId.set(identityText(id), station);
            }
            if (nameKey) {
                byName.set(nameKey, station);
                byName.set(detailedNameKey, station);
            }
            return station;
        }

        return { stations: stations, upsert: upsert };
    }

    function adaptTemperatureSource(source, merger) {
        if (!source || typeof source !== 'object') {
            return;
        }

        var hourly = Array.isArray(source.hourly) ? source.hourly.slice() : [];
        hourly.sort(function (left, right) {
            return (asTimestamp(right.utc || right.local) || 0) - (asTimestamp(left.utc || left.local) || 0);
        });
        // Météo-France publie parfois l'heure la plus récente avant qu'elle soit
        // remplie (rows vide en tout début d'heure) : on prend comme "valeur
        // actuelle" la première heure non vide, pas systématiquement l'index 0,
        // sinon toutes les stations perdent leur valeur courante le temps que
        // l'heure se remplisse.
        var currentHourIndex = hourly.findIndex(function (hour) {
            return hour && Array.isArray(hour.rows) && hour.rows.length > 0;
        });
        hourly.forEach(function (hour, hourIndex) {
            var rows = hour && Array.isArray(hour.rows) ? hour.rows : [];
            rows.forEach(function (row) {
                var observationTime = row.obs_time_utc || row.obs_time_local || hour.utc || hour.local;
                var observationValues = {
                    temperature: row.value,
                    dew_point: firstValue([row], ['dew_point', 'dewpoint', 'td']),
                    humidity: firstValue([row], ['humidity', 'relative_humidity', 'u']),
                    pressure_msl: firstValue([row], ['pressure_msl', 'pmer']),
                    pressure: firstValue([row], ['pressure', 'pres']),
                    wind_speed: firstValue([row], ['wind_speed', 'ff']),
                    wind_direction: firstValue([row], ['wind_direction', 'dd']),
                    visibility_km: firstValue([row], ['visibility_km', 'visibility', 'vv']),
                    snow_depth_cm: firstValue([row], ['snow_depth_cm', 'snow_depth', 'ht_neige']),
                    sunshine_1h_minutes: firstValue([row], ['sunshine_1h_minutes', 'sunshine_minutes', 'insolh']),
                    normal_tmax_month: row.normal_tmax_month,
                    normal_tmin_month: row.normal_tmin_month,
                    record_month_tmax: row.record_month_tmax,
                    record_month_tmin: row.record_month_tmin,
                    record_absolute_tmax: row.record_absolute_tmax,
                    record_absolute_tmin: row.record_absolute_tmin
                };
                var currentValues = {};
                if (hourIndex === currentHourIndex) {
                    Object.keys(observationValues).forEach(function (key) {
                        currentValues[key] = observationValues[key];
                        currentValues[key + '_time'] = observationTime;
                    });
                }
                merger.upsert(
                    Object.assign({ network: 'Météo-France' }, row),
                    currentValues,
                    observationTime,
                    { observed_at: observationTime, values: observationValues },
                    'Météo-France'
                );
            });
        });

        var tableMappings = {
            tn_provisoire: 'temp_min_24h',
            tx_provisoire: 'temp_max_24h',
            tn_finales: 'temp_min_clim_18',
            tx_finales: 'temp_max_clim_06'
        };
        var tables = source.tables && typeof source.tables === 'object' ? source.tables : {};
        Object.keys(tableMappings).forEach(function (tableKey) {
            var table = tables[tableKey];
            var rows = table && Array.isArray(table.rows) ? table.rows : [];
            rows.forEach(function (row) {
                var valueKey = tableMappings[tableKey];
                var observationTime = row.obs_time_utc || row.obs_time_local || table.period_end_utc || source.generated_at;
                var values = {};
                values[valueKey] = row.value;
                values[valueKey + '_time'] = observationTime;
                merger.upsert(Object.assign({ network: 'Météo-France' }, row), values, observationTime, null, 'Météo-France');
            });
        });

        if (!hourly.length) {
            extractStations(source).forEach(function (row) {
                merger.upsert(row, row.values || row, row.observed_at || source.generated_at, null, 'Météo-France');
            });
        }
    }

    function rainValues(row, periodHint) {
        row = row && typeof row === 'object' ? row : {};
        var sources = [row, row.values || {}, row.current || {}, row.observations || {}];
        var definitions = {
            rain_1h: ['rain_1h', 'rr1h', 'rr1', 'pluie_1h', 'cumul_1h'],
            rain_24h: ['rain_24h', 'rr24h', 'rr24', 'pluie_24h', 'cumul_24h'],
            rain_48h: ['rain_48h', 'rr48h', 'rr48', 'pluie_48h', 'cumul_48h'],
            rain_72h: ['rain_72h', 'rr72h', 'rr72', 'pluie_72h', 'cumul_72h']
        };
        var output = {};

        Object.keys(definitions).forEach(function (key) {
            var value = numericFromSources(sources, definitions[key]);
            if (value !== null) {
                output[key] = value;
            }
        });

        var nested = [row.periods, row.cumuls, row.accumulations].filter(function (item) {
            return item && typeof item === 'object';
        });
        var periodKeys = { '1h': 'rain_1h', '24h': 'rain_24h', '48h': 'rain_48h', '72h': 'rain_72h' };
        nested.forEach(function (container) {
            Object.keys(periodKeys).forEach(function (period) {
                var item = container[period];
                var value = item && typeof item === 'object'
                    ? numericFromSources([item], ['rain', 'pluie', 'cumul', 'value', 'rr'])
                    : toNumber(item);
                if (value !== null) {
                    output[periodKeys[period]] = value;
                }
            });
        });

        var hint = identityText(periodHint).replace(/\s/g, '');
        Object.keys(periodKeys).forEach(function (period) {
            if (hint === period || hint.indexOf(period) !== -1) {
                var hinted = numericFromSources(sources, ['rain', 'pluie', 'cumul', 'value', 'rr', 'rr_per']);
                if (hinted !== null) {
                    output[periodKeys[period]] = hinted;
                }
            }
        });
        return output;
    }

    function adaptRainSource(source, merger) {
        if (!source || typeof source !== 'object') {
            return;
        }
        var directRows = extractStations(source).filter(hasStationIdentity);
        directRows.forEach(function (row) {
            var observedAt = firstValue([row, row.values || {}], ['observed_at', 'validity_time', 'time', 'datetime', 'date', 'timestamp', 'dernier_releve']) || source.generated_at;
            var values = rainValues(row, '');
            Object.keys(values).forEach(function (key) { values[key + '_time'] = observedAt; });
            merger.upsert(row, values, observedAt, null, 'Météo-France');
        });

        ['periods', 'modes', 'tables'].forEach(function (containerKey) {
            var container = source[containerKey];
            if (!container || typeof container !== 'object') {
                return;
            }
            Object.keys(container).forEach(function (periodKey) {
                var period = container[periodKey];
                var rows = period && Array.isArray(period.rows) ? period.rows : extractStations(period);
                rows.forEach(function (row) {
                    var observedAt = firstValue([row], ['observed_at', 'validity_time', 'time', 'datetime', 'date', 'timestamp']) || period.generated_at || source.generated_at;
                    var values = rainValues(row, periodKey);
                    Object.keys(values).forEach(function (key) { values[key + '_time'] = observedAt; });
                    merger.upsert(row, values, observedAt, null, 'Météo-France');
                });
            });
        });
    }

    function adaptWindSource(source, merger) {
        if (!source || typeof source !== 'object') {
            return;
        }
        var modes = source.modes && typeof source.modes === 'object' ? source.modes : {};
        var mappings = {
            rafales_1h: 'wind_gust',
            rafales_24h: 'wind_gust_max_24h',
            rafales_jour: 'wind_gust_max_24h'
        };

        Object.keys(mappings).forEach(function (modeKey) {
            var mode = modes[modeKey];
            var rows = mode && Array.isArray(mode.rows) ? mode.rows : [];
            rows.forEach(function (row) {
                var observedAt = firstValue([row], ['observed_at', 'time', 'datetime', 'date', 'timestamp']) || source.generated_at;
                var value = numericFromSources([row], modeKey === 'rafales_jour'
                    ? ['rafale_jour_kmh', 'rafale_kmh', 'value']
                    : ['rafale_kmh', 'rafale_jour_kmh', 'value']);
                var values = {};
                values[mappings[modeKey]] = value;
                values[mappings[modeKey] + '_time'] = observedAt;
                merger.upsert(row, values, observedAt, null, 'Météo-France');
            });
        });

        if (!Object.keys(modes).length) {
            extractStations(source).forEach(function (row) {
                var observedAt = firstValue([row], ['observed_at', 'time', 'datetime', 'date', 'timestamp']) || source.generated_at;
                var windTime = firstValue([row], ['latest_mean_wind_time', 'observed_at', 'time', 'datetime']) || observedAt;
                var gustTime = firstValue([row], ['latest_gust_time', 'observed_at', 'time', 'datetime']) || observedAt;
                var gust24Time = firstValue([row], ['gust_24h_time', 'observed_at', 'time', 'datetime']) || observedAt;
                merger.upsert(row, {
                    wind_speed: firstValue([row], ['latest_mean_wind_kmh', 'wind_speed', 'wind', 'ff', 'vent_moyen']),
                    wind_speed_time: windTime,
                    wind_gust: firstValue([row], ['latest_gust_kmh', 'wind_gust', 'gust', 'fx', 'rafale', 'rafale_kmh']),
                    wind_gust_time: gustTime,
                    wind_gust_max_24h: firstValue([row], ['gust_24h_kmh', 'wind_gust_max_24h', 'gust_max_24h', 'rafale_max_24h']),
                    wind_gust_max_24h_time: gust24Time
                }, observedAt, null, 'Météo-France');
            });
        }
    }

    function adaptAutomaticPayload(payload) {
        if (!payload || !payload.sources || typeof payload.sources !== 'object') {
            return payload;
        }

        var merger = createStationMerger();
        adaptTemperatureSource(payload.sources.temperature, merger);
        adaptRainSource(payload.sources.rain, merger);
        adaptWindSource(payload.sources.wind, merger);

        var latest = 0;
        Object.keys(payload.sources).forEach(function (key) {
            var source = payload.sources[key];
            var candidate = firstValue([source], ['generated_at', 'latest_observation_at', 'updated_at', 'timestamp']);
            var timestamp = asTimestamp(candidate);
            latest = timestamp !== null && timestamp > latest ? timestamp : latest;
        });

        return {
            generated_at: latest || '',
            stations: merger.stations
        };
    }

    function generatedAt(payload, stations) {
        var direct = firstValue([payload], ['generated_at', 'updated_at', 'observation_time', 'timestamp', 'date', 'meta.generated_at', 'metadata.generated_at']);
        if (direct) {
            return direct;
        }
        var latest = stations.reduce(function (maximum, station) {
            var timestamp = asTimestamp(station.observedAt);
            return timestamp !== null && timestamp > maximum ? timestamp : maximum;
        }, 0);
        return latest || '';
    }

    function normalizeStation(raw, index) {
        raw = raw && typeof raw === 'object' ? raw : {};
        var values = raw.values && typeof raw.values === 'object' ? raw.values : {};
        var observations = raw.observations && typeof raw.observations === 'object' && !Array.isArray(raw.observations) ? raw.observations : {};
        var current = raw.current && typeof raw.current === 'object' ? raw.current : {};
        var sources = [raw, values, observations, current];

        var departmentCode = normalizeDepartmentCode(firstValue(sources, ['department_code', 'departement_code', 'code_departement', 'dept_code', 'dept', 'department.code', 'departement.code']));
        var departmentInfo = DEPARTMENTS[departmentCode];
        var departmentName = cleanText(firstValue(sources, ['department_name', 'departement_nom', 'nom_departement', 'department.name', 'departement.name', 'department', 'departement']));
        if (/^[0-9]{1,2}$|^2[AB]$/i.test(departmentName)) {
            departmentName = '';
        }
        if (!departmentName && departmentInfo) {
            departmentName = departmentInfo[0];
        }
        if ('2A/2B' === departmentCode) {
            departmentName = departmentName || 'Corse';
        }

        var region = cleanText(firstValue(sources, ['region', 'region_name', 'nom_region', 'region.name']));
        if (!region && departmentInfo) {
            region = departmentInfo[1];
        }
        if (!region && ('2A' === departmentCode || '2B' === departmentCode || '2A/2B' === departmentCode)) {
            region = 'Corse';
        }

        var network = normalizeNetwork(firstValue(sources, ['network', 'reseau', 'network_name', 'source', 'provider']));
        var quality = numericFromSources(sources, ['quality', 'quality_class', 'classe', 'classe_site', 'classement_site']);
        quality = quality === null ? null : Math.min(5, Math.max(1, Math.round(quality)));
        var siteQuality = firstValue(sources, ['site_quality', 'quality_by_parameter', 'qualite_site']);
        siteQuality = siteQuality && typeof siteQuality === 'object' ? siteQuality : {};

        var history = firstValue(sources, ['history', 'historique', 'timeseries', 'series']);
        if (!Array.isArray(history)) {
            history = [];
        }

        var observedAt = firstValue(sources, ['observed_at', 'observation_time', 'datetime', 'date', 'time', 'timestamp', 'validity_time', 'heure']);

        return {
            id: cleanText(firstValue(sources, ['id', 'station_id', 'code', 'numer_sta', 'station_code']), 'station-' + index),
            name: cleanText(firstValue(sources, ['name', 'station_name', 'station', 'nom', 'libelle', 'city', 'ville']), 'Station sans nom'),
            departmentCode: departmentCode,
            departmentName: departmentName || 'Département inconnu',
            region: region || 'Région inconnue',
            networkKey: network.key,
            networkLabel: network.label,
            quality: quality,
            siteQuality: siteQuality,
            altitude: numericFromSources(sources, ['altitude', 'elevation', 'alt', 'altitude_m']),
            observedAt: observedAt,
            url: normalizeUrl(firstValue(sources, ['url', 'station_url', 'link', 'lien'])),
            raw: raw,
            sources: sources,
            history: history
        };
    }

    function historySources(entry) {
        var values = entry && entry.values && typeof entry.values === 'object' ? entry.values : {};
        var observation = entry && entry.observation && typeof entry.observation === 'object' ? entry.observation : {};
        return [entry || {}, values, observation];
    }

    function historyTimestamp(entry) {
        return asTimestamp(firstValue(historySources(entry), ['observed_at', 'observation_time', 'datetime', 'date', 'time', 'timestamp', 'heure']));
    }

    function historySeries(station, aliases) {
        return station.history.map(function (entry) {
            return {
                time: historyTimestamp(entry),
                value: numericFromSources(historySources(entry), aliases)
            };
        }).filter(function (entry) {
            return entry.time !== null && entry.value !== null;
        }).sort(function (a, b) { return a.time - b.time; });
    }

    function currentTimestamp(station) {
        var timestamp = asTimestamp(station.observedAt);
        if (timestamp !== null) {
            return timestamp;
        }
        var history = station.history.map(historyTimestamp).filter(function (value) { return value !== null; });
        return history.length ? Math.max.apply(Math, history) : Date.now();
    }

    function valueHoursAgo(station, aliases, hours) {
        var series = historySeries(station, aliases);
        if (!series.length) {
            return null;
        }
        var target = currentTimestamp(station) - hours * 3600000;
        var best = null;
        series.forEach(function (entry) {
            var distance = Math.abs(entry.time - target);
            if (distance <= 90 * 60000 && (!best || distance < best.distance)) {
                best = { distance: distance, value: entry.value };
            }
        });
        return best ? best.value : null;
    }

    function statLastHours(station, aliases, hours, method) {
        var end = currentTimestamp(station);
        var start = end - hours * 3600000;
        var values = historySeries(station, aliases).filter(function (entry) {
            return entry.time >= start && entry.time <= end;
        }).map(function (entry) { return entry.value; });
        if (!values.length) {
            return null;
        }
        if (method === 'sum') {
            return values.reduce(function (total, value) { return total + value; }, 0);
        }
        return method === 'min' ? Math.min.apply(Math, values) : Math.max.apply(Math, values);
    }

    function statSinceUtcBoundary(station, aliases, hour, method) {
        var end = currentTimestamp(station);
        var boundary = new Date(end);
        boundary.setUTCMinutes(0, 0, 0);
        boundary.setUTCHours(hour);
        if (boundary.getTime() > end) {
            boundary.setUTCDate(boundary.getUTCDate() - 1);
        }
        var values = historySeries(station, aliases).filter(function (entry) {
            return entry.time >= boundary.getTime() && entry.time <= end;
        }).map(function (entry) { return entry.value; });
        if (!values.length) {
            return null;
        }
        return method === 'min' ? Math.min.apply(Math, values) : Math.max.apply(Math, values);
    }

    function derivedDewPoint(temperature, humidity) {
        if (temperature === null || humidity === null || humidity <= 0 || humidity > 100) {
            return null;
        }
        var a = 17.625;
        var b = 243.04;
        var gamma = Math.log(humidity / 100) + (a * temperature) / (b + temperature);
        return (b * gamma) / (a - gamma);
    }

    function explicitValue(station, parameter) {
        return numericFromSources(station.sources, parameter.aliases);
    }

    function parameterValue(station, parameter) {
        var explicit = explicitValue(station, parameter);
        if (explicit !== null) {
            if (parameter.key === 'visibility' && explicit > 200) {
                return explicit / 1000;
            }
            if (parameter.key === 'sunshine_24h' && explicit > 48) {
                return explicit / 60;
            }
            return explicit;
        }

        var temperatureAliases = PARAMETER_MAP.temperature.aliases;
        var currentTemperature = explicitValue(station, PARAMETER_MAP.temperature);
        var humidity = explicitValue(station, PARAMETER_MAP.humidity);
        var wind = explicitValue(station, PARAMETER_MAP.wind_speed);
        var dewPoint = explicitValue(station, PARAMETER_MAP.dew_point);
        var tempMin24 = explicitValue(station, PARAMETER_MAP.temp_min_24h);
        var tempMax24 = explicitValue(station, PARAMETER_MAP.temp_max_24h);
        var tempMinClim = explicitValue(station, PARAMETER_MAP.temp_min_clim_18);
        var tempMaxClim = explicitValue(station, PARAMETER_MAP.temp_max_clim_06);
        var normalTmax = numericFromSources(station.sources, ['normal_tmax_month']);
        var normalTmin = numericFromSources(station.sources, ['normal_tmin_month']);
        var recordMonthTmax = numericFromSources(station.sources, ['record_month_tmax']);
        var recordMonthTmin = numericFromSources(station.sources, ['record_month_tmin']);
        var recordAbsoluteTmax = numericFromSources(station.sources, ['record_absolute_tmax']);
        var recordAbsoluteTmin = numericFromSources(station.sources, ['record_absolute_tmin']);

        switch (parameter.key) {
            case 'temp_change_1h': {
                var tempOneHourAgo = valueHoursAgo(station, temperatureAliases, 1);
                return currentTemperature !== null && tempOneHourAgo !== null ? currentTemperature - tempOneHourAgo : null;
            }
            case 'temp_change_24h': {
                var tempOneDayAgo = valueHoursAgo(station, temperatureAliases, 24);
                return currentTemperature !== null && tempOneDayAgo !== null ? currentTemperature - tempOneDayAgo : null;
            }
            case 'temp_min_24h':
                return statLastHours(station, temperatureAliases, 24, 'min');
            case 'temp_max_24h':
                return statLastHours(station, temperatureAliases, 24, 'max');
            case 'temp_min_clim_18':
                return statSinceUtcBoundary(station, temperatureAliases, 18, 'min');
            case 'temp_max_clim_06':
                return statSinceUtcBoundary(station, temperatureAliases, 6, 'max');
            case 'anomaly_tmax_clim':
                return tempMaxClim !== null && normalTmax !== null ? tempMaxClim - normalTmax : null;
            case 'anomaly_tmin_clim':
                return tempMinClim !== null && normalTmin !== null ? tempMinClim - normalTmin : null;
            case 'anomaly_tmax_24h':
                return tempMax24 !== null && normalTmax !== null ? tempMax24 - normalTmax : null;
            case 'anomaly_tmin_24h':
                return tempMin24 !== null && normalTmin !== null ? tempMin24 - normalTmin : null;
            case 'monthly_record_tmax_gap':
                return tempMaxClim !== null && recordMonthTmax !== null ? tempMaxClim - recordMonthTmax : null;
            case 'monthly_record_tmin_gap':
                return tempMinClim !== null && recordMonthTmin !== null ? tempMinClim - recordMonthTmin : null;
            case 'absolute_record_tmax_gap':
                return tempMaxClim !== null && recordAbsoluteTmax !== null ? tempMaxClim - recordAbsoluteTmax : null;
            case 'absolute_record_tmin_gap':
                return tempMinClim !== null && recordAbsoluteTmin !== null ? tempMinClim - recordAbsoluteTmin : null;
            case 'dew_point':
                return derivedDewPoint(currentTemperature, humidity);
            case 'windchill':
                if (currentTemperature === null) {
                    return null;
                }
                if (wind === null || currentTemperature > 10 || wind <= 4.8) {
                    return currentTemperature;
                }
                return 13.12 + 0.6215 * currentTemperature - 11.37 * Math.pow(wind, 0.16) + 0.3965 * currentTemperature * Math.pow(wind, 0.16);
            case 'humidex':
                dewPoint = dewPoint === null ? derivedDewPoint(currentTemperature, humidity) : dewPoint;
                if (currentTemperature === null || dewPoint === null) {
                    return null;
                }
                var vapourPressure = 6.11 * Math.exp(5417.7530 * (1 / 273.16 - 1 / (273.15 + dewPoint)));
                return currentTemperature + 0.5555 * (vapourPressure - 10);
            case 'wind_gust_max_24h':
                return statLastHours(station, PARAMETER_MAP.wind_gust.aliases, 24, 'max');
            case 'pressure_change_3h':
            case 'pressure_change_12h':
            case 'pressure_change_24h': {
                var hours = Number(parameter.key.match(/\d+/)[0]);
                var currentPressure = explicitValue(station, PARAMETER_MAP.pressure_msl);
                var oldPressure = valueHoursAgo(station, PARAMETER_MAP.pressure_msl.aliases, hours);
                return currentPressure !== null && oldPressure !== null ? currentPressure - oldPressure : null;
            }
            case 'sunshine_24h': {
                var sunshineMinutes = statLastHours(
                    station,
                    ['sunshine_1h_minutes', 'sunshine_minutes', 'insolh'],
                    24,
                    'sum'
                );
                return sunshineMinutes === null ? null : sunshineMinutes / 60;
            }
            default:
                return null;
        }
    }

    function qualityKeysForParameter(parameter) {
        var key = parameter.key;
        if (key.indexOf('wind_') === 0 || key === 'windchill') {
            return key === 'windchill' ? ['temperature', 'wind'] : ['wind'];
        }
        if (key.indexOf('rain_') === 0) {
            return ['rain'];
        }
        if (key === 'humidity') {
            return ['humidity'];
        }
        if (key === 'humidex' || key === 'dew_point') {
            return ['temperature', 'humidity'];
        }
        if (key === 'sunshine_24h') {
            return ['radiation'];
        }
        if (
            key.indexOf('temp') === 0 || key.indexOf('anomaly_') === 0 ||
            key.indexOf('monthly_record_') === 0 || key.indexOf('absolute_record_') === 0
        ) {
            return ['temperature'];
        }
        return [];
    }

    function stationQuality(station, parameter) {
        var keys = qualityKeysForParameter(parameter);
        var qualityMap = station.siteQuality && typeof station.siteQuality === 'object' ? station.siteQuality : {};
        var values = keys.map(function (key) {
            return toNumber(firstValue([qualityMap], [key, key + '_class', 'classe_' + key]));
        }).filter(function (value) {
            return value !== null && value >= 1 && value <= 5;
        });
        if (values.length) {
            // Pour un indice calculé avec deux capteurs (humidex, point de rosée,
            // windchill), on retient la classe la moins favorable.
            return Math.max.apply(Math, values.map(Math.round));
        }
        if (keys.length === 1 && station.quality !== null) {
            return station.quality;
        }
        return null;
    }

    function valueTime(station, parameter) {
        var aliases = [
            parameter.key + '_time', parameter.key + '_at', parameter.key + '_datetime',
            'times.' + parameter.key, 'timestamps.' + parameter.key
        ];
        return firstValue(station.sources, aliases) || station.observedAt;
    }

    function compareText(left, right) {
        return left.localeCompare(right, 'fr', { sensitivity: 'base', numeric: true });
    }

    function uniqueBy(items, keyFunction) {
        var map = new Map();
        items.forEach(function (item) {
            var key = keyFunction(item);
            if (key && !map.has(key)) {
                map.set(key, item);
            }
        });
        return Array.from(map.values());
    }

    function createElement(tag, className, text) {
        var element = document.createElement(tag);
        if (className) {
            element.className = className;
        }
        if (text !== undefined) {
            element.textContent = text;
        }
        return element;
    }

    async function fetchJson(url) {
        var separator = url.indexOf('?') === -1 ? '?' : '&';
        var response = await fetch(url + separator + '_amco=' + Date.now(), {
            credentials: 'omit',
            cache: 'no-store',
            headers: { 'Accept': 'application/json' }
        });
        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }
        var data = await response.json();
        if (!data || typeof data !== 'object') {
            throw new Error('JSON vide ou invalide');
        }
        return data;
    }

    function isMeteoFranceWindSource(data) {
        var provider = cleanText(firstValue([data || {}], ['source.provider', 'provider', 'source']));
        provider = provider.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
        return provider.indexOf('meteo-france') !== -1 || provider.indexOf('meteo france') !== -1;
    }

    async function fetchAutomaticSources(requestedKeys) {
        var configured = CONFIG.sourceUrls && typeof CONFIG.sourceUrls === 'object' ? CONFIG.sourceUrls : {};
        var keys = Array.isArray(requestedKeys)
            ? requestedKeys.filter(function (key) {
                return Object.prototype.hasOwnProperty.call(configured, key);
            })
            : Object.keys(configured);
        if (!keys.length) {
            throw new Error('Sources automatiques absentes de la configuration du module.');
        }

        var loaded = await Promise.all(keys.map(async function (key) {
            var candidates = Array.isArray(configured[key]) ? configured[key] : [configured[key]];
            var lastError = null;
            for (var index = 0; index < candidates.length; index += 1) {
                try {
                    var data = await fetchJson(candidates[index]);
                    if (key === 'wind' && !isMeteoFranceWindSource(data)) {
                        throw new Error('Source rafales refusée : elle ne provient pas de l’API Météo-France.');
                    }
                    return { key: key, data: data };
                } catch (error) {
                    lastError = error;
                }
            }
            return { key: key, error: lastError || new Error('Source indisponible') };
        }));

        var sources = {};
        var failures = [];
        loaded.forEach(function (result) {
            if (result.data) {
                sources[result.key] = result.data;
            } else {
                failures.push(result.key);
            }
        });
        if (!Object.keys(sources).length) {
            throw new Error('Impossible de charger les fichiers automatiques d’observations.');
        }
        return {
            payload: { sources: sources },
            meta: { automatic: true, direct: true, partial: failures.length > 0, failed_sources: failures }
        };
    }

    async function completeMissingSources(result) {
        if (!result || typeof result !== 'object') {
            return result;
        }

        var payload = result.payload && typeof result.payload === 'object' ? result.payload : result;
        var currentSources = payload.sources && typeof payload.sources === 'object' ? payload.sources : {};
        var configured = CONFIG.sourceUrls && typeof CONFIG.sourceUrls === 'object' ? CONFIG.sourceUrls : {};
        var missing = Object.keys(configured).filter(function (key) {
            return !currentSources[key];
        });

        if (!missing.length) {
            return result;
        }

        try {
            var supplement = await fetchAutomaticSources(missing);
            payload.sources = Object.assign({}, currentSources, supplement.payload.sources || {});
            if (result.payload && typeof result.payload === 'object') {
                result.payload = payload;
            }

            var remaining = missing.filter(function (key) {
                return !payload.sources[key];
            });
            result.meta = Object.assign({}, result.meta || {}, {
                partial: remaining.length > 0,
                failed_sources: remaining,
                direct_completed: remaining.length < missing.length
            });
        } catch (error) {
            // La réponse REST reste utilisable : une source secondaire peut manquer.
        }

        return result;
    }

    var AUTO_REFRESH_MS = 180000;

    function RankingApp(root) {
        this.root = root;
        this.stations = [];
        this.filteredRows = [];
        this.page = 1;
        this.pageSize = Number(root.dataset.pageSize || CONFIG.pageSize || 50);
        this.meta = {};
        this.payload = {};
        this.autoRefreshTimer = null;

        this.elements = {
            subtitle: root.querySelector('.amco-subtitle'),
            date: root.querySelector('.amco-date strong'),
            message: root.querySelector('.amco-message'),
            refresh: root.querySelector('.amco-refresh'),
            parameter: root.querySelector('.amco-parameter'),
            ranking: root.querySelector('.amco-ranking'),
            department: root.querySelector('.amco-department'),
            region: root.querySelector('.amco-region'),
            search: root.querySelector('.amco-search'),
            showTime: root.querySelector('.amco-show-time'),
            showAltitude: root.querySelector('.amco-show-altitude'),
            count: root.querySelector('.amco-count'),
            pageSize: root.querySelector('.amco-page-size'),
            tbody: root.querySelector('tbody'),
            altitudeHeader: root.querySelector('.amco-col-altitude'),
            pagination: root.querySelector('.amco-pagination')
        };
    }

    RankingApp.prototype.init = function () {
        this.populateParameters();
        this.setPageSizeControl();
        this.bindEvents();
        this.load();
        this.startAutoRefresh();
    };

    RankingApp.prototype.startAutoRefresh = function () {
        var app = this;
        if (this.autoRefreshTimer) {
            return;
        }
        this.autoRefreshTimer = window.setInterval(function () {
            if (document.visibilityState === 'hidden' || app.root.classList.contains('is-loading')) {
                return;
            }
            app.load(false);
        }, AUTO_REFRESH_MS);

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible' && !app.root.classList.contains('is-loading')) {
                app.load(false);
            }
        });
    };

    RankingApp.prototype.populateParameters = function () {
        var select = this.elements.parameter;
        PARAMETERS.forEach(function (group) {
            var optgroup = document.createElement('optgroup');
            optgroup.label = group.group;
            group.items.forEach(function (parameter) {
                var option = document.createElement('option');
                option.value = parameter.key;
                option.textContent = parameter.label;
                optgroup.appendChild(option);
            });
            select.appendChild(optgroup);
        });
        select.value = 'temperature';
    };

    RankingApp.prototype.setPageSizeControl = function () {
        var select = this.elements.pageSize;
        var value = String(this.pageSize);
        if (!Array.from(select.options).some(function (option) { return option.value === value; })) {
            var option = document.createElement('option');
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        }
        select.value = value;
    };

    RankingApp.prototype.bindEvents = function () {
        var app = this;
        ['parameter', 'ranking', 'department'].forEach(function (name) {
            app.elements[name].addEventListener('change', function () {
                app.page = 1;
                app.render();
            });
        });

        this.elements.region.addEventListener('change', function () {
            app.populateDepartments();
            app.page = 1;
            app.render();
        });

        var searchTimer = null;
        this.elements.search.addEventListener('input', function () {
            window.clearTimeout(searchTimer);
            searchTimer = window.setTimeout(function () {
                app.page = 1;
                app.render();
            }, 120);
        });

        this.elements.showTime.addEventListener('change', function () { app.render(); });
        this.elements.showAltitude.addEventListener('change', function () { app.render(); });
        this.elements.pageSize.addEventListener('change', function () {
            app.pageSize = Number(app.elements.pageSize.value);
            app.page = 1;
            app.render();
        });
        this.elements.refresh.addEventListener('click', function () { app.load(true); });

    };

    RankingApp.prototype.load = async function (manual) {
        var app = this;
        this.setLoading(true);
        this.hideMessage();
        try {
            var endpoint = CONFIG.endpoint;
            var result = null;
            var usedDirectSources = false;
            if (endpoint) {
                try {
                    var separator = endpoint.indexOf('?') === -1 ? '?' : '&';
                    var response = await fetch(endpoint + separator + '_=' + Date.now(), {
                        credentials: 'same-origin',
                        cache: 'no-store',
                        headers: { 'Accept': 'application/json' }
                    });
                    result = await response.json().catch(function () { return {}; });
                    if (!response.ok) {
                        throw new Error(result.message || 'Impossible de charger les observations.');
                    }
                } catch (restError) {
                    result = await fetchAutomaticSources();
                    usedDirectSources = true;
                }
            } else {
                result = await fetchAutomaticSources();
                usedDirectSources = true;
            }

            if (!usedDirectSources) {
                result = await completeMissingSources(result);
            }

            this.meta = result.meta || {};
            this.payload = adaptAutomaticPayload(result.payload || result);
            this.stations = extractStations(this.payload).map(normalizeStation).filter(function (station) {
                return station.name && station.sources.length;
            });

            if (!this.stations.length && !usedDirectSources) {
                result = await fetchAutomaticSources();
                usedDirectSources = true;
                this.meta = result.meta || {};
                this.payload = adaptAutomaticPayload(result.payload || result);
                this.stations = extractStations(this.payload).map(normalizeStation).filter(function (station) {
                    return station.name && station.sources.length;
                });
            }

            if (!this.stations.length) {
                throw new Error('Aucune observation exploitable n’est disponible pour le moment.');
            }

            this.populateRegions();
            this.populateDepartments();
            this.elements.date.textContent = formatDate(generatedAt(this.payload, this.stations));

            if (this.meta.stale) {
                this.showMessage('Affichage de la dernière version valide des observations.', 'warning');
            } else if (manual) {
                this.showMessage('Classement actualisé.', 'success', true);
            }

            this.page = 1;
            this.render();
        } catch (error) {
            this.stations = [];
            this.elements.date.textContent = '—';
            this.showMessage(error && error.message ? error.message : 'Erreur de chargement des observations.', 'error');
            this.render();
        } finally {
            this.setLoading(false);
        }
    };

    RankingApp.prototype.setLoading = function (loading) {
        this.root.classList.toggle('is-loading', loading);
        this.elements.refresh.disabled = loading;
        this.elements.refresh.querySelector('span').textContent = loading ? '↻' : '↻';
    };

    RankingApp.prototype.showMessage = function (text, type, temporary) {
        var message = this.elements.message;
        message.textContent = text;
        message.className = 'amco-message amco-message-' + type;
        message.hidden = false;
        if (temporary) {
            window.setTimeout(function () { message.hidden = true; }, 2500);
        }
    };

    RankingApp.prototype.hideMessage = function () {
        this.elements.message.hidden = true;
    };

    RankingApp.prototype.populateRegions = function () {
        var select = this.elements.region;
        var previous = select.value;
        while (select.options.length > 1) {
            select.remove(1);
        }
        var regions = uniqueBy(this.stations, function (station) { return station.region; }).map(function (station) {
            return station.region;
        }).filter(function (region) { return region && region !== 'Région inconnue'; }).sort(compareText);
        regions.forEach(function (region) {
            var option = document.createElement('option');
            option.value = region;
            option.textContent = region;
            select.appendChild(option);
        });
        select.value = regions.indexOf(previous) !== -1 ? previous : '';
    };

    RankingApp.prototype.populateDepartments = function () {
        var select = this.elements.department;
        var previous = select.value;
        var region = this.elements.region.value;
        while (select.options.length > 1) {
            select.remove(1);
        }

        var departments = uniqueBy(this.stations.filter(function (station) {
            return !region || station.region === region;
        }), function (station) {
            return station.departmentCode || station.departmentName;
        }).map(function (station) {
            return {
                code: station.departmentCode,
                name: station.departmentName,
                key: station.departmentCode || station.departmentName
            };
        }).sort(function (left, right) {
            if (left.code && right.code) {
                return compareText(left.code, right.code);
            }
            return compareText(left.name, right.name);
        });

        departments.forEach(function (department) {
            var option = document.createElement('option');
            option.value = department.key;
            option.textContent = (department.code ? department.code + ' – ' : '') + department.name;
            select.appendChild(option);
        });
        select.value = departments.some(function (department) { return department.key === previous; }) ? previous : '';
    };

    RankingApp.prototype.currentParameter = function () {
        return PARAMETER_MAP[this.elements.parameter.value] || PARAMETER_MAP.temperature;
    };

    RankingApp.prototype.buildRows = function () {
        var app = this;
        var parameter = this.currentParameter();
        var region = this.elements.region.value;
        var department = this.elements.department.value;
        var query = this.elements.search.value.trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
        var direction = this.elements.ranking.value;

        var rows = this.stations.filter(function (station) {
            if (region && station.region !== region) {
                return false;
            }
            if (department && (station.departmentCode || station.departmentName) !== department) {
                return false;
            }
            if (query) {
                var haystack = [station.name, station.departmentName, station.departmentCode, station.region, station.networkLabel]
                    .join(' ').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
                if (haystack.indexOf(query) === -1) {
                    return false;
                }
            }
            return true;
        }).map(function (station) {
            return {
                station: station,
                value: parameterValue(station, parameter),
                valueTime: valueTime(station, parameter)
            };
        }).filter(function (row) {
            return row.value !== null && Number.isFinite(row.value);
        });

        rows.sort(function (left, right) {
            var difference = direction === 'min' ? left.value - right.value : right.value - left.value;
            return Math.abs(difference) > 1e-10 ? difference : compareText(left.station.name, right.station.name);
        });

        var previousValue = null;
        var previousRank = 0;
        rows.forEach(function (row, index) {
            if (previousValue !== null && Math.abs(row.value - previousValue) < 1e-10) {
                row.rank = previousRank;
            } else {
                row.rank = index + 1;
                previousRank = row.rank;
                previousValue = row.value;
            }
        });
        return rows;
    };

    RankingApp.prototype.render = function () {
        var parameter = this.currentParameter();
        var directionLabel = this.elements.ranking.value === 'min' ? 'minimums' : 'maximums';
        this.elements.subtitle.textContent = 'Classement des ' + directionLabel + ' en temps réel pour ' + parameter.label;
        this.filteredRows = this.buildRows();
        var count = this.filteredRows.length;
        this.elements.count.textContent = count.toLocaleString('fr-FR') + ' station' + (count === 1 ? ' classée' : 's classées');

        var pageCount = Math.max(1, Math.ceil(count / this.pageSize));
        this.page = Math.max(1, Math.min(this.page, pageCount));
        var start = (this.page - 1) * this.pageSize;
        var pageRows = this.filteredRows.slice(start, start + this.pageSize);

        this.renderTable(pageRows, parameter);
        this.renderPagination(pageCount);
    };

    RankingApp.prototype.renderTable = function (rows, parameter) {
        var tbody = this.elements.tbody;
        var showTime = this.elements.showTime.checked;
        var showAltitude = this.elements.showAltitude.checked;
        this.elements.altitudeHeader.hidden = !showAltitude;
        tbody.replaceChildren();

        if (!rows.length) {
            var emptyRow = document.createElement('tr');
            var emptyCell = createElement('td', 'amco-empty', 'Aucune donnée disponible pour ces filtres.');
            emptyCell.colSpan = showAltitude ? 6 : 5;
            emptyRow.appendChild(emptyCell);
            tbody.appendChild(emptyRow);
            return;
        }

        var fragment = document.createDocumentFragment();
        rows.forEach(function (row) {
            var station = row.station;
            var tr = document.createElement('tr');
            tr.appendChild(createElement('td', 'amco-rank', String(row.rank)));

            var stationCell = createElement('td', 'amco-station');
            if (station.url) {
                var link = createElement('a', '', station.name);
                link.href = station.url;
                link.target = '_blank';
                link.rel = 'noopener';
                stationCell.appendChild(link);
            } else {
                stationCell.textContent = station.name;
            }
            tr.appendChild(stationCell);

            var valueCell = createElement('td', 'amco-value');
            var formatted = new Intl.NumberFormat('fr-FR', {
                minimumFractionDigits: parameter.precision,
                maximumFractionDigits: parameter.precision,
                signDisplay: parameter.key.indexOf('change') !== -1 || parameter.key.indexOf('anomaly') !== -1 || parameter.key.indexOf('gap') !== -1 ? 'exceptZero' : 'auto'
            }).format(row.value);
            var strong = createElement('strong', '', formatted + (parameter.unit ? ' ' + parameter.unit : ''));
            valueCell.appendChild(strong);
            if (showTime) {
                var time = formatTime(row.valueTime);
                if (time) {
                    valueCell.appendChild(document.createTextNode(' '));
                    valueCell.appendChild(createElement('small', '', '(' + time + ')'));
                }
            }
            tr.appendChild(valueCell);

            tr.appendChild(createElement('td', 'amco-department-cell', station.departmentName + (station.departmentCode ? ' (' + station.departmentCode + ')' : '')));

            var networkCell = createElement('td', 'amco-network-cell', station.networkLabel);
            var quality = stationQuality(station, parameter);
            if (station.networkKey === 'mf' && quality !== null) {
                networkCell.title = 'Qualité du site Météo-France pour ' + parameter.label + ' : classe ' + quality;
            }
            tr.appendChild(networkCell);

            if (showAltitude) {
                tr.appendChild(createElement('td', 'amco-altitude', station.altitude === null ? '—' : Math.round(station.altitude).toLocaleString('fr-FR') + ' m'));
            }
            fragment.appendChild(tr);
        });
        tbody.appendChild(fragment);
    };

    RankingApp.prototype.renderPagination = function (pageCount) {
        var app = this;
        var nav = this.elements.pagination;
        nav.replaceChildren();
        if (pageCount <= 1) {
            return;
        }

        function button(label, page, disabled, current, ariaLabel) {
            var element = createElement('button', current ? 'is-current' : '', label);
            element.type = 'button';
            element.disabled = disabled;
            if (ariaLabel) {
                element.setAttribute('aria-label', ariaLabel);
            }
            if (current) {
                element.setAttribute('aria-current', 'page');
            }
            element.addEventListener('click', function () {
                app.page = page;
                app.render();
                app.root.querySelector('.amco-table-wrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
            return element;
        }

        nav.appendChild(button('‹', this.page - 1, this.page === 1, false, 'Page précédente'));
        var start = Math.max(1, this.page - 2);
        var end = Math.min(pageCount, this.page + 2);
        if (start > 1) {
            nav.appendChild(button('1', 1, false, this.page === 1));
            if (start > 2) {
                nav.appendChild(createElement('span', 'amco-ellipsis', '…'));
            }
        }
        for (var page = start; page <= end; page += 1) {
            nav.appendChild(button(String(page), page, false, page === this.page));
        }
        if (end < pageCount) {
            if (end < pageCount - 1) {
                nav.appendChild(createElement('span', 'amco-ellipsis', '…'));
            }
            nav.appendChild(button(String(pageCount), pageCount, false, this.page === pageCount));
        }
        nav.appendChild(button('›', this.page + 1, this.page === pageCount, false, 'Page suivante'));
    };

    function boot() {
        document.querySelectorAll('.amco-app').forEach(function (root) {
            if (!root.dataset.amcoReady) {
                root.dataset.amcoReady = '1';
                new RankingApp(root).init();
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
}());
