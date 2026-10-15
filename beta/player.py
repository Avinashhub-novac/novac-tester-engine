"""Storyline player selectors and in-page helper script.

Covers the modern and classic Storyline 360 / Storyline 3 HTML5 players.
If your published player uses different element ids, add them here.
"""

SEL = {
    "next": ["#next", "button.next", ".cs-next", "button[aria-label='Next' i]", "[data-ref='next']"],
    "prev": ["#prev", "button.prev", ".cs-prev", "button[aria-label='Previous' i]", "button[aria-label='Prev' i]", "[data-ref='prev']"],
    "submit": ["#submit", "button.submit", ".cs-submit", "[data-ref='submit']", "button[aria-label='Submit' i]"],
    "menu_toggle": ["#hamburger", "#outline-link", ".cs-menu-toggle", "button[aria-label*='menu' i]", "#menu-tab", "#outline-tab"],
    # course menu (outline) only: never the playback-speed or settings lists
    "menu_items": ["#outline-content .listitem", ".outline-links .listitem", "#outline-panel [role='treeitem']",
                   ".cs-outline [role='treeitem']", "#outline-content [role='treeitem']", "#outline-panel li a", ".cs-listitem"],
    "seekbar": ["#seek", ".cs-seekbar", "#seekbar", "[role='slider'][aria-label*='seek' i]", ".progress-bar[role='slider']", "[data-ref='seek']"],
    "captions": [".caption", ".cs-caption", "#caption", ".captions", ".cc-text", "[data-ref='caption']"],
    "slide": ["#slide", ".slide-container", "#slide-window", ".slide"],
    "title": [".cs-title", "#slide-title", ".slide-title", ".cs-slide-title"],
    "transcript": ["#transcript", "#transcript-link", ".cs-transcript-toggle", "button[aria-label*='transcript' i]",
                   "#notes-link", "#notes-tab", "button[aria-label*='notes' i]"],
    "transcript_panel": ["#transcript-panel", ".cs-transcript", "#transcript-content", ".transcript-content",
                         "#notes-panel", "#notes-content", "[role='dialog'][aria-label*='transcript' i]"],
}

# player-level buttons to exercise in the Buttons pass
PLAYER_BUTTONS = {
    "Menu": SEL["menu_toggle"],
    "Volume": ["#volume", ".cs-volume", "button[aria-label*='volume' i]", "button[aria-label*='mute' i]"],
    "Play/Pause": ["#play-pause", ".cs-pause", "button[aria-label*='pause' i]", "button[aria-label='Play' i]"],
    "Replay": ["#reset", "#replay", "button[aria-label*='replay' i]", "button[aria-label*='restart' i]"],
    "Closed captions": ["#cc", ".cs-cc", "button[aria-label*='caption' i]"],
    "Resources": ["#resources", "#resources-link", "button[aria-label*='resource' i]"],
    "Glossary": ["#glossary", "#glossary-link", "button[aria-label*='glossary' i]"],
    "Transcript": ["#transcript", "#transcript-link", "button[aria-label*='transcript' i]"],
    "Settings": ["#settings", "button[aria-label*='settings' i]"],
    "Exit": ["#exit", "button[aria-label*='exit' i]"],
}

VISITED_CLASSES = ["cs-viewed", "viewed", "visited", "is-visited", "complete", "completed"]
CURRENT_CLASSES = ["cs-selected", "selected", "active", "current", "is-current"]

# Installed in every page before course scripts run: a stub LMS API so
# index_lms.html launches outside an LMS, and an error collector.
INIT_SCRIPT = r"""
(() => {
  window.__beta = window.__beta || { errors: [] };
  window.addEventListener('error', e => window.__beta.errors.push(String(e.message || e)));
  window.addEventListener('unhandledrejection', e => window.__beta.errors.push('Unhandled promise: ' + String(e.reason)));
  // A test LMS (SCORM 1.2 and 2004). Frames share the top window's copy so every call is recorded in one place.
  let top = null;
  try { if (window.top !== window && window.top.__betaLms) top = window.top; } catch (e) {}
  if (top) { window.API = window.API || top.API; window.API_1484_11 = window.API_1484_11 || top.API_1484_11; return; }
  const lms = window.__betaLms = { calls: [], store: {}, version: '' };
  const rec = (v, fn, k, val) => { lms.version = lms.version || v; lms.calls.push({ t: Date.now(), fn, k: k || '', v: val === undefined ? '' : String(val).slice(0, 300) }); };
  const st = lms.store;
  const api12 = {
    LMSInitialize: () => { rec('SCORM 1.2', 'Initialize'); return 'true'; },
    LMSFinish: () => { rec('SCORM 1.2', 'Finish'); return 'true'; },
    LMSGetValue: k => st[k] || '',
    LMSSetValue: (k, v) => { st[k] = String(v); rec('SCORM 1.2', 'SetValue', k, v); return 'true'; },
    LMSCommit: () => { rec('SCORM 1.2', 'Commit'); return 'true'; },
    LMSGetLastError: () => '0', LMSGetErrorString: () => '', LMSGetDiagnostic: () => '' };
  const api2004 = {
    Initialize: () => { rec('SCORM 2004', 'Initialize'); return 'true'; },
    Terminate: () => { rec('SCORM 2004', 'Terminate'); return 'true'; },
    GetValue: k => st[k] || '',
    SetValue: (k, v) => { st[k] = String(v); rec('SCORM 2004', 'SetValue', k, v); return 'true'; },
    Commit: () => { rec('SCORM 2004', 'Commit'); return 'true'; },
    GetLastError: () => '0', GetErrorString: () => '', GetDiagnostic: () => '' };
  window.API = window.API || api12;
  window.API_1484_11 = window.API_1484_11 || api2004;
})();
"""

LMS_JS = r"""
() => { const l = window.__betaLms; return l ? { version: l.version, store: l.store, calls: l.calls.slice(-200) } : null; }
"""

# Evaluated in page: returns a snapshot of player state.
STATE_JS = r"""
(sel) => {
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.01;
  };
  const first = list => { for (const q of list) { for (const el of document.querySelectorAll(q)) { if (vis(el)) return el; } } return null; };
  const any = list => { for (const q of list) { const el = document.querySelector(q); if (el) return el; } return null; };
  const disabled = el => !el || el.disabled || el.getAttribute('aria-disabled') === 'true' ||
      /(^|\s)(cs-disabled|disabled|is-disabled)(\s|$)/.test(el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className || ''));
  const next = any(sel.next), prev = any(sel.prev), seek = first(sel.seekbar);
  const box = el => { if (!el || !vis(el)) return null; const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
  // current slide id: Storyline marks slide containers with data-model-id
  let slideIds = [];
  document.querySelectorAll('[data-model-id]').forEach(el => { if (vis(el)) slideIds.push(el.getAttribute('data-model-id')); });
  const container = first(sel.slide) || document.body;
  const text = (container.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 4000);
  const titleEl = first(sel.title);
  // screen-reader copy of the slide text (Storyline draws text as pictures/SVG, the readable copy lives here)
  let acc = '';
  for (const q of ['.acc-shadow-dom', '.acc-shadow-el', '[class*="acc-shadow"]', '#acc-content']) {
    document.querySelectorAll(q).forEach(el => { if (acc.length < 6000) acc += ' ' + (el.textContent || ''); });
    if (acc.trim()) break;
  }
  if (!acc.trim()) document.querySelectorAll('svg text').forEach(el => { if (acc.length < 6000 && vis(el)) acc += ' ' + el.textContent; });
  // Storyline 360 tells us which slide is showing
  let gid = '';
  try { const wm = window.DS && window.DS.windowManager;
        const sl = wm && (wm.getCurrentWindowSlide ? wm.getCurrentWindowSlide() : null);
        gid = sl ? String(sl.id || '') : ''; } catch (e) {}
  const caps = []; for (const q of sel.captions) document.querySelectorAll(q).forEach(el => { if (vis(el) && el.innerText.trim()) caps.push(el.innerText.trim()); });
  const media = [...document.querySelectorAll('audio,video')].map(m => ({src: m.currentSrc || m.src, paused: m.paused, t: m.currentTime, d: m.duration, muted: m.muted}));
  let seekVal = null, seekMax = null, seekFill = null;
  if (seek) {
    seekVal = parseFloat(seek.getAttribute('aria-valuenow')); seekMax = parseFloat(seek.getAttribute('aria-valuemax'));
    const fill = seek.querySelector('.cs-fill, .fill, .progress, .seek-fill, [class*="fill"]');
    if (fill) { const a = fill.getBoundingClientRect().width, b = seek.getBoundingClientRect().width; seekFill = b ? a / b : null; }
  }
  return {
    url: location.href, hash: location.hash, gid, acc: acc.replace(/\s+/g, ' ').trim().slice(0, 6000), slideIds, text, title: titleEl ? titleEl.innerText.trim() : '',
    nextExists: !!next && vis(next), nextDisabled: disabled(next) || !vis(next),
    prevExists: !!prev && vis(prev), prevDisabled: disabled(prev) || !vis(prev),
    nextBox: box(next), prevBox: box(prev), seekBox: box(seek),
    seekVisible: !!seek, seekVal, seekMax, seekFill, captions: [...new Set(caps)], media,
    errors: (window.__beta && window.__beta.errors.splice(0)) || [],
  };
}
"""

# DOM fingerprint used to tell whether a click "did something"
FINGERPRINT_JS = r"""
() => {
  const parts = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) < 0.02) return;
    const cls = typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '';
    parts.push(el.tagName + '#' + (el.id || '') + '.' + cls + '|' + (el.getAttribute('aria-pressed') || '') +
      (el.getAttribute('aria-expanded') || '') + (el.getAttribute('aria-checked') || '') + '|' + Math.round(r.x) + ',' + Math.round(r.y) +
      (el.childElementCount === 0 ? '|' + (el.textContent || '').slice(0, 40) : ''));
  });
  const m = [...document.querySelectorAll('audio,video')].map(x => x.paused + ':' + x.muted + ':' + x.volume).join(',');
  return parts.join('\n') + '\nMEDIA:' + m;
}
"""

# Lists clickable objects on the current slide (not player chrome).
CLICKABLES_JS = r"""
(sel) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth; };
  let root = null; for (const q of sel.slide) { root = document.querySelector(q); if (root) break; }
  root = root || document.body;
  const out = []; const seen = new Set();
  root.querySelectorAll('*').forEach(el => {
    if (!vis(el)) return;
    const s = getComputedStyle(el);
    const isBtn = el.getAttribute('role') === 'button' || el.tagName === 'BUTTON' || el.tagName === 'A' ||
                  el.getAttribute('data-acc-type') === 'button' || el.getAttribute('data-acc-type') === 'hotspot' ||
                  (s.cursor === 'pointer' && (!el.parentElement || getComputedStyle(el.parentElement).cursor !== 'pointer'));
    if (!isBtn) return;
    if (el.closest('[class*="scrollbar" i], [class*="scroll-bar" i]')) return;   // text box scrollbars are not items to open
    const r = el.getBoundingClientRect();
    // only what a learner can actually see and click: not covered, not see-through, not a hidden layer
    let op = 1; for (let p = el; p && p !== document.body; p = p.parentElement) op *= parseFloat(getComputedStyle(p).opacity || '1');
    if (op < 0.2) return;
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return;
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(top === el || el.contains(top) || top.contains(el))) return;
    const key = Math.round(r.x / 4) + ',' + Math.round(r.y / 4) + ',' + Math.round(r.width / 4);
    if (seen.has(key)) return; seen.add(key);
    const label = (el.getAttribute('aria-label') || el.getAttribute('data-acc-text') || el.innerText || el.id || el.tagName).trim().replace(/\s+/g, ' ').slice(0, 60);
    out.push({ label, x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height });
  });
  return out;
}
"""

MENU_JS = r"""
(args) => {
  const [sel, visited, current] = args;
  // the course menu only: never the playback-speed or settings lists
  const notMenu = el => el.closest('#speed-scroll-wrapper, [id*="speed" i], [class*="speed" i], [id*="settings" i], [class*="settings" i], [role="menu"]');
  let items = [], used = '';
  for (const q of sel.menu_items) { items = [...document.querySelectorAll(q)].filter(el => !notMenu(el)); if (items.length) { used = q; break; } }
  return items.map((el, i) => {
    const node = el.closest('li') || el;
    const cls = [node.className, el.className, (el.parentElement || {}).className].map(c => typeof c === 'string' ? c : '').join(' ');
    const has = list => list.some(c => new RegExp('(^|\\s)' + c + '(\\s|$)').test(cls));
    const text = (el.getAttribute('data-slide-title') || el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return { i, text, visited: has(visited) || el.getAttribute('data-visited') === 'true',
             current: has(current) || el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-current') === 'true' || el.getAttribute('aria-current') === 'page',
             visible: el.getBoundingClientRect().width > 0, sel: used,
             ref: el.getAttribute('data-ref') || (el.closest('[data-ref]') || {getAttribute: () => ''}).getAttribute('data-ref') || '',
             scene: /(^|\s)is-scene(\s|$)/.test(cls) };
  });
}
"""

# Visible text with positions (per text block and per word), used to outline the exact words an issue is about.
TEXTBOX_JS = r"""
() => {
  const out = [], words = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node;
  while ((node = walker.nextNode()) && out.length < 500) {
    const raw = node.textContent;
    if (!raw || !raw.trim()) continue;
    const el = node.parentElement; if (!el) continue;
    // the screen-reader copy is laid out differently from the text the learner sees: never use it for word positions
    if (el.closest('.acc-shadow-dom, .acc-shadow-el, [class*="acc-shadow"]')) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) < 0.05) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    const n = out.length;
    out.push({ t: raw.replace(/\s+/g, ' ').trim().slice(0, 600), b: [r.x, r.y, r.width, r.height] });
    const re = /\S+/g; let m;
    while ((m = re.exec(raw)) && words.length < 4000) {
      range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
      const w = range.getBoundingClientRect();
      if (w.width > 0) words.push({ w: m[0], n, b: [w.x, w.y, w.width, w.height] });
    }
  }
  // Storyline often draws text as graphics; its screen-reader copy sits invisibly over each text box.
  // Use those boxes (whole text box, not single words) so issues can still be outlined in the right place.
  const seenAcc = new Set();
  document.querySelectorAll('.acc-shadow-el, [class*="acc-shadow"] [data-acc-text], [data-acc-text]').forEach(el => {
    if (out.length >= 800) return;
    const t = (el.getAttribute('data-acc-text') || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 1500) return;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    let hidden = false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ps = getComputedStyle(p);
      if (ps.display === 'none' || ps.visibility === 'hidden') { hidden = true; break; }
    }
    if (hidden) return;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight) return;
    if (r.width > innerWidth * 0.98 && r.height > innerHeight * 0.9) return;   // the whole slide, not a text box
    const k = t + '|' + Math.round(r.x) + ',' + Math.round(r.y);
    if (seenAcc.has(k)) return; seenAcc.add(k);
    out.push({ t: t.slice(0, 600), b: [r.x, r.y, r.width, r.height], acc: true });
  });
  return { nodes: out, words };
}
"""

# Text of the open Transcript / Notes panel ('' when it is not open).
TRANSCRIPT_JS = r"""
(sel) => {
  for (const q of sel.transcript_panel) {
    for (const el of document.querySelectorAll(q)) {
      const r = el.getBoundingClientRect(), s = getComputedStyle(el);
      if (r.width > 20 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none') {
        const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (t) return t.slice(0, 20000);
      }
    }
  }
  return '';
}
"""

# Answer options of a question screen (radio buttons / check boxes, including Storyline's screen-reader copies).
OPTIONS_JS = r"""
() => {
  const out = [], seen = new Set();
  const q = "[role='radio'], [role='checkbox'], [data-acc-type='radiobutton'], [data-acc-type='checkbox'], input[type=radio], input[type=checkbox]";
  document.querySelectorAll(q).forEach(el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ps = getComputedStyle(p); if (ps.display === 'none' || ps.visibility === 'hidden') return;
    }
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight) return;
    const label = (el.getAttribute('aria-label') || el.getAttribute('data-acc-text') || el.innerText ||
                   (el.labels && el.labels[0] && el.labels[0].innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    const key = Math.round(r.x / 6) + ',' + Math.round(r.y / 6);
    if (seen.has(key)) return; seen.add(key);
    const checked = el.getAttribute('aria-checked') === 'true' || el.checked === true ||
                    el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-selected') === 'true';
    // which course object the option is: the accessibility copy names it (data-represents / id="acc-<id>")
    const holder = el.closest('[data-model-id]');
    const rep = (el.getAttribute('data-represents') || '').split('.').pop() || (el.id || '').replace(/^acc-/, '');
    out.push({ label, box: [r.x, r.y, r.width, r.height], checked, mid: holder ? holder.getAttribute('data-model-id') : rep,
               disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true' });
  });
  out.sort((a, b) => (a.box[1] - b.box[1]) || (a.box[0] - b.box[0]));
  return out;
}
"""

# Text that overlaps other visible text (both pieces on top, so the learner sees them on each other).
OVERLAP_JS = r"""
() => {
  const blocks = [];
  const add = (el, t, r) => {
    if (!t || r.width < 6 || r.height < 6 || r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight) return;
    blocks.push({ el, t: t.replace(/\s+/g, ' ').trim().slice(0, 120), b: [r.x, r.y, r.width, r.height] });
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange(); let node;
  while ((node = walker.nextNode()) && blocks.length < 400) {
    if (!node.textContent.trim()) continue;
    const el = node.parentElement; if (!el) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) < 0.2) continue;
    if (el.closest('.acc-shadow-dom, [class*="acc-shadow"]')) continue;
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) add(el, node.textContent, r);
  }
  const solid = el => {
    const s = getComputedStyle(el);
    if (['IMG', 'CANVAS', 'VIDEO'].includes(el.tagName)) return true;
    const m = (s.backgroundColor || '').match(/rgba?\(([^)]+)\)/);
    if (m) { const v = m[1].split(',').map(Number); if (v.length < 4 || v[3] > 0.5) return true; }
    if (el instanceof SVGElement && !['text', 'tspan', 'svg', 'g'].includes(el.tagName.toLowerCase())) {
      const f = s.fill || ''; if (f && f !== 'none' && !/rgba\([^)]*,\s*0\)/.test(f) && parseFloat(s.fillOpacity || '1') > 0.5) return true;
    }
    return false;
  };
  const covered = (a, b, x, y) => {
    const stack = document.elementsFromPoint(x, y);
    const pos = bl => stack.findIndex(e => e === bl.el || bl.el.contains(e));
    const ia = pos(a), ib = pos(b);
    if (ia < 0 || ib < 0) return true;
    const [upper, lower] = ia < ib ? [a, b] : [b, a];
    const lo = Math.max(ia, ib);
    for (let k = 0; k < lo; k++) {
      const e = stack[k];
      if (e === upper.el || upper.el.contains(e) || e.contains(lower.el)) continue;
      if (solid(e)) return true;
    }
    return false;
  };
  const onTop = (bl, x, y) => { const t = document.elementFromPoint(x, y); return !!t && (t === bl.el || bl.el.contains(t) || t.contains(bl.el)); };
  const hits = [];
  for (let i = 0; i < blocks.length; i++) for (let j = i + 1; j < blocks.length; j++) {
    const a = blocks[i], b = blocks[j];
    if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el) || a.t === b.t) continue;
    const x0 = Math.max(a.b[0], b.b[0]), y0 = Math.max(a.b[1], b.b[1]);
    const x1 = Math.min(a.b[0] + a.b[2], b.b[0] + b.b[2]), y1 = Math.min(a.b[1] + a.b[3], b.b[1] + b.b[3]);
    if (x1 - x0 < 6 || y1 - y0 < 4) continue;
    const small = Math.min(a.b[2] * a.b[3], b.b[2] * b.b[3]);
    if ((x1 - x0) * (y1 - y0) < 0.25 * small) continue;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    if (!onTop(a, cx, cy) && !onTop(b, cx, cy)) continue;
    if (covered(a, b, cx, cy)) continue;   // one piece sits on a solid box (a pop-up): only one is really seen
    hits.push({ a: a.t, b: b.t, box: [Math.min(a.b[0], b.b[0]), Math.min(a.b[1], b.b[1]),
               Math.max(a.b[0] + a.b[2], b.b[0] + b.b[2]) - Math.min(a.b[0], b.b[0]),
               Math.max(a.b[1] + a.b[3], b.b[1] + b.b[3]) - Math.min(a.b[1], b.b[1])] });
    if (hits.length >= 5) return hits;
  }
  return hits;
}
"""
