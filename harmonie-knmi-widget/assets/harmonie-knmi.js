(function () {
    'use strict';

    var COMMUNES_API = 'https://geo.api.gouv.fr/communes';
    var DIRECTIONS = [
        'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
        'S', 'SSO', 'SO', 'OSO', 'O', 'ONO', 'NO', 'NNO'
    ];
    var CONDITIONS = {
        0: { label: 'Indéterminé', icon: '•' },
        1: { label: 'Dégagé', icon: '☀️' },
        2: { label: 'Peu nuageux', icon: '🌤️' },
        3: { label: 'Nuageux', icon: '⛅' },
        4: { label: 'Couvert', icon: '☁️' },
        5: { label: 'Pluie', icon: '🌦️' },
        6: { label: 'Forte pluie', icon: '🌧️' },
        7: { label: 'Neige', icon: '❄️' },
        8: { label: 'Brouillard', icon: '🌫️' },
        9: { label: 'Très venteux', icon: '💨' }
    };

    function whenReady(callback) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', callback);
        } else {
            callback();
        }
    }

    function finite(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function formatNumber(value, decimals) {
        if (!finite(value)) {
            return '—';
        }
        return value.toLocaleString('fr-FR', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals
        });
    }

    function dateFormatter(timezone, options) {
        try {
            return new Intl.DateTimeFormat('fr-FR', Object.assign(
                { timeZone: timezone },
                options
            ));
        } catch (error) {
            return new Intl.DateTimeFormat('fr-FR', options);
        }
    }

    function windDirection(degrees) {
        if (!finite(degrees)) {
            return '';
        }
        return DIRECTIONS[Math.round(degrees / 22.5) % 16];
    }

    function temperatureClass(value) {
        if (!finite(value)) { return ''; }
        if (value >= 35) { return 'hkw-temp-extreme'; }
        if (value >= 30) { return 'hkw-temp-hot'; }
        if (value >= 22) { return 'hkw-temp-warm'; }
        if (value >= 12) { return 'hkw-temp-mild'; }
        if (value >= 4) { return 'hkw-temp-cool'; }
        return 'hkw-temp-cold';
    }

    function gustClass(value) {
        if (!finite(value)) { return ''; }
        if (value >= 80) { return 'hkw-gust-danger'; }
        if (value >= 60) { return 'hkw-gust-strong'; }
        if (value >= 40) { return 'hkw-gust-moderate'; }
        return '';
    }

    function createCell(tag, label, className) {
        var cell = document.createElement(tag);
        if (label) {
            cell.setAttribute('data-label', label);
        }
        if (className) {
            cell.className = className;
        }
        return cell;
    }

    function textSpan(text, className) {
        var span = document.createElement('span');
        span.textContent = text;
        if (className) {
            span.className = className;
        }
        return span;
    }

    function fetchJson(url, options) {
        return fetch(url, options || {}).then(function (response) {
            if (!response.ok) {
                throw new Error('Réponse HTTP ' + response.status);
            }
            return response.json();
        });
    }

    function initApp(app) {
        var baseUrl = (app.dataset.baseUrl || '').replace(/\/+$/, '');
        var defaultCode = app.dataset.defaultCode || '59183';
        var defaultDepartment = app.dataset.defaultDepartment || '59';
        var defaultName = app.dataset.defaultName || 'Dunkerque';
        var hours = Math.max(1, Math.min(48, parseInt(app.dataset.hours || '48', 10)));
        var timezone = app.dataset.timezone || 'Europe/Paris';
        var titlePrefix = app.dataset.titlePrefix || 'Prévisions HARMONIE';

        var input = app.querySelector('.hkw-city-input');
        var results = app.querySelector('.hkw-search-results');
        var status = app.querySelector('.hkw-search-status');
        var body = app.querySelector('[data-hkw-body]');
        var title = app.querySelector('[data-hkw-title]');
        var meta = app.querySelector('[data-hkw-meta]');
        var generated = app.querySelector('[data-hkw-generated]');
        var stale = app.querySelector('[data-hkw-stale]');

        var indexData = null;
        var departmentCache = new Map();
        var debounceTimer = null;
        var searchController = null;
        var resultButtons = [];
        var activeResult = -1;

        var dayFormat = dateFormatter(timezone, {
            weekday: 'short', day: '2-digit', month: '2-digit'
        });
        var hourFormat = dateFormatter(timezone, {
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
        });
        var fullFormat = dateFormatter(timezone, {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
        });

        function setStatus(message, error) {
            status.textContent = message;
            status.classList.toggle('hkw-search-error', Boolean(error));
        }

        function showTableMessage(message, error) {
            body.replaceChildren();
            var row = document.createElement('tr');
            var cell = createCell('td', '', error ? 'hkw-loading hkw-load-error' : 'hkw-loading');
            cell.colSpan = 10;
            cell.textContent = message;
            row.appendChild(cell);
            body.appendChild(row);
        }

        function closeResults() {
            results.hidden = true;
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            resultButtons = [];
            activeResult = -1;
        }

        function setActiveResult(position) {
            if (!resultButtons.length) {
                return;
            }
            activeResult = Math.max(0, Math.min(resultButtons.length - 1, position));
            resultButtons.forEach(function (button, index) {
                var active = index === activeResult;
                button.classList.toggle('is-active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            var selected = resultButtons[activeResult];
            input.setAttribute('aria-activedescendant', selected.id);
            selected.scrollIntoView({ block: 'nearest' });
        }

        function loadIndex() {
            return fetchJson(baseUrl + '/index.json', { cache: 'no-cache' })
                .then(function (payload) {
                    if (!payload || payload.status !== 'ok' || !payload.departments) {
                        throw new Error('Index national HARMONIE invalide');
                    }
                    indexData = payload;
                    var run = payload.model && payload.model.run_time
                        ? fullFormat.format(new Date(payload.model.run_time)).replace(':', 'h')
                        : 'indéterminé';
                    var resolution = payload.model && payload.model.resolution_km
                        ? formatNumber(payload.model.resolution_km, 1) + ' km'
                        : '—';
                    meta.textContent = 'Run du ' + run + ' • résolution ' + resolution;

                    if (payload.generated_at) {
                        generated.textContent = 'Mise à jour du tableau : ' +
                            fullFormat.format(new Date(payload.generated_at)).replace(':', 'h');
                        stale.hidden = (Date.now() - new Date(payload.generated_at).getTime()) <= 8 * 3600000;
                    }
                    return payload;
                });
        }

        function loadDepartment(code) {
            var normalized = String(code || '').toUpperCase();
            if (departmentCache.has(normalized)) {
                return departmentCache.get(normalized);
            }
            if (!indexData || !indexData.departments[normalized]) {
                return Promise.reject(new Error('Département hors couverture HARMONIE'));
            }
            var relativeFile = indexData.departments[normalized].file;
            var promise = fetchJson(baseUrl + '/' + relativeFile, { cache: 'default' })
                .then(function (payload) {
                    if (!payload || payload.status !== 'ok' || !Array.isArray(payload.communes)) {
                        throw new Error('Fichier départemental invalide');
                    }
                    return payload;
                })
                .catch(function (error) {
                    departmentCache.delete(normalized);
                    throw error;
                });
            departmentCache.set(normalized, promise);
            return promise;
        }

        function renderForecast(departmentData, commune) {
            var pointId = Number(commune[6]);
            var lowerTime = Date.now() - 3600000;
            var forecasts = (departmentData.forecast || []).filter(function (step) {
                return Array.isArray(step) && new Date(step[0]).getTime() >= lowerTime;
            }).slice(0, hours);

            if (!forecasts.length) {
                showTableMessage('Aucune échéance HARMONIE disponible pour cette commune.', true);
                return;
            }

            body.replaceChildren();
            var previousDay = '';
            forecasts.forEach(function (step) {
                var date = new Date(step[0]);
                var values = step[1] && step[1][pointId];
                if (!Array.isArray(values)) {
                    return;
                }

                var dayKey = date.toISOString().slice(0, 10);
                var row = document.createElement('tr');
                if (dayKey !== previousDay) {
                    row.classList.add('hkw-new-day');
                    previousDay = dayKey;
                }

                var dateCell = createCell('th', 'Date');
                dateCell.scope = 'row';
                dateCell.textContent = dayFormat.format(date);
                row.appendChild(dateCell);

                var hourCell = createCell('td', 'Heure', 'hkw-hour');
                hourCell.textContent = hourFormat.format(date).replace(':', 'h');
                row.appendChild(hourCell);

                var condition = CONDITIONS[Number(values[9])] || CONDITIONS[0];
                var conditionCell = createCell('td', 'Temps', 'hkw-condition');
                conditionCell.appendChild(textSpan(condition.icon, 'hkw-icon'));
                conditionCell.appendChild(textSpan(condition.label));
                row.appendChild(conditionCell);

                var temperatureCell = createCell(
                    'td', 'Température',
                    'hkw-temperature ' + temperatureClass(values[0])
                );
                temperatureCell.textContent = formatNumber(values[0], 1) + ' °C';
                row.appendChild(temperatureCell);

                var humidityCell = createCell('td', 'Humidité');
                humidityCell.textContent = formatNumber(values[1], 0) + ' %';
                row.appendChild(humidityCell);

                var rainCell = createCell(
                    'td', 'Pluie', finite(values[2]) && values[2] >= 0.1 ? 'hkw-rain' : ''
                );
                rainCell.textContent = formatNumber(values[2], 1) + ' mm';
                row.appendChild(rainCell);

                var cloudCell = createCell('td', 'Nuages');
                cloudCell.textContent = formatNumber(values[3], 0) + ' %';
                row.appendChild(cloudCell);

                var windCell = createCell('td', 'Vent');
                var windStrong = document.createElement('strong');
                windStrong.textContent = formatNumber(values[4], 0) + ' km/h';
                windCell.appendChild(windStrong);
                var direction = windDirection(values[5]);
                if (direction) {
                    var directionBadge = textSpan(direction, 'hkw-direction');
                    directionBadge.title = formatNumber(values[5], 0) + '°';
                    windCell.appendChild(directionBadge);
                }
                row.appendChild(windCell);

                var gustCell = createCell('td', 'Rafales', gustClass(values[6]));
                var gustStrong = document.createElement('strong');
                gustStrong.textContent = formatNumber(values[6], 0) + ' km/h';
                gustCell.appendChild(gustStrong);
                row.appendChild(gustCell);

                var pressureCell = createCell('td', 'Pression');
                pressureCell.textContent = formatNumber(values[7], 0) + ' hPa';
                row.appendChild(pressureCell);
                body.appendChild(row);
            });

            var postal = Array.isArray(commune[2]) && commune[2].length
                ? commune[2][0]
                : '';
            title.textContent = titlePrefix + ' — ' + commune[1];
            input.value = commune[1];
            app.dataset.cityCode = commune[0];
            app.dataset.cityDepartment = departmentData.department;
            setStatus(
                'Prévisions affichées pour ' + commune[1] +
                (postal ? ' (' + postal + ')' : '') + '.',
                false
            );
        }

        function selectCommune(candidate) {
            closeResults();
            input.value = candidate.nom;
            setStatus('Chargement des prévisions pour ' + candidate.nom + '…', false);
            showTableMessage('Chargement des prévisions…', false);
            loadDepartment(candidate.codeDepartement)
                .then(function (departmentData) {
                    var commune = departmentData.communes.find(function (item) {
                        return item[0] === candidate.code;
                    });
                    if (!commune) {
                        throw new Error('Commune absente du catalogue HARMONIE');
                    }
                    renderForecast(departmentData, commune);
                })
                .catch(function (error) {
                    showTableMessage('Prévisions indisponibles : ' + error.message, true);
                    setStatus(error.message, true);
                });
        }

        function displaySearchResults(candidates) {
            results.replaceChildren();
            resultButtons = [];
            activeResult = -1;
            if (!candidates.length) {
                closeResults();
                setStatus('Aucune commune métropolitaine trouvée.', true);
                return;
            }

            candidates.forEach(function (candidate, position) {
                var button = document.createElement('button');
                button.type = 'button';
                button.className = 'hkw-search-result';
                button.id = input.id + '-option-' + position;
                button.setAttribute('role', 'option');
                button.setAttribute('aria-selected', 'false');

                var name = textSpan(candidate.nom, 'hkw-result-name');
                var postals = Array.isArray(candidate.codesPostaux)
                    ? candidate.codesPostaux.join(', ')
                    : '';
                var details = textSpan(
                    (postals ? postals + ' • ' : '') +
                    'département ' + candidate.codeDepartement,
                    'hkw-result-details'
                );
                button.appendChild(name);
                button.appendChild(details);
                button.addEventListener('click', function () {
                    selectCommune(candidate);
                });
                results.appendChild(button);
                resultButtons.push(button);
            });
            results.hidden = false;
            input.setAttribute('aria-expanded', 'true');
            setStatus(candidates.length + ' commune(s) proposée(s).', false);
        }

        function searchCommunes(query) {
            if (!indexData) {
                return;
            }
            if (searchController) {
                searchController.abort();
            }
            searchController = new AbortController();
            var parameters = new URLSearchParams({
                fields: 'nom,code,codesPostaux,codeDepartement,population',
                format: 'json',
                boost: 'population',
                limit: '12'
            });
            if (/^\d{5}$/.test(query)) {
                parameters.set('codePostal', query);
            } else {
                parameters.set('nom', query);
            }
            setStatus('Recherche des communes…', false);
            fetchJson(COMMUNES_API + '?' + parameters.toString(), {
                signal: searchController.signal,
                cache: 'default'
            })
                .then(function (payload) {
                    var candidates = Array.isArray(payload) ? payload : [];
                    candidates = candidates.filter(function (candidate) {
                        var code = String(candidate.codeDepartement || '').toUpperCase();
                        return Boolean(indexData.departments[code]);
                    });
                    displaySearchResults(candidates.slice(0, 12));
                })
                .catch(function (error) {
                    if (error.name === 'AbortError') {
                        return;
                    }
                    closeResults();
                    setStatus('Recherche momentanément indisponible.', true);
                });
        }

        input.addEventListener('input', function () {
            window.clearTimeout(debounceTimer);
            var query = input.value.trim();
            if (query.length < 2) {
                closeResults();
                setStatus('Saisissez au moins deux lettres ou un code postal.', false);
                return;
            }
            if (/^\d+$/.test(query) && query.length < 5) {
                closeResults();
                setStatus('Saisissez les cinq chiffres du code postal.', false);
                return;
            }
            debounceTimer = window.setTimeout(function () {
                searchCommunes(query);
            }, 280);
        });

        input.addEventListener('keydown', function (event) {
            if (results.hidden || !resultButtons.length) {
                return;
            }
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActiveResult(activeResult + 1);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActiveResult(activeResult <= 0 ? 0 : activeResult - 1);
            } else if (event.key === 'Enter' && activeResult >= 0) {
                event.preventDefault();
                resultButtons[activeResult].click();
            } else if (event.key === 'Escape') {
                closeResults();
            }
        });

        document.addEventListener('click', function (event) {
            if (!app.contains(event.target)) {
                closeResults();
            }
        });

        if (!baseUrl) {
            showTableMessage('Adresse des données HARMONIE non configurée.', true);
            return;
        }
        loadIndex()
            .then(function () {
                return loadDepartment(defaultDepartment);
            })
            .then(function (departmentData) {
                var commune = departmentData.communes.find(function (item) {
                    return item[0] === defaultCode;
                });
                if (!commune) {
                    throw new Error('Commune initiale absente du catalogue');
                }
                renderForecast(departmentData, commune);
            })
            .catch(function (error) {
                showTableMessage(
                    'Les données nationales ne sont pas encore disponibles : ' + error.message,
                    true
                );
                setStatus('Données nationales indisponibles.', true);
            });
    }

    whenReady(function () {
        document.querySelectorAll('[data-hkw-app]').forEach(initApp);
    });
}());
