/* Room Planner — Floor Plan Measurement Form with a live overview sketch.
 * Clients who paid for architecture drawings enter room layouts (name, size, floor, position), attach photos, and submit the
 * package to the Tru Life back office (fixed server-side recipient). There is
 * intentionally no download/export/share control for the client.
 * Depends on globals from index.html: API, authFetch, currentUser, go,
 * IN_STORE_APP, showAuthGate.
 */
(function () {
  'use strict';

  const DIR = { N: 90, NE: 45, E: 0, SE: 315, S: 270, SW: 225, W: 180, NW: 135 };
  const DIR_NAMES = { N: 'North', NE: 'Northeast', E: 'East', SE: 'Southeast', S: 'South', SW: 'Southwest', W: 'West', NW: 'Northwest' };
  const POSITIONS = [
    ['front-left', 'Front left'], ['front-center', 'Front center'], ['front-right', 'Front right'],
    ['middle-left', 'Middle left'], ['center', 'Center'], ['middle-right', 'Middle right'],
    ['back-left', 'Back left'], ['back-center', 'Back center'], ['back-right', 'Back right'],
  ];
  const PROJECT_TYPES = ['Full interior renovation', 'Kitchen remodel', 'Bathroom remodel', 'Layout change / open concept', 'Addition', 'New build', 'Other'];
  const FLOOR_SCOPES = ['1st floor', '2nd floor', '3rd floor', 'Basement', 'Multiple floors', 'Whole house'];
  const ROOM_FLOORS = ['1st floor', '2nd floor', '3rd floor', 'Basement'];
  const DEFAULT_ROOMS = ['Living Room', 'Kitchen', 'Primary Bedroom', 'Bedroom 2', 'Bathroom'];
  const MAX_PHOTOS = 20;
  const ROOM_COLORS = ['#e8eefc', '#fdf3d7', '#e3f6ea', '#fbe6e6', '#efe7fb', '#e2f3f7', '#fff0e0', '#eef0f3'];

  let state = null;
  let photos = {};          // key -> { label, dataUrl }  (memory only, not saved in draft)
  let access = null;        // null = unknown, true/false after check
  let accessFor = null;     // user id the access check belongs to
  let canGrant = false;     // owner/staff: can give clients access
  let submitting = false;
  let lastDone = null;
  let sketchTimer = null, draftTimer = null;
  let uidSeq = 1;
  // currentUser / IN_STORE_APP are top-level `let`s in index.html: reachable
  // by name from this script, but not as window properties.
  const cu = () => (typeof currentUser !== 'undefined' ? currentUser : null);
  const inStore = () => (typeof IN_STORE_APP !== 'undefined' && !!IN_STORE_APP);

  const $ = (sel, root) => (root || document).querySelector(sel);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const uid = () => 'k' + Date.now().toString(36) + (uidSeq++).toString(36);
  const newRef = () => (crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : (Date.now().toString(16) + Math.random().toString(16).slice(2)));
  const num = (v, max) => { const n = parseFloat(v); return isFinite(n) && n > 0 ? Math.min(n, max) : 0; };
  const ftOf = (ft, inch) => num(ft, 1000) + Math.min(num(inch, 11.99), 11.99) / 12;
  function fmt(ft) { const t = Math.round(ft * 12); return Math.floor(t / 12) + "' " + (t % 12) + '"'; }
  function fmtShort(ft) { const t = Math.round(ft * 12); const i = t % 12; return Math.floor(t / 12) + "'" + (i ? ' ' + i + '"' : ''); }
  const wallLabel = (i) => (i < 26 ? String.fromCharCode(65 + i) : 'W' + (i + 1));

  function blankState(user) {
    return {
      client_ref: newRef(),
      client: { name: (user && user.name) || '', phone: (user && user.phone) || '', email: (user && user.email) || '', project_type: '', address: '', floors: '', year_built: '' },
      front_faces: 'S',
      walls: [],
      rooms: DEFAULT_ROOMS.map(n => ({ id: uid(), name: n, length_ft: '', length_in: '', width_ft: '', width_in: '', floor: '1st floor', position: '', note: '' })),
      extraPhotos: [],
      fixed_elements: '', ceiling_heights: '', comments: '',
      sketchFloor: '1st floor',
    };
  }
  const draftKey = () => 'trulife.roomplan.draft.' + (cu() ? cu().id : 'guest');
  function saveDraftSoon() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      try { localStorage.setItem(draftKey(), JSON.stringify(state)); } catch (_) {}
      const n = $('#rp-draft-note'); if (n) n.textContent = 'Draft saved on this device (photos are kept until you leave this page).';
    }, 400);
  }
  function loadDraft() {
    try {
      const raw = localStorage.getItem(draftKey());
      if (!raw) return null;
      const d = JSON.parse(raw);
      if (!d || !Array.isArray(d.walls) || !Array.isArray(d.rooms) || !d.client) return null;
      d.extraPhotos = Array.isArray(d.extraPhotos) ? d.extraPhotos : [];
      d.client_ref = d.client_ref || newRef();
      return d;
    } catch (_) { return null; }
  }
  function clearDraft() { try { localStorage.removeItem(draftKey()); } catch (_) {} }

  // ---------- Geometry ----------
  // Walk the exterior clockwise from the front-left corner. A wall facing D
  // is walked at (D - 90°): a north-facing wall runs west→east. The plan is
  // rotated so the front of the building points down on screen.
  function computeOutline() {
    const rot = (270 - (DIR[state.front_faces] != null ? DIR[state.front_faces] : 270)) * Math.PI / 180;
    let x = 0, y = 0;
    const pts = [[0, 0]], segs = [], missing = [];
    let perimeter = 0;
    state.walls.forEach((w, i) => {
      const len = ftOf(w.feet, w.inches);
      perimeter += len;
      if (len <= 0) return;
      if (DIR[w.facing] == null) { missing.push(wallLabel(i)); return; }
      const a = (DIR[w.facing] - 90) * Math.PI / 180 + rot;
      const nx = x + len * Math.cos(a), ny = y + len * Math.sin(a);
      segs.push({ label: wallLabel(i), len, x1: x, y1: -y, x2: nx, y2: -ny, ox: -Math.sin(a), oy: -Math.cos(a) });
      x = nx; y = ny; pts.push([x, -y]);   // store in screen coords (y down)
    });
    const gap = segs.length ? Math.hypot(x, y) : null;
    const closed = segs.length >= 3 && gap <= 0.5;
    let area = null;
    if (closed) {
      let s = 0;
      for (let i = 0; i < pts.length; i++) { const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length]; s += x1 * y2 - x2 * y1; }
      area = Math.abs(s) / 2;
    }
    let bounds = null;
    if (segs.length >= 2) {
      const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
      bounds = { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
      if (bounds.x1 - bounds.x0 < 1 || bounds.y1 - bounds.y0 < 1) bounds = null;
    }
    return { pts, segs, gap, closed, area, bounds, missing, perimeter, rot };
  }

  function floorsInUse() {
    const set = new Set(['1st floor']);
    state.rooms.forEach(r => { if (r.floor) set.add(r.floor); });
    return ROOM_FLOORS.filter(f => set.has(f));
  }
  function roomsOnFloor(floor) {
    return state.rooms.map((r, i) => ({ r, i })).filter(({ r }) => (r.floor || '1st floor') === floor);
  }

  function placeRooms(floor, ol) {
    const list = roomsOnFloor(floor).map(({ r, i }) => ({
      r, i, w: ftOf(r.length_ft, r.length_in), h: ftOf(r.width_ft, r.width_in),
    })).filter(o => o.w > 0 && o.h > 0);
    let b = ol.bounds, virtual = false;
    if (!b) {
      const total = list.reduce((s, o) => s + o.w * o.h, 0);
      const maxW = list.reduce((m, o) => Math.max(m, o.w), 0), maxH = list.reduce((m, o) => Math.max(m, o.h), 0);
      const side = Math.max(Math.sqrt(total * 2.4), maxW * 1.1, maxH * 1.1, 10);
      b = { x0: 0, y0: 0, x1: side, y1: side }; virtual = true;
    }
    const used = {};
    const placed = [], unplaced = [];
    list.forEach(o => {
      const pos = o.r.position;
      if (!pos) { unplaced.push(o); return; }
      const [row, col] = pos === 'center' ? ['middle', 'center'] : pos.split('-');
      const off = used[pos] || 0;
      let x, y;
      if (col === 'left') x = b.x0 + off;
      else if (col === 'right') x = b.x1 - o.w - off;
      else x = (b.x0 + b.x1) / 2 - o.w / 2;
      if (row === 'back') y = b.y0 + (col === 'center' ? off : 0);
      else if (row === 'front') y = b.y1 - o.h - (col === 'center' ? off : 0);
      else y = (b.y0 + b.y1) / 2 - o.h / 2 + (col === 'center' ? off : 0);
      used[pos] = off + (col === 'center' ? o.h : o.w);
      placed.push({ ...o, x, y });
    });
    // Rooms without a position line up below the outline.
    let cx = b.x0;
    const rowY = b.y1 + Math.max((b.y1 - b.y0) * 0.12, 3);
    unplaced.forEach(o => { placed.push({ ...o, x: cx, y: rowY, loose: true }); cx += o.w + 2; });
    const oversize = list.filter(o => !virtual && (o.w > b.x1 - b.x0 + 0.5 || o.h > b.y1 - b.y0 + 0.5)).map(o => o.r.name || ('Room ' + (o.i + 1)));
    return { placed, b, virtual, unplacedCount: unplaced.length, oversize };
  }

  // ---------- SVG sketch ----------
  function buildSVG(floor, opts) {
    opts = opts || {};
    const ol = computeOutline();
    const pr = placeRooms(floor, ol);
    const items = [];
    let X0 = pr.b.x0, Y0 = pr.b.y0, X1 = pr.b.x1, Y1 = pr.b.y1;
    pr.placed.forEach(p => { X0 = Math.min(X0, p.x); Y0 = Math.min(Y0, p.y); X1 = Math.max(X1, p.x + p.w); Y1 = Math.max(Y1, p.y + p.h); });
    ol.pts.forEach(([x, y]) => { X0 = Math.min(X0, x); Y0 = Math.min(Y0, y); X1 = Math.max(X1, x); Y1 = Math.max(Y1, y); });
    const ext = Math.max(X1 - X0, Y1 - Y0, 12);
    const pad = ext * 0.14;
    const fs = ext * 0.034, sw = ext * 0.006;
    const vx = X0 - pad, vy = Y0 - pad - (opts.title ? fs * 3 : 0), vw = (X1 - X0) + pad * 2, vh = (Y1 - Y0) + pad * 2 + (opts.title ? fs * 3 : 0);
    const font = 'font-family="Helvetica, Arial, sans-serif"';

    // sketch-pad grid every 5 ft (or 10 ft for big plans)
    const step = ext > 120 ? 10 : 5;
    let grid = '';
    for (let gx = Math.ceil(vx / step) * step; gx <= vx + vw; gx += step) grid += `<line x1="${gx}" y1="${vy}" x2="${gx}" y2="${vy + vh}"/>`;
    for (let gy = Math.ceil(vy / step) * step; gy <= vy + vh; gy += step) grid += `<line x1="${vx}" y1="${gy}" x2="${vx + vw}" y2="${gy}"/>`;
    items.push(`<rect x="${vx}" y="${vy}" width="${vw}" height="${vh}" fill="#fbfcff"/>`);
    items.push(`<g stroke="#e3e8f5" stroke-width="${sw * 0.6}">${grid}</g>`);

    if (opts.title) {
      items.push(`<text x="${vx + pad * 0.5}" y="${vy + fs * 1.6}" ${font} font-size="${fs * 1.15}" font-weight="700" fill="#031158">${esc(opts.title)}</text>`);
    }

    // virtual box if no exterior entered
    if (pr.virtual && pr.placed.length) {
      items.push(`<rect x="${pr.b.x0}" y="${pr.b.y0}" width="${pr.b.x1 - pr.b.x0}" height="${pr.b.y1 - pr.b.y0}" fill="none" stroke="#b1b5be" stroke-width="${sw}" stroke-dasharray="${sw * 4} ${sw * 3}"/>`);
      items.push(`<text x="${pr.b.x0 + fs * 0.4}" y="${pr.b.y0 - fs * 0.5}" ${font} font-size="${fs * 0.8}" fill="#8a8f99">Approximate layout — rooms arranged by position</text>`);
    }

    // rooms
    pr.placed.forEach((p, k) => {
      const color = ROOM_COLORS[p.i % ROOM_COLORS.length];
      const name = p.r.name || ('Room ' + (p.i + 1));
      const lf = Math.min(fs, p.w / Math.max(name.length, 4) * 1.7, p.h / 3);
      items.push(`<rect x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" fill="${color}" fill-opacity="0.92" stroke="#5a6aa8" stroke-width="${sw * 0.8}" ${p.loose ? `stroke-dasharray="${sw * 3} ${sw * 2}"` : ''}/>`);
      items.push(`<text x="${p.x + p.w / 2}" y="${p.y + p.h / 2 - lf * 0.15}" ${font} font-size="${lf}" font-weight="700" fill="#031158" text-anchor="middle">${esc(name)}</text>`);
      items.push(`<text x="${p.x + p.w / 2}" y="${p.y + p.h / 2 + lf * 1.05}" ${font} font-size="${lf * 0.82}" fill="#434549" text-anchor="middle">${esc(fmtShort(p.w))} × ${esc(fmtShort(p.h))}</text>`);
    });
    if (pr.unplacedCount) {
      const ly = pr.placed.filter(p => p.loose)[0].y;
      items.push(`<text x="${pr.b.x0}" y="${ly - fs * 0.45}" ${font} font-size="${fs * 0.78}" fill="#d97706">Position not set — choose where these rooms sit</text>`);
    }

    // exterior outline
    if (ol.segs.length) {
      const d = 'M ' + ol.pts.map(p => p[0].toFixed(3) + ' ' + p[1].toFixed(3)).join(' L ') + (ol.closed ? ' Z' : '');
      items.push(`<path d="${d}" fill="${ol.closed ? 'rgba(3,17,88,0.04)' : 'none'}" stroke="#031158" stroke-width="${sw * 2.4}" stroke-linejoin="round" stroke-linecap="round"/>`);
      if (!ol.closed && ol.segs.length >= 2) {
        const [ex, ey] = ol.pts[ol.pts.length - 1];
        items.push(`<line x1="${ex}" y1="${ey}" x2="0" y2="0" stroke="#dc2626" stroke-width="${sw * 1.4}" stroke-dasharray="${sw * 4} ${sw * 3}"/>`);
        items.push(`<circle cx="${ex}" cy="${ey}" r="${sw * 3}" fill="#dc2626"/>`);
      }
      items.push(`<circle cx="0" cy="0" r="${sw * 3.2}" fill="#daa61f" stroke="#fff" stroke-width="${sw}"/>`);
      ol.segs.forEach(s => {
        const mx = (s.x1 + s.x2) / 2, my = (s.y1 + s.y2) / 2;
        const off = fs * 1.5;
        const tx = mx + s.ox * off, ty = my + s.oy * off;
        const r = fs * 0.72;
        items.push(`<circle cx="${tx}" cy="${ty - fs * 0.35}" r="${r}" fill="#daa61f"/>`);
        items.push(`<text x="${tx}" y="${ty - fs * 0.35 + fs * 0.33}" ${font} font-size="${fs * 0.9}" font-weight="700" fill="#fff" text-anchor="middle">${esc(s.label)}</text>`);
        items.push(`<text x="${tx}" y="${ty + fs * 1.15}" ${font} font-size="${fs * 0.82}" font-weight="700" fill="#031158" text-anchor="middle">${esc(fmtShort(s.len))}</text>`);
      });
    }

    // north arrow + front marker
    const nAng = (90 * Math.PI / 180) + ol.rot;
    const ax = vx + vw - pad * 0.55, ay = vy + pad * 0.6 + (opts.title ? fs * 3 : 0), al = fs * 1.4;
    const dx = Math.cos(nAng), dy = -Math.sin(nAng);
    items.push(`<g stroke="#031158" stroke-width="${sw * 1.4}" fill="#031158"><line x1="${ax - dx * al}" y1="${ay - dy * al}" x2="${ax + dx * al}" y2="${ay + dy * al}"/>` +
      `<polygon points="${ax + dx * al * 1.25},${ay + dy * al * 1.25} ${ax + dx * al * 0.7 - dy * al * 0.35},${ay + dy * al * 0.7 + dx * al * 0.35} ${ax + dx * al * 0.7 + dy * al * 0.35},${ay + dy * al * 0.7 - dx * al * 0.35}" stroke="none"/></g>`);
    items.push(`<text x="${ax + dx * al * 2}" y="${ay + dy * al * 2 + fs * 0.35}" ${font} font-size="${fs * 0.9}" font-weight="700" fill="#031158" text-anchor="middle">N</text>`);
    if (ol.segs.length || pr.placed.length) {
      items.push(`<text x="${(pr.b.x0 + pr.b.x1) / 2}" y="${vy + vh - fs * 0.6}" ${font} font-size="${fs * 0.78}" fill="#8a8f99" text-anchor="middle" letter-spacing="${fs * 0.1}">FRONT</text>`);
    } else {
      items.push(`<text x="${vx + vw / 2}" y="${vy + vh / 2}" ${font} font-size="${fs * 0.95}" fill="#8a8f99" text-anchor="middle">Enter wall lengths and directions</text>`);
      items.push(`<text x="${vx + vw / 2}" y="${vy + vh / 2 + fs * 1.4}" ${font} font-size="${fs * 0.95}" fill="#8a8f99" text-anchor="middle">to watch your plan come together</text>`);
    }

    const size = opts.width ? `width="${opts.width}" height="${Math.round(opts.width * vh / vw)}"` : '';
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vx} ${vy} ${vw} ${vh}" ${size} role="img" aria-label="Floor plan overview sketch">${items.join('')}</svg>`;
    return { svg, ol, pr };
  }

  function renderSketch() {
    const host = $('#rp-canvas');
    if (!host || !state) return;
    const floors = floorsInUse();
    if (!floors.includes(state.sketchFloor)) state.sketchFloor = floors[0];
    const tabs = $('#rp-floor-tabs');
    tabs.innerHTML = floors.length > 1 ? floors.map(f => `<button type="button" data-floor="${esc(f)}" class="${f === state.sketchFloor ? 'active' : ''}">${esc(f)}</button>`).join('') : '';
    const { svg, ol, pr } = buildSVG(state.sketchFloor);
    host.innerHTML = svg;
    const floorArea = roomsOnFloor(state.sketchFloor).reduce((s, { r }) => s + ftOf(r.length_ft, r.length_in) * ftOf(r.width_ft, r.width_in), 0);
    $('#rp-stat-perim').textContent = roomsOnFloor(state.sketchFloor).length ? String(roomsOnFloor(state.sketchFloor).length) : '—';
    const totalArea = state.rooms.reduce((t, r) => t + ftOf(r.length_ft, r.length_in) * ftOf(r.width_ft, r.width_in), 0);
    $('#rp-stat-area').textContent = totalArea > 0 ? Math.round(totalArea).toLocaleString() + ' sf' : '—';
    $('#rp-stat-rooms').textContent = floorArea > 0 ? Math.round(floorArea).toLocaleString() + ' sf' : '—';
    const checks = [];
    const measuredHere = roomsOnFloor(state.sketchFloor).filter(({ r }) => ftOf(r.length_ft, r.length_in) > 0 && ftOf(r.width_ft, r.width_in) > 0).length;
    if (!measuredHere) checks.push(['info', 'Add a length and width to each room to build the sketch.']);
    else checks.push(['ok', `${measuredHere} room${measuredHere > 1 ? 's' : ''} drawn to scale on this floor.`]);
    if (pr.unplacedCount) checks.push(['info', `${pr.unplacedCount} room${pr.unplacedCount > 1 ? 's' : ''} on this floor need${pr.unplacedCount > 1 ? '' : 's'} a position.`]);
    if (pr.oversize.length) checks.push(['warn', `${pr.oversize.join(', ')} ${pr.oversize.length > 1 ? 'are' : 'is'} larger than the exterior outline.`]);
    $('#rp-checks').innerHTML = checks.map(([c, t]) => `<li class="${c}">${esc(t)}</li>`).join('');
    updateSteps(ol);
  }
  function sketchSoon() { clearTimeout(sketchTimer); sketchTimer = setTimeout(renderSketch, 60); }

  function updateSteps(ol) {
    const c = state.client;
    const done = [
      !!(c.name && c.phone && c.project_type && c.address),
      state.rooms.some(r => ftOf(r.length_ft, r.length_in) > 0 && ftOf(r.width_ft, r.width_in) > 0),
      Object.keys(photos).length > 0 || !!(state.fixed_elements || state.ceiling_heights || state.comments),
    ];
    document.querySelectorAll('#rp-steps .rp-step').forEach((el, i) => el.classList.toggle('done', done[i]));
  }

  async function sketchPNG(floor) {
    const title = (state.client.address || 'Floor plan overview') + ' — ' + floor;
    const { svg } = buildSVG(floor, { width: 1600, title });
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        try {
          const cv = document.createElement('canvas');
          cv.width = img.naturalWidth || 1600; cv.height = img.naturalHeight || 1200;
          const ctx = cv.getContext('2d');
          ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, cv.width, cv.height);
          ctx.drawImage(img, 0, 0, cv.width, cv.height);
          resolve(cv.toDataURL('image/png'));
        } catch (_) { resolve(null); }
      };
      img.onerror = () => resolve(null);
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    });
  }

  // ---------- Photos ----------
  async function compressImage(file) {
    const MAX = 1600;
    let source, w, h;
    try {
      source = await createImageBitmap(file, { imageOrientation: 'from-image' });
      w = source.width; h = source.height;
    } catch (_) {
      source = await new Promise((res, rej) => {
        const url = URL.createObjectURL(file);
        const im = new Image();
        im.onload = () => { URL.revokeObjectURL(url); res(im); };
        im.onerror = () => { URL.revokeObjectURL(url); rej(new Error('unreadable')); };
        im.src = url;
      });
      w = source.naturalWidth; h = source.naturalHeight;
    }
    const scale = Math.min(1, MAX / Math.max(w, h));
    const cv = document.createElement('canvas');
    cv.width = Math.round(w * scale); cv.height = Math.round(h * scale);
    cv.getContext('2d').drawImage(source, 0, 0, cv.width, cv.height);
    let q = 0.82, out = cv.toDataURL('image/jpeg', q);
    while (out.length > 2.8e6 && q > 0.45) { q -= 0.12; out = cv.toDataURL('image/jpeg', q); }
    return out;
  }
  function photoSlots() {
    const slots = state.rooms.map((r, i) => ({ key: 'room:' + r.id, label: r.name || ('Room ' + (i + 1)), removable: false }));
    state.extraPhotos.forEach((p) => slots.push({ key: 'extra:' + p.id, label: p.label || 'Extra photo', extraId: p.id, removable: true }));
    return slots;
  }
  function renderPhotos() {
    const grid = $('#rp-photos');
    if (!grid) return;
    const cam = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>';
    grid.innerHTML = photoSlots().map(s => {
      const p = photos[s.key];
      return `<label class="rp-photo ${p ? 'has-img' : ''}" data-key="${esc(s.key)}">` +
        (p ? `<img src="${p.dataUrl}" alt="${esc(s.label)} photo"><span class="rp-photo-cap">${esc(s.label)}</span><button type="button" class="rp-photo-del" data-del-photo="${esc(s.key)}" aria-label="Remove photo">×</button>`
          : `${cam}<span>${esc(s.label)}</span><span style="font-weight:500;font-size:0.72rem;color:var(--gray-2)">Tap to add photo</span>`) +
        (s.removable && !p ? `<button type="button" class="rp-photo-del" data-del-extra="${esc(s.extraId)}" aria-label="Remove slot">×</button>` : '') +
        `<input type="file" accept="image/*" data-photo-input="${esc(s.key)}"></label>`;
    }).join('');
    const count = Object.keys(photos).length;
    $('#rp-photo-count').textContent = count ? `${count} of ${MAX_PHOTOS} photos attached` : '';
  }

  // ---------- Form rendering ----------
  const opt = (list, val, ph) => (ph != null ? `<option value="">${esc(ph)}</option>` : '') + list.map(o => {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(v)}" ${v === val ? 'selected' : ''}>${esc(l)}</option>`;
  }).join('');
  const dirOpts = (val) => opt(Object.keys(DIR).map(k => [k, k + ' · ' + DIR_NAMES[k]]), val, '—');

  function renderWalls() {
    if (!$('#rp-walls-body')) return;
    $('#rp-walls-body').innerHTML = state.walls.map((w, i) => `
      <tr data-wall="${w.id}">
        <td><div class="rp-wall-label">${esc(wallLabel(i))}</div></td>
        <td><div class="rp-unit"><input type="number" inputmode="numeric" min="0" max="1000" step="1" data-wf="feet" value="${esc(w.feet)}" placeholder="0" aria-label="Wall ${esc(wallLabel(i))} feet"><i>ft</i></div></td>
        <td><div class="rp-unit"><input type="number" inputmode="decimal" min="0" max="11.99" step="0.25" data-wf="inches" value="${esc(w.inches)}" placeholder="0" aria-label="Wall ${esc(wallLabel(i))} inches"><i>in</i></div></td>
        <td><select data-wf="facing" aria-label="Wall ${esc(wallLabel(i))} direction facing">${dirOpts(w.facing)}</select></td>
        <td>${state.walls.length > 1 ? `<button type="button" class="rp-x" data-del-wall="${w.id}" aria-label="Remove wall ${esc(wallLabel(i))}">×</button>` : ''}</td>
      </tr>`).join('');
  }

  function roomArea(r) { const a = ftOf(r.length_ft, r.length_in) * ftOf(r.width_ft, r.width_in); return a > 0 ? Math.round(a).toLocaleString() + ' sq ft' : ''; }
  function renderRooms() {
    $('#rp-rooms').innerHTML = state.rooms.map((r, i) => `
      <div class="rp-room" data-room="${r.id}">
        <div class="rp-room-head">
          <span class="rp-room-tag">Room ${i + 1}</span>
          <input type="text" data-rf="name" value="${esc(r.name)}" maxlength="60" placeholder="Room name" aria-label="Room ${i + 1} name">
          <button type="button" class="rp-x" data-del-room="${r.id}" aria-label="Remove room ${i + 1}">×</button>
        </div>
        <div class="rp-room-body">
          <div class="rp-dims">
            <div><span class="rp-lbl">Length (left–right)</span><div class="rp-ftin">
              <div class="rp-unit"><input type="number" inputmode="numeric" min="0" max="500" data-rf="length_ft" value="${esc(r.length_ft)}" placeholder="0" aria-label="Length feet"><i>ft</i></div>
              <div class="rp-unit"><input type="number" inputmode="decimal" min="0" max="11.99" step="0.25" data-rf="length_in" value="${esc(r.length_in)}" placeholder="0" aria-label="Length inches"><i>in</i></div></div></div>
            <div class="rp-times">×</div>
            <div><span class="rp-lbl">Width (front–back)</span><div class="rp-ftin">
              <div class="rp-unit"><input type="number" inputmode="numeric" min="0" max="500" data-rf="width_ft" value="${esc(r.width_ft)}" placeholder="0" aria-label="Width feet"><i>ft</i></div>
              <div class="rp-unit"><input type="number" inputmode="decimal" min="0" max="11.99" step="0.25" data-rf="width_in" value="${esc(r.width_in)}" placeholder="0" aria-label="Width inches"><i>in</i></div></div></div>
          </div>
          <div class="rp-room-area" data-room-area>${esc(roomArea(r))}</div>
          <div class="rp-room-grid">
            <div><span class="rp-lbl">Floor</span><select data-rf="floor">${opt(ROOM_FLOORS, r.floor)}</select></div>
            <div><span class="rp-lbl">Position in the house</span><select data-rf="position">${opt(POSITIONS, r.position, 'Select position…')}</select></div>
          </div>
          <div style="margin-bottom:12px"><span class="rp-lbl">Quick note (closet, en-suite, open to…)</span><input type="text" data-rf="note" value="${esc(r.note)}" maxlength="200" placeholder="Optional"></div>
        </div>
      </div>`).join('');
  }

  function renderTool() {
    const c = state.client;
    $('#rp-root').innerHTML = grantPanelHTML() + `
      <div class="rp-how">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M3 21V3l18 18"/><path d="M7 21v-4"/><path d="M11 21v-2"/><path d="M15 21v-4"/></svg>
        <div><b>How this works</b><p>Fill out each section at the property. The sketch updates as you type so you can check the shapes look right. When you submit, everything goes straight to the Tru Life back office, where our team reviews the details and prepares the drawings with our architect.</p></div>
      </div>
      <div class="rp-steps" id="rp-steps">
        <button type="button" class="rp-step" data-jump="rp-sec-1"><span>1</span>Client info</button>
        <button type="button" class="rp-step" data-jump="rp-sec-3"><span>2</span>Rooms</button>
        <button type="button" class="rp-step" data-jump="rp-sec-4"><span>3</span>Photos & notes</button>
      </div>
      <div class="rp-layout">
        <div class="rp-form-col">
          <div class="card rp-section" id="rp-sec-1">
            <div class="rp-sec-head"><div class="rp-sec-num">1</div><div><h3>Client &amp; Property Info</h3><p>Who you are and where the property is.</p></div></div>
            <div class="rp-sec-body">
              <div class="field-row">
                <div class="field"><label for="rp-name">Full name <span class="rp-req">*</span></label><input id="rp-name" data-cf="name" value="${esc(c.name)}" maxlength="120" placeholder="e.g. Maria Johnson" autocomplete="name"></div>
                <div class="field"><label for="rp-phone">Phone number <span class="rp-req">*</span></label><input id="rp-phone" data-cf="phone" type="tel" value="${esc(c.phone)}" maxlength="40" placeholder="(555) 000-0000" autocomplete="tel"></div>
              </div>
              <div class="field-row">
                <div class="field"><label for="rp-email">Email address</label><input id="rp-email" data-cf="email" type="email" value="${esc(c.email)}" maxlength="200" placeholder="you@email.com" autocomplete="email"></div>
                <div class="field"><label for="rp-ptype">Project type <span class="rp-req">*</span></label><select id="rp-ptype" data-cf="project_type">${opt(PROJECT_TYPES, c.project_type, 'Select one…')}</select></div>
              </div>
              <div class="field"><label for="rp-addr">Property address <span class="rp-req">*</span></label><input id="rp-addr" data-cf="address" value="${esc(c.address)}" maxlength="240" placeholder="123 Main Street, City, State ZIP" autocomplete="street-address"></div>
              <div class="field-row">
                <div class="field"><label for="rp-floors">Floor(s) being renovated</label><select id="rp-floors" data-cf="floors">${opt(FLOOR_SCOPES, c.floors, 'Select…')}</select></div>
                <div class="field"><label for="rp-year">Approx. year built</label><input id="rp-year" data-cf="year_built" inputmode="numeric" value="${esc(c.year_built)}" maxlength="12" placeholder="e.g. 1985"></div>
              </div>
            </div>
          </div>

          <div class="card rp-section" id="rp-sec-3">
            <div class="rp-sec-head"><div class="rp-sec-num">2</div><div><h3>Room Layouts</h3><p>Measure each room wall to wall and choose roughly where it sits in the house.</p></div></div>
            <div class="rp-sec-body">
              <div class="rp-tip"><span>📐</span><span>Length runs left to right as you face the front of the house; width runs front to back. Position is approximate — it only arranges the overview sketch.</span></div>
              <div id="rp-rooms"></div>
              <button type="button" class="rp-add" data-add-room>+ Add room</button>
            </div>
          </div>

          <div class="card rp-section" id="rp-sec-4">
            <div class="rp-sec-head"><div class="rp-sec-num">3</div><div><h3>Photos &amp; Fixed Elements</h3><p>One photo per room, plus any fixed features the architect must know.</p></div></div>
            <div class="rp-sec-body">
              <div class="rp-tip"><span>📸</span><span>Stand in the doorway of each room and take a wide-angle photo facing in. Photos help us verify openings and spot features measurements might miss.</span></div>
              <div class="rp-photos" id="rp-photos"></div>
              <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap"><button type="button" class="rp-add" data-add-photo>+ Add room photo</button><span id="rp-photo-count" class="rp-draft-note"></span></div>
              <div class="field"><label for="rp-fixed">Fixed elements &amp; special notes</label><textarea id="rp-fixed" data-tf="fixed_elements" rows="4" maxlength="3000" placeholder="Load-bearing walls, staircase location and direction, fireplace/chimney, utility closets, HVAC units, structural columns, low ceilings, etc.">${esc(state.fixed_elements)}</textarea></div>
              <div class="field"><label for="rp-ceil">Ceiling heights (if they vary)</label><input id="rp-ceil" data-tf="ceiling_heights" maxlength="500" value="${esc(state.ceiling_heights)}" placeholder="e.g. Main rooms: 9 ft, Kitchen: 8 ft, Basement: 7.5 ft"></div>
              <div class="field"><label for="rp-comments">Additional comments</label><textarea id="rp-comments" data-tf="comments" rows="3" maxlength="3000" placeholder="Anything else the architect or back office should know…">${esc(state.comments)}</textarea></div>
            </div>
          </div>

          <div id="rp-error" class="rp-error" style="display:none" role="alert"></div>
          <div class="rp-submit-bar">
            <button type="button" class="btn btn-gold" data-submit>Submit to Tru Life</button>
            <div><p>Your submission goes directly to the Tru Life Properties back office.</p><div class="rp-draft-note" id="rp-draft-note"></div></div>
          </div>
        </div>

        <aside class="rp-sketch-col">
          <div class="card rp-sketch" id="rp-sketch">
            <div class="rp-sketch-head">
              <div><h3>Plan overview</h3><span class="rp-live">Live sketch</span></div>
              <button type="button" class="rp-sketch-toggle" data-toggle-sketch>Hide</button>
            </div>
            <div class="rp-sketch-body">
              <div class="rp-floor-tabs" id="rp-floor-tabs"></div>
              <div class="rp-canvas" id="rp-canvas"></div>
              <div class="rp-stats">
                <div class="rp-stat"><small>Rooms</small><b id="rp-stat-perim">—</b></div>
                <div class="rp-stat"><small>All floors</small><b id="rp-stat-area">—</b></div>
                <div class="rp-stat"><small>This floor</small><b id="rp-stat-rooms">—</b></div>
              </div>
              <ul class="rp-checks" id="rp-checks"></ul>
              <p class="rp-sketch-note">Simple overview to check your shapes — not to scale for construction. The detailed drawings are prepared by our team after review.</p>
            </div>
          </div>
        </aside>
      </div>
      <div class="rp-modal" id="rp-modal" aria-hidden="true"><div class="rp-modal-card" role="dialog" aria-modal="true" aria-labelledby="rp-modal-title"></div></div>`;
    renderWalls(); renderRooms(); renderPhotos(); renderSketch();
  }

  // Access rule: only clients who paid for architecture drawings (plus
  // owner/staff). Other purchases do not unlock it.
  function renderLocked() {
    $('#rp-root').innerHTML = `
      <div class="card rp-locked">
        <div class="rp-lock-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
        <h3>Room Planner opens with architecture drawings</h3>
        <p>The Floor Plan Measurement Form is for clients who have paid for Tru Life architecture drawings. Once your drawings payment is confirmed, it unlocks on this account automatically.</p>
        <p class="rp-lock-sub">Paid for drawings directly with our team? We'll turn it on for the email on this account${cu() && cu().email ? ` (<b>${esc(cu().email)}</b>)` : ''}.</p>
        <div class="rp-lock-actions">
          <button type="button" class="btn btn-gold" data-rp-go="architecture">See Architecture</button>
          <button type="button" class="btn btn-ghost" data-rp-recheck>I already paid — check again</button>
        </div>
      </div>`;
  }

  // ---------- Back office: grant access (owner/staff only) ----------
  function grantPanelHTML() {
    if (!canGrant) return '';
    return `
      <details class="card rp-grant" id="rp-grant">
        <summary><b>Back office</b> · Give a client Room Planner access</summary>
        <div class="rp-grant-body">
          <p>Use this when a client paid for architecture drawings outside the app (invoice, in person). In-app architecture purchases unlock automatically. The client must already have an app account.</p>
          <div class="rp-grant-form">
            <div class="field"><input type="email" id="rp-grant-email" placeholder="Client's app account email" maxlength="254" autocomplete="off" aria-label="Client account email"></div>
            <div class="field"><input type="text" id="rp-grant-note" placeholder="Note (e.g. Invoice #1042)" maxlength="300" aria-label="Note"></div>
            <button type="button" class="btn btn-gold" data-rp-grant>Grant access</button>
          </div>
          <div id="rp-grant-msg" class="rp-grant-msg" role="status"></div>
          <div id="rp-grant-list" class="rp-grant-list"><span class="rp-draft-note">Loading…</span></div>
        </div>
      </details>`;
  }
  async function loadGrants() {
    const box = $('#rp-grant-list'); if (!box) return;
    try {
      const res = await authFetch('/api/room-planner/grants');
      const list = res.ok ? await res.json() : [];
      box.innerHTML = list.length
        ? `<div class="rp-lbl">Clients with access (${list.length})</div>` + list.map(g => `
            <div class="rp-grant-row"><div><b>${esc(g.name || g.email)}</b><small>${esc(g.email)}${g.note ? ' · ' + esc(g.note) : ''}</small></div>
            <button type="button" class="btn btn-ghost" data-rp-revoke="${esc(g.user_id)}" data-email="${esc(g.email)}">Remove</button></div>`).join('')
        : '<span class="rp-draft-note">No manual grants yet.</span>';
    } catch (_) { box.innerHTML = '<span class="rp-draft-note">Could not load the list.</span>'; }
  }
  async function grantAccess(btn) {
    const email = ($('#rp-grant-email').value || '').trim();
    const note = ($('#rp-grant-note').value || '').trim();
    const msg = $('#rp-grant-msg');
    if (!email) { msg.textContent = 'Enter the client\'s account email.'; msg.className = 'rp-grant-msg err'; return; }
    btn.disabled = true;
    try {
      const res = await authFetch('/api/room-planner/grants', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, note }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not grant access.');
      msg.textContent = `Access granted to ${data.email}.`; msg.className = 'rp-grant-msg ok';
      $('#rp-grant-email').value = ''; $('#rp-grant-note').value = '';
      loadGrants();
    } catch (err) { msg.textContent = err.message; msg.className = 'rp-grant-msg err'; }
    btn.disabled = false;
  }
  async function revokeAccess(btn) {
    if (!confirm('Remove Room Planner access for ' + btn.dataset.email + '?')) return;
    btn.disabled = true;
    try {
      const res = await authFetch('/api/room-planner/grants/' + encodeURIComponent(btn.dataset.rpRevoke), { method: 'DELETE' });
      if (!res.ok) throw new Error();
    } catch (_) { alert('Could not remove access. Try again.'); }
    loadGrants();
  }
  function renderLoading(msg) {
    $('#rp-root').innerHTML = `<div class="card rp-locked"><p>${esc(msg || 'Checking your access…')}</p></div>`;
  }
  function renderDone(res) {
    $('#rp-root').innerHTML = `
      <div class="card rp-done">
        <div class="rp-lock-ico" style="background:var(--green-soft);color:var(--green)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><polyline points="20 6 9 17 4 12"/></svg></div>
        <h3>Measurements submitted</h3>
        <div class="rp-ref">${esc(res.reference)}</div>
        <p>Your measurements, photos, and sketch were sent to the Tru Life Properties back office. Our team will review the details and reach out if anything needs a second look. A confirmation was sent to your account email.</p>
        ${res.email_sent === false ? '<p class="rp-draft-note">Your submission is saved with us; our team will be notified shortly.</p>' : ''}
        <div class="rp-lock-actions"><button type="button" class="btn btn-outline" data-rp-new>Start another property</button></div>
      </div>`;
  }

  // ---------- Submit ----------
  function showError(msg) { const e = $('#rp-error'); if (!e) return; e.textContent = msg || ''; e.style.display = msg ? '' : 'none'; if (msg) e.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  function validate() {
    const c = state.client;
    const missing = [];
    if (!c.name.trim()) missing.push('full name');
    if (!c.phone.trim()) missing.push('phone number');
    if (!c.project_type) missing.push('project type');
    if (!c.address.trim()) missing.push('property address');
    if (missing.length) return 'Please add your ' + missing.join(', ') + ' in section 1.';
    const rooms = state.rooms.filter(r => ftOf(r.length_ft, r.length_in) > 0 && ftOf(r.width_ft, r.width_in) > 0).length;
    if (!rooms) return 'Add at least one room with a length and width in section 2.';
    return '';
  }
  function openConfirm() {
    const err = validate();
    if (err) { showError(err); return; }
    showError('');
    const ol = computeOutline();
    const rooms = state.rooms.filter(r => ftOf(r.length_ft, r.length_in) > 0 && ftOf(r.width_ft, r.width_in) > 0).length;
    const warns = [];
    const emptyRooms = state.rooms.length - rooms;
    if (emptyRooms > 0) warns.push(`${emptyRooms} room${emptyRooms > 1 ? 's have' : ' has'} no dimensions and will be sent as names only.`);
    const card = $('#rp-modal .rp-modal-card');
    card.innerHTML = `
      <h3 id="rp-modal-title">Submit to Tru Life?</h3>
      <div class="rp-dest">Sending to: <b>Tru Life Properties back office</b></div>
      <ul>
        <li>${esc(state.client.address)}</li>
        <li>${rooms} measured room${rooms === 1 ? '' : 's'} · ${state.rooms.length} room${state.rooms.length === 1 ? '' : 's'} listed</li>
        <li>${Object.keys(photos).length} photo${Object.keys(photos).length === 1 ? '' : 's'} · overview sketch</li>
      </ul>
      ${warns.length ? `<div class="rp-tip" style="background:var(--amber-soft)"><span>⚠️</span><span>${warns.map(esc).join('<br>')}<br>You can still submit — our team will follow up — or go back and fix it.</span></div>` : ''}
      <div id="rp-modal-err" class="rp-error" style="display:none"></div>
      <div class="rp-modal-actions">
        <button type="button" class="btn btn-ghost" data-rp-cancel>Go back</button>
        <button type="button" class="btn btn-gold" data-rp-confirm>Submit</button>
      </div>`;
    $('#rp-modal').classList.add('open');
    $('#rp-modal').setAttribute('aria-hidden', 'false');
  }
  function closeModal() { const m = $('#rp-modal'); if (m) { m.classList.remove('open'); m.setAttribute('aria-hidden', 'true'); } }

  async function doSubmit(btn) {
    if (submitting) return;
    submitting = true;
    btn.disabled = true; btn.textContent = 'Sending…';
    const modalErr = $('#rp-modal-err');
    try {
      const floors = floorsInUse().filter(f => f === '1st floor' || roomsOnFloor(f).length);
      const sketches = [];
      for (const f of floors.slice(0, 4)) {
        const png = await sketchPNG(f);
        if (png) sketches.push({ floor: f, png });
      }
      const photoList = photoSlots().filter(s => photos[s.key]).map(s => ({ label: s.label, data_url: photos[s.key].dataUrl }));
      const body = {
        client_ref: state.client_ref,
        client: state.client,
        front_faces: state.front_faces,
        walls: state.walls.map((w, i) => ({ label: wallLabel(i), feet: num(w.feet, 1000), inches: num(w.inches, 11.99), facing: w.facing })),
        rooms: state.rooms.map(r => ({ name: r.name, length_ft: num(r.length_ft, 500), length_in: num(r.length_in, 11.99), width_ft: num(r.width_ft, 500), width_in: num(r.width_in, 11.99), floor: r.floor, position: r.position, note: r.note })),
        fixed_elements: state.fixed_elements, ceiling_heights: state.ceiling_heights, comments: state.comments,
        photos: photoList, sketches,
      };
      const res = await authFetch('/api/room-planner/submit', { method: 'POST', body: JSON.stringify(body) });
      let data = {};
      try { data = await res.json(); } catch (_) {}
      if (!res.ok) {
        if (res.status === 403) { access = false; closeModal(); renderLocked(); return; }
        const msg = typeof data.detail === 'string' ? data.detail : (res.status === 413 ? 'Photos are too large. Remove a few and try again.' : 'Could not submit right now. Your draft is saved — please try again.');
        throw new Error(msg);
      }
      clearDraft();
      photos = {};
      state = blankState(cu());
      lastDone = data;
      closeModal();
      renderDone(data);
      window.scrollTo(0, 0);
    } catch (e) {
      if (modalErr) { modalErr.textContent = e.message || 'Could not submit. Please try again.'; modalErr.style.display = ''; }
    } finally {
      submitting = false;
      if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = 'Submit'; }
    }
  }

  // ---------- Events (delegated) ----------
  function findItem(list, el, attr) { const host = el.closest('[' + attr + ']'); return host ? list.find(x => x.id === host.getAttribute(attr)) : null; }
  function onInput(e) {
    const t = e.target;
    if (!state || !t.closest('#rp-root')) return;
    if (t.dataset.cf) { state.client[t.dataset.cf] = t.value; updateSteps(); }
    else if (t.dataset.tf) { state[t.dataset.tf] = t.value; updateSteps(); }
    else if (t.dataset.front !== undefined) { state.front_faces = t.value; sketchSoon(); }
    else if (t.dataset.wf) { const w = findItem(state.walls, t, 'data-wall'); if (w) { w[t.dataset.wf] = t.value; sketchSoon(); } }
    else if (t.dataset.rf) {
      const r = findItem(state.rooms, t, 'data-room');
      if (r) {
        r[t.dataset.rf] = t.value;
        const a = t.closest('.rp-room').querySelector('[data-room-area]'); if (a) a.textContent = roomArea(r);
        if (t.dataset.rf === 'name') { const cap = document.querySelector(`.rp-photo[data-key="room:${r.id}"] .rp-photo-cap, .rp-photo[data-key="room:${r.id}"] span`); if (cap) cap.textContent = r.name || 'Room'; }
        if (t.dataset.rf === 'floor' && e.type === 'change') state.sketchFloor = r.floor;
        sketchSoon();
      }
    } else return;
    saveDraftSoon();
  }
  async function onFile(e) {
    const t = e.target;
    if (!t.dataset || !t.dataset.photoInput || !t.files || !t.files[0]) return;
    const key = t.dataset.photoInput;
    if (!photos[key] && Object.keys(photos).length >= MAX_PHOTOS) { alert('You can attach up to ' + MAX_PHOTOS + ' photos.'); t.value = ''; return; }
    const tile = t.closest('.rp-photo');
    if (tile) tile.insertAdjacentHTML('beforeend', '<div class="rp-photo-busy">Processing…</div>');
    try {
      const dataUrl = await compressImage(t.files[0]);
      const slot = photoSlots().find(s => s.key === key);
      photos[key] = { label: slot ? slot.label : 'Photo', dataUrl };
    } catch (_) {
      alert('That photo could not be read. Try a JPG or PNG image.');
    }
    renderPhotos(); updateSteps();
  }
  function onClick(e) {
    const t = e.target;
    if (!t.closest || !t.closest('#rp-root')) return;
    let el;
    if ((el = t.closest('[data-add-wall]'))) {
      state.walls.push({ id: uid(), feet: '', inches: '', facing: '' }); renderWalls(); sketchSoon(); saveDraftSoon();
      const rows = document.querySelectorAll('#rp-walls-body tr'); const last = rows[rows.length - 1]; if (last) last.querySelector('input').focus();
    } else if ((el = t.closest('[data-del-wall]'))) {
      state.walls = state.walls.filter(w => w.id !== el.dataset.delWall); renderWalls(); sketchSoon(); saveDraftSoon();
    } else if ((el = t.closest('[data-add-room]'))) {
      state.rooms.push({ id: uid(), name: 'Room ' + (state.rooms.length + 1), length_ft: '', length_in: '', width_ft: '', width_in: '', floor: state.sketchFloor || '1st floor', position: '', note: '' });
      renderRooms(); renderPhotos(); sketchSoon(); saveDraftSoon();
      const rooms = document.querySelectorAll('#rp-rooms .rp-room'); const last = rooms[rooms.length - 1]; if (last) { const n = last.querySelector('[data-rf="name"]'); n.focus(); n.select(); }
    } else if ((el = t.closest('[data-del-room]'))) {
      const r = state.rooms.find(x => x.id === el.dataset.delRoom);
      if (r && (ftOf(r.length_ft, r.length_in) > 0 || photos['room:' + r.id]) && !confirm('Remove ' + (r.name || 'this room') + '?')) return;
      delete photos['room:' + el.dataset.delRoom];
      state.rooms = state.rooms.filter(x => x.id !== el.dataset.delRoom); renderRooms(); renderPhotos(); sketchSoon(); saveDraftSoon();
    } else if ((el = t.closest('[data-add-photo]'))) {
      const label = (prompt('What is this photo of? (e.g. Hallway, Garage, Staircase)') || '').trim();
      if (!label) return;
      state.extraPhotos.push({ id: uid(), label: label.slice(0, 60) }); renderPhotos(); saveDraftSoon();
    } else if ((el = t.closest('[data-del-photo]'))) {
      e.preventDefault(); delete photos[el.dataset.delPhoto]; renderPhotos(); updateSteps();
    } else if ((el = t.closest('[data-del-extra]'))) {
      e.preventDefault(); state.extraPhotos = state.extraPhotos.filter(p => p.id !== el.dataset.delExtra); renderPhotos(); saveDraftSoon();
    } else if ((el = t.closest('[data-floor]'))) {
      state.sketchFloor = el.dataset.floor; renderSketch();
    } else if ((el = t.closest('[data-toggle-sketch]'))) {
      const s = $('#rp-sketch'); s.classList.toggle('collapsed'); el.textContent = s.classList.contains('collapsed') ? 'Show' : 'Hide';
    } else if ((el = t.closest('[data-jump]'))) {
      const sec = document.getElementById(el.dataset.jump); if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if ((el = t.closest('[data-submit]'))) {
      openConfirm();
    } else if ((el = t.closest('[data-rp-cancel]'))) {
      closeModal();
    } else if ((el = t.closest('[data-rp-confirm]'))) {
      doSubmit(el);
    } else if (t.id === 'rp-modal') {
      if (!submitting) closeModal();
    } else if ((el = t.closest('[data-rp-go]'))) {
      if (typeof go === 'function') go(el.dataset.rpGo);
    } else if ((el = t.closest('[data-rp-grant]'))) {
      grantAccess(el);
    } else if ((el = t.closest('[data-rp-revoke]'))) {
      revokeAccess(el);
    } else if ((el = t.closest('[data-rp-recheck]'))) {
      access = null; rpOnShow();
    } else if ((el = t.closest('[data-rp-new]'))) {
      lastDone = null; state = blankState(cu()); renderTool(); if (canGrant) loadGrants();
    }
  }
  document.addEventListener('input', onInput);
  document.addEventListener('change', (e) => { if (e.target && e.target.type === 'file') onFile(e); else onInput(e); });
  document.addEventListener('click', onClick);

  // ---------- Lifecycle ----------
  async function checkAccess() {
    const user = cu();
    if (!user) return false;
    if (access !== null && accessFor === user.id) return access;
    renderLoading();
    try {
      const res = await authFetch('/api/room-planner/access');
      const data = await res.json();
      access = !!(res.ok && data.allowed);
      canGrant = !!(res.ok && data.can_grant);
    } catch (_) { access = false; canGrant = false; }
    accessFor = user.id;
    return access;
  }
  async function rpOnShow() {
    if (!$('#rp-root')) return;
    if (!cu()) { if (typeof showAuthGate === 'function') showAuthGate('login', 'room-planner'); return; }
    const ok = await checkAccess();
    if (!ok) { renderLocked(); return; }
    if (lastDone) { renderDone(lastDone); return; }
    if (!state) state = loadDraft() || blankState(cu());
    renderTool();
    if (canGrant) loadGrants();
  }
  function rpReset() {
    state = null; photos = {}; access = null; accessFor = null; canGrant = false; lastDone = null; submitting = false;
    const root = $('#rp-root'); if (root) root.innerHTML = '';
  }
  window.rpOnShow = rpOnShow;
  window.rpReset = rpReset;
  // Exposed for the automated sketch tests only.
  window.__rp = { computeOutline: () => computeOutline(), setState: (s) => { state = Object.assign(blankState(null), s); }, getState: () => state, buildSVG: (f, o) => buildSVG(f, o).svg };

  // Each sign-in starts from that account's own draft and access check.
  const isActive = () => { const scr = document.getElementById('screen-room-planner'); return !!(scr && scr.classList.contains('active')); };
  if (typeof onceAuthed === 'function') onceAuthed(() => { rpReset(); if (isActive()) rpOnShow(); });
  else if (isActive()) rpOnShow();
})();
