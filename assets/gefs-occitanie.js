(function () {
  "use strict";

  var META = {
    mediane: { field: "precipitation_cumulative_median_mm", label: "Cumul médian", unit: "mm", probability: false },
    moyenne: { field: "precipitation_cumulative_mean_mm", label: "Cumul moyen", unit: "mm", probability: false },
    p10: { field: "precipitation_cumulative_p10_mm", label: "Scénario sec P10", unit: "mm", probability: false },
    p90: { field: "precipitation_cumulative_p90_mm", label: "Scénario humide P90", unit: "mm", probability: false },
    proba1: { field: "precipitation_cumulative_prob_ge_1mm_pct", label: "Probabilité ≥ 1 mm", unit: "%", probability: true },
    proba10: { field: "precipitation_cumulative_prob_ge_10mm_pct", label: "Probabilité ≥ 10 mm", unit: "%", probability: true },
    proba30: { field: "precipitation_cumulative_prob_ge_30mm_pct", label: "Probabilité ≥ 30 mm", unit: "%", probability: true },
    proba50: { field: "precipitation_cumulative_prob_ge_50mm_pct", label: "Probabilité ≥ 50 mm", unit: "%", probability: true }
  };

  var RAIN = [
    { max: 0.1, color: "#f8fafc", label: "0" }, { max: 1, color: "#bfdbfe", label: "0,1–0,9" },
    { max: 5, color: "#60a5fa", label: "1–4,9" }, { max: 10, color: "#2563eb", label: "5–9,9" },
    { max: 20, color: "#22c55e", label: "10–19,9" }, { max: 30, color: "#eab308", label: "20–29,9" },
    { max: 50, color: "#f97316", label: "30–49,9" }, { max: Infinity, color: "#dc2626", label: "≥ 50" }
  ];
  var PROB = [
    { max: 10, color: "#f8fafc", label: "< 10 %" }, { max: 30, color: "#bfdbfe", label: "10–29 %" },
    { max: 50, color: "#60a5fa", label: "30–49 %" }, { max: 70, color: "#22c55e", label: "50–69 %" },
    { max: 90, color: "#f59e0b", label: "70–89 %" }, { max: Infinity, color: "#dc2626", label: "≥ 90 %" }
  ];

  function finite(value) {
    var number = Number(value);
    return value === null || value === "" || !Number.isFinite(number) ? null : number;
  }

  function formatDate(value) {
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris" }).replace(",", "");
  }

  function resolveUrl(relative, base) {
    try { return new URL(relative, base).href; } catch (error) { return relative; }
  }

  function fetchJson(url) {
    return fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "_amgefs=" + Date.now(), { cache: "no-store" })
      .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); });
  }

  function color(value, palette) {
    for (var i = 0; i < palette.length; i += 1) if (value < palette[i].max) return palette[i].color;
    return palette[palette.length - 1].color;
  }

  function init(root) {
    var runSelect = root.querySelector(".js-gefs-run");
    var frameSelect = root.querySelector(".js-gefs-frame");
    var indicatorSelect = root.querySelector(".js-gefs-indicator");
    var loading = root.querySelector(".js-gefs-loading");
    var legend = root.querySelector(".js-gefs-legend");
    var dateNode = root.querySelector(".js-gefs-date");
    var note = root.querySelector(".js-gefs-note");
    var map = L.map(root.querySelector(".js-gefs-map"), { zoomControl: true }).setView([43.65, 2.25], 7);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 12, attribution: "© OpenStreetMap" }).addTo(map);
    map.setMaxBounds([[41.7, -0.9], [45.3, 5.3]]);
    var layer = L.layerGroup().addTo(map);
    var manifest = null;
    var runIndex = null;
    var frame = null;
    indicatorSelect.value = root.dataset.defaultIndicator || "mediane";

    function setLoading(message) { loading.textContent = message; loading.hidden = !message; }

    function buildLegend(meta) {
      var palette = meta.probability ? PROB : RAIN;
      legend.innerHTML = "<strong>" + meta.label + " (" + meta.unit + ")</strong>" + palette.map(function (item) {
        return '<span><i style="background:' + item.color + '"></i>' + item.label + "</span>";
      }).join("");
    }

    function render() {
      if (!frame) return;
      layer.clearLayers();
      var meta = META[indicatorSelect.value] || META.mediane;
      var values = frame.fields && frame.fields[meta.field];
      var grid = frame.grid || {};
      var latitudes = grid.latitudes || [], longitudes = grid.longitudes || [], mask = grid.mask || [];
      var palette = meta.probability ? PROB : RAIN;
      if (!values) { setLoading("Indicateur indisponible dans cette échéance."); return; }
      for (var r = 0; r < latitudes.length; r += 1) {
        for (var c = 0; c < longitudes.length; c += 1) {
          if (!mask[r] || Number(mask[r][c]) !== 1) continue;
          var value = finite(values[r] && values[r][c]);
          if (value === null) continue;
          var rectangle = L.rectangle([[latitudes[r] - 0.25, longitudes[c] - 0.25], [latitudes[r] + 0.25, longitudes[c] + 0.25]], {
            color: "#ffffff", weight: 0.5, fillColor: color(value, palette), fillOpacity: 0.78
          }).addTo(layer);
          rectangle.bindTooltip(meta.label + " : <strong>" + value.toLocaleString("fr-FR", { maximumFractionDigits: 1 }) + " " + meta.unit + "</strong>");
        }
      }
      var summary = frame.regional_summary_cumulative || {};
      root.querySelector(".js-gefs-median").textContent = finite(summary.median_mm) === null ? "—" : summary.median_mm.toLocaleString("fr-FR") + " mm";
      root.querySelector(".js-gefs-range").textContent = finite(summary.p10_mm) === null ? "—" : summary.p10_mm.toLocaleString("fr-FR") + "–" + summary.p90_mm.toLocaleString("fr-FR") + " mm";
      root.querySelector(".js-gefs-prob10").textContent = finite(summary.prob_ge_10mm_pct) === null ? "—" : summary.prob_ge_10mm_pct.toLocaleString("fr-FR") + " %";
      root.querySelector(".js-gefs-members").textContent = frame.member_count + " / 31";
      dateNode.textContent = "Valable " + formatDate(frame.valid_utc);
      buildLegend(meta);
      setLoading("");
    }

    function loadFrame() {
      if (!runIndex || !runIndex.frames || !runIndex.frames.length) return;
      var entry = runIndex.frames[Math.max(0, Math.min(runIndex.frames.length - 1, Number(frameSelect.value || 0)))];
      setLoading("Chargement GEFS +" + String(entry.forecast_hour).padStart(3, "0") + " h…");
      return fetchJson(resolveUrl(entry.file, runIndex._url)).then(function (data) {
        if (data.status !== "ok" || !data.grid || !data.fields) throw new Error("échéance invalide");
        frame = data;
        render();
      }).catch(function (error) { setLoading("Impossible de charger l’échéance : " + error.message); });
    }

    function loadRun() {
      if (!manifest || !manifest.runs || !manifest.runs.length) return;
      var entry = manifest.runs[Math.max(0, Math.min(manifest.runs.length - 1, Number(runSelect.value || 0)))];
      setLoading("Chargement du run " + entry.run_id + "…");
      return fetchJson(resolveUrl(entry.index, root.dataset.indexUrl)).then(function (data) {
        if (data.status !== "ok" || !Array.isArray(data.frames) || !data.frames.length) throw new Error("index du run invalide");
        data._url = resolveUrl(entry.index, root.dataset.indexUrl);
        runIndex = data;
        frameSelect.innerHTML = data.frames.map(function (item, index) {
          return '<option value="' + index + '">+' + String(item.forecast_hour).padStart(3, "0") + " h · " + formatDate(item.valid_utc) + "</option>";
        }).join("");
        var maxIndex = data.frames.reduce(function (best, item, index) {
          return Number(item.forecast_hour) > Number(data.frames[best].forecast_hour) ? index : best;
        }, 0);
        frameSelect.value = String(maxIndex);
        note.textContent = data.frames.length + " échéances · run " + data.run_id + " · prévision probabiliste automatique, sans valeur de vigilance officielle.";
        return loadFrame();
      }).catch(function (error) { setLoading("Impossible de charger le run : " + error.message); });
    }

    function loadManifest() {
      setLoading("Chargement des quatre runs GEFS…");
      return fetchJson(root.dataset.indexUrl).then(function (data) {
        if (data.status !== "ok" || !Array.isArray(data.runs) || !data.runs.length) throw new Error("aucun run publié");
        if (!data.area || !Array.isArray(data.area.codes) || data.area.codes.length !== 1 || data.area.codes[0] !== "76") throw new Error("les données publiées incluent encore PACA ; attente du prochain run Occitanie seule");
        manifest = data;
        runSelect.innerHTML = data.runs.map(function (item, index) {
          return '<option value="' + index + '">' + item.run_id + " · " + item.frame_count + " échéances</option>";
        }).join("");
        return loadRun();
      }).catch(function (error) { setLoading("Données GEFS indisponibles : " + error.message); });
    }

    runSelect.addEventListener("change", loadRun);
    frameSelect.addEventListener("change", loadFrame);
    indicatorSelect.addEventListener("change", render);
    root.querySelector(".js-gefs-refresh").addEventListener("click", loadManifest);
    loadManifest();
    window.setTimeout(function () { map.invalidateSize(); }, 100);
  }

  function boot() { document.querySelectorAll(".am-gefs").forEach(init); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
}());
