/* wxgrid web UI — vanilla JS, no build step, no CDN.
 *
 * The page is written the way a Chinese public forecast is read:
 *   1. the county seat: this period and the next (今天夜间 → 明天白天)
 *   2. alerts
 *   3. 0–72 h every 3 hours, for any township
 *   4. five days, each split into 白天 / 夜间, with the high/low curve
 *   5. every township for the chosen date, ranked by elevation
 *   6. models and method; the plain-text bulletin and the API, folded
 * No probability appears in the forecast itself; it is listed only in the
 * detail tables (3-hourly table, township sheet).
 *
 * DOM contract: every id used here exists in index.html and every class has a
 * rule in styles.css. Namespaces: t-* hero, a-* alerts, hc-* 3-hourly chart,
 * ht-* 3-hourly table, wk-* week, r-* township rows, s-* sheet.
 */
'use strict';

const S = {
  doc: null,       // product JSON on screen
  runs: [],        // index.json entries
  file: null,      // index `file` of the product on screen (null = latest without index)
  date: null,      // date selected for the township band
  names: null,     // id -> name
  back: null,      // element to refocus when the sheet closes
  town3h: null,    // township shown in the 3-hourly band
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const n1 = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
const md = (iso) => `${Number(iso.slice(5, 7))}月${Number(iso.slice(8, 10))}日`;
const WD7 = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const wdOf = (iso) => WD7[new Date(`${iso.slice(0, 10)}T00:00:00Z`).getUTCDay()];
const RANK = { 关注: 0, 蓝色: 1, 黄色: 2, 橙色: 3, 红色: 4 };
const ALERT_CLASS = { 红色: 'a-red', 橙色: 'a-orange', 黄色: 'a-yellow', 蓝色: 'a-blue', 关注: 'a-note' };

/* ---------- local time (the product's own offset, default Beijing) ---------- */
const tzH = () => (S.doc && Number.isFinite(S.doc.meta.tz) ? S.doc.meta.tz : 8);
const tzLabel = () => { const h = tzH(); return `${h < 0 ? '-' : '+'}${String(Math.abs(h)).padStart(2, '0')}:00`; };
/** "YYYY-MM-DDTHH:00" local -> epoch ms. */
const localMs = (iso) => new Date(`${iso}:00${tzLabel()}`).getTime();
const todayLocal = () => new Date(Date.now() + tzH() * 3600e3).toISOString().slice(0, 10);
const hourOf = (iso) => Number(iso.slice(11, 13));
/** 今天 / 明天 / 后天 relative to *now*, not to the issue time: a stored product ages. */
const dayDiff = (date) => Math.round((Date.parse(`${date}T00:00:00Z`) - Date.parse(`${todayLocal()}T00:00:00Z`)) / 864e5);
function relName(date) {
  const d = dayDiff(date);
  return d === -1 ? '昨天' : (['今天', '明天', '后天'][d] || wdOf(date));
}
/* After midnight the night that began yesterday is still running: call it 今天凌晨. */
const periodLabel = (q) => (dayDiff(q.date) === -1 && q.kind === 'night' ? '今天凌晨' : `${relName(q.date)}${q.name}`);
/** Week column title: yesterday's date only survives as tonight's remainder -> 今晨. */
const colName = (date) => (dayDiff(date) === -1 ? '今晨' : relName(date));
const colDate = (date) => (dayDiff(date) === -1 ? todayLocal() : date);
const spanText = (q) => `${String(hourOf(q.start_local)).padStart(2, '0')}—${String(hourOf(q.end_local)).padStart(2, '0')}时`;

/* ---------- theme: day / night ---------- */
function initTheme() {
  // index.html already applied the saved theme in <head> to avoid a flash.
  paintThemeBtn();
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'night' ? 'day' : 'night';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('wxgrid-theme', next); } catch { /* private mode */ }
    paintThemeBtn();
  });
}
function paintThemeBtn() {
  const night = document.documentElement.dataset.theme === 'night';
  $('theme').textContent = night ? '日间' : '夜间';
  $('theme').setAttribute('aria-label', night ? '切换到日间主题' : '切换到夜间主题');
}

/* ---------- temperature → colour (one fixed scale for the whole page) ---------- */
const TSTOPS = [
  [-10, [104, 110, 240]], [0, [86, 156, 255]], [10, [64, 196, 222]], [18, [96, 204, 150]],
  [24, [236, 196, 64]], [30, [255, 146, 64]], [35, [242, 88, 70]], [40, [206, 40, 92]],
];
function tcol(t) {
  if (!Number.isFinite(t)) return 'transparent';
  if (t <= TSTOPS[0][0]) return `rgb(${TSTOPS[0][1].join(' ')})`;
  for (let i = 1; i < TSTOPS.length; i++) {
    const [t1, c1] = TSTOPS[i];
    if (t <= t1) {
      const [t0, c0] = TSTOPS[i - 1];
      const k = (t - t0) / (t1 - t0);
      return `rgb(${c0.map((v, j) => Math.round(v + (c1[j] - v) * k)).join(' ')})`;
    }
  }
  return `rgb(${TSTOPS[TSTOPS.length - 1][1].join(' ')})`;
}
function tgrad(lo, hi, dir) {
  const n = 6;
  return `linear-gradient(${dir},${Array.from({ length: n + 1 }, (_, i) => tcol(lo + ((hi - lo) * i) / n)).join(',')})`;
}

/* ---------- data ---------- */
async function api(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json();
}
function nameOf(id) {
  if (!S.names) S.names = new Map((S.doc.townships || []).map(t => [t.id, t.name]));
  return S.names.get(id) || id;
}
function seatId() {
  const sid = S.doc.conclusions && S.doc.conclusions.seat_id;
  if (sid) return sid;
  const t = (S.doc.townships || []).find(x => x.name === S.doc.meta.seat);
  return t ? t.id : null;
}
const cellOf = (q, id) => (q ? q.cells.find(c => c.point === id) || null : null);

/** Periods that have not ended yet. */
function livePeriods() {
  const now = Date.now();
  return S.doc.periods.filter(q => localMs(q.end_local) > now);
}
/** Dates with at least one live period: {date, weekday, day: period|null, night: period|null}. */
function liveDays() {
  const now = Date.now();
  const live = (i) => (i === null || i === undefined ? null
    : (localMs(S.doc.periods[i].end_local) > now ? S.doc.periods[i] : null));
  return S.doc.days.map(d => ({ date: d.date, weekday: d.weekday, day: live(d.day), night: live(d.night),
    dayGone: d.day !== null && d.day !== undefined && !live(d.day) }))
    .filter(d => d.day || d.night);
}
/** The public 3-hourly rows for one township, windows not yet ended. */
function rows3h(id) {
  const s = S.doc.series3h;
  if (!s || !s.points || !s.points[id]) return [];
  const p = s.points[id];
  const now = Date.now();
  const keys = ['weather', 'temp', 'precip', 'snow', 'cloud', 'wind_dir', 'wind_name', 'wind_force', 'wind_speed', 'gust', 'pop'];
  return s.times.map((time, i) => {
    const r = { time, lead: s.lead_h[i] };
    for (const k of keys) r[k] = p[k] ? p[k][i] : null;
    return r;
  }).filter(r => localMs(r.time) > now);
}
/** The seat's 3-hourly value nearest to now, within 90 min. */
function nowValue() {
  const s = S.doc.series3h;
  const sid = seatId();
  if (!s || !s.points || !s.points[sid]) return null;
  const now = Date.now();
  let best = -1, bd = Infinity;
  s.times.forEach((t, i) => { const d = Math.abs(localMs(t) - now); if (d < bd) { bd = d; best = i; } });
  if (best < 0 || bd > 90 * 60e3) return null;
  return { time: s.times[best], temp: s.points[sid].temp[best] };
}

/* ---------- weather → icon ---------- */
function kindOf(text, night = false) {
  const t = text || '';
  if (t.includes('雪')) return 'snow';
  if (t.includes('暴雨') || t.includes('雷') || t.includes('强降水')) return 'storm';
  if (t.includes('大雨') || t.includes('中雨')) return 'rain';
  if (t.includes('雨')) return 'drizzle';
  if (t.includes('阴')) return 'overcast';
  if (t.includes('多云')) return night ? 'moon-partly' : 'partly';
  return night ? 'moon' : 'clear';
}
const GLYPH = {
  clear: `<circle cx="32" cy="32" r="12" class="g-sun"/>
    <g class="g-ray">${[0, 45, 90, 135, 180, 225, 270, 315].map(a =>
      `<line x1="32" y1="10" x2="32" y2="17" transform="rotate(${a} 32 32)"/>`).join('')}</g>`,
  partly: `<circle cx="25" cy="25" r="10" class="g-sun"/>
    <g class="g-ray">${[200, 245, 290].map(a =>
      `<line x1="25" y1="9" x2="25" y2="14" transform="rotate(${a} 25 25)"/>`).join('')}</g>
    <path class="g-cloud" d="M26 46h17a8 8 0 0 0 .8-16 11 11 0 0 0-20.9 3A7 7 0 0 0 26 46z"/>`,
  overcast: `<path class="g-cloud-hi" d="M18 34h20a7 7 0 0 0 .7-14 10 10 0 0 0-19 2.8A6.2 6.2 0 0 0 18 34z"/>
    <path class="g-cloud" d="M23 48h20a7.6 7.6 0 0 0 .8-15.2 10.8 10.8 0 0 0-20.5 3A6.8 6.8 0 0 0 23 48z"/>`,
  drizzle: `<path class="g-cloud" d="M21 38h21a8.5 8.5 0 0 0 .9-17 12 12 0 0 0-22.8 3.3A7.5 7.5 0 0 0 21 38z"/>
    <g class="g-drop"><line x1="26" y1="44" x2="24" y2="53"/><line x1="36" y1="44" x2="34" y2="53"/></g>`,
  rain: `<path class="g-cloud" d="M21 36h21a8.5 8.5 0 0 0 .9-17 12 12 0 0 0-22.8 3.3A7.5 7.5 0 0 0 21 36z"/>
    <g class="g-drop"><line x1="23" y1="42" x2="20" y2="55"/><line x1="32" y1="42" x2="29" y2="55"/>
      <line x1="41" y1="42" x2="38" y2="55"/></g>`,
  storm: `<path class="g-cloud-dark" d="M21 34h21a8.5 8.5 0 0 0 .9-17 12 12 0 0 0-22.8 3.3A7.5 7.5 0 0 0 21 34z"/>
    <path class="g-bolt" d="M32 37l-8 12h6l-3 10 11-14h-7l4-8z"/>
    <g class="g-drop"><line x1="22" y1="40" x2="19" y2="50"/><line x1="43" y1="40" x2="40" y2="50"/></g>`,
  snow: `<path class="g-cloud" d="M21 36h21a8.5 8.5 0 0 0 .9-17 12 12 0 0 0-22.8 3.3A7.5 7.5 0 0 0 21 36z"/>
    <g class="g-flake">${[24, 32, 40].map(x =>
      `<g transform="translate(${x} 48)"><line x1="-5" y1="0" x2="5" y2="0"/><line x1="0" y1="-5" x2="0" y2="5"/>
       <line x1="-3.5" y1="-3.5" x2="3.5" y2="3.5"/><line x1="-3.5" y1="3.5" x2="3.5" y2="-3.5"/></g>`).join('')}</g>`,
  moon: `<path class="g-moon" d="M36 12a20 20 0 1 0 16 30A16 16 0 1 1 36 12z"/>`,
  'moon-partly': `<path class="g-moon" d="M27 10a15 15 0 1 0 12 23A12 12 0 1 1 27 10z"/>
    <path class="g-cloud" d="M26 50h17a8 8 0 0 0 .8-16 11 11 0 0 0-20.9 3A7 7 0 0 0 26 50z"/>`,
};
/* Decorative: the weather phrase is always printed next to the icon. */
function glyph(text, size, night = false) {
  const k = kindOf(text, night);
  return `<svg class="wx wx-${k}" viewBox="0 0 64 64" width="${size}" height="${size}"
    aria-hidden="true" focusable="false">${GLYPH[k]}</svg>`;
}

/** How the detail probability was made, for the table footnotes. */
function popNote(threeHourly) {
  const m = S.doc.meta;
  if (m.pop_source === 'multimodel') {
    return `降水概率为 ${m.pop_members || 8} 家模式中（各自订正后）${threeHourly ? '该 3 小时' : '该时段'}有 ≥0.1 mm 降水的比例，仅作明细参考。`;
  }
  if (m.pop_source === 'gefs' || m.pop_members) {
    return `降水概率为 NOAA GEFS ${m.pop_members || 21} 个成员中${threeHourly ? '所在 6 小时' : '该时段'}有 ≥0.1 mm 降水的比例，仅作明细参考。`;
  }
  return '本次没有降水概率。';
}

/* ---------- 1. hero: this period and the next, at the county seat ---------- */
function periodCard(q, cls) {
  const c = cellOf(q, seatId()) || q.cells[0];
  const night = q.kind === 'night';
  const tname = night ? '最低气温' : '最高气温';
  return `<article class="${cls}">
    <p class="t-kick"><b>${esc(periodLabel(q))}</b><span>${spanText(q)}</span></p>
    <div class="t-read">
      ${glyph(c.weather, cls === 't-now' ? 84 : 52, night)}
      <div class="t-nums">
        <p class="t-temp"><span class="t-hi">${n1(c.temp)}</span><span class="t-unit">℃</span></p>
        <p class="t-tname">${tname}</p>
      </div>
    </div>
    <p class="t-wx">${esc(c.weather)}</p>
    <p class="t-wind">${esc(c.wind_text)}</p>
    ${c.windows && c.windows.length ? `<p class="t-rain">降水时段 ${esc(c.windows_text)}</p>` : ''}
  </article>`;
}

function renderToday() {
  const box = $('today');
  const live = livePeriods();
  const m = S.doc.meta;
  if (!live.length) {
    box.className = 'today';
    box.innerHTML = '<p class="boot">这份预报的所有时段都已过去，请等待下一次发布。</p>';
    return;
  }
  const cur = live[0], next = live[1];
  const c0 = cellOf(cur, seatId()) || cur.cells[0];
  document.body.dataset.sky = kindOf(c0.weather, cur.kind === 'night');
  const now = nowValue();
  const issued = (m.issue_local || '').replace('T', ' ');
  box.className = 'today ready';
  box.innerHTML = `
      <div class="t-head">
        <h1 class="t-where">${esc(m.county)}<span class="t-sep">·</span>${esc(nameOf(c0.point))}${
          c0.point === seatId() ? '<span class="t-badge">县城</span>' : ''}</h1>
        <p class="t-date">${md(todayLocal())} ${wdOf(todayLocal())}${
          now ? ` · 此刻约 <b>${n1(now.temp)}℃</b><span class="dim">（${hourOf(now.time)}时预报值）</span>` : ''}</p>
      </div>
      <div class="t-cards">
        ${periodCard(cur, 't-now')}
        ${next ? periodCard(next, 't-next') : ''}
      </div>
    <div class="t-side">
      <p class="t-sum">未来五天</p>
      <p class="t-say">${esc(S.doc.conclusions.headline)}</p>
      <p class="t-src">
        ${issued ? `<b>${esc(issued)}</b> 发布` : ''}${m.engine === 'multimodel' ? '' : ` · 起报 <b>${esc(m.run)}</b> UTC`}
        · ${m.engine === 'multimodel' ? `${(m.sources || []).length} 家模式融合${m.calibration ? ' + 站点订正' : ''}`
          : esc((m.sources || []).map(s => ({ ecmwf: 'ECMWF IFS', gfs: 'NOAA GFS' }[s] || s)).join(' + ')) + ' 融合'}
        · ${m.n_townships} 个乡镇
      </p>
    </div>`;
}

/* ---------- 2. alerts: one row per type ---------- */
function dateList(dates) {
  return dates.map((x, i) =>
    (i > 0 && x.slice(5, 7) === dates[i - 1].slice(5, 7)) ? `${Number(x.slice(8, 10))}日` : md(x)).join('、');
}
function renderAlerts() {
  const box = $('alerts');
  const list = (S.doc.conclusions.alerts || []).filter(a => a.date >= todayLocal());
  if (!list.length) { box.hidden = true; box.innerHTML = ''; return; }
  const byType = new Map();
  for (const a of list) {
    const cur = byType.get(a.type);
    if (!cur) byType.set(a.type, { ...a, dates: [a.date] });
    else {
      cur.dates.push(a.date);
      if ((RANK[a.level] ?? 0) > (RANK[cur.level] ?? 0)) Object.assign(cur, { level: a.level, detail: a.detail, criterion: a.criterion });
    }
  }
  box.hidden = false;
  box.innerHTML = [...byType.values()].map(a => `
    <div class="alert ${ALERT_CLASS[a.level] || 'a-note'}">
      <span class="a-type">${esc(a.type)}</span>
      <span class="a-lvl">${a.level === '关注' ? '关注' : `达${esc(a.level)}预警标准`}</span>
      <span class="a-what">${esc(a.detail)}<small>${esc(a.criterion || '')}</small></span>
      <span class="a-when">${dateList(a.dates)}</span>
    </div>`).join('') +
    '<p class="a-foot">据模式预报推算，仅作提示；以当地气象台发布的预警信号为准。</p>';
}

/* ---------- 3. 0–72 h every 3 hours ---------- */
const isNight3 = (r) => { const s = (hourOf(r.time) + 21) % 24; return s >= 20 || s < 8; };
/** Local date a 3-hour window is shown under: its end time, except that the 21–24
 * window (ending "00时") stays with the evening it belongs to. */
const winDate = (r) => new Date(localMs(r.time) - 3600e3 + tzH() * 3600e3).toISOString().slice(0, 10);

function smoothPath(pts) {
  if (!pts.length) return '';
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

/* One SVG, one column per 3-hour window: date, hour, icon, temperature curve,
 * rain bars, wind. Columns stretch to the host's width and scroll below 40 px. */
function chart3h(rows, { host, uid = 'hc', label = '' } = {}) {
  const n = rows.length;
  if (!n) return '<p class="hr-empty">这一时段已全部过去</p>';
  const avail = host ? host.clientWidth : 960;
  const cw = Math.max(40, Math.floor(avail / n));
  const W = n * cw, H = 236;
  const x = (i) => i * cw + cw / 2;
  const temps = rows.map(r => r.temp).filter(Number.isFinite);
  let lo = Math.min(...temps), hi = Math.max(...temps);
  if (hi - lo < 4) { const m = (hi + lo) / 2; lo = m - 2; hi = m + 2; }
  const T0 = 88, T1 = 148;
  const yT = (t) => T1 - ((t - lo) / (hi - lo)) * (T1 - T0);
  const P0 = 200, PH = 30;
  const maxP = Math.max(3, ...rows.map(r => r.precip ?? 0));
  const bg = [], top = [], icons = [], labels = [], bars = [], winds = [], hits = [];
  rows.forEach((r, i) => {
    const h = hourOf(r.time), cx = x(i), night = isNight3(r);
    if (night) bg.push(`<rect class="hc-night" x="${i * cw}" y="0" width="${cw}" height="${H}"/>`);
    const d = winDate(r);
    if (i === 0 || d !== winDate(rows[i - 1])) {
      if (i > 0) bg.push(`<line class="hc-day" x1="${i * cw}" y1="4" x2="${i * cw}" y2="${H - 4}"/>`);
      top.push(`<text class="hc-date" x="${i * cw + 6}" y="14">${relName(d)} ${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}</text>`);
    }
    top.push(`<text class="hc-hour" x="${cx}" y="33">${String(h).padStart(2, '0')}时</text>`);
    icons.push(`<g transform="translate(${cx - 12},41) scale(.375)">${GLYPH[kindOf(r.weather, night)]}</g>`);
    if (Number.isFinite(r.temp)) labels.push(`<text class="hc-t" x="${cx}" y="${(yT(r.temp) - 8).toFixed(1)}">${Math.round(r.temp)}°</text>`);
    const p = r.precip ?? 0;
    if (p >= 0.1) {
      const bh = Math.max(2, (p / maxP) * PH);
      bars.push(`<rect class="hc-bar${(r.snow ?? 0) >= 0.1 ? ' snow' : ''}" x="${i * cw + 9}" y="${(P0 - bh).toFixed(1)}" width="${cw - 18}" height="${bh.toFixed(1)}" rx="3"/>`);
      bars.push(`<text class="hc-p" x="${cx}" y="${(P0 - bh - 4).toFixed(1)}">${p >= 10 ? Math.round(p) : p.toFixed(1)}</text>`);
    }
    if (Number.isFinite(r.wind_dir)) {
      winds.push(`<g transform="translate(${cx - 9},221) rotate(${(r.wind_dir + 180) % 360})"><path class="hc-arrow" d="M0,-6 L4.2,4.5 L0,2.2 L-4.2,4.5Z"/></g>` +
        `<text class="hc-wf${(r.wind_force ?? 0) >= 6 ? ' hi' : ''}" x="${cx + 1}" y="225">${r.wind_force ?? '—'}级</text>`);
    }
    hits.push(`<rect class="hc-hit" x="${i * cw}" y="0" width="${cw}" height="${H}"><title>${esc(
      `${md(r.time)} ${String((h + 21) % 24).padStart(2, '0')}—${String(h || 24).padStart(2, '0')}时  ${r.weather}  ` +
      `${n1(r.temp, 1)}℃  降水 ${n1(p, 1)} mm  ${r.wind_name || ''}${r.wind_force ?? ''}级`)}</title></rect>`);
  });
  const pts = rows.map((r, i) => [x(i), yT(r.temp)]).filter(p => Number.isFinite(p[1]));
  const line = smoothPath(pts);
  const area = pts.length ? `${line} L${pts[pts.length - 1][0].toFixed(1)},${P0 - PH - 12} L${pts[0][0].toFixed(1)},${P0 - PH - 12} Z` : '';
  const mid = (hi + lo) / 2;
  return `<div class="hc-scroll" tabindex="0" role="region" aria-label="${esc(label)}逐3小时预报图">
    <svg class="hc" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="${esc(label)}未来 ${n * 3} 小时：气温 ${Math.round(Math.min(...temps))} 到 ${Math.round(Math.max(...temps))} 摄氏度">
      <defs>
        <linearGradient id="${uid}-t" gradientUnits="userSpaceOnUse" x1="0" y1="${T0}" x2="0" y2="${T1}">
          <stop offset="0" stop-color="${tcol(hi)}"/><stop offset=".5" stop-color="${tcol(mid)}"/><stop offset="1" stop-color="${tcol(lo)}"/>
        </linearGradient>
        <linearGradient id="${uid}-a" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="${tcol(hi)}" stop-opacity=".2"/><stop offset="1" stop-color="${tcol(lo)}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${bg.join('')}
      <line class="hc-base" x1="0" y1="${P0 + .5}" x2="${W}" y2="${P0 + .5}"/>
      <path d="${area}" fill="url(#${uid}-a)"/>
      <path class="hc-line" d="${line}" stroke="url(#${uid}-t)"/>
      ${pts.map(p => `<circle class="hc-dot" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.8"/>`).join('')}
      ${top.join('')}${icons.join('')}${labels.join('')}${bars.join('')}${winds.join('')}${hits.join('')}
    </svg></div>`;
}

/* Detail table: the only place (besides the township sheet) that lists the probability. */
function table3h(rows, id) {
  let day = '';
  const body = rows.map(r => {
    const h = hourOf(r.time);
    const d = winDate(r);
    const head = d !== day ? (day = d, `<tr class="ht-day"><th colspan="8" scope="colgroup">${md(d)} ${wdOf(d)}</th></tr>`) : '';
    const p = r.precip ?? 0;
    return `${head}<tr>
      <th scope="row">${String((h + 21) % 24).padStart(2, '0')}—${String(h || 24).padStart(2, '0')}时</th>
      <td><span class="ht-wx">${glyph(r.weather, 20, isNight3(r))}<span>${esc(r.weather)}</span></span></td>
      <td class="ht-n"><b>${n1(r.temp, 1)}</b>℃</td>
      <td class="ht-n">${p >= 0.05 ? n1(p, 1) : '—'}</td>
      <td>${esc(r.wind_name || '—')}${r.wind_force ?? ''}级 <span class="dim">${n1(r.wind_speed, 1)} m/s</span></td>
      <td class="ht-n">${n1(r.gust, 1)}</td>
      <td class="ht-n">${n1(r.cloud)}%</td>
      <td class="ht-n">${r.pop === null || r.pop === undefined ? '—' : r.pop + '%'}</td>
    </tr>`;
  }).join('');
  const href = `/api/3h/${encodeURIComponent(id)}${S.file ? `?run=${encodeURIComponent(S.file)}` : ''}`;
  return `<details class="ht-fold">
    <summary>逐3小时明细 <span class="dim">含降水概率 · ${rows.length} 个时段</span></summary>
    <div class="ht-wrap"><table class="ht">
      <thead><tr><th scope="col">时段</th><th scope="col">天气</th><th scope="col">气温</th><th scope="col">降水 mm</th>
        <th scope="col">风</th><th scope="col">阵风 m/s</th><th scope="col">云量</th><th scope="col">降水概率</th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <p class="ht-api">气温、风为该时刻的值，天气与降水为前 3 小时；${esc(popNote(true))}
      数据：<a href="${esc(href)}" target="_blank" rel="noopener">GET ${esc(href)}</a></p>
  </details>`;
}

function renderTownPicker() {
  const sel = $('hr-town');
  const sid = seatId();
  const towns = [...(S.doc.townships || [])].sort((a, b) =>
    (b.id === sid) - (a.id === sid) || (b.elevation ?? 0) - (a.elevation ?? 0));
  if (!S.town3h || !towns.some(t => t.id === S.town3h)) S.town3h = sid || (towns[0] && towns[0].id);
  sel.innerHTML = towns.map(t => `<option value="${esc(t.id)}"${t.id === S.town3h ? ' selected' : ''}>${
    esc(t.name)}${t.id === sid ? '（县城）' : ''} · ${n1(t.elevation)} m</option>`).join('');
}

function render3h() {
  const id = S.town3h;
  const host = $('hr-chart');
  const rows = rows3h(id);
  const t = (S.doc.townships || []).find(x => x.id === id) || {};
  if (!S.doc.series3h) {
    host.innerHTML = '<p class="hr-empty">这一起报时次没有逐3小时产品</p>';
    $('hr-table').innerHTML = ''; $('hr-days').innerHTML = ''; $('hr-sub').textContent = '';
    return;
  }
  host.innerHTML = chart3h(rows, { host, uid: 'hb', label: t.name || '' });
  $('hr-table').innerHTML = rows.length ? table3h(rows, id) : '';
  $('hr-sub').textContent = `${t.name || ''} · 海拔 ${n1(t.elevation)} m · 未来 ${rows.length * 3} 小时`;
  // date chips jump the scroller
  const firsts = [];
  rows.forEach((r, i) => { if (i === 0 || winDate(r) !== winDate(rows[i - 1])) firsts.push([i, winDate(r)]); });
  $('hr-days').innerHTML = firsts.map(([i, d], k) =>
    `<button type="button" class="chip${k === 0 ? ' on' : ''}" data-col="${i}">${relName(d)}<span>${
      Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}</span></button>`).join('');
  const scroller = host.querySelector('.hc-scroll');
  if (!scroller) return;
  const cw = scroller.scrollWidth / Math.max(rows.length, 1);
  const chips = [...$('hr-days').querySelectorAll('.chip')];
  chips.forEach(b => b.addEventListener('click', () =>
    scroller.scrollTo({ left: Math.max(0, Number(b.dataset.col) * cw - 4), behavior: 'smooth' })));
  scroller.addEventListener('scroll', () => {
    const col = (scroller.scrollLeft + cw) / cw;
    let on = 0;
    firsts.forEach(([i], k) => { if (col >= i) on = k; });
    chips.forEach((c, k) => c.classList.toggle('on', k === on));
  }, { passive: true });
}

/* ---------- 4. five days, 白天 / 夜间 ---------- */
function renderWeek() {
  const box = $('week');
  const days = liveDays();
  if (!days.length) { box.innerHTML = ''; return; }
  // default to the first date that still has its daytime, not last night's remainder
  if (!S.date || !days.some(d => d.date === S.date)) S.date = (days.find(d => d.day) || days[0]).date;
  const sid = seatId();
  const his = days.map(d => (d.day ? (cellOf(d.day, sid) || {}).temp : null));
  const los = days.map(d => (d.night ? (cellOf(d.night, sid) || {}).temp : null));
  const all = [...his, ...los].filter(Number.isFinite);
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = Math.max(hi - lo, 4);
  const CH = 96, PAD = 22;                         // chart row height, label room
  const y = (t) => PAD + (1 - (t - lo) / span) * (CH - 2 * PAD);
  const n = days.length;
  const X = (i) => ((i + 0.5) / n) * 100;
  const line = (vals) => vals.map((v, i) => (Number.isFinite(v) ? `${X(i).toFixed(2)},${y(v).toFixed(1)}` : null))
    .filter(Boolean).join(' ');
  const dots = (vals, cls) => vals.map((v, i) => (Number.isFinite(v)
    ? `<i class="wk-dot ${cls}" style="left:${X(i).toFixed(2)}%;top:${y(v).toFixed(1)}px"></i>
       <b class="wk-lab ${cls}" style="left:${X(i).toFixed(2)}%;top:${(cls === 'hi' ? y(v) - 24 : y(v) + 7).toFixed(1)}px">${Math.round(v)}°</b>`
    : '')).join('');

  const half = (q, kind, gone) => {
    if (!q) return `<span class="wk-half ${kind} none"><span class="wk-ico"></span><em>${gone ? '已过' : '—'}</em></span>`;
    const c = cellOf(q, sid) || q.cells[0];
    return `<span class="wk-half ${kind}">${glyph(c.weather, 36, kind === 'night')}<em>${esc(c.weather)}</em></span>`;
  };
  const wind = (q) => {
    if (!q) return '<span class="wk-wind none">—</span>';
    const c = cellOf(q, sid) || q.cells[0];
    return `<span class="wk-wind">${esc(c.wind_name || '')}<b>${esc(c.force_text || '')}</b></span>`;
  };

  box.innerHTML = `<div class="wk" style="--n:${n}">
    ${days.map((d, i) => `<button type="button" class="wk-col${d.date === S.date ? ' on' : ''}" data-date="${d.date}"
        aria-pressed="${d.date === S.date}" aria-label="${esc(`${colName(d.date)} ${md(colDate(d.date))}`)}">
      <span class="wk-dow">${colName(d.date)}</span>
      <span class="wk-date">${Number(colDate(d.date).slice(5, 7))}/${Number(colDate(d.date).slice(8, 10))}</span>
      <span class="wk-tag">白天</span>
      ${half(d.day, 'day', d.dayGone)}
      <span class="wk-gap" style="height:${CH}px"></span>
      ${half(d.night, 'night', false)}
      <span class="wk-tag">夜间</span>
      <span class="wk-winds">${wind(d.day)}${wind(d.night)}</span>
    </button>`).join('')}
    <div class="wk-chart" style="height:${CH}px" aria-hidden="true">
      <svg viewBox="0 0 100 ${CH}" preserveAspectRatio="none">
        <polyline class="wk-line hi" points="${line(his)}"/>
        <polyline class="wk-line lo" points="${line(los)}"/>
      </svg>
      ${dots(his, 'hi')}${dots(los, 'lo')}
    </div>
  </div>
  <p class="note">县城 ${esc(nameOf(sid))}：上排白天天气与最高气温，下排夜间天气与最低气温；风为白天 / 夜间。点一天看各乡镇。</p>`;

  // the chart row sits exactly where the columns leave their gap
  const gap = box.querySelector('.wk-gap');
  const wk = box.querySelector('.wk');
  box.querySelector('.wk-chart').style.top = `${gap.getBoundingClientRect().top - wk.getBoundingClientRect().top}px`;

  box.querySelectorAll('.wk-col').forEach(b => b.addEventListener('click', () => {
    S.date = b.dataset.date;
    renderWeek();
    renderTowns();
    box.querySelector(`[data-date="${S.date}"]`).focus();
  }));
}

/* ---------- 5. townships for the chosen date, by elevation ---------- */
function renderTowns() {
  const d = liveDays().find(x => x.date === S.date) || liveDays()[0];
  if (!d) { $('towns').innerHTML = ''; return; }
  const sid = seatId();
  const rows = S.doc.townships.map(t => ({
    t, day: cellOf(d.day, t.id), night: cellOf(d.night, t.id),
  })).sort((a, b) => (b.t.elevation ?? 0) - (a.t.elevation ?? 0));
  const temps = rows.flatMap(r => [r.day && r.day.temp, r.night && r.night.temp]).filter(Number.isFinite);
  const lo = Math.floor(Math.min(...temps)), hi = Math.ceil(Math.max(...temps));
  const span = Math.max(hi - lo, 1);
  const rainTop = Math.max(10, ...rows.map(r => (r.day?.precip ?? 0) + (r.night?.precip ?? 0)));

  $('towns-when').textContent = dayDiff(d.date) === -1
    ? `今天凌晨（${md(d.date)}夜间，至 08 时） · 按海拔从高到低`
    : `${relName(d.date)} ${md(d.date)} ${wdOf(d.date)}${d.day ? '' : '（白天已过，仅夜间）'} · 按海拔从高到低`;

  const wx = (c, night) => (c ? `${glyph(c.weather, 22, night)}<em>${esc(c.weather)}</em>` : '<em class="dim">—</em>');
  const head = `<div class="town town-head" aria-hidden="true">
      <span class="r-name">乡镇</span><span class="r-alt">海拔</span>
      <span class="r-wx">白天</span><span class="r-wx">夜间</span>
      <span class="r-scale"><span class="r-track">
        <b class="h-lo">${lo}°</b><i class="h-grad" style="background:${tgrad(lo, hi, '90deg')}"></i><b class="h-hi">${hi}°</b>
      </span></span>
      <span class="r-wind">风 白天 / 夜间</span><span class="r-rain">降水 mm</span>
    </div>`;
  const body = rows.map(({ t, day, night }) => {
    const tl = night ? night.temp : null, th = day ? day.temp : null;
    const a = Number.isFinite(tl) ? tl : th, b = Number.isFinite(th) ? th : tl;
    const left = ((a - lo) / span) * 100;
    const width = Math.max(((b - a) / span) * 100, 1.5);
    const rain = (day?.precip ?? 0) + (night?.precip ?? 0);
    const rainW = rain >= 0.05 ? Math.max((rain / rainTop) * 100, 3) : 0;
    const seat = t.id === sid;
    const windD = day ? day.wind_text : '—';
    const windN = night ? (day && day.wind_name === night.wind_name ? night.force_text : night.wind_text) : '—';
    const label = `${t.name}${seat ? '（县城）' : ''}，海拔 ${n1(t.elevation)} 米，` +
      `${day ? `白天${day.weather}，最高${n1(th)}度，` : ''}${night ? `夜间${night.weather}，最低${n1(tl)}度，` : ''}` +
      `降水 ${n1(rain, 1)} 毫米`;
    return `<button class="town${seat ? ' is-seat' : ''}" type="button" data-id="${esc(t.id)}" aria-label="${esc(label)}">
      <span class="r-name">${esc(t.name)}${seat ? '<i class="r-seat">县城</i>' : ''}</span>
      <span class="r-alt">${n1(t.elevation)}<i>m</i></span>
      <span class="r-wx">${wx(day, false)}</span>
      <span class="r-wx">${wx(night, true)}</span>
      <span class="r-scale"><span class="r-track">
        <i class="r-span" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%;background:${tgrad(a, b, '90deg')}"></i>
        ${Number.isFinite(tl) ? `<b class="r-lo" style="left:${left.toFixed(1)}%">${n1(tl)}</b>` : ''}
        ${Number.isFinite(th) ? `<b class="r-hi" style="left:${(left + width).toFixed(1)}%">${n1(th)}</b>` : ''}
      </span></span>
      <span class="r-wind"><span>${esc(windD)}</span><span class="dim">${esc(windN)}</span></span>
      <span class="r-rain"><span class="r-bar"><i style="width:${rainW.toFixed(1)}%"></i></span>
        <em>${rain >= 0.05 ? n1(rain, 1) : '—'}</em></span>
    </button>`;
  }).join('');
  $('towns').innerHTML = head + body;
  $('towns').querySelectorAll('button.town').forEach(b => b.addEventListener('click', () => openSheet(b.dataset.id, b)));
}

/* ---------- township sheet: 3-hourly + every period, with probabilities ---------- */
function openSheet(id, from) {
  const t = S.doc.townships.find(x => x.id === id);
  if (!t) return;
  const dz = (t.elevation ?? 0) - (t.model_elevation ?? 0);
  const corr = -dz * 0.0065;
  const rows = livePeriods().map((q, i) => {
    const c = cellOf(q, id);
    if (!c) return '';
    const night = q.kind === 'night';
    return `<tr class="${night ? 'night' : ''}${i === 0 ? ' on' : ''}">
      <th scope="row"><b>${esc(periodLabel(q))}</b><span>${md(q.date)} ${spanText(q)}</span></th>
      <td class="s-wx">${glyph(c.weather, 24, night)}<span>${esc(c.weather)}</span></td>
      <td class="s-t"><span>${night ? '最低' : '最高'}</span><b>${n1(c.temp)}°</b></td>
      <td class="s-wind">${esc(c.wind_text)}</td>
      <td class="s-num">${(c.precip ?? 0) >= 0.05 ? `${n1(c.precip, 1)} mm` : '—'}</td>
      <td class="s-win">${esc(c.windows_text || '—')}</td>
      <td class="s-num">${c.pop === null || c.pop === undefined ? '—' : `${Math.round(c.pop / 10) * 10}%`}</td>
    </tr>`;
  }).join('');
  const href = `/api/forecast/${encodeURIComponent(id)}${S.file ? `?run=${encodeURIComponent(S.file)}` : ''}`;
  $('sheet-body').innerHTML = `
    <h3 id="sheet-title" class="s-title">${esc(t.name)}</h3>
    <p class="s-meta">海拔 <b>${n1(t.elevation)} m</b> · 模式地形 ${n1(t.model_elevation)} m
      · ${Number(t.lat).toFixed(3)}°N ${Number(t.lon).toFixed(3)}°E</p>
    <p class="s-note">比模式地形${dz >= 0 ? '高' : '低'} ${n1(Math.abs(dz))} m，按 −6.5 K/km 递减率，气温订正约 ${
      corr >= 0 ? '+' : '−'}${n1(Math.abs(corr), 1)} K。</p>
    <h4 class="s-h">未来三天 · 逐3小时</h4>
    <div id="s-3h" class="s-hourly"></div>
    <h4 class="s-h">白天 / 夜间</h4>
    <div class="s-wrap"><table class="s-table">
      <thead><tr><th scope="col">时段</th><th scope="col">天气</th><th scope="col">气温</th><th scope="col">风</th>
        <th scope="col">降水</th><th scope="col">降水时段</th><th scope="col">降水概率</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <p class="ht-api">${esc(popNote(false))}
      数据：<a href="${esc(href)}" target="_blank" rel="noopener">GET ${esc(href)}</a></p>`;

  S.back = from || document.activeElement;
  const sheet = $('sheet');
  sheet.hidden = false;
  document.body.classList.add('lock');
  const box = $('s-3h');
  const r3 = rows3h(id);
  box.innerHTML = chart3h(r3, { host: box, uid: 'hs', label: t.name }) + (r3.length ? table3h(r3, id) : '');
  requestAnimationFrame(() => sheet.classList.add('on'));
  $('sheet-close').focus();
}
function closeSheet() {
  const sheet = $('sheet');
  if (sheet.hidden) return;
  sheet.classList.remove('on');
  document.body.classList.remove('lock');
  setTimeout(() => { sheet.hidden = true; }, 180);
  if (S.back && document.contains(S.back)) S.back.focus();
  S.back = null;
}
function trapFocus(e) {
  if (e.key !== 'Tab' || $('sheet').hidden) return;
  const f = [...$('sheet').querySelectorAll('button, [href], summary, [tabindex]:not([tabindex="-1"])')];
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ---------- method, bulletin, API ---------- */
/* Model cards: what actually went into the product on screen. */
const MODEL_INFO = {
  ecmwf_ifs: ['ECMWF IFS', '欧洲中期天气预报中心', '9 km · 逐小时至 90 h，其后 3 h', '公认中期预报技巧最高的物理模式。'],
  ecmwf_aifs025_single: ['ECMWF AIFS', '欧洲中期天气预报中心', '0.25° · 6 h', 'ECMWF 的 AI 模式，用再分析训练的图神经网络。'],
  icon_global: ['DWD ICON', '德国气象局', '13 km · 逐小时至 78 h', '非静力物理模式，本地检验气温最准的单家之一。'],
  ukmo_global_deterministic_10km: ['UKMO', '英国气象局', '10 km · 逐小时', '统一模式（Unified Model）全球版。'],
  cma_grapes_global: ['CMA GRAPES', '中国气象局', '0.125° · 3 h · 约 5.5 天', '我国自主研发的全球同化预报系统。'],
  gem_global: ['CMC GEM', '加拿大气象局', '15 km · 3 h', '加拿大全球环境多尺度模式。'],
  meteofrance_arpege_world: ['ARPEGE', '法国气象局', '0.25° · 约 4 天', '法国全球谱模式。'],
  ncep_aigfs025: ['NOAA AIGFS', '美国国家环境预报中心', '0.25° · 6 h', 'NOAA 的 AI 全球模式。'],
  ecmwf: ['ECMWF IFS', '欧洲中期天气预报中心', '0.25° · 3 h', '开放数据 GRIB，本地降尺度。'],
  gfs: ['NOAA GFS', '美国国家环境预报中心', '0.25° · 逐小时至 120 h', '开放数据 GRIB，本地降尺度。'],
};
function renderMethod() {
  const m = S.doc.meta;
  const multi = m.engine === 'multimodel';
  const cards = (m.sources || []).map(id => {
    const [name, org, spec, note] = MODEL_INFO[id] || [id, '', '', ''];
    return `<article class="src"><p class="src-tag">${esc(org)}</p><h3>${esc(name)}</h3>
      <p class="src-spec">${esc(spec)}</p><p>${esc(note)}</p></article>`;
  });
  if (m.pop_source !== 'multimodel') {
    cards.push(`<article class="src src-aux"><p class="src-tag">明细用 · ${m.pop_members || 21} 个成员</p><h3>NOAA GEFS</h3>
      <p class="src-spec">0.5° · 6 h 降水</p><p>集合预报，只用来给明细表算降水概率。</p></article>`);
  }
  cards.push(`<article class="src src-aux"><p class="src-tag">订正用 · 7 个国家站</p><h3>SYNOP 实况</h3>
    <p class="src-spec">每 3 小时 · 经 OGIMET 转发</p><p>武夷山、邵武、浦城、南城、景德镇、衢州、南昌的气温、降水实况。</p></article>`);
  $('sources').innerHTML = cards.join('');
  $('sources').classList.toggle('grib', !multi);
}

/* ---------- accuracy: out-of-sample verification against the stations ---------- */
function renderAccuracy() {
  const v = S.doc.verification;
  const box = $('acc');
  if (!v || v.error || !v.configs) {
    $('acc-sub').textContent = '';
    box.innerHTML = '<p class="hr-empty">还没有足够的检验样本。</p>';
    return;
  }
  $('acc-sub').textContent = `${md(v.window[0])}—${md(v.window[1])} · ${v.days} 天 · 周边 ${v.stations.length} 个国家站 · 全部为样本外成绩`;
  const order = ['new', 'new_raw', 'old', 'ecmwf'];
  const leads = ['1', '2', '3', '4', '5'];
  const get = (c, l) => c.scores[l] || c.scores[Number(l)];
  const metric = [
    ['白天最高气温', 'tmax', 'acc2', '误差 ≤2 ℃ 的比例', '%', true],
    ['夜间最低气温', 'tmin', 'acc2', '误差 ≤2 ℃ 的比例', '%', true],
    ['晴雨', 'rain', 'pc', '12 小时有无降水判对的比例', '%', true],
    ['降水 TS', 'rain', 'ts', '有雨时段的命中评分', '', true],
    ['中雨以上 TS', 'rain5', 'ts', '12 小时 ≥5 mm 的命中评分', '', true],
  ];
  const best = {};
  for (const [, v1, v2] of metric) for (const l of leads) {
    const vals = order.map(k => v.configs[k] ? (get(v.configs[k], l)[v1] || {})[v2] : null).filter(Number.isFinite);
    best[`${v1}.${v2}.${l}`] = Math.max(...vals);
  }
  const rows = metric.map(([label, v1, v2, hint, unit]) => `
    <tr class="acc-h"><th scope="rowgroup" colspan="6">${label}<small>${hint}</small></th></tr>
    ${order.filter(k => v.configs[k]).map(k => {
      const c = v.configs[k];
      return `<tr class="${k === 'new' ? 'on' : ''}"><th scope="row">${esc(c.label)}</th>${leads.map(l => {
        const x = (get(c, l)[v1] || {})[v2];
        const top = Number.isFinite(x) && x === best[`${v1}.${v2}.${l}`];
        return `<td class="${top ? 'top' : ''}">${Number.isFinite(x) ? `${x.toFixed(0)}${unit}` : '—'}</td>`;
      }).join('')}</tr>`;
    }).join('')}`).join('');
  const mae = (k, f, l) => { const c = v.configs[k]; const s = c && get(c, l)[f]; return s && Number.isFinite(s.mae) ? s.mae : null; };
  const n1n = mae('new', 'tmax', '1'), o1 = mae('old', 'tmax', '1');
  box.innerHTML = `
    <div class="acc-key">
      <div><b>${n1(n1n, 2)} ℃</b><span>明天白天最高气温平均误差</span><em>原方案 ${n1(o1, 2)} ℃</em></div>
      <div><b>${n1(mae('new', 'tmin', '1'), 2)} ℃</b><span>今夜最低气温平均误差</span><em>原方案 ${n1(mae('old', 'tmin', '1'), 2)} ℃</em></div>
      <div><b>${n1(get(v.configs.new, '1').rain.pc)}%</b><span>明天晴雨准确率</span><em>原方案 ${n1(get(v.configs.old, '1').rain.pc)}%</em></div>
    </div>
    <div class="acc-wrap"><table class="acc-t">
      <thead><tr><th scope="col">方案</th>${leads.map(l => `<th scope="col">第${l}天</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${v.pop ? `<p class="note">明细里的降水概率（8 家模式各自订正后的有雨比例）：Brier 技巧评分 ${n1(v.pop.bss * 100)}%（相对气候概率）；
      ${v.pop.reliability.filter(r => r.n).map(r => `预报 ${Math.round(r.bin[0] * 100)}—${Math.round(r.bin[1] * 100)}% 时实况有雨 ${n1(r.observed * 100)}%`).join('，')}。</p>` : ''}
    <p class="note">${esc(v.notes || '')} 每列最好的成绩加粗。</p>`;
}
function renderText() { $('doc-text').textContent = S.doc.text || ''; }

const ENDPOINTS = [
  ['/api/forecast/{乡镇}', '某乡镇白天/夜间预报（id 或名称均可）：天气、气温、风、降水量、降水时段；明细含降水概率'],
  ['/api/3h/{乡镇}', '某乡镇未来 72 小时逐3小时预报，按行；?hours=24 只取前 24 小时'],
  ['/api/3h', '全部乡镇逐3小时预报，按列存放'],
  ['/api/latest', '最新产品全量（periods / days / series3h / conclusions / text）'],
  ['/api/summary', '仅摘要：起报信息、结论、各时段县级概况（不含乡镇明细）'],
  ['/api/runs', '历史起报时次索引，新到旧'],
  ['/api/runs/{file}', '按索引里的 file 字段取某一次起报的完整产品；其余接口加 ?run={file} 同理'],
  ['/api/townships', '乡镇名录：id、名称、经纬度、海拔'],
  ['/api/hourly/{乡镇}', '逐小时序列（参考）：3 小时值按 GFS 逐时形态插值得到，逐时精度有限，页面不展示'],
  ['/api/health', '健康检查'],
];
function renderApi() {
  const first = S.runs[0] ? encodeURIComponent(S.runs[0].file) : null;
  const seat = encodeURIComponent(S.doc.meta.seat || '');
  $('api-list').innerHTML = ENDPOINTS.map(([path, desc]) => {
    let href = path;
    if (path.includes('{file}')) href = first ? path.replace('{file}', first) : null;
    if (path.includes('{乡镇}')) href = path.replace('{乡镇}', seat);
    return `<div class="ep"><code>GET ${esc(path)}</code><span>${esc(desc)}</span>
      ${href ? `<a href="${esc(href)}" target="_blank" rel="noopener">打开</a>` : '<span></span>'}</div>`;
  }).join('');
  $('api-curl').textContent =
    `# 县城白天/夜间预报\n` +
    `curl -s '${location.origin}/api/forecast/${seat}' | python3 -c '\n` +
    `import json,sys\n` +
    `for p in json.load(sys.stdin)["periods"]:\n` +
    `    print(p["label"], p["weather"], ("最高" if p["kind"]=="day" else "最低"), round(p["temp"]), "℃", p["wind_text"])'\n\n` +
    `# 县城未来 24 小时逐3小时\ncurl -s '${location.origin}/api/3h/${seat}?hours=24' | python3 -m json.tool`;
}

function renderRuns() {
  const sel = $('runs');
  if (!S.runs.length) { sel.hidden = true; return; }
  sel.hidden = false;
  sel.innerHTML = S.runs.map(r => {
    const label = r.engine === 'multimodel' && r.issue_local
      ? `${r.issue_local.slice(5, 16).replace('T', ' ')} 发布` : `${r.run} UTC 起报`;
    return `<option value="${esc(r.file)}"${r.file === S.file ? ' selected' : ''}>${esc(label)}</option>`;
  }).join('');
}
async function pickRun(file) {
  const sel = $('runs');
  sel.disabled = true;
  try {
    const doc = await api(`/api/runs/${encodeURIComponent(file)}`);
    if (!doc.periods) throw new Error('这一时次是旧格式产品（没有白天/夜间分段），无法显示');
    S.doc = doc; S.file = file; S.names = null; S.date = null;
    paint();
  } catch (err) {
    fail(err.message);
  }
  sel.disabled = false;
}

function bjTime(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso || '—';
  return t.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function paint() {
  renderToday();
  renderAlerts();
  renderTownPicker();
  render3h();
  renderWeek();
  renderTowns();
  renderMethod();
  renderAccuracy();
  renderText();
  renderApi();
  renderRuns();
  const m = S.doc.meta;
  $('foot').innerHTML =
    `<span>${esc(m.county)} · ${m.engine === 'multimodel' ? `${esc((m.sources || []).length)} 家模式融合` : `${esc(m.run)} UTC 起报`}` +
    ` · 生成于 ${esc(bjTime(m.generated))} 北京时</span>` +
    `<span>${m.engine === 'multimodel' ? '模式数据：<a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo.com</a>（CC BY 4.0）· ' : ''}` +
    `ECMWF 开放数据、NOAA GFS/GEFS、Copernicus DEM、OGIMET · 自动生成，仅供参考</span>`;
}

function wire() {
  $('runs').addEventListener('change', e => pickRun(e.target.value));
  $('hr-town').addEventListener('change', e => { S.town3h = e.target.value; render3h(); });
  $('sheet-close').addEventListener('click', closeSheet);
  $('sheet').addEventListener('click', e => { if (e.target.id === 'sheet') closeSheet(); });
  $('copy-doc').addEventListener('click', copyDoc);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); trapFocus(e); });
  let t = null;  // re-fit the charts to a new width
  window.addEventListener('resize', () => {
    clearTimeout(t);
    t = setTimeout(() => { if (S.doc && S.doc.periods) { render3h(); renderWeek(); } }, 200);
  });
}
async function copyDoc() {
  const btn = $('copy-doc');
  const was = btn.textContent;
  try { await navigator.clipboard.writeText(S.doc.text || ''); btn.textContent = '已复制'; }
  catch { btn.textContent = '复制失败'; }
  setTimeout(() => { btn.textContent = was; }, 1400);
}
function fail(msg) {
  const box = $('today');
  box.className = 'today';
  box.innerHTML = `<div class="empty">
    <h2>暂时无法显示预报</h2>
    <p>定时任务尚未产出新格式的预报，或数据目录为空。手动生成一次：</p>
    <pre>python3 -m wxgrid publish --townships yanshan_townships.csv \\
  --county 铅山县 --seat 河口镇 --data-dir /var/lib/wxgrid</pre>
    <p class="dim">${esc(String(msg || ''))}</p></div>`;
}

async function boot() {
  initTheme();
  wire();
  try {
    S.doc = await api('/api/latest');
    if (!S.doc.periods) throw new Error('产品是旧格式（没有白天/夜间分段），请重新发布一次');
  } catch (err) {
    return fail(err.message);
  }
  try { S.runs = await api('/api/runs'); } catch { S.runs = []; }
  const top = S.runs[0];
  S.file = top && top.run === S.doc.meta.run ? top.file : null;
  paint();
}

boot();
