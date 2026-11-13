/*!
 * AM Stats — sonde de mesure d'audience sans cookie (v1.0.0)
 * Aucun cookie, aucun identifiant, rien n'est écrit sur l'appareil du visiteur
 * (sauf s'il demande lui-même à être exclu via amStats.optOut()).
 *
 * Intégration :
 *   <script defer src="https://www.example.com/am-stats/tracker.min.js"></script>
 * Options (attributs data-*) :
 *   data-endpoint="…/collect.php"  URL de réception (déduite de l'emplacement du script par défaut)
 *   data-spa="true"                 suit aussi les changements d'URL des applications monopage
 *   data-respect-dnt="true"         ignore les visiteurs ayant activé Do Not Track / GPC
 *   data-allow-local="true"         autorise localhost (tests)
 * Les mêmes options peuvent être définies avant le script : window.AM_STATS = { endpoint: '…' }.
 */
(function (w, d, n) {
  'use strict';

  var script = d.currentScript;
  var cfg = w.AM_STATS || {};
  var OPT_KEY = 'am_stats_ignore';

  function opt(name, key) {
    if (cfg[key] !== undefined) return String(cfg[key]);
    return script ? script.getAttribute('data-' + name) : null;
  }

  function storage(fn) {
    try { return fn(w.localStorage); } catch (e) { return null; }
  }

  // API publique (droit d'opposition) : amStats.optOut(), amStats.optIn(), amStats.isOptedOut()
  var api = w.amStats = {
    optOut: function () { storage(function (s) { s.setItem(OPT_KEY, '1'); }); },
    optIn: function () { storage(function (s) { s.removeItem(OPT_KEY); }); },
    isOptedOut: function () { return storage(function (s) { return s.getItem(OPT_KEY) === '1'; }) === true; }
  };

  var endpoint = opt('endpoint', 'endpoint') ||
    (script && script.src ? script.src.replace(/[^\/?#]*([?#].*)?$/, 'collect.php') : '');
  var loc = w.location;
  var ua = n.userAgent || '';

  // Filtrage de base : visiteur exclu, robots, navigateurs automatisés, protocoles exotiques.
  if (!endpoint || api.isOptedOut()) return;
  if (n.webdriver || w._phantom || w.callPhantom || w.__nightmare || w.Cypress || w.domAutomation ||
      /bot|crawl|spider|slurp|headless|lighthouse|phantom|selenium|puppeteer|playwright|preview/i.test(ua)) return;
  if (loc.protocol !== 'https:' && loc.protocol !== 'http:') return;
  if (/^(localhost|127(\.\d+){3}|\[::1\])$/.test(loc.hostname) && opt('allow-local', 'allowLocal') !== 'true') return;
  if (opt('respect-dnt', 'respectDnt') === 'true' &&
      (n.doNotTrack === '1' || w.doNotTrack === '1' || n.globalPrivacyControl === true)) return;

  /**
   * Type d'appareil d'après la largeur d'écran (en pixels CSS).
   * Sur écran tactile, on prend le plus petit côté pour neutraliser l'orientation.
   *   < 600 px                    → mobile
   *   600 à 1 099 px et tactile   → tablette
   *   sinon                       → ordinateur (desktop)
   */
  function device() {
    var s = w.screen || {};
    var coarse = !!(w.matchMedia && w.matchMedia('(pointer: coarse)').matches);
    var width = coarse ? Math.min(s.width || 0, s.height || 0) : (s.width || 0);
    if (!width) width = w.innerWidth || 0;
    if (width < 600) return 'mobile';
    if (coarse && width < 1100) return 'tablet';
    return 'desktop';
  }

  // Source de campagne éventuelle (?utm_source=…, ?ref=…, ?source=…). Le reste de
  // la chaîne de requête n'est jamais transmis (risque de données personnelles).
  function campaign() {
    var m = /[?&](?:utm_source|ref|source)=([^&#]*)/i.exec(loc.search);
    if (!m) return '';
    try { return decodeURIComponent(m[1].replace(/\+/g, ' ')).slice(0, 100); } catch (e) { return ''; }
  }

  var lastUrl = null;

  function send() {
    var url = loc.protocol + '//' + loc.host + loc.pathname;
    if (url === lastUrl) return;
    var first = lastUrl === null;
    lastUrl = url;

    var body = JSON.stringify({
      u: url,                       // URL sans paramètres ni ancre
      r: first ? d.referrer : '',   // provenance (seul le domaine est conservé côté serveur)
      s: campaign(),
      d: device()
    });

    var sent = false;
    try {
      if (n.sendBeacon) sent = n.sendBeacon(endpoint, new Blob([body], { type: 'text/plain;charset=UTF-8' }));
    } catch (e) { sent = false; }
    if (!sent && w.fetch) {
      try {
        w.fetch(endpoint, {
          method: 'POST', body: body, keepalive: true, mode: 'no-cors', credentials: 'omit',
          headers: { 'Content-Type': 'text/plain;charset=UTF-8' }
        })['catch'](function () {});
      } catch (e) { /* rien */ }
    }
  }

  // Pages préchargées (Speculation Rules / prerender) : on attend l'affichage réel.
  function track() {
    if (d.prerendering) {
      d.addEventListener('prerenderingchange', send, { once: true });
    } else if (d.visibilityState === 'prerender') {
      var onVisible = function () {
        if (d.visibilityState !== 'prerender') {
          d.removeEventListener('visibilitychange', onVisible);
          send();
        }
      };
      d.addEventListener('visibilitychange', onVisible);
    } else {
      send();
    }
  }

  // Applications monopage (optionnel).
  if (opt('spa', 'spa') === 'true' && w.history && w.history.pushState) {
    var push = w.history.pushState;
    w.history.pushState = function () {
      var result = push.apply(this, arguments);
      send();
      return result;
    };
    w.addEventListener('popstate', send);
  }

  // Retour arrière servi depuis le cache (bfcache) : nouvelle page vue.
  w.addEventListener('pageshow', function (e) {
    if (e.persisted) { lastUrl = null; send(); }
  });

  track();
})(window, document, navigator);
