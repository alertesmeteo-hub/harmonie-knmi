(function () {
  "use strict";

  var AREAS = {
    france: {
      bounds: [[41.0, -5.8], [51.6, 10.1]],
      analysisBounds: [[41.0, -5.8], [51.6, 10.1]],
      center: [46.6, 2.25], zoom: 6, minZoom: 5,
      label: { south: 42.2, west: -4.5, north: 50.8, east: 8.5 }
    },
    europe: {
      // Cadrage d'affichage centré sur le continent.
      // L'analyse GFS conserve une emprise beaucoup plus large autour de l'Europe.
      bounds: [[34.0, -12.0], [71.5, 40.0]],
      analysisBounds: [[30.0, -35.0], [75.0, 50.0]],
      center: [52.0, 13.0], zoom: 4.75, minZoom: 3,
      label: { south: 35.0, west: -10.0, north: 70.5, east: 38.0 }
    }
  };

  var DENSITIES_REAL = { "tres-lisible": 78, "lisible": 60, "dense": 44, "toutes": 0 };
  var DENSITIES_GFS = { "tres-lisible": 180, "lisible": 150, "dense": 120, "toutes": 90 };

  var VARIABLE_META = {
    pression: { label: "Pression au niveau de la mer", unit: "hPa", decimals: 0, field: "pressure_hpa" },
    temperature: { label: "Température à 2 m", unit: "°C", decimals: 0, field: "temperature_2m_c" },
    pluie: { label: "Précipitations", unit: "mm", decimals: 1, field: "precipitation_mm" },
    vent: { label: "Vent moyen à 10 m", unit: "km/h", decimals: 0, field: "wind_speed_kmh" }
  };

  var LEGENDS = {
    pression: [
      { max: 990, color: "#6d28d9", label: "< 990" },
      { max: 1000, color: "#2563eb", label: "990–999" },
      { max: 1010, color: "#0ea5e9", label: "1000–1009" },
      { max: 1020, color: "#22c55e", label: "1010–1019" },
      { max: 1030, color: "#eab308", label: "1020–1029" },
      { max: 1040, color: "#f97316", label: "1030–1039" },
      { max: Infinity, color: "#dc2626", label: "≥ 1040" }
    ],
    temperature: [
      { max: -15, color: "#4c1d95", label: "< -15" },
      { max: -5, color: "#4338ca", label: "-15 à -6" },
      { max: 0, color: "#2563eb", label: "-5 à -1" },
      { max: 5, color: "#0ea5e9", label: "0 à 4" },
      { max: 10, color: "#14b8a6", label: "5 à 9" },
      { max: 15, color: "#22c55e", label: "10 à 14" },
      { max: 20, color: "#84cc16", label: "15 à 19" },
      { max: 25, color: "#eab308", label: "20 à 24" },
      { max: 30, color: "#f59e0b", label: "25 à 29" },
      { max: 35, color: "#f97316", label: "30 à 34" },
      { max: 40, color: "#ef4444", label: "35 à 39" },
      { max: Infinity, color: "#991b1b", label: "≥ 40" }
    ],
    pluie: [
      { max: 0.1, color: "#f8fafc", label: "0" },
      { max: 1, color: "#bfdbfe", label: "0,1–0,9" },
      { max: 3, color: "#60a5fa", label: "1–2,9" },
      { max: 7, color: "#2563eb", label: "3–6,9" },
      { max: 15, color: "#22c55e", label: "7–14,9" },
      { max: 30, color: "#eab308", label: "15–29,9" },
      { max: 50, color: "#f97316", label: "30–49,9" },
      { max: Infinity, color: "#dc2626", label: "≥ 50" }
    ],
    vent: [
      { max: 10, color: "#dbeafe", label: "< 10" },
      { max: 20, color: "#93c5fd", label: "10–19" },
      { max: 30, color: "#38bdf8", label: "20–29" },
      { max: 40, color: "#22c55e", label: "30–39" },
      { max: 50, color: "#eab308", label: "40–49" },
      { max: 70, color: "#f97316", label: "50–69" },
      { max: 90, color: "#ef4444", label: "70–89" },
      { max: Infinity, color: "#7f1d1d", label: "≥ 90" }
    ]
  };

  function finite(value) {
    if (value === null || value === undefined || value === "") return null;
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function safe(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  function formatNumber(value, decimals) {
    if (!Number.isFinite(Number(value))) return "—";
    var n = Number(value);
    var d = decimals == null ? 1 : decimals;
    if (Math.abs(n - Math.round(n)) < 0.05) d = 0;
    return n.toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function formatDate(value) {
    if (!value) return "—";
    var d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString("fr-FR", {
      weekday: "short", day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris"
    }).replace(",", "");
  }

  function formatValid(value) {
    if (!value) return "—";
    var d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString("fr-FR", {
      weekday: "short", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris"
    }).replace(",", "");
  }

  function normalized(value) {
    return String(value == null ? "" : value).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }

  function prettyName(value) {
    var text = String(value || "Station").trim();
    return text || "Station";
  }

  function seaLevelPressure(row) {
    var pmer = finite(row.pressure_msl != null ? row.pressure_msl : (row.pmer != null ? row.pmer : row.PMER));
    if (pmer !== null && pmer > 2000) pmer /= 100;
    if (pmer !== null && pmer >= 850 && pmer <= 1100) return { value: pmer, reduced: false };
    var stationPressure = finite(row.pressure != null ? row.pressure : (row.pres != null ? row.pres : row.PRES));
    if (stationPressure !== null && stationPressure > 2000) stationPressure /= 100;
    var altitude = finite(row.altitude_m != null ? row.altitude_m : row.altitude);
    var temperature = finite(row.temperature != null ? row.temperature : row.t);
    if (stationPressure !== null && stationPressure >= 700 && stationPressure <= 1100 && altitude !== null && altitude >= -50 && altitude <= 3000) {
      var tk = (temperature !== null ? temperature : 12) + 273.15;
      var reduced = stationPressure * Math.exp((9.80665 * altitude) / (287.05 * tk));
      if (reduced >= 850 && reduced <= 1100) return { value: reduced, reduced: true };
    }
    return { value: null, reduced: false };
  }

  function normalizeRealRow(row) {
    if (!row || finite(row.lat) === null || finite(row.lon) === null) return null;
    var pressure = seaLevelPressure(row);
    return {
      pressure: pressure.value,
      reduced: pressure.reduced,
      source: row
    };
  }

  function passesPressure(row, rule) {
    if (!rule || rule === "all") return true;
    if (row.pressure === null) return false;
    var parts = rule.split(":");
    if (parts[0] === "lt") return row.pressure < Number(parts[1]);
    if (parts[0] === "ge") return row.pressure >= Number(parts[1]);
    if (parts[0] === "between") return row.pressure >= Number(parts[1]) && row.pressure < Number(parts[2]);
    return true;
  }

  function passesAltitude(row, rule) {
    if (!rule || rule === "all") return true;
    var altitude = finite(row.altitude_m != null ? row.altitude_m : row.altitude);
    if (altitude === null) return false;
    var parts = rule.split(":");
    if (parts[0] === "lt") return altitude < Number(parts[1]);
    if (parts[0] === "ge") return altitude >= Number(parts[1]);
    if (parts[0] === "between") return altitude >= Number(parts[1]) && altitude < Number(parts[2]);
    return true;
  }

  function colorFor(variable, value) {
    var legend = LEGENDS[variable] || LEGENDS.pression;
    for (var i = 0; i < legend.length; i += 1) {
      if (value < legend[i].max) return legend[i].color;
    }
    return legend[legend.length - 1].color;
  }

  function textColor(variable, value) {
    if (variable === "pluie" && value < 1) return "#1e3a5f";
    if (variable === "temperature" && value >= 10 && value < 30) return "#17221a";
    if (variable === "pression" && value >= 1005 && value < 1035) return "#17221a";
    if (variable === "vent" && value < 40) return "#17221a";
    return "#fff";
  }

  function windArrow(deg) {
    if (!Number.isFinite(Number(deg))) return "";
    var arrows = ["↓", "↙", "←", "↖", "↑", "↗", "→", "↘"];
    return arrows[Math.round((Number(deg) % 360) / 45) % 8];
  }

  function variableValue(frame, variable, row, col) {
    var meta = VARIABLE_META[variable] || VARIABLE_META.pression;
    var matrix = frame.fields && frame.fields[meta.field];
    if (!Array.isArray(matrix) || !Array.isArray(matrix[row])) return null;
    return finite(matrix[row][col]);
  }

  function gfsPoint(frame, row, col) {
    var lat = frame.grid && Array.isArray(frame.grid.latitudes) ? finite(frame.grid.latitudes[row]) : null;
    var lon = frame.grid && Array.isArray(frame.grid.longitudes) ? finite(frame.grid.longitudes[col]) : null;
    if (lat === null || lon === null) return null;
    return { lat: lat, lon: lon };
  }

  function subsetGfs(frame, area, variable) {
    var result = [];
    if (!frame || !frame.grid || !frame.fields) return result;
    var lats = frame.grid.latitudes || [];
    var lons = frame.grid.longitudes || [];
    var b = AREAS[area].bounds;
    var south = b[0][0], west = b[0][1], north = b[1][0], east = b[1][1];
    for (var r = 0; r < lats.length; r += 1) {
      var lat = finite(lats[r]);
      if (lat === null || lat < south || lat > north) continue;
      for (var c = 0; c < lons.length; c += 1) {
        var lon = finite(lons[c]);
        if (lon === null || lon < west || lon > east) continue;
        var value = variableValue(frame, variable, r, c);
        if (value === null) continue;
        result.push({ row: r, col: c, lat: lat, lon: lon, value: value });
      }
    }
    return result;
  }

  function buildGfsPressureGrid(frame, area) {
    if (!frame || !frame.grid || !frame.fields || !Array.isArray(frame.fields.pressure_hpa)) return null;
    var b = AREAS[area].analysisBounds || AREAS[area].bounds;
    var lats = frame.grid.latitudes || [];
    var lons = frame.grid.longitudes || [];
    var rowIdx = [], colIdx = [];

    // En vue Europe, le fichier GFS est déjà téléchargé sur toute l'emprise
    // synoptique (-35W / 50E et 30N / 75N). On utilise donc toute la grille
    // disponible pour les isobares afin d'éviter les coupures créées par un
    // second sous-découpage côté navigateur. La vue France reste limitée.
    if (area === "europe") {
      for (var ri = 0; ri < lats.length; ri += 1) {
        if (finite(lats[ri]) !== null) rowIdx.push(ri);
      }
      for (var ci = 0; ci < lons.length; ci += 1) {
        if (finite(lons[ci]) !== null) colIdx.push(ci);
      }
    } else {
      lats.forEach(function (lat, i) { if (lat >= b[0][0] && lat <= b[1][0]) rowIdx.push(i); });
      lons.forEach(function (lon, i) { if (lon >= b[0][1] && lon <= b[1][1]) colIdx.push(i); });
    }
    if (rowIdx.length < 3 || colIdx.length < 3) return null;
    var values = rowIdx.map(function (ri) {
      return colIdx.map(function (ci) { return finite(frame.fields.pressure_hpa[ri][ci]); });
    });
    var latitudes = rowIdx.map(function (i) { return Number(lats[i]); });
    var longitudes = colIdx.map(function (i) { return Number(lons[i]); });
    var dy = latitudes.length > 1 ? latitudes[1] - latitudes[0] : 0.5;
    var dx = longitudes.length > 1 ? longitudes[1] - longitudes[0] : 0.5;
    return {
      values: values,
      nx: longitudes.length,
      ny: latitudes.length,
      dx: dx,
      dy: dy,
      bounds: { west: longitudes[0], south: latitudes[0], east: longitudes[longitudes.length - 1], north: latitudes[latitudes.length - 1] },
      lats: latitudes,
      lons: longitudes,
      coordinateGrid: true
    };
  }

  function idwValue(lat, lon, stations) {
    var nearest = [];
    for (var i = 0; i < stations.length; i += 1) {
      var s = stations[i];
      var dx = (lon - s.lon) * Math.cos(lat * Math.PI / 180);
      var dy = lat - s.lat;
      var d2 = dx * dx + dy * dy;
      if (d2 < 0.000001) return s.pressure;
      if (nearest.length < 16) {
        nearest.push({ d2: d2, p: s.pressure });
        if (nearest.length === 16) nearest.sort(function (a, b) { return a.d2 - b.d2; });
      } else if (d2 < nearest[15].d2) {
        nearest[15] = { d2: d2, p: s.pressure };
        nearest.sort(function (a, b) { return a.d2 - b.d2; });
      }
    }
    if (!nearest.length) return null;
    var sum = 0, weights = 0;
    for (var j = 0; j < nearest.length; j += 1) {
      var w = 1 / Math.pow(nearest[j].d2 + 0.0035, 1.12);
      sum += nearest[j].p * w;
      weights += w;
    }
    return weights ? sum / weights : null;
  }

  function buildRealPressureGrid(rows, area) {
    var stations = rows.filter(function (row) { return row.pressure !== null; }).map(function (row) {
      return { lat: Number(row.source.lat), lon: Number(row.source.lon), pressure: row.pressure };
    });
    if (stations.length < 12) return null;
    var b = AREAS[area].analysisBounds || AREAS[area].bounds;
    var bounds = { south: b[0][0], west: b[0][1], north: b[1][0], east: b[1][1], nx: area === "europe" ? 150 : 116, ny: area === "europe" ? 88 : 82 };
    var nx = bounds.nx, ny = bounds.ny;
    var values = new Array(ny);
    var dy = (bounds.north - bounds.south) / (ny - 1);
    var dx = (bounds.east - bounds.west) / (nx - 1);
    for (var y = 0; y < ny; y += 1) {
      values[y] = new Array(nx);
      var lat = bounds.south + y * dy;
      for (var x = 0; x < nx; x += 1) {
        var lon = bounds.west + x * dx;
        values[y][x] = idwValue(lat, lon, stations);
      }
    }
    return { values: values, dx: dx, dy: dy, bounds: bounds, nx: nx, ny: ny, coordinateGrid: false };
  }

  function edgePoint(edge, x, y, level, grid) {
    var vals = grid.values;
    var a, b, t;
    function ratio(v1, v2) {
      if (v1 === v2) return 0.5;
      return Math.max(0, Math.min(1, (level - v1) / (v2 - v1)));
    }
    function pointAt(xx, yy) {
      if (grid.coordinateGrid) return [grid.lats[yy], grid.lons[xx]];
      return [grid.bounds.south + yy * grid.dy, grid.bounds.west + xx * grid.dx];
    }
    if (edge === 0) {
      a = vals[y + 1][x]; b = vals[y + 1][x + 1]; t = ratio(a, b);
      var p0 = pointAt(x, y + 1), p1 = pointAt(x + 1, y + 1);
      return [p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1])];
    }
    if (edge === 1) {
      a = vals[y + 1][x + 1]; b = vals[y][x + 1]; t = ratio(a, b);
      var p2 = pointAt(x + 1, y + 1), p3 = pointAt(x + 1, y);
      return [p2[0] + t * (p3[0] - p2[0]), p2[1] + t * (p3[1] - p2[1])];
    }
    if (edge === 2) {
      a = vals[y][x]; b = vals[y][x + 1]; t = ratio(a, b);
      var p4 = pointAt(x, y), p5 = pointAt(x + 1, y);
      return [p4[0] + t * (p5[0] - p4[0]), p4[1] + t * (p5[1] - p4[1])];
    }
    a = vals[y + 1][x]; b = vals[y][x]; t = ratio(a, b);
    var p6 = pointAt(x, y + 1), p7 = pointAt(x, y);
    return [p6[0] + t * (p7[0] - p6[0]), p6[1] + t * (p7[1] - p6[1])];
  }

  function pairsFor(code, centerHigh) {
    switch (code) {
      case 1: return [[0, 3]];
      case 2: return [[0, 1]];
      case 3: return [[3, 1]];
      case 4: return [[1, 2]];
      case 5: return centerHigh ? [[0, 1], [2, 3]] : [[0, 3], [1, 2]];
      case 6: return [[0, 2]];
      case 7: return [[3, 2]];
      case 8: return [[2, 3]];
      case 9: return [[0, 2]];
      case 10: return centerHigh ? [[0, 3], [1, 2]] : [[0, 1], [2, 3]];
      case 11: return [[1, 2]];
      case 12: return [[3, 1]];
      case 13: return [[0, 1]];
      case 14: return [[0, 3]];
      default: return [];
    }
  }

  function contourSegments(level, grid) {
    var result = [];
    for (var y = 0; y < grid.ny - 1; y += 1) {
      for (var x = 0; x < grid.nx - 1; x += 1) {
        var bl = grid.values[y][x], br = grid.values[y][x + 1];
        var tl = grid.values[y + 1][x], tr = grid.values[y + 1][x + 1];
        if ([bl, br, tl, tr].some(function (v) { return v === null || !Number.isFinite(v); })) continue;
        var code = (tl >= level ? 1 : 0) | (tr >= level ? 2 : 0) | (br >= level ? 4 : 0) | (bl >= level ? 8 : 0);
        if (code === 0 || code === 15) continue;
        var centerHigh = ((tl + tr + br + bl) / 4) >= level;
        var pairs = pairsFor(code, centerHigh);
        for (var p = 0; p < pairs.length; p += 1) {
          result.push([edgePoint(pairs[p][0], x, y, level, grid), edgePoint(pairs[p][1], x, y, level, grid)]);
        }
      }
    }
    return result;
  }

  function pointKey(point) {
    return Number(point[0]).toFixed(5) + ":" + Number(point[1]).toFixed(5);
  }

  function joinContourSegments(segments) {
    if (!segments || !segments.length) return [];
    var endpointMap = new Map(), used = new Array(segments.length).fill(false);
    segments.forEach(function (segment, index) {
      [0, 1].forEach(function (end) {
        var key = pointKey(segment[end]);
        if (!endpointMap.has(key)) endpointMap.set(key, []);
        endpointMap.get(key).push({ index: index, end: end });
      });
    });
    function nextAttached(key) {
      var candidates = endpointMap.get(key) || [];
      for (var i = 0; i < candidates.length; i += 1) if (!used[candidates[i].index]) return candidates[i];
      return null;
    }
    var paths = [];
    for (var start = 0; start < segments.length; start += 1) {
      if (used[start]) continue;
      used[start] = true;
      var line = [segments[start][0], segments[start][1]];
      while (true) {
        var tail = nextAttached(pointKey(line[line.length - 1]));
        if (!tail) break;
        used[tail.index] = true;
        var segTail = segments[tail.index];
        line.push(segTail[tail.end === 0 ? 1 : 0]);
        if (pointKey(line[line.length - 1]) === pointKey(line[0])) break;
      }
      while (pointKey(line[0]) !== pointKey(line[line.length - 1])) {
        var head = nextAttached(pointKey(line[0]));
        if (!head) break;
        used[head.index] = true;
        var segHead = segments[head.index];
        line.unshift(segHead[head.end === 0 ? 1 : 0]);
      }
      if (line.length >= 2) paths.push(line);
    }
    return paths;
  }

  function smoothContourPath(points, iterations) {
    if (!points || points.length < 4) return points || [];
    var out = points.slice(), passes = Math.max(1, iterations || 1);
    for (var pass = 0; pass < passes; pass += 1) {
      if (out.length < 4) break;
      var closed = pointKey(out[0]) === pointKey(out[out.length - 1]);
      var source = closed ? out.slice(0, -1) : out.slice(), next = [];
      if (closed) {
        for (var i = 0; i < source.length; i += 1) {
          var p0 = source[i], p1 = source[(i + 1) % source.length];
          next.push([0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1]]);
          next.push([0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1]]);
        }
        if (next.length) next.push(next[0].slice());
      } else {
        next.push(source[0]);
        for (var j = 0; j < source.length - 1; j += 1) {
          var a = source[j], b = source[j + 1];
          next.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
          next.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
        }
        next.push(source[source.length - 1]);
      }
      out = next;
    }
    return out;
  }

  function relaxContourPath(points, passes, strength) {
    if (!points || points.length < 5) return points || [];
    var out = points.map(function (p) { return p.slice(); });
    var rounds = Math.max(0, passes || 0);
    var k = Math.max(0.05, Math.min(0.45, strength == null ? 0.28 : strength));
    for (var pass = 0; pass < rounds; pass += 1) {
      var closed = pointKey(out[0]) === pointKey(out[out.length - 1]);
      var source = closed ? out.slice(0, -1) : out.slice();
      var next = source.map(function (p) { return p.slice(); });
      var start = closed ? 0 : 1;
      var end = closed ? source.length : source.length - 1;
      for (var i = start; i < end; i += 1) {
        var prev = source[(i - 1 + source.length) % source.length];
        var cur = source[i];
        var nxt = source[(i + 1) % source.length];
        next[i] = [
          (1 - 2 * k) * cur[0] + k * prev[0] + k * nxt[0],
          (1 - 2 * k) * cur[1] + k * prev[1] + k * nxt[1]
        ];
      }
      if (closed && next.length) next.push(next[0].slice());
      out = next;
    }
    return out;
  }

  function smoothPressureGrid(grid, passes) {
    if (!grid || !grid.values || !passes) return grid;
    var current = grid.values.map(function (row) { return row.slice(); });
    for (var pass = 0; pass < passes; pass += 1) {
      var next = current.map(function (row) { return row.slice(); });
      for (var y = 1; y < grid.ny - 1; y += 1) {
        for (var x = 1; x < grid.nx - 1; x += 1) {
          var c = current[y][x];
          var n = current[y + 1][x], s = current[y - 1][x];
          var e = current[y][x + 1], w = current[y][x - 1];
          var ne = current[y + 1][x + 1], nw = current[y + 1][x - 1];
          var se = current[y - 1][x + 1], sw = current[y - 1][x - 1];
          if ([c, n, s, e, w, ne, nw, se, sw].some(function (v) { return !Number.isFinite(v); })) continue;
          next[y][x] = (4 * c + 2 * (n + s + e + w) + ne + nw + se + sw) / 16;
        }
      }
      current = next;
    }
    var copy = Object.assign({}, grid);
    copy.values = current;
    return copy;
  }

  function buildSmoothContours(level, grid) {
    // On lisse d'abord le champ de pression, puis la géométrie de la courbe.
    // Cela évite l'effet « marches d'escalier » du Marching Squares sans
    // exploser le nombre de sommets comme le ferait un lissage géométrique excessif.
    var workingGrid = smoothPressureGrid(grid, grid.coordinateGrid ? 4 : 5);
    return joinContourSegments(contourSegments(level, workingGrid)).map(function (path) {
      // Relaxation douce puis Chaikin : les angles résiduels disparaissent sans
      // déplacer brutalement les centres d'action.
      var relaxed = relaxContourPath(path, grid.coordinateGrid ? 3 : 4, grid.coordinateGrid ? 0.24 : 0.27);
      return smoothContourPath(relaxed, grid.coordinateGrid ? 5 : 6);
    }).filter(function (path) { return path.length >= 4; });
  }

  function init(root) {
    if (typeof window.L === "undefined") {
      root.querySelector(".js-loading").textContent = "La bibliothèque cartographique n’a pas pu être chargée.";
      return;
    }

    var mapEl = root.querySelector(".js-map");
    mapEl.style.height = (Number(root.dataset.mapHeight) || 650) + "px";

    var modeTabs = Array.prototype.slice.call(root.querySelectorAll(".js-mode-tab"));
    var areaSelect = root.querySelector(".js-area");
    var areaWrap = root.querySelector(".js-area-wrap");
    var periodWrap = root.querySelector(".js-period-wrap");
    var isobarsWrap = root.querySelector(".js-isobars-wrap");
    var intervalWrap = root.querySelector(".js-interval-wrap");
    var densityWrap = root.querySelector(".js-density-wrap");
    var variableSelect = root.querySelector(".js-variable");
    var periodSelect = root.querySelector(".js-period");
    var filterSelect = root.querySelector(".js-filter");
    var isobarsSelect = root.querySelector(".js-isobars");
    var intervalSelect = root.querySelector(".js-interval");
    var densitySelect = root.querySelector(".js-density");
    var altitudeSelect = root.querySelector(".js-altitude");
    var searchInput = root.querySelector(".js-search");
    var resetButton = root.querySelector(".js-reset");
    var refreshButton = root.querySelector(".js-refresh");
    var loading = root.querySelector(".js-loading");
    var legendEl = root.querySelector(".js-legend");
    var notice = root.querySelector(".js-notice");
    var dateEl = root.querySelector(".js-date");
    var eyebrow = root.querySelector(".js-eyebrow");
    var subtitle = root.querySelector(".js-subtitle");
    var explain = root.querySelector(".js-explain");
    var periodTitle = root.querySelector(".js-period-title");
    var variableWrap = root.querySelector(".js-variable-wrap");
    var pressureFilterWrap = root.querySelector(".js-pressure-filter-wrap");
    var altitudeWrap = root.querySelector(".js-altitude-wrap");
    var searchWrap = root.querySelector(".js-search-wrap");
    var minLabel = root.querySelector(".js-min-label");
    var maxLabel = root.querySelector(".js-max-label");
    var countLabel = root.querySelector(".js-count-label");
    var rangeLabel = root.querySelector(".js-range-label");
    var minEl = root.querySelector(".js-min");
    var maxEl = root.querySelector(".js-max");
    var countEl = root.querySelector(".js-count");
    var rangeEl = root.querySelector(".js-range");
    var timeline = root.querySelector(".js-timeline");
    var timelinePlayer = root.querySelector(".js-timeline-player");
    var timelineRange = root.querySelector(".js-timeline-range");
    var timelineLabel = root.querySelector(".js-timeline-label");
    var riskStrip = root.querySelector(".js-riskstrip");
    var prevButton = root.querySelector(".js-prev");
    var playButton = root.querySelector(".js-play");
    var nextButton = root.querySelector(".js-next");
    var speedSelect = root.querySelector(".js-speed");

    var allowedMode = ["reel", "gfs", "compare"].indexOf(root.dataset.defaultMode) >= 0 ? root.dataset.defaultMode : "reel";
    var mode = allowedMode;
    areaSelect.value = AREAS[root.dataset.defaultArea] ? root.dataset.defaultArea : "france";
    if (mode === "compare") areaSelect.value = "europe";
    variableSelect.value = VARIABLE_META[root.dataset.defaultVariable] ? root.dataset.defaultVariable : "pression";
    densitySelect.value = DENSITIES_REAL[root.dataset.defaultDensity] !== undefined ? root.dataset.defaultDensity : "tres-lisible";
    intervalSelect.value = ["2", "4", "5"].indexOf(String(root.dataset.defaultInterval)) >= 0 ? String(root.dataset.defaultInterval) : "2";

    var initial = AREAS[areaSelect.value];
    var map = window.L.map(mapEl, { zoomControl: true, minZoom: initial.minZoom, maxZoom: 10, maxBoundsViscosity: 1.0, preferCanvas: true, zoomSnap: 0.25, zoomDelta: 0.25 });
    var tile = window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© OpenStreetMap"
    }).addTo(map);
    var stationLayer = window.L.layerGroup().addTo(map);
    var gfsLayer = window.L.layerGroup().addTo(map);
    var isobarLayer = window.L.layerGroup().addTo(map);
    var compareRealIsobars = window.L.layerGroup().addTo(map);
    var compareGfsIsobars = window.L.layerGroup().addTo(map);
    var centerLayer = window.L.layerGroup().addTo(map);

    var realData = null, gfsIndex = null, gfsFrame = null;
    var requestSerial = 0, renderTimer = null, contourCache = {}, playTimer = null, playRunning = false;
    var playbackToken = 0, frameCache = new Map(), selectedRiskKey = null;

    function areaConfig() { return AREAS[areaSelect.value] || AREAS.france; }

    function applyAreaView(reset) {
      var cfg = areaConfig();
      map.setMinZoom(cfg.minZoom);
      map.setMaxBounds(window.L.latLngBounds(cfg.bounds).pad(0.04));
      // L'analyse GFS reste large (-35/+50°), mais l'affichage Europe est centré
      // sur le continent. fitBounds sur un écran large ajoutait trop d'Atlantique
      // et faisait apparaître le Groenland / l'Asie centrale.
      if (areaSelect.value === "europe") {
        var width = mapEl.clientWidth || root.clientWidth || 1000;
        var europeZoom = width >= 1050 ? 4.75 : (width >= 720 ? 4.25 : 3.50);
        map.setView(cfg.center, europeZoom, { animate: false });
      } else if (reset) map.setView(cfg.center, cfg.zoom);
      else map.fitBounds(cfg.bounds, { padding: [6, 6] });
      window.setTimeout(function () { map.invalidateSize(); }, 50);
    }

    function modeUi() {
      modeTabs.forEach(function (button) {
        button.classList.toggle("is-active", button.dataset.mode === mode);
      });
      var gfs = mode === "gfs";
      var compare = mode === "compare";
      variableWrap.hidden = !gfs;
      // En comparaison, la zone est volontairement fixée sur l'Europe.
      if (compare) areaSelect.value = "europe";
      areaWrap.hidden = mode !== "gfs";
      pressureFilterWrap.hidden = gfs || compare;
      altitudeWrap.hidden = gfs || compare;
      searchWrap.hidden = gfs || compare;
      // GFS : l'échéance se choisit uniquement avec la timeline du bas.
      // Comparaison : on retire tous les réglages secondaires demandés.
      if (periodWrap) periodWrap.hidden = gfs || compare;
      if (isobarsWrap) isobarsWrap.hidden = gfs || compare;
      if (intervalWrap) intervalWrap.hidden = gfs || compare;
      if (densityWrap) densityWrap.hidden = compare;
      periodTitle.textContent = "Heure d’observation";
      timeline.hidden = !(gfs || compare);
      if (timelinePlayer) timelinePlayer.hidden = compare;
      if (gfs || compare) {
        isobarsSelect.value = "on";
        intervalSelect.value = "2";
      }
      if (!gfs) variableSelect.value = "pression";
      if (gfs) {
        eyebrow.textContent = "PRÉVISIONS NOAA / NCEP GFS 0,25° — J+15";
        subtitle.textContent = VARIABLE_META[variableSelect.value].label + " • France / Europe • timeline jusqu’à J+15 • extrêmes 15 jours";
        explain.textContent = "Le GFS est généré 4 fois par jour. La timeline affiche les échéances par pas de 6 h jusqu’à +360 h. En vue Europe, les isobares utilisent toute la grille synoptique GFS. Un clic sur une journée de l’indice 15 jours ouvre directement la carte de pression et ses isobares pour cette journée.";
      } else if (compare) {
        eyebrow.textContent = "COMPARAISON ANALYSE RÉELLE ↔ GFS";
        subtitle.textContent = "Isobares observées et GFS superposées • noir = réel • bleu = GFS";
        explain.textContent = "Comparaison de l’analyse de pression issue des observations avec le champ GFS. Les courbes sont calculées sur l’emprise sélectionnée (France ou Europe) et fortement lissées avant affichage.";
      } else if (areaSelect.value === "europe") {
        eyebrow.textContent = "OBSERVATIONS METAR EUROPE";
        subtitle.textContent = "Pression au niveau de la mer (SLP), sinon QNH/altimètre • isobares Europe complètes";
        explain.textContent = "Les isobares observées sont interpolées sur toute l’Europe à partir des stations disponibles, puis raccordées et fortement lissées. Elles constituent une analyse objective et non une prévision.";
      } else {
        eyebrow.textContent = "OBSERVATIONS MÉTÉO-FRANCE";
        subtitle.textContent = "Pression ramenée au niveau de la mer (PMER) • isobares limitées à la France et très lissées";
        explain.textContent = "Les isobares relient les zones de même pression au niveau de la mer. En vue France, elles sont désormais calculées et tracées uniquement sur l’emprise France, avec un double lissage du champ et des courbes.";
      }
      intervalSelect.disabled = isobarsSelect.value === "off";
    }

    function buildRealPeriods() {
      periodSelect.innerHTML = "";
      (realData && realData.hourly ? realData.hourly : []).forEach(function (period, index) {
        var option = document.createElement("option");
        option.value = String(index);
        option.textContent = period.local_label || formatDate(period.utc);
        periodSelect.appendChild(option);
      });
    }

    function resolveFrameUrl(entry) {
      if (!entry) return null;
      if (/^https?:\/\//i.test(entry.url || "")) return entry.url;
      var base = root.dataset.gfsIndexUrl || "";
      var slash = base.lastIndexOf("/");
      var dir = slash >= 0 ? base.slice(0, slash + 1) : base;
      return dir + (entry.file || entry.url || "");
    }

    function buildGfsPeriods() {
      var current = Number(periodSelect.value || 0);
      periodSelect.innerHTML = "";
      var frames = gfsIndex && Array.isArray(gfsIndex.frames) ? gfsIndex.frames : [];
      frames.forEach(function (entry, index) {
        var option = document.createElement("option");
        option.value = String(index);
        option.textContent = "+" + String(entry.forecast_hour).padStart(3, "0") + " h • " + formatValid(entry.valid_utc);
        periodSelect.appendChild(option);
      });
      if (frames.length) periodSelect.value = String(Math.max(0, Math.min(frames.length - 1, current)));
      if (timelineRange) {
        timelineRange.max = String(Math.max(0, frames.length - 1));
        timelineRange.value = periodSelect.value || "0";
      }
      renderRisks();
      updateTimelineUi();
    }

    function riskText(level) {
      if (level === "fort") return "Fort";
      if (level === "modere") return "Modéré";
      if (level === "faible") return "Faible";
      return "—";
    }

    function riskDayKey(day) {
      return String(day.date_utc || day.period_start_utc || day.period_end_utc || day.day || "");
    }

    function frameIndexForRiskDay(day) {
      var frames = gfsIndex && Array.isArray(gfsIndex.frames) ? gfsIndex.frames : [];
      if (!frames.length) return 0;

      var startMs = Date.parse(day.period_start_utc || day.date_utc || "");
      var endMs = Date.parse(day.period_end_utc || day.date_utc || "");
      var targetMs = NaN;
      if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) {
        targetMs = startMs + (endMs - startMs) / 2;
      } else if (Number.isFinite(startMs)) {
        targetMs = startMs + 12 * 3600 * 1000;
      } else if (Number.isFinite(endMs)) {
        targetMs = endMs - 12 * 3600 * 1000;
      }

      // Repli compatible avec les anciens index où seul day=1..15 est présent.
      if (!Number.isFinite(targetMs)) {
        var targetHour = Math.max(0, (Number(day.day || 1) - 1) * 24 + 12);
        var bestByHour = 0, bestHourDiff = Infinity;
        frames.forEach(function (entry, idx) {
          var diff = Math.abs(Number(entry.forecast_hour) - targetHour);
          if (diff < bestHourDiff) { bestHourDiff = diff; bestByHour = idx; }
        });
        return bestByHour;
      }

      var best = 0, bestDiff = Infinity;
      frames.forEach(function (entry, idx) {
        var validMs = Date.parse(entry.valid_utc || "");
        if (!Number.isFinite(validMs)) return;
        var diff = Math.abs(validMs - targetMs);
        // Préférence forte pour une échéance réellement incluse dans la journée.
        if (Number.isFinite(startMs) && Number.isFinite(endMs) && validMs >= startMs && validMs <= endMs) diff *= 0.2;
        if (diff < bestDiff) { bestDiff = diff; best = idx; }
      });
      return best;
    }

    function renderRisks() {
      if (!riskStrip) return;
      riskStrip.innerHTML = "";
      var area = areaSelect.value;
      var list = gfsIndex && gfsIndex.risks && gfsIndex.risks.by_area ? gfsIndex.risks.by_area[area] : null;
      if (!Array.isArray(list) || !list.length) {
        riskStrip.innerHTML = '<span class="am-pr__risk-empty">Indice 15 jours en attente du workflow GFS.</span>';
        return;
      }
      list.forEach(function (day) {
        var card = document.createElement("button");
        card.type = "button";
        card.className = "am-pr__riskday";
        card.dataset.day = String(day.day || 1);
        card.dataset.riskKey = riskDayKey(day);
        if (selectedRiskKey && card.dataset.riskKey === selectedRiskKey) card.classList.add("is-selected");
        var date = new Date(day.period_end_utc || day.date_utc);
        var label = Number.isNaN(date.getTime()) ? ("J+" + day.day) : date.toLocaleDateString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "Europe/Paris" });
        var storm = day.storm || {}, rain = day.heavy_rain || {};
        card.innerHTML = '<strong>' + safe(label) + '</strong>' +
          '<span>💨 <i class="risk-' + safe(storm.level || "indisponible") + '">' + safe(riskText(storm.level)) + '</i><small>' + (storm.max_gust_kmh == null ? "—" : safe(formatNumber(storm.max_gust_kmh, 0)) + " km/h") + '</small></span>' +
          '<span>🌧️ <i class="risk-' + safe(rain.level || "indisponible") + '">' + safe(riskText(rain.level)) + '</i><small>' + (rain.max_24h_mm == null ? "—" : safe(formatNumber(rain.max_24h_mm, 1)) + " mm/24h") + '</small></span>';
        card.title = "Afficher les isobares GFS Europe de cette journée";
        card.addEventListener("click", function (event) {
          event.preventDefault();
          event.stopPropagation();
          stopPlayback();

          // Le raccourci 15 jours ouvre toujours la synoptique Europe demandée.
          mode = "gfs";
          areaSelect.value = "europe";
          variableSelect.value = "pression";
          isobarsSelect.value = "on";
          intervalSelect.disabled = false;
          selectedRiskKey = riskDayKey(day);
          contourCache = {};
          modeUi();
          applyAreaView(false);

          var best = frameIndexForRiskDay(day);
          periodSelect.value = String(best);
          if (timelineRange) timelineRange.value = String(best);
          updateTimelineUi();

          riskStrip.querySelectorAll(".am-pr__riskday").forEach(function (item) {
            item.classList.toggle("is-selected", item.dataset.riskKey === selectedRiskKey);
          });

          loading.textContent = "Chargement des isobares GFS Europe pour " + label + "…";
          loading.hidden = false;
          window.requestAnimationFrame(function () { loadGfsFrame(); });
        });
        riskStrip.appendChild(card);
      });
    }

    function updateTimelineUi() {
      if (!timelineRange || !timelineLabel) return;
      var frames = gfsIndex && Array.isArray(gfsIndex.frames) ? gfsIndex.frames : [];
      var idx = Math.max(0, Math.min(frames.length - 1, Number(periodSelect.value || 0)));
      timelineRange.max = String(Math.max(0, frames.length - 1));
      timelineRange.value = String(idx);
      var entry = frames[idx];
      timelineLabel.textContent = entry ? ("+" + String(entry.forecast_hour).padStart(3, "0") + " h • " + formatValid(entry.valid_utc)) : "—";
    }

    function stopPlayback() {
      playRunning = false;
      playbackToken += 1;
      if (playTimer) window.clearTimeout(playTimer);
      playTimer = null;
      if (playButton) {
        playButton.disabled = false;
        playButton.textContent = "▶ Lecture";
      }
    }

    function stepTimeline(delta) {
      var frames = gfsIndex && Array.isArray(gfsIndex.frames) ? gfsIndex.frames : [];
      if (!frames.length) return;
      var idx = Math.max(0, Math.min(frames.length - 1, Number(periodSelect.value || 0) + delta));
      periodSelect.value = String(idx);
      timelineRange.value = String(idx);
      if (mode === "compare") loadCompareFrame(); else loadGfsFrame();
    }

    function currentRealPeriod() {
      var periods = realData && Array.isArray(realData.hourly) ? realData.hourly : [];
      if (mode === "compare") return periods[0] || { rows: [] };
      var index = Math.max(0, Math.min(periods.length - 1, Number(periodSelect.value || 0)));
      return periods[index] || { rows: [] };
    }

    function allRealRows() {
      return (currentRealPeriod().rows || []).map(normalizeRealRow).filter(function (row) {
        return row && row.pressure !== null && row.pressure >= 850 && row.pressure <= 1100;
      });
    }

    function filteredRealRows() {
      var rows = allRealRows();
      var query = normalized(searchInput.value.trim());
      return rows.filter(function (row) {
        var station = row.source;
        if (!passesPressure(row, filterSelect.value)) return false;
        if (!passesAltitude(station, altitudeSelect.value)) return false;
        if (query) {
          var haystack = normalized([station.name, station.id, station.icao_id, station.department, station.departement, station.dept].join(" "));
          if (haystack.indexOf(query) === -1) return false;
        }
        return true;
      }).sort(function (a, b) { return a.pressure - b.pressure; });
    }

    function thinRealRows(rows, spacing) {
      if (!spacing) return rows;
      var buckets = new Map(), zoom = map.getZoom();
      rows.forEach(function (row) {
        var p = map.project([Number(row.source.lat), Number(row.source.lon)], zoom);
        var key = Math.floor(p.x / spacing) + ":" + Math.floor(p.y / spacing);
        var previous = buckets.get(key);
        if (!previous || Math.abs(row.pressure - 1013.25) > Math.abs(previous.pressure - 1013.25)) buckets.set(key, row);
      });
      return Array.from(buckets.values());
    }

    function popupReal(row) {
      var s = row.source;
      var sourceLabel = s.pressure_source || (row.reduced ? "Calculée depuis pression station" : "PMER observée");
      return '<div class="am-pr-popup__title">' + safe(prettyName(s.name)) + '</div>' +
        '<div class="am-pr-popup__place">' + safe(s.department || s.departement || s.icao_id || s.id || "") + '</div>' +
        '<div class="am-pr-popup__value">' + safe(formatNumber(row.pressure, 1)) + ' hPa</div>' +
        '<div class="am-pr-popup__grid">' +
          '<span>Source pression</span><span>' + safe(sourceLabel) + '</span>' +
          '<span>Altitude</span><span>' + (finite(s.altitude_m) === null ? "—" : Math.round(Number(s.altitude_m)) + " m") + '</span>' +
          '<span>Température</span><span>' + (finite(s.temperature) === null && finite(s.t) === null ? "—" : formatNumber(finite(s.temperature) !== null ? s.temperature : s.t, 1) + " °C") + '</span>' +
          '<span>Point de rosée</span><span>' + (finite(s.dew_point) === null ? "—" : formatNumber(s.dew_point, 1) + " °C") + '</span>' +
          '<span>Observation</span><span>' + safe(formatDate(s.obs_time_utc || currentRealPeriod().utc)) + '</span>' +
        '</div>';
    }

    function drawCentersReal(allRows) {
      centerLayer.clearLayers();
      if (!allRows.length) return;
      var sorted = allRows.slice().sort(function (a, b) { return a.pressure - b.pressure; });
      var low = sorted[0], high = sorted[sorted.length - 1];
      [
        { row: low, letter: "D", cls: "am-pr-center am-pr-center--low", label: "Dépression – minimum observé" },
        { row: high, letter: "A", cls: "am-pr-center am-pr-center--high", label: "Anticyclone – maximum observé" }
      ].forEach(function (item) {
        var icon = window.L.divIcon({ className: "am-pr-center-icon", html: '<span class="' + item.cls + '">' + item.letter + '</span>', iconSize: [1, 1], iconAnchor: [0, 0] });
        var marker = window.L.marker([Number(item.row.source.lat), Number(item.row.source.lon)], { icon: icon, interactive: true, zIndexOffset: 300 });
        marker.bindTooltip(item.label + " · " + formatNumber(item.row.pressure, 1) + " hPa · " + safe(prettyName(item.row.source.name)), { direction: "top", offset: [0, -17] });
        marker.addTo(centerLayer);
      });
    }

    function pressureValuesFromGrid(grid) {
      var values = [];
      if (!grid) return values;
      grid.values.forEach(function (row) { row.forEach(function (v) { if (Number.isFinite(v)) values.push(v); }); });
      return values;
    }

    function drawIsobarsOn(layer, grid, cacheKey, options) {
      options = options || {};
      if (options.clear !== false) layer.clearLayers();
      if (isobarsSelect.value === "off" || !grid) return;
      var step = Number(intervalSelect.value || 2);
      var cached = contourCache[cacheKey + ":" + step];
      if (!cached) {
        var pressures = pressureValuesFromGrid(grid);
        if (!pressures.length) return;
        var minP = Math.max(900, Math.min.apply(null, pressures));
        var maxP = Math.min(1080, Math.max.apply(null, pressures));
        cached = [];
        var start = Math.ceil(minP / step) * step;
        var end = Math.floor(maxP / step) * step;
        for (var level = start; level <= end; level += step) cached.push({ level: level, paths: buildSmoothContours(level, grid) });
        contourCache[cacheKey + ":" + step] = cached;
      }

      var lb = areaConfig().label;
      var color = options.color || "#263645";
      var opacity = options.opacity == null ? 0.82 : options.opacity;
      var dashArray = options.dashArray || null;
      var totalLabelsPlaced = 0;
      var maxTotalLabels = Number.isFinite(Number(options.maxTotalLabels)) ? Math.max(0, Number(options.maxTotalLabels)) : Infinity;
      cached.forEach(function (contour) {
        (contour.paths || []).forEach(function (path) {
          window.L.polyline(path, {
            color: color, weight: contour.level % 4 === 0 ? 2.35 : 1.55, opacity: opacity,
            interactive: false, smoothFactor: 0.05, lineCap: "round", lineJoin: "round", dashArray: dashArray
          }).addTo(layer);
        });
        var paths = contour.paths || [];
        if (!paths.length || options.labels === false) return;
        var candidates = paths.filter(function (path) {
          if (path.length < 12) return false;
          var middle = path[Math.floor(path.length / 2)];
          return middle[0] > lb.south && middle[0] < lb.north && middle[1] > lb.west && middle[1] < lb.east;
        });
        var chosen = candidates.length ? candidates.slice().sort(function (a, b) { return b.length - a.length; })[0] : paths.slice().sort(function (a, b) { return b.length - a.length; })[0];
        if (chosen && chosen.length) {
          var wanted = Math.max(1, Math.min(3, Number(options.labelsPerContour || 1)));
          var fractions = wanted >= 3 ? [0.23, 0.52, 0.81] : (wanted === 2 ? [0.34, 0.69] : [0.52]);
          var style = 'border-color:' + color + ';color:' + color + ';background:' + (options.labelBg || 'rgba(255,255,255,.90)');
          fractions.forEach(function (fraction) {
            if (totalLabelsPlaced >= maxTotalLabels) return;
            var idx = Math.max(0, Math.min(chosen.length - 1, Math.floor((chosen.length - 1) * fraction)));
            var point = chosen[idx];
            if (!point || point[0] <= lb.south || point[0] >= lb.north || point[1] <= lb.west || point[1] >= lb.east) return;
            var icon = window.L.divIcon({ className: "am-pr-isobar-label-icon", html: '<span class="am-pr-isobar-label" style="' + style + '">' + contour.level + '</span>', iconSize: [1, 1], iconAnchor: [0, 0] });
            window.L.marker(point, { icon: icon, interactive: false, zIndexOffset: options.zIndexOffset || 50 }).addTo(layer);
            totalLabelsPlaced += 1;
          });
        }
      });
      layer.bringToBack && layer.bringToBack();
    }

    function drawIsobars(grid, cacheKey, options) {
      var cfg = Object.assign({ color: "#263645" }, options || {});
      drawIsobarsOn(isobarLayer, grid, cacheKey, cfg);
    }

    function setLegend(variable) {
      var legend = LEGENDS[variable] || LEGENDS.pression;
      var meta = VARIABLE_META[variable] || VARIABLE_META.pression;
      legendEl.innerHTML = '<strong>' + safe(meta.label) + ' (' + safe(meta.unit) + ')</strong>' + legend.map(function (item) {
        return '<span class="am-pr__legend-item"><i class="am-pr__swatch" style="background:' + item.color + '"></i>' + safe(item.label) + '</span>';
      }).join("") + (isobarsSelect.value === "on" ? '<span class="am-pr__legend-item"><i class="am-pr__linekey"></i>Isobares</span>' : '');
    }

    function renderReal() {
      if (!realData) return;
      stationLayer.clearLayers(); gfsLayer.clearLayers(); centerLayer.clearLayers();
      var period = currentRealPeriod();
      var allRows = allRealRows(), rows = filteredRealRows();
      var sortedAll = allRows.slice().sort(function (a, b) { return a.pressure - b.pressure; });
      var low = sortedAll.length ? sortedAll[0] : null, high = sortedAll.length ? sortedAll[sortedAll.length - 1] : null;
      minLabel.textContent = "Pression minimale"; maxLabel.textContent = "Pression maximale"; countLabel.textContent = "Stations disponibles"; rangeLabel.textContent = "Écart de pression";
      minEl.textContent = low ? formatNumber(low.pressure, 1) + " hPa · " + prettyName(low.source.name) : "—";
      maxEl.textContent = high ? formatNumber(high.pressure, 1) + " hPa · " + prettyName(high.source.name) : "—";
      countEl.textContent = allRows.length.toLocaleString("fr-FR");
      rangeEl.textContent = low && high ? formatNumber(high.pressure - low.pressure, 1) + " hPa" : "—";
      dateEl.textContent = period.local_label || formatDate(period.utc);

      var grid = buildRealPressureGrid(allRows, areaSelect.value);
      drawIsobars(grid, "real:" + areaSelect.value + ":" + String(periodSelect.value || 0));
      drawCentersReal(allRows);

      var viewport = map.getBounds().pad(0.15);
      var visible = rows.filter(function (row) { return viewport.contains([Number(row.source.lat), Number(row.source.lon)]); });
      var spacing = searchInput.value.trim() ? 0 : (DENSITIES_REAL[densitySelect.value] || 0);
      if (areaSelect.value === "europe" && spacing) spacing += 8;
      var selected = thinRealRows(visible, spacing);
      var lowId = low ? String(low.source.id) : "", highId = high ? String(high.source.id) : "";

      selected.forEach(function (row) {
        var station = row.source, color = colorFor("pression", row.pressure);
        var extra = String(station.id) === lowId ? " is-min" : (String(station.id) === highId ? " is-max" : "");
        var icon = window.L.divIcon({
          className: "am-pr-divicon",
          html: '<span class="am-pr-marker' + extra + '" style="background:' + color + ';color:' + textColor("pression", row.pressure) + '">' + safe(formatNumber(row.pressure, 0)) + '</span>',
          iconSize: [1, 1], iconAnchor: [0, 0]
        });
        var marker = window.L.marker([Number(station.lat), Number(station.lon)], { icon: icon, riseOnHover: true, zIndexOffset: 120 });
        marker.bindTooltip(safe(prettyName(station.name)) + " · " + safe(formatNumber(row.pressure, 1)) + " hPa", { direction: "top", offset: [0, -13] });
        marker.bindPopup(popupReal(row), { maxWidth: 370 });
        marker.addTo(stationLayer);
      });

      notice.hidden = allRows.length > 0 && rows.length > 0 && !realData.warning;
      if (realData.warning) notice.textContent = realData.warning;
      else if (allRows.length && !rows.length) notice.textContent = "Aucune station ne correspond aux filtres actuels ; les isobares restent calculées avec toutes les observations disponibles.";
      else if (!allRows.length) notice.textContent = "Aucune pression au niveau de la mer n’est disponible pour cette période.";
      setLegend("pression");
    }

    function gfsMeta(variable) { return VARIABLE_META[variable] || VARIABLE_META.pression; }

    function gfsPopup(point, frame) {
      var r = point.row, c = point.col, variable = variableSelect.value, meta = gfsMeta(variable);
      var p = variableValue(frame, "pression", r, c);
      var t = variableValue(frame, "temperature", r, c);
      var rain = variableValue(frame, "pluie", r, c);
      var wind = variableValue(frame, "vent", r, c);
      var gust = frame.fields && frame.fields.gust_kmh && frame.fields.gust_kmh[r] ? finite(frame.fields.gust_kmh[r][c]) : null;
      var dir = frame.fields && frame.fields.wind_direction_deg && frame.fields.wind_direction_deg[r] ? finite(frame.fields.wind_direction_deg[r][c]) : null;
      return '<div class="am-pr-popup__title">GFS 0,25° • +' + safe(String(frame.forecast_hour).padStart(3, "0")) + ' h</div>' +
        '<div class="am-pr-popup__place">' + formatNumber(point.lat, 2) + '° / ' + formatNumber(point.lon, 2) + '°</div>' +
        '<div class="am-pr-popup__value">' + safe(formatNumber(point.value, meta.decimals)) + ' ' + safe(meta.unit) + '</div>' +
        '<div class="am-pr-popup__grid">' +
          '<span>Pression</span><span>' + (p === null ? "—" : formatNumber(p, 0) + " hPa") + '</span>' +
          '<span>Température 2 m</span><span>' + (t === null ? "—" : formatNumber(t, 1) + " °C") + '</span>' +
          '<span>Précipitations</span><span>' + (rain === null ? "—" : formatNumber(rain, 1) + " mm") + '</span>' +
          '<span>Vent 10 m</span><span>' + (wind === null ? "—" : formatNumber(wind, 0) + " km/h " + windArrow(dir)) + '</span>' +
          '<span>Rafales</span><span>' + (gust === null ? "—" : formatNumber(gust, 0) + " km/h") + '</span>' +
          '<span>Validité</span><span>' + safe(formatValid(frame.valid_utc)) + '</span>' +
          '<span>Pluie – période</span><span>' + safe(frame.precipitation_period_label || "échéance GFS") + '</span>' +
        '</div>';
    }

    function thinGfsPoints(points, spacing) {
      if (!spacing) return points;
      var buckets = new Map(), zoom = map.getZoom();
      points.forEach(function (point) {
        var p = map.project([point.lat, point.lon], zoom);
        var key = Math.floor(p.x / spacing) + ":" + Math.floor(p.y / spacing);
        var old = buckets.get(key);
        if (!old || Math.abs(point.value) > Math.abs(old.value)) buckets.set(key, point);
      });
      return Array.from(buckets.values());
    }

    function limitGfsLabels(points, maxCount) {
      if (!Array.isArray(points) || points.length <= maxCount) return points || [];
      var out = [], step = points.length / maxCount;
      for (var i = 0; i < maxCount; i += 1) {
        out.push(points[Math.min(points.length - 1, Math.floor(i * step))]);
      }
      return out;
    }

    function gfsExtrema15d(variable) {
      var meta = gfsMeta(variable);
      var byArea = gfsIndex && gfsIndex.extrema_15d && gfsIndex.extrema_15d.by_area ? gfsIndex.extrema_15d.by_area[areaSelect.value] : null;
      var item = byArea && byArea[meta.field] ? byArea[meta.field] : null;
      return item || null;
    }

    function formatExtremaPoint(point, meta, withDate) {
      if (!point || finite(point.value) === null) return "—";
      var text = formatNumber(point.value, meta.decimals) + " " + meta.unit;
      if (withDate && point.valid_utc) text += " • " + formatValid(point.valid_utc);
      return text;
    }

    function extremaTooltip(point) {
      if (!point || finite(point.value) === null) return "";
      var bits = [];
      if (point.valid_utc) bits.push(formatValid(point.valid_utc));
      if (finite(point.lat) !== null && finite(point.lon) !== null) bits.push(formatNumber(point.lat, 2) + "° / " + formatNumber(point.lon, 2) + "°");
      return bits.join(" • ");
    }

    function renderGfs() {
      if (!gfsFrame) return;
      stationLayer.clearLayers(); gfsLayer.clearLayers(); centerLayer.clearLayers();
      var variable = variableSelect.value, meta = gfsMeta(variable);
      var points = subsetGfs(gfsFrame, areaSelect.value, variable);
      var viewport = map.getBounds().pad(0.12);
      var visible = points.filter(function (p) { return viewport.contains([p.lat, p.lon]); });
      var spacing = DENSITIES_GFS[densitySelect.value];
      if (spacing === undefined) spacing = DENSITIES_GFS["tres-lisible"];
      var selected = thinGfsPoints(visible, spacing);
      // Pression : pas de semis de chiffres de grille, lecture directement sur les isobares.
      // T°, pluie et vent : nombre de valeurs fortement plafonné.
      if (variable === "pression") selected = [];
      else selected = limitGfsLabels(selected, areaSelect.value === "europe" ? 22 : 16);
      var values = points.map(function (p) { return p.value; });
      var min = values.length ? Math.min.apply(null, values) : null, max = values.length ? Math.max.apply(null, values) : null;

      var extrema15 = gfsExtrema15d(variable);
      minLabel.textContent = "Minimum sur 15 jours"; maxLabel.textContent = "Maximum sur 15 jours"; countLabel.textContent = "Points de grille – échéance"; rangeLabel.textContent = variable === "vent" ? "Rafale max sur 15 jours" : "Amplitude 15 jours";
      minEl.textContent = extrema15 ? formatExtremaPoint(extrema15.min, meta, false) : (min === null ? "—" : formatNumber(min, meta.decimals) + " " + meta.unit);
      maxEl.textContent = extrema15 ? formatExtremaPoint(extrema15.max, meta, false) : (max === null ? "—" : formatNumber(max, meta.decimals) + " " + meta.unit);
      minEl.title = extrema15 ? extremaTooltip(extrema15.min) : "";
      maxEl.title = extrema15 ? extremaTooltip(extrema15.max) : "";
      countEl.textContent = points.length.toLocaleString("fr-FR");
      if (variable === "vent") {
        var gustMeta = { unit: "km/h", decimals: 0 };
        var gustByArea = gfsIndex && gfsIndex.extrema_15d && gfsIndex.extrema_15d.by_area ? gfsIndex.extrema_15d.by_area[areaSelect.value] : null;
        var gustExt = gustByArea && gustByArea.gust_kmh ? gustByArea.gust_kmh : null;
        rangeEl.textContent = gustExt ? formatExtremaPoint(gustExt.max, gustMeta, false) : "—";
        rangeEl.title = gustExt ? extremaTooltip(gustExt.max) : "";
      } else if (extrema15 && extrema15.min && extrema15.max && finite(extrema15.min.value) !== null && finite(extrema15.max.value) !== null) {
        rangeEl.textContent = formatNumber(Number(extrema15.max.value) - Number(extrema15.min.value), meta.decimals) + " " + meta.unit;
      } else {
        rangeEl.textContent = min !== null && max !== null ? formatNumber(max - min, meta.decimals) + " " + meta.unit : "—";
      }
      dateEl.textContent = "Run " + formatValid(gfsFrame.run_utc) + " • +" + String(gfsFrame.forecast_hour).padStart(3, "0") + " h • " + formatValid(gfsFrame.valid_utc);
      updateTimelineUi();
      renderRisks();

      var pressureGrid = buildGfsPressureGrid(gfsFrame, areaSelect.value);
      var pressurePlayback = playRunning && mode === "gfs" && variable === "pression";
      drawIsobars(pressureGrid, "gfs:" + (gfsFrame.run_utc || "") + ":" + gfsFrame.forecast_hour + ":" + areaSelect.value, {
        labelsPerContour: 1,
        maxTotalLabels: areaSelect.value === "europe" ? 8 : 6
      });

      // En pression, aucune valeur de grille : seulement quelques valeurs sur les isobares.

      selected.forEach(function (point) {
        var value = point.value, color = colorFor(variable, value), label = formatNumber(value, meta.decimals);
        if (variable === "vent") {
          var dirRow = gfsFrame.fields.wind_direction_deg && gfsFrame.fields.wind_direction_deg[point.row];
          var dir = dirRow ? finite(dirRow[point.col]) : null;
          label = windArrow(dir) + " " + formatNumber(value, 0);
        }
        var icon = window.L.divIcon({
          className: "am-pr-divicon",
          html: '<span class="am-pr-marker am-pr-marker--gfs am-pr-marker--' + variable + '" style="background:' + color + ';color:' + textColor(variable, value) + '">' + safe(label) + '</span>',
          iconSize: [1, 1], iconAnchor: [0, 0]
        });
        var marker = window.L.marker([point.lat, point.lon], { icon: icon, riseOnHover: true, zIndexOffset: 110 });
        marker.bindTooltip(meta.label + " · " + formatNumber(value, meta.decimals) + " " + meta.unit + " · " + formatValid(gfsFrame.valid_utc), { direction: "top", offset: [0, -13] });
        marker.bindPopup(gfsPopup(point, gfsFrame), { maxWidth: 370 });
        marker.addTo(gfsLayer);
      });

      notice.hidden = points.length > 0;
      if (!points.length) notice.textContent = "Aucune valeur GFS disponible pour cette zone et cette variable.";
      else if (variable === "pluie" && gfsFrame.precipitation_period_label) {
        notice.hidden = false;
        notice.textContent = "Précipitations GFS : " + gfsFrame.precipitation_period_label + ". Le module affiche la période d’accumulation portée par le GRIB, pas un cumul inventé.";
      }
      setLegend(variable);
    }

    function renderCompare() {
      if (!realData || !gfsFrame) return;
      stationLayer.clearLayers(); gfsLayer.clearLayers(); centerLayer.clearLayers(); isobarLayer.clearLayers();
      compareRealIsobars.clearLayers(); compareGfsIsobars.clearLayers();
      var allRows = allRealRows();
      var realGrid = buildRealPressureGrid(allRows, areaSelect.value);
      var gfsGrid = buildGfsPressureGrid(gfsFrame, areaSelect.value);
      // En comparaison on évite le semis de chiffres : uniquement quelques
      // valeurs posées sur les isobares elles-mêmes.
      drawIsobarsOn(compareRealIsobars, realGrid, "compare-real:" + areaSelect.value + ":" + String(currentRealPeriod().utc || 0), {
        color: "#263645", opacity: 0.90, labelBg: "rgba(255,255,255,.94)", zIndexOffset: 70, labelsPerContour: 1, maxTotalLabels: 3
      });
      drawIsobarsOn(compareGfsIsobars, gfsGrid, "compare-gfs:" + String(gfsFrame.run_utc || "") + ":" + gfsFrame.forecast_hour + ":" + areaSelect.value, {
        color: "#1976d2", opacity: 0.86, labelBg: "rgba(235,246,255,.94)", zIndexOffset: 80, labelsPerContour: 1, maxTotalLabels: 3
      });

      var realVals = allRows.map(function (r) { return r.pressure; }).filter(Number.isFinite);
      var gfsVals = pressureValuesFromGrid(gfsGrid);
      var rmin = realVals.length ? Math.min.apply(null, realVals) : null, rmax = realVals.length ? Math.max.apply(null, realVals) : null;
      var gmin = gfsVals.length ? Math.min.apply(null, gfsVals) : null, gmax = gfsVals.length ? Math.max.apply(null, gfsVals) : null;
      minLabel.textContent = "Pression minimale obs / prévue";
      maxLabel.textContent = "Pression maximale obs / prévue";
      countLabel.textContent = "Écart minimum GFS − obs";
      rangeLabel.textContent = "Écart maximum GFS − obs";
      minEl.textContent = (rmin == null ? "—" : formatNumber(rmin, 1)) + " / " + (gmin == null ? "—" : formatNumber(gmin, 1)) + " hPa";
      maxEl.textContent = (rmax == null ? "—" : formatNumber(rmax, 1)) + " / " + (gmax == null ? "—" : formatNumber(gmax, 1)) + " hPa";
      countEl.textContent = (rmin == null || gmin == null) ? "—" : ((gmin - rmin >= 0 ? "+" : "") + formatNumber(gmin - rmin, 1) + " hPa");
      rangeEl.textContent = (rmax == null || gmax == null) ? "—" : ((gmax - rmax >= 0 ? "+" : "") + formatNumber(gmax - rmax, 1) + " hPa");
      dateEl.textContent = "Réel " + formatDate(currentRealPeriod().utc) + " • GFS " + formatValid(gfsFrame.valid_utc);

      // Pas de valeurs de stations en mode Réel ↔ GFS : cela surchargeait la
      // comparaison. Les valeurs utiles restent directement sur les isobares.
      legendEl.innerHTML = '<strong>Comparaison isobares</strong>' +
        '<span class="am-pr__legend-item"><i class="am-pr__linekey"></i>Réel / observations</span>' +
        '<span class="am-pr__legend-item"><i class="am-pr__linekey am-pr__linekey--gfs"></i>GFS</span>';
      notice.hidden = !!(realGrid && gfsGrid);
      if (!realGrid || !gfsGrid) notice.textContent = "Comparaison partielle : une des deux grilles de pression est indisponible.";
      updateTimelineUi(); renderRisks();
    }

    function render() {
      modeUi();
      compareRealIsobars.clearLayers(); compareGfsIsobars.clearLayers();
      if (mode === "gfs") renderGfs();
      else if (mode === "compare") renderCompare();
      else renderReal();
    }

    function scheduleRender() {
      window.clearTimeout(renderTimer);
      renderTimer = window.setTimeout(render, 80);
    }

    function realDataUrl() { return areaSelect.value === "europe" ? root.dataset.europeUrl : root.dataset.jsonUrl; }

    function fetchJson(url, token) {
      return fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + token + "=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
        .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); });
    }

    function loadCompareFrame() {
      if (!gfsIndex || !gfsIndex.frames || !gfsIndex.frames.length) return loadCompare();
      var entry = gfsIndex.frames[Math.max(0, Math.min(gfsIndex.frames.length - 1, Number(periodSelect.value || 0)))];
      var url = resolveFrameUrl(entry), serial = ++requestSerial;
      loading.textContent = "Chargement de la comparaison Réel / GFS +" + String(entry.forecast_hour).padStart(3, "0") + " h…";
      loading.hidden = false; refreshButton.disabled = true;
      fetchJson(url, "_amcmpf").then(function (json) {
        if (serial !== requestSerial) return;
        if (json.status !== "ok" || !json.grid || !json.fields) throw new Error("Fichier GFS invalide.");
        gfsFrame = json; contourCache = {}; loading.hidden = true; renderCompare();
      }).catch(function (error) {
        if (serial !== requestSerial) return;
        loading.textContent = "Impossible de charger la comparaison : " + error.message; loading.hidden = false;
      }).finally(function () { if (serial === requestSerial) refreshButton.disabled = false; });
    }

    function loadCompare() {
      var serial = ++requestSerial;
      loading.textContent = "Chargement Réel + GFS pour comparaison…"; loading.hidden = false; refreshButton.disabled = true;
      var realPromise = fetchJson(realDataUrl(), "_amcmpreal");
      var indexPromise = gfsIndex ? Promise.resolve(gfsIndex) : fetchJson(root.dataset.gfsIndexUrl, "_amcmpidx");
      Promise.all([realPromise, indexPromise]).then(function (items) {
        if (serial !== requestSerial) return null;
        var real = items[0], index = items[1];
        if (real.status !== "ok" || !Array.isArray(real.hourly) || !real.hourly.length) throw new Error("Observations réelles indisponibles.");
        if (index.status !== "ok" || !Array.isArray(index.frames) || !index.frames.length) throw new Error("Index GFS indisponible.");
        realData = real; gfsIndex = index; buildRealPeriods();
        // En comparaison, le sélecteur d'échéance appartient au GFS.
        buildGfsPeriods();
        var entry = gfsIndex.frames[Math.max(0, Math.min(gfsIndex.frames.length - 1, Number(periodSelect.value || 0)))];
        return fetchJson(resolveFrameUrl(entry), "_amcmpframe");
      }).then(function (frame) {
        if (!frame || serial !== requestSerial) return;
        if (frame.status !== "ok" || !frame.grid || !frame.fields) throw new Error("Fichier GFS invalide.");
        gfsFrame = frame; contourCache = {}; loading.hidden = true; renderCompare();
      }).catch(function (error) {
        if (serial !== requestSerial) return;
        console.error("Comparaison :", error); loading.textContent = "Impossible de charger la comparaison : " + error.message; loading.hidden = false;
      }).finally(function () { if (serial === requestSerial) refreshButton.disabled = false; });
    }

    function loadReal() {
      var serial = ++requestSerial;
      loading.textContent = areaSelect.value === "europe" ? "Chargement des observations METAR européennes…" : "Chargement des observations Météo-France…";
      loading.hidden = false; refreshButton.disabled = true;
      var url = realDataUrl();
      fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + "_ampr=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
        .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
        .then(function (json) {
          if (serial !== requestSerial) return;
          if (json.status !== "ok" || !Array.isArray(json.hourly) || !json.hourly.length) throw new Error("Le fichier ne contient aucune observation exploitable.");
          realData = json; contourCache = {}; buildRealPeriods(); loading.hidden = true; render();
          window.setTimeout(function () { map.invalidateSize(); }, 100);
        })
        .catch(function (error) {
          if (serial !== requestSerial) return;
          console.error("Carte Réel :", error); loading.textContent = "Impossible de charger les observations : " + error.message; loading.hidden = false;
        })
        .finally(function () { if (serial === requestSerial) refreshButton.disabled = false; });
    }

    function loadGfsIndex(thenLoadFrame) {
      var serial = ++requestSerial;
      loading.textContent = "Chargement de l’index GFS…"; loading.hidden = false; refreshButton.disabled = true;
      var url = root.dataset.gfsIndexUrl;
      return fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + "_amgfs=" + Date.now(), { cache: "no-store" })
        .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
        .then(function (json) {
          if (serial !== requestSerial) return false;
          if (json.status !== "ok" || !Array.isArray(json.frames) || !json.frames.length) throw new Error("Index GFS vide ou invalide.");
          gfsIndex = json; frameCache.clear(); buildGfsPeriods();
          if (typeof thenLoadFrame === "function") return thenLoadFrame();
          return loadGfsFrame();
        })
        .catch(function (error) {
          if (serial !== requestSerial) return false;
          console.error("GFS index :", error); loading.textContent = "Impossible de charger les données GFS : " + error.message + ". Vérifie que le workflow GFS a déjà publié observations/gfs/index.json."; loading.hidden = false; refreshButton.disabled = false;
          return false;
        });
    }

    function fetchGfsFrame(entry, preferCache) {
      var url = resolveFrameUrl(entry);
      if (!url) return Promise.reject(new Error("URL d’échéance GFS absente."));
      var key = String(entry.file || entry.url || url);
      if (frameCache.has(key)) return Promise.resolve(frameCache.get(key));
      var requestUrl = preferCache ? url : (url + (url.indexOf("?") === -1 ? "?" : "&") + "_amgfsf=" + Date.now());
      return fetch(requestUrl, { cache: preferCache ? "force-cache" : "no-store" })
        .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
        .then(function (json) {
          if (json.status !== "ok" || !json.grid || !json.fields) throw new Error("Fichier GFS invalide.");
          frameCache.set(key, json);
          // Limite simple du cache mémoire : assez pour une animation fluide sans grossir indéfiniment.
          if (frameCache.size > 12) {
            var firstKey = frameCache.keys().next().value;
            frameCache.delete(firstKey);
          }
          return json;
        });
    }

    function prefetchGfsFrame(index) {
      if (!gfsIndex || !Array.isArray(gfsIndex.frames) || !gfsIndex.frames.length) return;
      var idx = Math.max(0, Math.min(gfsIndex.frames.length - 1, index));
      var entry = gfsIndex.frames[idx];
      fetchGfsFrame(entry, true).catch(function () { /* préchargement opportuniste */ });
    }

    function loadGfsFrame(options) {
      options = options || {};
      if (!gfsIndex || !gfsIndex.frames || !gfsIndex.frames.length) {
        return loadGfsIndex(function () { return loadGfsFrame(options); });
      }
      var idx = Math.max(0, Math.min(gfsIndex.frames.length - 1, Number(periodSelect.value || 0)));
      var entry = gfsIndex.frames[idx];
      var serial = options.playback ? null : ++requestSerial;
      var token = options.playbackToken;
      loading.textContent = "Chargement GFS +" + String(entry.forecast_hour).padStart(3, "0") + " h…"; loading.hidden = false; refreshButton.disabled = true;
      return fetchGfsFrame(entry, !!options.playback)
        .then(function (json) {
          if (options.playback) {
            if (!playRunning || token !== playbackToken || mode !== "gfs") return false;
          } else if (serial !== requestSerial) {
            return false;
          }
          gfsFrame = json; contourCache = {}; loading.hidden = true; render();
          window.setTimeout(function () { map.invalidateSize(); }, 40);
          if (options.playback) prefetchGfsFrame(idx + 1 < gfsIndex.frames.length ? idx + 1 : 0);
          return true;
        })
        .catch(function (error) {
          if (options.playback && token !== playbackToken) return false;
          if (!options.playback && serial !== requestSerial) return false;
          console.error("GFS frame :", error); loading.textContent = "Impossible de charger cette échéance GFS : " + error.message; loading.hidden = false;
          return false;
        })
        .finally(function () {
          if (options.playback || serial === requestSerial) refreshButton.disabled = false;
        });
    }

    function loadCurrent() {
      stationLayer.clearLayers(); gfsLayer.clearLayers(); isobarLayer.clearLayers(); compareRealIsobars.clearLayers(); compareGfsIsobars.clearLayers(); centerLayer.clearLayers(); notice.hidden = true;
      modeUi();
      if (mode === "gfs") {
        if (gfsIndex) loadGfsFrame(); else loadGfsIndex(loadGfsFrame);
      } else if (mode === "compare") loadCompare();
      else loadReal();
    }

    modeTabs.forEach(function (button) {
      button.addEventListener("click", function () {
        var next = button.dataset.mode;
        if (next === mode) return;
        stopPlayback(); mode = next;
        if (mode === "reel") areaSelect.value = AREAS[root.dataset.defaultArea] ? root.dataset.defaultArea : "france";
        if (mode === "compare") areaSelect.value = "europe";
        searchInput.value = ""; periodSelect.innerHTML = ""; gfsFrame = null; realData = null;
        applyAreaView(true); loadCurrent();
      });
    });
    areaSelect.addEventListener("change", function () {
      selectedRiskKey = null; searchInput.value = ""; contourCache = {}; applyAreaView(true);
      if (mode === "gfs" && gfsFrame) { renderRisks(); render(); } else loadCurrent();
    });
    variableSelect.addEventListener("change", function () { modeUi(); render(); });
    periodSelect.addEventListener("change", function () { selectedRiskKey = null; stopPlayback(); if (mode === "gfs") loadGfsFrame(); else if (mode === "compare") loadCompareFrame(); else render(); });
    filterSelect.addEventListener("change", render);
    isobarsSelect.addEventListener("change", function () { intervalSelect.disabled = isobarsSelect.value === "off"; render(); });
    intervalSelect.addEventListener("change", render);
    densitySelect.addEventListener("change", render);
    altitudeSelect.addEventListener("change", render);
    searchInput.addEventListener("input", scheduleRender);
    refreshButton.addEventListener("click", function () { stopPlayback(); if (mode === "gfs" || mode === "compare") { gfsIndex = null; gfsFrame = null; } realData = null; loadCurrent(); });
    resetButton.addEventListener("click", function () { applyAreaView(true); });
    if (timelineRange) {
      timelineRange.addEventListener("input", function () { stopPlayback(); periodSelect.value = timelineRange.value; updateTimelineUi(); });
      timelineRange.addEventListener("change", function () { if (mode === "compare") loadCompareFrame(); else if (mode === "gfs") loadGfsFrame(); });
    }
    if (prevButton) prevButton.addEventListener("click", function () { stopPlayback(); stepTimeline(-1); });
    if (nextButton) nextButton.addEventListener("click", function () { stopPlayback(); stepTimeline(1); });
    function playbackDelayMs() {
      var value = speedSelect ? Number(speedSelect.value) : 1600;
      return Number.isFinite(value) ? Math.max(400, Math.min(6000, value)) : 1600;
    }
    function playNextFrame(token) {
      if (!playRunning || token !== playbackToken || mode !== "gfs") return;
      var frames = gfsIndex && gfsIndex.frames ? gfsIndex.frames : [];
      if (!frames.length) { stopPlayback(); return; }
      var idx = Number(periodSelect.value || 0);
      if (!Number.isFinite(idx)) idx = 0;
      idx = idx >= frames.length - 1 ? 0 : idx + 1;
      periodSelect.value = String(idx);
      if (timelineRange) timelineRange.value = String(idx);
      updateTimelineUi();
      if (playButton) {
        playButton.disabled = false;
        playButton.textContent = "⏳ +" + String(frames[idx].forecast_hour).padStart(3, "0") + " h";
      }
      loadGfsFrame({ playback: true, playbackToken: token }).then(function (ok) {
        if (!playRunning || token !== playbackToken) return;
        if (!ok) { stopPlayback(); return; }
        if (playButton) {
          playButton.disabled = false;
          playButton.textContent = "⏸ Pause";
        }
        playTimer = window.setTimeout(function () { playNextFrame(token); }, playbackDelayMs());
      });
    }
    if (speedSelect) speedSelect.addEventListener("change", function () {
      // La nouvelle vitesse s'applique dès l'échéance suivante sans casser l'animation.
    });
    if (playButton) playButton.addEventListener("click", function () {
      if (playRunning) { stopPlayback(); render(); return; }
      if (mode !== "gfs") return;
      playRunning = true;
      playbackToken += 1;
      var token = playbackToken;
      playButton.textContent = "⏸ Pause";
      prefetchGfsFrame(Math.min((Number(periodSelect.value || 0) + 1), (gfsIndex && gfsIndex.frames ? gfsIndex.frames.length - 1 : 0)));
      playNextFrame(token);
    });
    map.on("zoomend moveend", scheduleRender);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(function () { map.invalidateSize(false); }).observe(root);

    modeUi(); setLegend("pression"); applyAreaView(false); loadCurrent();
  }

  function boot() {
    document.querySelectorAll(".am-pr[data-json-url]").forEach(function (root) {
      if (root.dataset.initialized === "1") return;
      root.dataset.initialized = "1";
      init(root);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

// v1.3.8 — Europe recentrée, comparaison robuste, métriques pression rétablies
