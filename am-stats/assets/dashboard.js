/* AM Stats — logique du tableau de bord (aucune dépendance hormis Chart.js). */
(function () {
  'use strict';

  var body = document.body;
  var API = body.getAttribute('data-api') || 'api.php';
  var SITE = (body.getAttribute('data-site') || '').replace(/\/+$/, '');
  var TODAY = body.getAttribute('data-today');
  var OPT_KEY = 'am_stats_ignore';
  var REALTIME_EVERY = 30000;       // 30 s
  var SUMMARY_EVERY = 300000;       // 5 min (si la période inclut aujourd'hui)

  var fmtInt = new Intl.NumberFormat('fr-FR');
  var fmtDec = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var fmtPct = new Intl.NumberFormat('fr-FR', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var fmtDelta = new Intl.NumberFormat('fr-FR', { style: 'percent', maximumFractionDigits: 1, signDisplay: 'always' });
  var fmtShortDay = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  var fmtWeekDay = new Intl.DateTimeFormat('fr-FR', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  var fmtLongDay = new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
  var fmtTime = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  var PERIOD_NAMES = { today: "Aujourd'hui", yesterday: 'Hier', '7d': '7 derniers jours', '30d': '30 derniers jours', custom: 'Période personnalisée' };
  var DEVICE_NAMES = { mobile: 'Mobile', tablet: 'Tablette', desktop: 'Ordinateur' };

  var $ = function (id) { return document.getElementById(id); };
  var main = $('main');

  var state = { period: '7d', from: null, to: null };
  var lastData = null;
  var chart = null;
  var loadToken = 0;
  var summaryTimer = null;

  /* ---------- Utilitaires ---------- */

  function parseDay(s) {
    var p = String(s).split('-');
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function showError(message) {
    var box = $('alert');
    if (!message) { box.hidden = true; box.textContent = ''; return; }
    box.textContent = message;
    box.hidden = false;
  }

  function api(params) {
    var qs = new URLSearchParams(params).toString();
    return fetch(API + '?' + qs, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    }).then(function (res) {
      if (res.status === 401) {
        window.location.reload();
        throw new Error('Session expirée.');
      }
      return res.json().catch(function () { return {}; }).then(function (json) {
        if (!res.ok) throw new Error(json.error || ('Erreur HTTP ' + res.status));
        return json;
      });
    });
  }

  /* ---------- État ⇄ URL ---------- */

  function readStateFromUrl() {
    var q = new URLSearchParams(window.location.search);
    var p = q.get('period');
    if (p && PERIOD_NAMES[p]) state.period = p;
    if (state.period === 'custom') {
      state.from = q.get('from');
      state.to = q.get('to');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(state.from || '') || !/^\d{4}-\d{2}-\d{2}$/.test(state.to || '')) {
        state.period = '7d';
        state.from = state.to = null;
      }
    }
  }

  function writeStateToUrl() {
    var q = new URLSearchParams();
    q.set('period', state.period);
    if (state.period === 'custom') { q.set('from', state.from); q.set('to', state.to); }
    history.replaceState(null, '', '?' + q.toString());
  }

  function syncControls() {
    document.querySelectorAll('.seg').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-period') === state.period));
    });
    $('custom-range').hidden = state.period !== 'custom';
    if (state.period === 'custom') {
      $('range-from').value = state.from || '';
      $('range-to').value = state.to || '';
    }
  }

  /* ---------- Chargement ---------- */

  function load() {
    var token = ++loadToken;
    var params = { action: 'summary', period: state.period };
    if (state.period === 'custom') { params.from = state.from; params.to = state.to; }
    main.setAttribute('aria-busy', 'true');

    return api(params).then(function (data) {
      if (token !== loadToken) return;
      showError(null);
      lastData = data;
      render(data);
      scheduleSummaryRefresh(data);
    }).catch(function (err) {
      if (token !== loadToken) return;
      showError('Impossible de charger les statistiques : ' + err.message);
    }).then(function () {
      if (token === loadToken) main.setAttribute('aria-busy', 'false');
    });
  }

  function scheduleSummaryRefresh(data) {
    clearTimeout(summaryTimer);
    if (data.range.to !== TODAY) return;
    summaryTimer = setTimeout(function tick() {
      if (document.hidden) { summaryTimer = setTimeout(tick, SUMMARY_EVERY); return; }
      load();
    }, SUMMARY_EVERY);
  }

  function refreshRealtime() {
    if (document.hidden) return;
    api({ action: 'realtime' }).then(renderRealtime).catch(function () { /* silencieux */ });
  }

  /* ---------- Rendu ---------- */

  function render(data) {
    renderPeriodLabel(data.range);
    renderKpis(data);
    renderChart(data);
    renderSeriesTable(data);
    renderPages(data.pages);
    renderSources(data.sources, data.totals.visits);
    renderDevices(data.devices);
    renderRealtime(data.realtime);
    $('foot-meta').textContent = 'Calculé en ' + fmtInt.format(Math.round(data.meta.query_ms)) + ' ms · fuseau ' + data.meta.timezone + ' · AM Stats';
  }

  function renderPeriodLabel(range) {
    var name = PERIOD_NAMES[range.period] || '';
    var text;
    if (range.from === range.to) {
      text = name + ' · ' + fmtLongDay.format(parseDay(range.from));
    } else {
      text = name + ' · du ' + fmtShortDay.format(parseDay(range.from)) + ' au ' + fmtLongDay.format(parseDay(range.to)).replace(/^\S+\s/, '') + ' (' + range.days + ' jours)';
    }
    $('period-label').textContent = text;
  }

  function deltaNode(target, current, previous, previousRange) {
    var node = $(target);
    node.replaceChildren();
    var context = range2text(previousRange);
    if (!previous) {
      node.appendChild(el('span', 'delta-flat', current ? 'Nouveau' : '—'));
      node.appendChild(document.createTextNode(' ' + context));
      return;
    }
    var ratio = (current - previous) / previous;
    var cls = Math.abs(ratio) < 0.005 ? 'delta-flat' : (ratio > 0 ? 'delta-up' : 'delta-down');
    var text = cls === 'delta-flat' ? '= stable' : (cls === 'delta-up' ? '▲ ' : '▼ ') + fmtDelta.format(ratio);
    var span = el('span', cls, text);
    span.setAttribute('aria-label', (ratio >= 0 ? 'hausse de ' : 'baisse de ') + fmtPct.format(Math.abs(ratio)));
    node.appendChild(span);
    node.appendChild(document.createTextNode(' ' + context));
  }

  function range2text(prev) {
    if (!prev) return '';
    var base = prev.from === prev.to
      ? 'vs ' + fmtWeekDay.format(parseDay(prev.from))
      : 'vs ' + fmtShortDay.format(parseDay(prev.from)) + ' – ' + fmtShortDay.format(parseDay(prev.to));
    return base + (prev.same_hour ? ' (même heure)' : '');
  }

  function renderKpis(data) {
    var t = data.totals, p = data.previous, prevRange = data.range.previous;
    $('kpi-visitors').textContent = fmtInt.format(t.visitors);
    $('kpi-pageviews').textContent = fmtInt.format(t.pageviews);
    $('kpi-ratio').textContent = t.visitors ? fmtDec.format(t.ratio) : '—';
    deltaNode('kpi-visitors-delta', t.visitors, p.visitors, prevRange);
    deltaNode('kpi-pageviews-delta', t.pageviews, p.pageviews, prevRange);
    deltaNode('kpi-ratio-delta', t.ratio, p.ratio, prevRange);
  }

  function renderRealtime(rt) {
    if (!rt) return;
    $('kpi-active').textContent = fmtInt.format(rt.active);
    $('live-updated').textContent = 'mis à jour à ' + fmtTime.format(new Date());
    var strip = $('live-strip');
    var list = $('live-pages');
    list.replaceChildren();
    if (!rt.pages || !rt.pages.length) { strip.hidden = true; return; }
    rt.pages.forEach(function (pg) {
      var li = el('li');
      li.title = pg.path;
      li.appendChild(document.createTextNode(pg.path));
      var badge = el('strong', 'live-count', fmtInt.format(pg.visitors));
      badge.title = pg.visitors + ' visiteur' + (pg.visitors > 1 ? 's' : '') + ' sur cette page';
      li.appendChild(badge);
      list.appendChild(li);
    });
    strip.hidden = false;
  }

  /* ---------- Graphique (Chart.js) ---------- */

  var crosshair = {
    id: 'amCrosshair',
    beforeDatasetsDraw: function (c) {
      var active = c.tooltip && c.tooltip.getActiveElements ? c.tooltip.getActiveElements() : [];
      if (!active || !active.length) return;
      var x = active[0].element.x;
      var area = c.chartArea;
      var g = c.ctx;
      g.save();
      g.beginPath();
      g.moveTo(x, area.top);
      g.lineTo(x, area.bottom);
      g.lineWidth = 1;
      g.strokeStyle = cssVar('--baseline');
      g.stroke();
      g.restore();
    }
  };

  function chartLabels(data) {
    var hourly = data.range.granularity === 'hour';
    return data.series.labels.map(function (l) {
      return hourly ? (l + ' h') : fmtShortDay.format(parseDay(l));
    });
  }

  function renderChart(data) {
    if (typeof window.Chart !== 'function') {
      showError('Chart.js n\'a pas pu être chargé (assets/vendor/chart.umd.min.js).');
      return;
    }
    var hourly = data.range.granularity === 'hour';
    var c = {
      s1: cssVar('--series-1'), s2: cssVar('--series-2'), grid: cssVar('--grid'),
      base: cssVar('--baseline'), muted: cssVar('--text-muted'), text: cssVar('--text'),
      surface: cssVar('--surface')
    };
    var labelsFull = data.series.labels.map(function (l) {
      return hourly
        ? (l + ' h – ' + (l + 1) + ' h')
        : fmtLongDay.format(parseDay(l));
    });

    function dataset(label, values, color) {
      return {
        label: label,
        data: values,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        tension: 0.25,
        pointRadius: values.length === 1 ? 4 : 0,
        pointHoverRadius: 5,
        pointHitRadius: 14,
        pointHoverBorderWidth: 2,
        pointHoverBorderColor: c.surface,
        spanGaps: false
      };
    }

    var config = {
      type: 'line',
      data: {
        labels: chartLabels(data),
        datasets: [
          dataset('Pages vues', data.series.pageviews, c.s1),
          dataset('Visiteurs uniques', data.series.visitors, c.s2)
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { top: 6, right: 6 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: c.surface,
            titleColor: c.text,
            bodyColor: c.text,
            borderColor: c.base,
            borderWidth: 1,
            padding: 10,
            cornerRadius: 8,
            boxWidth: 10,
            boxHeight: 10,
            usePointStyle: true,
            titleFont: { weight: '600' },
            callbacks: {
              title: function (items) { return items.length ? labelsFull[items[0].dataIndex] : ''; },
              label: function (item) {
                return ' ' + item.dataset.label + ' : ' + (item.parsed.y === null ? '—' : fmtInt.format(item.parsed.y));
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: c.base },
            ticks: { color: c.muted, maxRotation: 0, autoSkip: true, autoSkipPadding: 14, font: { size: 11 } }
          },
          y: {
            beginAtZero: true,
            grid: { color: c.grid, drawTicks: false },
            border: { display: false },
            ticks: {
              color: c.muted, padding: 8, precision: 0, maxTicksLimit: 6, font: { size: 11 },
              callback: function (v) { return fmtInt.format(v); }
            }
          }
        }
      },
      plugins: [crosshair]
    };

    if (chart) chart.destroy();
    chart = new window.Chart($('traffic-chart'), config);
  }

  function renderSeriesTable(data) {
    var hourly = data.range.granularity === 'hour';
    $('series-col').textContent = hourly ? 'Heure' : 'Jour';
    var tbody = $('series-table').tBodies[0];
    tbody.replaceChildren();
    data.series.labels.forEach(function (l, i) {
      var pv = data.series.pageviews[i];
      if (pv === null) return;
      var tr = el('tr');
      tr.appendChild(el('td', null, hourly ? (l + ' h') : fmtWeekDay.format(parseDay(l))));
      tr.appendChild(el('td', 'num', fmtInt.format(pv)));
      tr.appendChild(el('td', 'num', fmtInt.format(data.series.visitors[i])));
      tbody.appendChild(tr);
    });
  }

  /* ---------- Classements ---------- */

  function emptyRow(tbody, cols, text) {
    var tr = el('tr', 'empty');
    var td = el('td', null, text || 'Aucune donnée sur cette période');
    td.colSpan = cols;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function labelCell(share, content) {
    var td = el('td', 'label-cell');
    var bar = el('span', 'bar');
    bar.style.width = Math.max(0, Math.min(100, share * 100)).toFixed(2) + '%';
    var wrap = el('span', 'label-text');
    wrap.appendChild(content);
    td.appendChild(bar);
    td.appendChild(wrap);
    return td;
  }

  function renderPages(rows) {
    var tbody = $('pages-table').tBodies[0];
    tbody.replaceChildren();
    $('pages-meta').textContent = rows.length ? 'top ' + rows.length : '';
    if (!rows.length) return emptyRow(tbody, 3);
    var max = rows[0].pageviews || 1;
    rows.forEach(function (r) {
      var tr = el('tr');
      var link = el('a', null, r.path);
      link.href = SITE + r.path;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.title = r.path;
      tr.appendChild(labelCell(r.pageviews / max, link));
      tr.appendChild(el('td', 'num', fmtInt.format(r.pageviews)));
      tr.appendChild(el('td', 'num', fmtInt.format(r.visitors)));
      tbody.appendChild(tr);
    });
  }

  function sourceName(s) {
    return s === '(direct)' ? 'Accès direct / favoris' : s;
  }

  function renderSources(rows, totalVisits) {
    var tbody = $('sources-table').tBodies[0];
    tbody.replaceChildren();
    $('sources-meta').textContent = totalVisits ? fmtInt.format(totalVisits) + ' visites' : '';
    if (!rows.length) return emptyRow(tbody, 3);
    var max = rows[0].visits || 1;
    rows.forEach(function (r) {
      var tr = el('tr');
      var span = el('span', null, sourceName(r.source));
      span.title = sourceName(r.source);
      tr.appendChild(labelCell(r.visits / max, span));
      tr.appendChild(el('td', 'num', fmtInt.format(r.visits)));
      tr.appendChild(el('td', 'num', totalVisits ? fmtPct.format(r.visits / totalVisits) : '—'));
      tbody.appendChild(tr);
    });
  }

  function renderDevices(rows) {
    var tbody = $('devices-table').tBodies[0];
    var bar = $('device-bar');
    tbody.replaceChildren();
    bar.replaceChildren();
    var total = rows.reduce(function (s, r) { return s + r.visitors; }, 0);
    if (!total) {
      bar.hidden = true;
      return emptyRow(tbody, 3);
    }
    bar.hidden = false;
    var parts = [];
    rows.forEach(function (r) {
      var share = r.visitors / total;
      var name = DEVICE_NAMES[r.device] || r.device;
      if (r.visitors > 0) {
        var seg = el('span', 'dev-' + r.device);
        seg.style.flexGrow = String(r.visitors);
        seg.style.flexBasis = '0';
        seg.title = name + ' : ' + fmtPct.format(share);
        bar.appendChild(seg);
      }
      parts.push(name + ' ' + fmtPct.format(share));

      var tr = el('tr');
      var td = el('td');
      td.appendChild(el('span', 'dev-swatch dev-' + r.device));
      td.appendChild(document.createTextNode(name));
      tr.appendChild(td);
      tr.appendChild(el('td', 'num', fmtInt.format(r.visitors)));
      tr.appendChild(el('td', 'num', fmtPct.format(share)));
      tbody.appendChild(tr);
    });
    bar.setAttribute('aria-label', 'Répartition des visiteurs : ' + parts.join(', '));
  }

  /* ---------- Exclusion de son propre navigateur ---------- */

  function setupOptOut() {
    var box = $('optout');
    if (!box) return;
    try {
      box.checked = window.localStorage.getItem(OPT_KEY) === '1';
    } catch (e) {
      box.disabled = true;
      return;
    }
    box.addEventListener('change', function () {
      try {
        if (box.checked) window.localStorage.setItem(OPT_KEY, '1');
        else window.localStorage.removeItem(OPT_KEY);
      } catch (e) { /* stockage indisponible */ }
    });
  }

  /* ---------- Événements ---------- */

  document.querySelectorAll('.seg').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var p = btn.getAttribute('data-period');
      if (p === 'custom') {
        state.period = 'custom';
        if (!state.from || !state.to) {
          var to = parseDay(TODAY);
          var from = new Date(to.getTime() - 13 * 86400000);
          state.from = from.toISOString().slice(0, 10);
          state.to = TODAY;
        }
        syncControls();
        $('range-from').focus();
        return;
      }
      state.period = p;
      state.from = state.to = null;
      syncControls();
      writeStateToUrl();
      load();
    });
  });

  $('custom-range').addEventListener('submit', function (e) {
    e.preventDefault();
    var f = $('range-from').value, t = $('range-to').value;
    if (!f || !t) return;
    if (f > t) { var x = f; f = t; t = x; }
    state.period = 'custom';
    state.from = f;
    state.to = t;
    syncControls();
    writeStateToUrl();
    load();
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refreshRealtime();
  });

  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onScheme = function () { if (lastData) renderChart(lastData); };
    if (mq.addEventListener) mq.addEventListener('change', onScheme);
    else if (mq.addListener) mq.addListener(onScheme);
  }

  /* ---------- Démarrage ---------- */

  readStateFromUrl();
  syncControls();
  setupOptOut();
  load();
  setInterval(refreshRealtime, REALTIME_EVERY);
})();
