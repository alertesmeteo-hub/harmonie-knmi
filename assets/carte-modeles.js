(function () {
  "use strict";

  var BASE = "https://raw.githubusercontent.com/alertesmeteo-hub/";
  var MODELS = [
    { id: "GFS", label: "GFS 0,25°", range: "J+15", map: "native" },
    { id: "AROME", label: "AROME 1,3 km", repo: "arome-meteofrance", map: true },
    { id: "HARMONIE", label: "HARMONIE-AROME 5,5 km", repo: "harmonie", map: true },
    { id: "ARPEGE_EU", label: "ARPEGE Europe 0,1°", repo: "arpege-meteo-france", map: true },
    { id: "ARPEGE_GLOBAL", label: "ARPEGE Global 0,25°", repo: "ARPEGE-0.25", map: true },
    { id: "ECMWF", label: "ECMWF IFS 0,25°", repo: "cep", map: true },
    { id: "AIFS", label: "ECMWF AIFS 0,25°", repo: "aifs", map: true },
    { id: "ICON_EU", label: "ICON-EU 7 km", repo: "ICON-EU-7-km", map: false },
    { id: "ICON_GLOBAL", label: "ICON Global 13 km", repo: "ICON-GLOBAL-13-km", map: false },
    { id: "UKMO", label: "UKMO Global 10 km", repo: "UKMO-GLOBAL-10-km", map: false },
    { id: "GEFS", label: "GEFS (31 membres)", repo: "harmonie-knmi", map: false },
    { id: "GDPS", label: "GDPS", map: false }
  ];
  var VARIABLES = [
    ["pression", "Pression / isobares"],
    ["temperature", "Température 2 m"],
    ["pluie_1h", "Précipitations"],
    ["vent", "Vent 10 m"],
    ["rafales", "Rafales"]
  ];

  function dateLabel(value) {
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("fr-FR", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris"
    });
  }

  function boot(root) {
    if (root.dataset.modelsReady === "1" || !window.L) return;
    root.dataset.modelsReady = "1";
    var list = root.querySelector(".js-model-list");
    var view = root.querySelector(".js-model-view");
    var mapElement = root.querySelector(".js-model-map");
    var status = root.querySelector(".js-model-status");
    var variable = root.querySelector(".js-model-variable");
    var run = root.querySelector(".js-model-run");
    var range = root.querySelector(".js-model-range");
    var time = root.querySelector(".js-model-time");
    var previous = root.querySelector(".js-model-prev");
    var next = root.querySelector(".js-model-next");
    if (!list || !view || !mapElement) return;

    var current = null;
    var manifest = null;
    var modelMap = null;
    var image = null;
    var border = null;
    var request = 0;
    var buttons = {};

    function updateButtons() {
      MODELS.forEach(function (model) {
        var button = buttons[model.id];
        var gfsTab = root.querySelector('.js-mode-tab[data-mode="gfs"]');
        var active = current ? current.id === model.id : model.id === "GFS" && !!(gfsTab && gfsTab.classList.contains("is-active"));
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function showNative() {
      request += 1;
      current = null;
      manifest = null;
      root.classList.remove("am-pr--external-model");
      view.hidden = true;
      updateButtons();
      window.setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 50);
    }

    function statusText(message, error) {
      status.textContent = message;
      status.classList.toggle("is-error", !!error);
      status.hidden = false;
    }

    function clearImages() {
      if (modelMap && image) modelMap.removeLayer(image);
      if (modelMap && border) modelMap.removeLayer(border);
      image = null;
      border = null;
    }

    function imageUrl(path) {
      if (!current || !current.repo || !/^(?:maps\/)[a-zA-Z0-9_./-]+$/.test(path || "")) return null;
      return BASE + current.repo + "/data/" + path;
    }

    function renderFrame() {
      if (!manifest || !current || !modelMap) return;
      var step = manifest.steps[Number(range.value) || 0];
      var key = variable.value;
      var path = step && step.files && step.files[key];
      var url = imageUrl(path);
      if (!url) { clearImages(); statusText("Cette variable n’est pas disponible à cette échéance.", true); return; }
      var bounds = [[manifest.bounds.south, manifest.bounds.west], [manifest.bounds.north, manifest.bounds.east]];
      clearImages();
      image = window.L.imageOverlay(url, bounds, { opacity: 0.88, interactive: false, crossOrigin: true });
      image.on("load", function () { status.hidden = true; });
      image.on("error", function () { statusText("Image indisponible pour cette échéance. Réessayez dans quelques instants.", true); });
      image.addTo(modelMap);
      var borderUrl = imageUrl(manifest.overlay);
      if (borderUrl) border = window.L.imageOverlay(borderUrl, bounds, { opacity: 0.8, interactive: false }).addTo(modelMap);
      time.textContent = "+" + String(step.lead_hour).padStart(3, "0") + " h · " + dateLabel(step.valid_time);
      statusText("Chargement de la carte…", false);
    }

    function populateVariables() {
      variable.innerHTML = "";
      var layers = manifest.layers || {};
      VARIABLES.forEach(function (item) {
        if (!layers[item[0]]) return;
        var option = document.createElement("option");
        option.value = item[0];
        option.textContent = item[1];
        variable.appendChild(option);
      });
      variable.value = layers.pression ? "pression" : (variable.options[0] ? variable.options[0].value : "");
    }

    function showModel(model) {
      request += 1;
      var serial = request;
      current = model;
      manifest = null;
      root.classList.add("am-pr--external-model");
      view.hidden = false;
      updateButtons();
      clearImages();
      run.textContent = model.label;
      time.textContent = "—";
      if (!model.map) {
        variable.innerHTML = "";
        range.max = "0";
        range.value = "0";
        statusText("Aucune carte " + model.label + " n’est encore publiée dans un format compatible avec cette vue.", true);
        if (model.repo) {
          var source = document.createElement("a");
          source.href = "https://github.com/alertesmeteo-hub/" + model.repo;
          source.target = "_blank";
          source.rel = "noopener noreferrer";
          source.textContent = "Voir la source";
          status.appendChild(document.createTextNode(" "));
          status.appendChild(source);
        }
        if (modelMap) modelMap.invalidateSize();
        return;
      }
      statusText("Chargement du catalogue " + model.label + "…", false);
      var url = BASE + model.repo + "/data/maps/index.json";
      fetch(url + "?v=" + Date.now(), { cache: "no-store" })
        .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
        .then(function (data) {
          if (serial !== request) return;
          if (data.status !== "ok" || !Array.isArray(data.steps) || !data.steps.length || !data.bounds || !data.layers) throw new Error("catalogue incomplet");
          manifest = data;
          populateVariables();
          range.min = "0";
          range.max = String(data.steps.length - 1);
          range.value = "0";
          run.textContent = model.label + " · run " + dateLabel(data.run_time) + " · " + data.steps.length + " échéances";
          if (!modelMap) {
            modelMap = window.L.map(mapElement, { zoomControl: true, minZoom: 3, maxZoom: 10 });
            window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(modelMap);
          }
          var bounds = [[data.bounds.south, data.bounds.west], [data.bounds.north, data.bounds.east]];
          modelMap.invalidateSize();
          modelMap.fitBounds(bounds, { padding: [8, 8] });
          renderFrame();
          window.setTimeout(function () { modelMap.invalidateSize(); }, 80);
        })
        .catch(function (error) {
          if (serial !== request) return;
          statusText("Carte " + model.label + " indisponible : " + error.message, true);
        });
    }

    MODELS.forEach(function (model) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "am-pr__model-choice";
      button.dataset.model = model.id;
      var name = document.createElement("span");
      name.textContent = model.label;
      var badge = document.createElement("small");
      badge.textContent = model.map ? (model.range || "Carte") : "Sans carte";
      button.appendChild(name);
      button.appendChild(badge);
      button.addEventListener("click", function () {
        if (model.map === "native") {
          showNative();
          var gfsTab = root.querySelector('.js-mode-tab[data-mode="gfs"]');
          if (gfsTab) gfsTab.click();
        } else showModel(model);
      });
      buttons[model.id] = button;
      list.appendChild(button);
    });

    root.querySelectorAll(".js-mode-tab").forEach(function (button) {
      button.addEventListener("click", showNative);
    });
    variable.addEventListener("change", renderFrame);
    range.addEventListener("input", renderFrame);
    previous.addEventListener("click", function () { range.value = String(Math.max(0, Number(range.value) - 1)); renderFrame(); });
    next.addEventListener("click", function () { range.value = String(Math.min(Number(range.max), Number(range.value) + 1)); renderFrame(); });
    updateButtons();
  }

  function start() {
    document.querySelectorAll(".am-pr[data-gfs-index-url]").forEach(boot);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}());
