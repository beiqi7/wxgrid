/* wxgrid web UI — vanilla JS, no build step, no CDN.
 *
 * Single-column narrative: today at the county seat first, then the five-day
 * track, then the townships ranked by elevation (the whole point of the system),
 * then the plain-text bulletin and the API reference folded away.
 *
 * DOM contract: every id used here exists in index.html, and every class emitted
 * here has a rule in styles.css. Township-row classes are `r-*`, hero classes
 * `t-*`, sheet classes `s-*`, rail classes `d-*`, alert classes `a-*`.
 */
'use strict';

const S = {
  doc: null,      // full product JSON
  runs: [],       // index.json entries
  day: 0,         // selected day for the township band
  names: null,    // id -> name
  back: null,     // element to refocus when the sheet closes
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const n1 = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
const pct10 = (p) => (p === null || p === undefined ? null : Math.round(p / 10) * 10);
const md = (iso) => `${Number(iso.slice(5, 7))}月${Number(iso.slice(8, 10))}日`;
const RANK = { 蓝色: 1, 黄色: 2, 橙色: 3, 红色: 4 };
const ALERT_CLASS = { 红色: 'a-red', 橙色: 'a-orange', 黄色: 'a-yellow', 蓝色: 'a-blue' };

/* ---------- theme: day / night ---------- */
function initTheme() {
  // index.html already applied the saved theme in <head> to avoid a flash.
  paintThemeBtn();
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'night' ? 'day' : 'night';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('wxgrid-theme', next); } catch { /* private mode */ }
    paintThemeBtn();  // every colour is a CSS variable, nothing to re-render
  });
}
function paintThemeBtn() {
  const night = document.documentElement.dataset.theme === 'night';
  $('theme').textContent = night ? '日间' : '夜间';
  $('theme').setAttribute('aria-label', night ? '切换到日间主题' : '切换到夜间主题');
}

/* ---------- temperature → colour ----------
 * One fixed scale for the whole page, so 19° looks the same on every row and
 * every day, and a cool ridge township is visibly cooler than the valley. */
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
/** CSS gradient that follows tcol() between lo and hi. */
function tgrad(lo, hi, dir) {
  const n = 6;
  const stops = Array.from({ length: n + 1 }, (_, i) => tcol(lo + ((hi - lo) * i) / n));
  return `linear-gradient(${dir},${stops.join(',')})`;
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
/* The county seat's township id. Products published before seat_id was added to
 * `conclusions` only carry meta.seat (a name), so match on that as a fallback. */
function seatId() {
  const sid = S.doc.conclusions && S.doc.conclusions.seat_id;
  if (sid) return sid;
  const t = (S.doc.townships || []).find(x => x.name === S.doc.meta.seat);
  return t ? t.id : null;
}
/* The seat's cell for a day; if the seat is not in the roster, the first cell.
 * The hero labels the place with nameOf(cell.point), so a fallback is never mislabelled. */
function seatCell(dayIdx) {
  const day = S.doc.days[dayIdx];
  if (!day) return null;
  const sid = seatId();
  return day.cells.find(c => c.point === sid) || day.cells[0] || null;
}

/* ---------- weather → icon kind ---------- */
function kindOf(text) {
  const t = text || '';
  if (t.includes('雪')) return 'snow';
  if (t.includes('暴雨') || t.includes('雷')) return 'storm';
  if (t.includes('大雨') || t.includes('中雨')) return 'rain';
  if (t.includes('雨')) return 'drizzle';
  if (t.includes('阴')) return 'overcast';
  if (t.includes('多云')) return 'cloudy';
  if (t.includes('少云')) return 'partly';
  return 'clear';
}

/* Hand-built SVG on a 64-box so it scales cleanly. */
const GLYPH = {
  clear: `<circle cx="32" cy="32" r="12" class="g-sun"/>
    <g class="g-ray">${[0, 45, 90, 135, 180, 225, 270, 315].map(a =>
      `<line x1="32" y1="10" x2="32" y2="17" transform="rotate(${a} 32 32)"/>`).join('')}</g>`,
  partly: `<circle cx="25" cy="25" r="10" class="g-sun"/>
    <g class="g-ray">${[200, 245, 290].map(a =>
      `<line x1="25" y1="9" x2="25" y2="14" transform="rotate(${a} 25 25)"/>`).join('')}</g>
    <path class="g-cloud" d="M26 46h17a8 8 0 0 0 .8-16 11 11 0 0 0-20.9 3A7 7 0 0 0 26 46z"/>`,
  cloudy: `<path class="g-cloud" d="M21 45h21a8.5 8.5 0 0 0 .9-17 12 12 0 0 0-22.8 3.3A7.5 7.5 0 0 0 21 45z"/>`,
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
};

/* Decorative by default: the weather phrase is always printed next to the icon. */
function glyph(text, size) {
  const k = kindOf(text);
  return `<svg class="wx wx-${k}" viewBox="0 0 64 64" width="${size}" height="${size}"
    aria-hidden="true" focusable="false">${GLYPH[k]}</svg>`;
}

/* ---------- today ---------- */
function renderToday() {
  const box = $('today');
  const d = S.doc.days[0];
  const c = seatCell(0);
  const m = S.doc.meta;
  if (!d || !c) {
    box.className = 'today';
    box.innerHTML = '<p class="boot">没有可用的预报日</p>';
    return;
  }
  const isSeat = c.point === seatId();
  document.body.dataset.sky = kindOf(c.weather);
  const pop = pct10(c.pop);
  const partial = d.hours < 24 ? `<span class="tag">今日仅覆盖 ${d.hours} 小时</span>` : '';

  box.className = 'today ready';
  box.innerHTML = `
    <div class="t-left">
      <h1 class="t-where">${esc(m.county)}<span class="t-sep">·</span>${esc(nameOf(c.point))}${
        isSeat ? '<span class="t-badge">县城</span>' : ''}</h1>
      <p class="t-date">${md(d.date)} ${esc(d.weekday)}</p>
      <div class="t-read">
        ${glyph(c.weather, 92)}
        <div class="t-nums">
          <p class="t-temp"><span class="t-hi">${n1(c.tmax)}</span><span class="t-unit">℃</span></p>
          <p class="t-lo"><span>最低</span><b>${n1(c.tmin)}℃</b></p>
        </div>
      </div>
      <p class="t-wx">${esc(c.weather)}<span class="t-wind">${esc(c.wind_text)}</span></p>
    </div>
    <div class="t-right">
      <p class="t-say">${esc(S.doc.conclusions.headline)}</p>
      <dl class="t-facts">
        <div><dt>降水概率</dt><dd>${pop === null ? '—' : pop + '%'}</dd></div>
        <div><dt>日降水量</dt><dd>${n1(c.precip, 1)}<small> mm</small></dd></div>
        <div class="wide"><dt>降水时段</dt><dd>${esc(c.windows_text || '—')}</dd></div>
      </dl>
      <p class="t-src">
        起报 <b>${esc(m.run)}</b> UTC · 成员 <b>${esc(m.member)}</b>
        · ${esc((m.sources || []).join(' + '))}${m.pop_members ? ` · GEFS ${m.pop_members} 成员` : ''}
        · ${m.n_townships} 个乡镇 ${partial}
      </p>
    </div>`;
}

/* ---------- alerts: one row per type, a type repeated on four days is one line ---------- */
function dateList(dates) {
  return dates.map((x, i) =>
    (i > 0 && x.slice(5, 7) === dates[i - 1].slice(5, 7)) ? `${Number(x.slice(8, 10))}日` : md(x)).join('、');
}
function renderAlerts() {
  const box = $('alerts');
  const list = S.doc.conclusions.alerts || [];
  if (!list.length) { box.hidden = true; box.innerHTML = ''; return; }

  const byType = new Map();
  for (const a of list) {
    const cur = byType.get(a.type);
    if (!cur) byType.set(a.type, { ...a, dates: [a.date] });
    else {
      cur.dates.push(a.date);
      if ((RANK[a.level] || 0) > (RANK[cur.level] || 0)) { cur.level = a.level; cur.detail = a.detail; }
    }
  }
  box.hidden = false;
  box.innerHTML = [...byType.values()].map(a => `
    <div class="alert ${ALERT_CLASS[a.level] || 'a-blue'}">
      <span class="a-type">${esc(a.type)}</span>
      <span class="a-lvl">${esc(a.level)}</span>
      <span class="a-what">${esc(String(a.detail || '').replace(/^\d{4}-\d{2}-\d{2}\s*/, ''))}</span>
      <span class="a-when">${dateList(a.dates)}</span>
    </div>`).join('');
}

/* ---------- 5-day rail ---------- */
function renderDays() {
  const rail = $('rail');
  const days = S.doc.days;
  const lo = Math.min(...days.map(d => d.county.tmin_min).filter(Number.isFinite));
  const hi = Math.max(...days.map(d => d.county.tmax_max).filter(Number.isFinite));
  const span = Math.max(hi - lo, 1);

  rail.innerHTML = days.map((d, i) => {
    const wx = d.county.weather || (seatCell(i) || {}).weather || '';
    const tmax = d.county.tmax_max, tmin = d.county.tmin_min;
    // Bars share one scale across the week, so its shape reads at a glance.
    const top = ((hi - tmax) / span) * 100;
    const bot = ((tmin - lo) / span) * 100;
    const pop = pct10(d.county.pop_max);
    const rain = d.county.precip_max ?? 0;
    return `<button class="day${i === S.day ? ' on' : ''}" type="button" data-day="${i}"
        aria-pressed="${i === S.day}">
      <span class="d-dow">${i === 0 ? '今天' : esc(d.weekday)}</span>
      <span class="d-date">${Number(d.date.slice(5, 7))}/${Number(d.date.slice(8, 10))}</span>
      ${glyph(wx, 44)}
      <span class="d-wx">${esc(wx)}</span>
      <span class="d-bar"><i style="top:${top.toFixed(1)}%;bottom:${bot.toFixed(1)}%;background:${
        tgrad(tmax, tmin, '180deg')}"></i></span>
      <span class="d-t"><b>${n1(tmax)}°</b><span>${n1(tmin)}°</span></span>
      <span class="d-rain">${rain >= 0.1 ? `${n1(rain, 1)} mm` : '无雨'}${pop === null ? '' : ` · ${pop}%`}</span>
      ${d.hours < 24 ? `<span class="d-part" title="该日只覆盖 ${d.hours} 小时">${d.hours}h</span>` : ''}
    </button>`;
  }).join('');

  rail.querySelectorAll('.day').forEach(b => b.addEventListener('click', () => {
    S.day = Number(b.dataset.day);
    renderDays();
    renderTowns();
    rail.querySelector(`[data-day="${S.day}"]`).focus();
  }));
}

/* ---------- townships, ordered by elevation ----------
 * A 28 km model cell is one number; the lapse-rate correction is what separates
 * a 590 m village from a 56 m one. Rows go high -> low and the temperature bars
 * share one scale, so the cooling with height is visible without reading digits.
 * The header row uses the same 7-column grid as the rows, so its temperature
 * legend sits exactly above the bars. */
function renderTowns() {
  const d = S.doc.days[S.day];
  if (!d) return;
  const byId = new Map(S.doc.townships.map(t => [t.id, t]));
  const sid = seatId();
  const rows = d.cells
    .map(c => ({ c, t: byId.get(c.point) }))
    .filter(r => r.t)
    .sort((a, b) => (b.t.elevation ?? 0) - (a.t.elevation ?? 0));

  const temps = rows.flatMap(r => [r.c.tmin, r.c.tmax]).filter(Number.isFinite);
  const lo = Math.floor(Math.min(...temps)), hi = Math.ceil(Math.max(...temps));
  const span = Math.max(hi - lo, 1);
  // Rain bars: at least a 10 mm scale, so 0.3 mm of drizzle stays a sliver
  // instead of filling the bar on a dry day.
  const rainTop = Math.max(10, ...rows.map(r => r.c.precip ?? 0));

  $('towns-when').textContent =
    `${md(d.date)} ${d.weekday}${d.hours < 24 ? `（仅覆盖 ${d.hours} 小时）` : ''} · 按海拔从高到低`;

  const head = `<div class="town town-head" aria-hidden="true">
      <span class="r-name">乡镇</span>
      <span class="r-alt">海拔</span>
      <span class="r-wx">天气</span>
      <span class="r-scale"><span class="r-track">
        <b class="h-lo">${lo}°</b><i class="h-grad" style="background:${tgrad(lo, hi, '90deg')}"></i><b class="h-hi">${hi}°</b>
      </span></span>
      <span class="r-rain">降水 mm</span>
      <span class="r-pop">概率</span>
      <span class="r-wind">风</span>
    </div>`;

  const body = rows.map(({ c, t }) => {
    const left = ((c.tmin - lo) / span) * 100;
    const width = Math.max(((c.tmax - c.tmin) / span) * 100, 2);
    const rain = c.precip ?? 0;
    const rainW = rain >= 0.05 ? Math.max((rain / rainTop) * 100, 3) : 0;
    const pop = pct10(c.pop);
    const seat = t.id === sid;
    const label = `${t.name}${seat ? '（县城）' : ''}，海拔 ${n1(t.elevation)} 米，${c.weather}，` +
      `${n1(c.tmin)} 到 ${n1(c.tmax)} 摄氏度，降水 ${n1(rain, 1)} 毫米` +
      `${pop === null ? '' : `，概率 ${pop}%`}，${c.wind_text}`;
    return `<button class="town${seat ? ' is-seat' : ''}" type="button" data-id="${esc(t.id)}"
        aria-label="${esc(label)}">
      <span class="r-name">${esc(t.name)}${seat ? '<i class="r-seat">县城</i>' : ''}</span>
      <span class="r-alt">${n1(t.elevation)}<i>m</i></span>
      <span class="r-wx">${glyph(c.weather, 24)}<em>${esc(c.weather)}</em></span>
      <span class="r-scale"><span class="r-track">
        <i class="r-span" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%;background:${
          tgrad(c.tmin, c.tmax, '90deg')}"></i>
        <b class="r-lo" style="left:${left.toFixed(1)}%">${n1(c.tmin)}</b>
        <b class="r-hi" style="left:${(left + width).toFixed(1)}%">${n1(c.tmax)}</b>
      </span></span>
      <span class="r-rain"><span class="r-bar"><i style="width:${rainW.toFixed(1)}%"></i></span>
        <em>${rain >= 0.05 ? n1(rain, 1) : '—'}</em></span>
      <span class="r-pop">${pop === null ? '—' : `${pop}%`}</span>
      <span class="r-wind">${esc(c.wind_text)}</span>
    </button>`;
  }).join('');

  $('towns').innerHTML = head + body;
  $('towns').querySelectorAll('button.town').forEach(b =>
    b.addEventListener('click', () => openSheet(b.dataset.id, b)));
}

/* ---------- township sheet ---------- */
function openSheet(id, from) {
  const t = S.doc.townships.find(x => x.id === id);
  if (!t) return;
  const dz = (t.elevation ?? 0) - (t.model_elevation ?? 0);
  const corr = -dz * 0.0065;

  const rows = S.doc.days.map((d, i) => {
    const c = d.cells.find(x => x.point === id);
    if (!c) return '';
    const pop = pct10(c.pop);
    return `<tr${i === S.day ? ' class="on"' : ''}>
      <th scope="row"><b>${i === 0 ? '今天' : esc(d.weekday)}</b><span>${md(d.date)}</span></th>
      <td class="s-wx">${glyph(c.weather, 24)}<span>${esc(c.weather)}</span></td>
      <td class="s-t"><b>${n1(c.tmax)}°</b><span>${n1(c.tmin)}°</span></td>
      <td class="s-num">${(c.precip ?? 0) >= 0.05 ? `${n1(c.precip, 1)} mm` : '—'}</td>
      <td class="s-num">${pop === null ? '—' : `${pop}%`}</td>
      <td class="s-wind">${esc(c.wind_text)}</td>
      <td class="s-win">${esc(c.windows_text || '—')}</td>
    </tr>`;
  }).join('');

  $('sheet-body').innerHTML = `
    <h3 id="sheet-title" class="s-title">${esc(t.name)}</h3>
    <p class="s-meta">海拔 <b>${n1(t.elevation)} m</b> · 模式地形 ${n1(t.model_elevation)} m
      · ${Number(t.lat).toFixed(3)}°N ${Number(t.lon).toFixed(3)}°E</p>
    <p class="s-note">比模式地形${dz >= 0 ? '高' : '低'} ${n1(Math.abs(dz))} m，按 −6.5 K/km 递减率，气温订正约 ${
      corr >= 0 ? '+' : '−'}${n1(Math.abs(corr), 1)} K。</p>
    <div class="s-wrap"><table class="s-table">
      <thead><tr><th scope="col">日期</th><th scope="col">天气</th><th scope="col">气温</th>
        <th scope="col">降水</th><th scope="col">概率</th><th scope="col">风</th><th scope="col">降水时段</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;

  S.back = from || document.activeElement;
  const sheet = $('sheet');
  sheet.hidden = false;
  document.body.classList.add('lock');
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

/* Keep Tab inside the dialog while it is open. */
function trapFocus(e) {
  if (e.key !== 'Tab' || $('sheet').hidden) return;
  const f = [...$('sheet').querySelectorAll('button, [href], [tabindex]:not([tabindex="-1"])')];
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ---------- bulletin text + API reference (both collapsed by default) ---------- */
function renderText() {
  $('doc-text').textContent = S.doc.text || '';
}

const ENDPOINTS = [
  ['/api/latest', '最新产品全量：每日每乡镇的天气、气温、降水、概率、风、降水时段'],
  ['/api/summary', '仅摘要：起报信息、结论、每日县级极值（体积小，适合轮询）'],
  ['/api/runs', '历史起报时次索引，新到旧'],
  ['/api/runs/{file}', '按索引里的 file 字段取某一次起报的完整产品'],
  ['/api/townships', '乡镇名录：id、名称、经纬度、海拔'],
  ['/api/health', '健康检查：已存时次数量与最新时次'],
];

function renderApi() {
  const first = S.runs[0] ? encodeURIComponent(S.runs[0].file) : null;
  $('api-list').innerHTML = ENDPOINTS.map(([path, desc]) => {
    const href = path.includes('{file}') ? (first ? path.replace('{file}', first) : null) : path;
    return `<div class="ep">
      <code>GET ${esc(path)}</code>
      <span>${esc(desc)}</span>
      ${href ? `<a href="${esc(href)}" target="_blank" rel="noopener">打开</a>` : '<span></span>'}
    </div>`;
  }).join('');
  $('api-curl').textContent = `curl -s ${location.origin}/api/summary | python3 -m json.tool`;
}

/* ---------- run picker ---------- */
function renderRuns() {
  const sel = $('runs');
  if (!S.runs.length) { sel.hidden = true; return; }
  sel.hidden = false;
  sel.innerHTML = S.runs.map(r =>
    `<option value="${esc(r.file)}"${r.run === S.doc.meta.run ? ' selected' : ''}>${esc(r.run)} UTC 起报</option>`).join('');
}

async function pickRun(file) {
  const sel = $('runs');
  sel.disabled = true;
  try {
    S.doc = await api(`/api/runs/${encodeURIComponent(file)}`);
    S.names = null;  // the roster belongs to the previous run
    S.day = 0;
    paint();
  } catch (err) {
    fail(err.message);
  }
  sel.disabled = false;
}

/* ---------- wiring ---------- */
function bjTime(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso || '—';
  return t.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

function paint() {
  renderToday();
  renderAlerts();
  renderDays();
  renderTowns();
  renderText();
  renderApi();
  renderRuns();
  const m = S.doc.meta;
  $('foot').innerHTML =
    `<span>${esc(m.county)} · ${esc(m.run)} UTC 起报 · 生成于 ${esc(bjTime(m.generated))} 北京时</span>` +
    `<span>数据：ECMWF 开放数据、NOAA GFS/GEFS · 自动生成，仅供参考</span>`;
}

function wire() {
  $('runs').addEventListener('change', e => pickRun(e.target.value));
  $('sheet-close').addEventListener('click', closeSheet);
  $('sheet').addEventListener('click', e => { if (e.target.id === 'sheet') closeSheet(); });
  $('copy-doc').addEventListener('click', copyDoc);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSheet();
    trapFocus(e);
  });
}

async function copyDoc() {
  const btn = $('copy-doc');
  const was = btn.textContent;
  try {
    await navigator.clipboard.writeText(S.doc.text || '');
    btn.textContent = '已复制';
  } catch {
    btn.textContent = '复制失败';
  }
  setTimeout(() => { btn.textContent = was; }, 1400);
}

function fail(msg) {
  const box = $('today');
  box.className = 'today';
  box.innerHTML = `<div class="empty">
    <h2>还没有预报产品</h2>
    <p>定时任务尚未产出第一份预报，或数据目录为空。手动生成一次：</p>
    <pre>python3 -m wxgrid publish --townships yanshan_townships.csv \\
  --county 铅山县 --seat 河口镇 --data-dir /var/lib/wxgrid</pre>
    <p class="dim">${esc(String(msg || ''))}</p></div>`;
}

async function boot() {
  initTheme();
  wire();
  try {
    S.doc = await api('/api/latest');
  } catch (err) {
    return fail(err.message);
  }
  try {
    S.runs = await api('/api/runs');
  } catch {
    S.runs = [];
  }
  paint();
}

boot();
