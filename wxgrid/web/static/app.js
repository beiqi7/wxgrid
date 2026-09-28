/* wxgrid web UI — vanilla JS, no build step, no CDN.
 *
 * Reads the published product from the read-only API and renders:
 * hero + alerts, 5-day strip, temperature/precipitation SVG charts,
 * a township scatter map, a per-day township table, and the bulletin text.
 */
'use strict';

const S = {
  doc: null,        // the full product JSON
  runs: [],         // index.json entries
  dayIdx: { temp: 0, precip: 0, table: 0 },
  mapMetric: 'tmax',
  mapDay: 0,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined && txt !== null) n.textContent = String(txt);
  return n;
};
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));

/* ---------- theme ---------- */
function initTheme() {
  const saved = localStorage.getItem('wxgrid-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  paintThemeBtn();
  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('wxgrid-theme', next);
    paintThemeBtn();
    if (S.doc) renderAll();  // charts embed theme colours
  });
}
function paintThemeBtn() {
  $('theme-toggle').textContent = document.documentElement.dataset.theme === 'dark' ? '☀' : '☾';
}
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---------- data ---------- */
async function api(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json();
}

/** township id -> display name, from the product's roster. */
function nameOf(id) {
  if (!S._names) S._names = new Map((S.doc.townships || []).map(t => [t.id, t.name]));
  return S._names.get(id) || id;
}

async function boot() {
  initTheme();
  wireStatic();
  try {
    S.doc = await api('/api/latest');
  } catch (err) {
    return fail(`还没有已发布的预报产品。请先运行一次 <code>python3 -m wxgrid publish</code>。<br><small>${err.message}</small>`);
  }
  try {
    S.runs = await api('/api/runs');
  } catch { S.runs = []; }
  fillRunSelect();
  renderApi();
  renderAll();
}

function fail(html) {
  const hero = $('hero');
  hero.classList.remove('is-loading');
  hero.innerHTML = `<div class="hero-empty">${html}</div>`;
}

function fillRunSelect() {
  const sel = $('run-select');
  sel.innerHTML = '';
  const cur = S.doc.meta.run;
  const seen = new Set();
  for (const r of S.runs) {
    if (seen.has(r.file)) continue;
    seen.add(r.file);
    const o = el('option', null, `${r.run} · ${r.county || ''}`.trim());
    o.value = r.file;
    if (r.run === cur) o.selected = true;
    sel.appendChild(o);
  }
  if (!S.runs.length) {
    const o = el('option', null, `${cur}（当前）`);
    o.value = '';
    sel.appendChild(o);
  }
  sel.addEventListener('change', async () => {
    if (!sel.value) return;
    sel.disabled = true;
    try {
      S.doc = await api(`/api/runs/${encodeURIComponent(sel.value)}`);
      S.dayIdx = { temp: 0, precip: 0, table: 0 };
      S.mapDay = 0;
      S._names = null;  // roster belongs to the previous run
      renderAll();
    } catch (err) { fail(err.message); }
    sel.disabled = false;
  });
}

/* ---------- weather icons (inline SVG, keyed off the Chinese phrase) ---------- */
function iconKind(text) {
  const t = text || '';
  if (t.includes('雪')) return 'snow';
  if (t.includes('暴雨')) return 'storm';
  if (t.includes('大雨') || t.includes('中雨')) return 'rain';
  if (t.includes('雨')) return 'drizzle';
  if (t.includes('阴')) return 'overcast';
  if (t.includes('多云')) return 'cloudy';
  if (t.includes('少云')) return 'partly';
  return 'clear';
}

const ICON = {
  clear: '<circle cx="24" cy="24" r="9" class="i-sun"/><g class="i-ray">' +
    [0, 45, 90, 135, 180, 225, 270, 315].map(a =>
      `<line x1="24" y1="6" x2="24" y2="12" transform="rotate(${a} 24 24)"/>`).join('') + '</g>',
  partly: '<circle cx="18" cy="19" r="7.5" class="i-sun"/><path class="i-cloud" d="M20 35h13a6 6 0 0 0 .6-12 8.5 8.5 0 0 0-16.1 2.3A5.5 5.5 0 0 0 20 35z"/>',
  cloudy: '<path class="i-cloud" d="M16 34h16a6.5 6.5 0 0 0 .7-13 9.5 9.5 0 0 0-18 2.6A5.8 5.8 0 0 0 16 34z"/>',
  overcast: '<path class="i-cloud2" d="M14 27h16a5.5 5.5 0 0 0 .6-11 8.5 8.5 0 0 0-16 2.2A5 5 0 0 0 14 27z"/>' +
    '<path class="i-cloud" d="M18 38h16a6 6 0 0 0 .6-12 9 9 0 0 0-17 2.4A5.4 5.4 0 0 0 18 38z"/>',
  drizzle: '<path class="i-cloud" d="M15 28h16a6.2 6.2 0 0 0 .7-12.4 9 9 0 0 0-17.1 2.5A5.5 5.5 0 0 0 15 28z"/>' +
    '<g class="i-drop"><line x1="19" y1="33" x2="17" y2="39"/><line x1="26" y1="33" x2="24" y2="39"/></g>',
  rain: '<path class="i-cloud" d="M15 27h16a6.2 6.2 0 0 0 .7-12.4 9 9 0 0 0-17.1 2.5A5.5 5.5 0 0 0 15 27z"/>' +
    '<g class="i-drop"><line x1="17" y1="32" x2="14" y2="41"/><line x1="24" y1="32" x2="21" y2="41"/><line x1="31" y1="32" x2="28" y2="41"/></g>',
  storm: '<path class="i-cloud2" d="M14 26h18a6.5 6.5 0 0 0 .7-13 9.5 9.5 0 0 0-18 2.6A5.8 5.8 0 0 0 14 26z"/>' +
    '<polygon class="i-bolt" points="24,29 18,40 23,40 20,46 30,34 25,34 28,29"/>',
  snow: '<path class="i-cloud" d="M15 27h16a6.2 6.2 0 0 0 .7-12.4 9 9 0 0 0-17.1 2.5A5.5 5.5 0 0 0 15 27z"/>' +
    '<g class="i-flake">' + [16, 24, 32].map(x =>
      `<g transform="translate(${x} 37)"><line x1="-4" y1="0" x2="4" y2="0"/><line x1="0" y1="-4" x2="0" y2="4"/>` +
      `<line x1="-3" y1="-3" x2="3" y2="3"/><line x1="-3" y1="3" x2="3" y2="-3"/></g>`).join('') + '</g>',
};

function icon(text, size = 48) {
  const k = iconKind(text);
  return `<svg class="wx-icon wx-${k}" viewBox="0 0 48 48" width="${size}" height="${size}" role="img" aria-label="${text || '天气'}">${ICON[k]}</svg>`;
}

/* ---------- hero ---------- */
function renderHero() {
  const m = S.doc.meta, c = S.doc.conclusions, d0 = S.doc.days[0];
  const seat = seatCell(0);
  const hero = $('hero');
  hero.classList.remove('is-loading');
  hero.dataset.kind = iconKind(seat ? seat.weather : d0.county.weather);
  hero.innerHTML = `
    <div class="hero-main">
      <div class="hero-icon">${icon(seat ? seat.weather : d0.county.weather, 104)}</div>
      <div class="hero-txt">
        <div class="hero-place">${m.county || '—'} <span class="hero-seat">${m.seat || ''}</span></div>
        <div class="hero-temp">${num(seat ? seat.tmax : d0.county.tmax_max)}<span class="deg">℃</span>
          <span class="hero-lo">/ ${num(seat ? seat.tmin : d0.county.tmin_min)}℃</span></div>
        <div class="hero-cond">${seat ? seat.weather : d0.county.weather} · ${seat ? seat.wind_text : '—'}</div>
        <div class="hero-chips">
          <span class="chip">降水概率 ${seat && seat.pop !== null ? num(seat.pop) + '%' : '—'}</span>
          <span class="chip">降水时段 ${seat ? seat.windows_text : '—'}</span>
          <span class="chip">${m.n_townships} 个乡镇</span>
        </div>
      </div>
    </div>
    <div class="hero-side">
      <div class="hero-headline">${c.headline || ''}</div>
      <dl class="hero-meta">
        <div><dt>起报</dt><dd>${m.run} UTC</dd></div>
        <div><dt>成员</dt><dd>${m.member}</dd></div>
        <div><dt>模式</dt><dd>${(m.sources || []).join(' + ')}</dd></div>
        <div><dt>集合</dt><dd>${m.pop_members ? m.pop_members + ' 成员' : '未取'}</dd></div>
        <div><dt>生成</dt><dd>${(m.generated || '').replace('T', ' ').replace('+00:00', 'Z')}</dd></div>
      </dl>
    </div>`;
}

function seatCell(dayIdx) {
  const id = S.doc.conclusions.seat_id;
  const day = S.doc.days[dayIdx];
  if (!day) return null;
  return day.cells.find(c => c.point === id) || day.cells[0] || null;
}

/* ---------- alerts ---------- */
const LEVEL_CLASS = { 红色: 'lv-red', 橙色: 'lv-orange', 黄色: 'lv-yellow', 蓝色: 'lv-blue' };

function renderAlerts() {
  const box = $('alerts');
  const list = S.doc.conclusions.alerts || [];
  box.innerHTML = '';
  box.hidden = list.length === 0;
  for (const a of list) {
    const n = el('div', `alert-chip ${LEVEL_CLASS[a.level] || 'lv-blue'}`);
    n.innerHTML = `<span class="dot"></span><span class="alert-tag">${a.type}<i>${a.level}</i></span>`
      + `<span class="alert-detail">${a.detail}</span>`;
    box.appendChild(n);
  }
}

/* ---------- 5-day strip ---------- */
function renderDayStrip() {
  const strip = $('day-strip');
  strip.innerHTML = '';
  S.doc.days.forEach((d, i) => {
    const seat = seatCell(i);
    const card = el('button', 'day-card');
    card.type = 'button';
    if (i === S.dayIdx.table) card.classList.add('is-active');
    card.dataset.kind = iconKind(seat ? seat.weather : d.county.weather);
    const partial = d.hours < 24 ? `<span class="day-partial">仅 ${d.hours} h</span>` : '';
    card.innerHTML = `
      <div class="day-when"><strong>${d.date.slice(5).replace('-', '月')}日</strong><span>${d.weekday}</span></div>
      ${icon(seat ? seat.weather : d.county.weather, 52)}
      <div class="day-wx">${seat ? seat.weather : d.county.weather}</div>
      <div class="day-temp"><b>${num(seat ? seat.tmax : d.county.tmax_max)}°</b><span>${num(seat ? seat.tmin : d.county.tmin_min)}°</span></div>
      <div class="day-bars">
        <span class="day-pop" style="--v:${d.county.pop_max === null ? 0 : d.county.pop_max}%">概率 ${d.county.pop_max === null ? '—' : num(d.county.pop_max) + '%'}</span>
        <span class="day-rain">雨 ${num(d.county.precip_max, 1)} mm</span>
      </div>
      ${partial}`;
    card.addEventListener('click', () => {
      S.dayIdx = { temp: i, precip: i, table: i };
      S.mapDay = i;
      render();
      $('days-section').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
    strip.appendChild(card);
  });
}

/* ---------- day segmented controls ---------- */
function renderSegs() {
  const mk = (host, key, onPick) => {
    const box = $(host);
    box.innerHTML = '';
    S.doc.days.forEach((d, i) => {
      const b = el('button', 'seg-btn' + (i === S.dayIdx[key] ? ' is-on' : ''));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', i === S.dayIdx[key] ? 'true' : 'false');
      b.textContent = d.date.slice(5);
      b.addEventListener('click', () => { S.dayIdx[key] = i; onPick(); });
      box.appendChild(b);
    });
  };
  mk('temp-day-seg', 'temp', () => { renderSegs(); renderTempChart(); });
  mk('precip-day-seg', 'precip', () => { renderSegs(); renderPrecipChart(); });
  mk('table-day-seg', 'table', () => { renderSegs(); renderTable(); renderDayStrip(); });

  const mbox = $('map-metric-seg');
  mbox.innerHTML = '';
  [['tmax', '最高气温'], ['precip', '降水量'], ['elevation', '海拔'], ['pop', '降水概率']].forEach(([k, label]) => {
    const b = el('button', 'seg-btn' + (k === S.mapMetric ? ' is-on' : ''));
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.textContent = label;
    b.addEventListener('click', () => { S.mapMetric = k; renderSegs(); renderMap(); });
    mbox.appendChild(b);
  });
}

/* ---------- temperature range chart ---------- */
function renderTempChart() {
  const day = S.doc.days[S.dayIdx.temp];
  const host = $('temp-chart');
  if (!day) { host.innerHTML = ''; return; }
  const rows = day.cells
    .map(c => ({ name: nameOf(c.point), lo: c.tmin, hi: c.tmax }))
    .filter(r => r.lo !== null && r.hi !== null)
    .sort((a, b) => b.hi - a.hi);
  if (!rows.length) { host.innerHTML = '<p class="muted">该日无有效数据</p>'; return; }

  const lo = Math.floor(Math.min(...rows.map(r => r.lo)) - 1);
  const hi = Math.ceil(Math.max(...rows.map(r => r.hi)) + 1);
  const W = 560, rowH = 24, padL = 84, padR = 44, padT = 26;
  const H = padT + rows.length * rowH + 12;
  const x = v => padL + ((v - lo) / (hi - lo)) * (W - padL - padR);

  const ticks = [];
  const stepT = (hi - lo) > 25 ? 10 : 5;
  for (let t = Math.ceil(lo / stepT) * stepT; t <= hi; t += stepT) ticks.push(t);

  const bars = rows.map((r, i) => {
    const y = padT + i * rowH + rowH / 2;
    const x1 = x(r.lo), x2 = x(r.hi);
    return `<g class="tr-row">
      <text class="tr-name" x="${padL - 10}" y="${y + 4}" text-anchor="end">${r.name}</text>
      <line class="tr-track" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>
      <line class="tr-bar" x1="${x1}" y1="${y}" x2="${x2}" y2="${y}"/>
      <circle class="tr-lo" cx="${x1}" cy="${y}" r="4"/>
      <circle class="tr-hi" cx="${x2}" cy="${y}" r="4"/>
      <text class="tr-val tr-val-lo" x="${x1 - 8}" y="${y + 4}" text-anchor="end">${num(r.lo)}</text>
      <text class="tr-val tr-val-hi" x="${x2 + 8}" y="${y + 4}">${num(r.hi)}</text>
    </g>`;
  }).join('');

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="svg-chart" role="img"
      aria-label="${day.date} 各乡镇最低到最高气温区间">
    <defs><linearGradient id="tgrad" x1="0" x2="1">
      <stop offset="0" stop-color="var(--cool)"/><stop offset="1" stop-color="var(--warm)"/>
    </linearGradient></defs>
    ${ticks.map(t => `<g><line class="tr-grid" x1="${x(t)}" y1="${padT - 12}" x2="${x(t)}" y2="${H - 8}"/>
      <text class="tr-tick" x="${x(t)}" y="${padT - 16}" text-anchor="middle">${t}℃</text></g>`).join('')}
    ${bars}
  </svg>`;
}

/* ---------- precipitation bar chart ---------- */
function renderPrecipChart() {
  const day = S.doc.days[S.dayIdx.precip];
  const host = $('precip-chart');
  if (!day) { host.innerHTML = ''; return; }
  const rows = day.cells
    .map(c => ({ name: nameOf(c.point), v: c.precip === null ? 0 : c.precip, pop: c.pop }))
    .sort((a, b) => b.v - a.v);
  const max = Math.max(1, ...rows.map(r => r.v));

  const W = 560, rowH = 24, padL = 84, padR = 56, padT = 24;
  const H = padT + rows.length * rowH + 12;
  const w = v => (v / max) * (W - padL - padR);

  const grades = [[0.1, 'g0'], [10, 'g1'], [25, 'g2'], [50, 'g3'], [100, 'g4']];
  const gradeOf = v => {
    let g = 'g0';
    for (const [cut, cls] of grades) if (v >= cut) g = cls;
    return g;
  };

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="svg-chart" role="img"
      aria-label="${day.date} 各乡镇日降水量">
    ${rows.map((r, i) => {
      const y = padT + i * rowH + 4;
      const bw = Math.max(r.v > 0 ? 2 : 0, w(r.v));
      return `<g class="pb-row">
        <text class="pb-name" x="${padL - 10}" y="${y + 12}" text-anchor="end">${r.name}</text>
        <rect class="pb-track" x="${padL}" y="${y}" width="${W - padL - padR}" height="15" rx="7.5"/>
        <rect class="pb-bar ${gradeOf(r.v)}" x="${padL}" y="${y}" width="${bw}" height="15" rx="7.5"/>
        <text class="pb-val" x="${padL + bw + 8}" y="${y + 12}">${r.v.toFixed(1)}${r.pop !== null ? ` · ${num(r.pop)}%` : ''}</text>
      </g>`;
    }).join('')}
  </svg>
  <div class="legend">
    <span class="lg g0">无</span><span class="lg g1">小雨</span><span class="lg g2">中雨</span>
    <span class="lg g3">大雨</span><span class="lg g4">暴雨</span>
  </div>`;
}

/* ---------- township map (equirectangular scatter, cos-lat corrected) ---------- */
const RAMPS = {
  tmax: ['#3b82f6', '#22d3ee', '#a3e635', '#fbbf24', '#f97316', '#ef4444'],
  precip: ['#1e293b', '#0ea5e9', '#22d3ee', '#34d399', '#fbbf24', '#ef4444'],
  pop: ['#1e293b', '#334155', '#0ea5e9', '#38bdf8', '#7dd3fc', '#e0f2fe'],
  elevation: ['#14532d', '#3f6212', '#a16207', '#b45309', '#a8a29e', '#f5f5f4'],
};
const METRIC_LABEL = { tmax: '最高气温 ℃', precip: '降水量 mm', pop: '降水概率 %', elevation: '海拔 m' };

function lerpColor(a, b, t) {
  const p = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const [r1, g1, b1] = p(a), [r2, g2, b2] = p(b);
  const r = Math.round(r1 + (r2 - r1) * t), g = Math.round(g1 + (g2 - g1) * t), bb = Math.round(b1 + (b2 - b1) * t);
  return `rgb(${r},${g},${bb})`;
}

function rampColor(ramp, t) {
  if (!isFinite(t)) return 'var(--muted)';
  const x = Math.max(0, Math.min(1, t)) * (ramp.length - 1);
  const i = Math.min(ramp.length - 2, Math.floor(x));
  return lerpColor(ramp[i], ramp[i + 1], x - i);
}

function metricValues() {
  const day = S.doc.days[S.dayIdx.table];
  const byId = new Map((day ? day.cells : []).map(c => [c.point, c]));
  return S.doc.townships.map(t => {
    const c = byId.get(t.id);
    if (S.mapMetric === 'elevation') return t.elevation;
    if (!c) return null;
    return S.mapMetric === 'tmax' ? c.tmax : S.mapMetric === 'precip' ? c.precip : c.pop;
  });
}

function renderMap() {
  const host = $('map');
  const pts = S.doc.townships;
  if (!pts.length) { host.innerHTML = ''; return; }
  const vals = metricValues();
  const finite = vals.filter(v => v !== null && isFinite(v));
  let lo = finite.length ? Math.min(...finite) : 0;
  let hi = finite.length ? Math.max(...finite) : 1;
  if (hi - lo < 1e-9) { hi = lo + 1; }
  if (S.mapMetric === 'precip' || S.mapMetric === 'pop') lo = 0;

  const lats = pts.map(p => p.lat), lons = pts.map(p => p.lon);
  const latMid = (Math.min(...lats) + Math.max(...lats)) / 2;
  const kx = Math.cos(latMid * Math.PI / 180);           // keep the county's real shape
  const xs = lons.map(v => v * kx);
  const pad = 0.035;
  const x0 = Math.min(...xs) - pad * kx, x1 = Math.max(...xs) + pad * kx;
  const y0 = Math.min(...lats) - pad, y1 = Math.max(...lats) + pad;
  const W = 520, H = Math.max(260, Math.round(W * (y1 - y0) / Math.max(1e-9, x1 - x0)));
  const px = v => ((v * kx - x0) / (x1 - x0)) * W;
  const py = v => H - ((v - y0) / (y1 - y0)) * H;

  const ramp = RAMPS[S.mapMetric];
  const marks = pts.map((p, i) => {
    const v = vals[i];
    const t = v === null || !isFinite(v) ? NaN : (v - lo) / (hi - lo);
    const r = 9 + (isFinite(t) ? t * 5 : 0);
    return `<g class="mp" tabindex="0" role="button" data-id="${p.id}"
        aria-label="${p.name} ${METRIC_LABEL[S.mapMetric]} ${v === null ? '无数据' : num(v)}">
      <circle class="mp-halo" cx="${px(p.lon).toFixed(1)}" cy="${py(p.lat).toFixed(1)}" r="${(r + 6).toFixed(1)}"/>
      <circle class="mp-dot" cx="${px(p.lon).toFixed(1)}" cy="${py(p.lat).toFixed(1)}" r="${r.toFixed(1)}"
              fill="${rampColor(ramp, t)}"/>
      <text class="mp-lbl" x="${px(p.lon).toFixed(1)}" y="${(py(p.lat) - r - 6).toFixed(1)}"
            text-anchor="middle">${p.name}</text>
      <text class="mp-num" x="${px(p.lon).toFixed(1)}" y="${(py(p.lat) + 4).toFixed(1)}"
            text-anchor="middle">${v === null || !isFinite(v) ? '–' : num(v)}</text>
    </g>`;
  }).join('');

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="svg-map" role="group"
      aria-label="乡镇分布，按${METRIC_LABEL[S.mapMetric]}着色">${marks}</svg>`;
  host.querySelectorAll('.mp').forEach(g => {
    const open = () => openDrawer(g.dataset.id);
    g.addEventListener('click', open);
    g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });

  const stops = RAMPS[S.mapMetric].map((c, i, a) =>
    `<stop offset="${(i / (a.length - 1) * 100).toFixed(0)}%" stop-color="${c}"/>`).join('');
  $('map-legend').innerHTML = `<span class="muted">${METRIC_LABEL[S.mapMetric]}</span>
    <svg class="legend-bar" viewBox="0 0 200 12" preserveAspectRatio="none" aria-hidden="true">
      <defs><linearGradient id="lgrad" x1="0" x2="1">${stops}</linearGradient></defs>
      <rect x="0" y="0" width="200" height="12" rx="6" fill="url(#lgrad)"/>
    </svg>
    <span class="muted">${num(lo)} → ${num(hi)}</span>
    <span class="muted legend-day">${S.doc.days[S.dayIdx.table] ? S.doc.days[S.dayIdx.table].date : ''}</span>`;
}

/* ---------- township detail table ---------- */
function renderTable() {
  const day = S.doc.days[S.dayIdx.table];
  const tbl = $('town-table');
  if (!day) { tbl.innerHTML = ''; return; }
  const byId = new Map(S.doc.townships.map(t => [t.id, t]));
  const tmaxes = day.cells.map(c => c.tmax).filter(v => v !== null && isFinite(v));
  const lo = Math.min(...tmaxes), hi = Math.max(...tmaxes);
  const rows = day.cells.map(c => {
    const t = byId.get(c.point) || { name: c.point, elevation: null };
    const heat = tmaxes.length && hi > lo ? (c.tmax - lo) / (hi - lo) : 0;
    return `<tr tabindex="0" data-id="${c.point}">
      <th scope="row">${t.name}</th>
      <td class="num">${t.elevation === null ? '–' : Math.round(t.elevation)}</td>
      <td>${c.weather}</td>
      <td class="num t-cell" style="--heat:${rampColor(RAMPS.tmax, heat)}">
        ${num(c.tmin, 0)}～${num(c.tmax, 0)}</td>
      <td class="num">${num(c.precip, 1)}</td>
      <td class="num">${c.pop === null ? '–' : Math.round(c.pop / 10) * 10 + '%'}</td>
      <td>${c.wind_text}</td>
      <td class="win">${c.windows_text}</td>
    </tr>`;
  }).join('');
  tbl.innerHTML = `<caption class="sr-only">${day.date} 各乡镇预报明细</caption>
    <thead><tr>
      <th scope="col">乡镇</th><th scope="col">海拔m</th><th scope="col">天气</th>
      <th scope="col">气温℃</th><th scope="col">降水mm</th><th scope="col">概率</th>
      <th scope="col">风</th><th scope="col">降水时段</th>
    </tr></thead><tbody>${rows}</tbody>`;
  tbl.querySelectorAll('tbody tr').forEach(tr => {
    const open = () => openDrawer(tr.dataset.id);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); open(); } });
  });
}

/* ---------- drawer: one township, all days ---------- */
function openDrawer(pointId) {
  const t = S.doc.townships.find(x => x.id === pointId);
  if (!t) return;
  const rows = S.doc.days.map(d => {
    const c = d.cells.find(x => x.point === pointId);
    if (!c) return '';
    return `<tr>
      <th scope="row">${d.date.slice(5)}<small>${d.weekday}</small></th>
      <td class="ic">${icon(c.weather, 26)}</td>
      <td>${c.weather}</td>
      <td class="num">${num(c.tmin, 0)}～${num(c.tmax, 0)}℃</td>
      <td class="num">${num(c.precip, 1)}mm</td>
      <td class="num">${c.pop === null ? '–' : Math.round(c.pop / 10) * 10 + '%'}</td>
      <td>${c.wind_text}</td>
      <td class="win">${c.windows_text}</td>
    </tr>`;
  }).join('');
  $('drawer-body').innerHTML = `
    <header class="drawer-head">
      <div>
        <h3>${t.name}</h3>
        <p class="muted">海拔 ${t.elevation === null ? '–' : Math.round(t.elevation)} m
          · 模式地形 ${t.model_elevation === null ? '–' : Math.round(t.model_elevation)} m
          · ${t.lat.toFixed(4)}°N ${t.lon.toFixed(4)}°E</p>
      </div>
    </header>
    <div class="table-wrap"><table class="drawer-table"><tbody>${rows}</tbody></table></div>
    <p class="muted fine">气温已按 ${t.elevation === null ? '' : Math.round(t.elevation)} m
      与模式地形高差做递减率订正（−6.5 K/km）。</p>`;
  const d = $('drawer');
  d.hidden = false;
  requestAnimationFrame(() => d.classList.add('open'));
  $('drawer-close').focus();
}

function closeDrawer() {
  const d = $('drawer');
  d.classList.remove('open');
  setTimeout(() => { d.hidden = true; }, 200);
}

/* ---------- bulletin text + API docs ---------- */
function renderText() {
  $('bulletin-text').textContent = S.doc.text || '（无文稿）';
}

const API_DOCS = [
  ['GET', '/api/latest', '最新一次产品的完整 JSON：meta、逐日县级摘要、逐乡镇明细、结论、文稿'],
  ['GET', '/api/summary', '仅 meta + 结论 + 逐日县级摘要，体积小，适合看板轮询'],
  ['GET', '/api/runs', '已存起报时次索引，最新在前'],
  ['GET', '/api/runs/{file}', '按索引里的 file 取某一次历史产品'],
  ['GET', '/api/townships', '乡镇名录：id、名称、经纬度、海拔'],
  ['GET', '/api/health', '存活探针：产品数量与最新起报时次'],
];

function renderApi() {
  $('api-list').innerHTML = API_DOCS.map(([m, path, desc]) => `
    <div class="api-row">
      <code class="api-path"><span class="verb">${m}</span>${path}</code>
      <span class="muted">${desc}</span>
      <button class="ghost-btn copy-api" data-path="${path}">复制</button>
    </div>`).join('');
  $('api-list').querySelectorAll('.copy-api').forEach(b => {
    b.addEventListener('click', () => {
      copy(location.origin + b.dataset.path.replace('{file}', ''), b);
    });
  });
}

async function copy(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  const old = btn.textContent;
  btn.textContent = '已复制';
  setTimeout(() => { btn.textContent = old; }, 1200);
}

/* ---------- render-all + boot ---------- */
function renderAll() {
  renderHero();
  renderAlerts();
  renderDayStrip();
  renderSegs();
  renderTempChart();
  renderPrecipChart();
  renderMap();
  renderTable();
  renderText();
  renderFooter();
}

function renderFooter() {
  const m = S.doc.meta;
  $('foot-meta').textContent =
    `起报 ${m.run} · 生成 ${(m.generated || '').replace('T', ' ').replace('+00:00', 'Z')} · ${m.n_townships} 个乡镇`;
}

function wireStatic() {
  // theme button is wired in initTheme()
  $('copy-text').addEventListener('click', () => copy(S.doc.text || '', $('copy-text')));
  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer').addEventListener('click', e => { if (e.target.id === 'drawer') closeDrawer(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('drawer').hidden) closeDrawer(); });
  window.addEventListener('resize', debounce(() => {
    if (S.doc) { renderTempChart(); renderPrecipChart(); renderMap(); }
  }, 180));
}

function debounce(fn, ms) {
  let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

boot();
