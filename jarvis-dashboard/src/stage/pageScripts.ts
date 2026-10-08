/* eslint-disable no-var */
// Functions that run INSIDE web pages (live pages via webview.executeJavaScript) and
// also directly in the dashboard (reader views, notes, summaries). They are written in
// plain ES5 with no outside references — no arrow functions, spread, for...of or
// imports — so Function.prototype.toString() yields a self-contained script even after
// Babel and minification.

/** Build a script that calls `fn` with JSON-encoded arguments, for executeJavaScript. */
export function callInPage(fn: Function, ...args: unknown[]) {
  return '(' + fn.toString() + ')(' + args.map(a => JSON.stringify(a === undefined ? null : a)).join(',') + ')';
}

/**
 * Find `quote` in the text under `rootSelector` (or the whole page), paint it with the CSS
 * Custom Highlight API, draw a glowing labelled box around it and scroll it into view.
 * An empty quote clears the mark. `key` keeps marks from different panels apart.
 */
export function jarvisHighlight(quote: string, label: string, key: string, rootSelector: string | null): boolean {
  var doc = document;
  var win: any = window;
  var root: any = rootSelector ? doc.querySelector(rootSelector) : doc.body;
  var store = win.__jarvisHL || (win.__jarvisHL = {});
  var prev = store[key];
  if (prev && prev.box && prev.box.parentNode) prev.box.parentNode.removeChild(prev.box);
  if (prev && prev.ro) prev.ro.disconnect();
  var hlName = 'jarvis-' + String(key).replace(/[^a-zA-Z0-9_-]/g, '');
  var hasHL = !!(win.CSS && win.CSS.highlights && win.Highlight);
  if (hasHL) win.CSS.highlights.delete(hlName);
  store[key] = null;
  if (!quote || !root) return false;

  var head = doc.head || doc.documentElement;
  if (!doc.getElementById('__jarvis_hl_style')) {
    var st = doc.createElement('style');
    st.id = '__jarvis_hl_style';
    st.textContent =
      '.__jarvis_hl_box{position:absolute;pointer-events:none;border:2px solid #3dff8f;border-radius:6px;' +
      'z-index:2147483647;animation:__jarvis_pulse 1.6s ease-in-out infinite}' +
      '.__jarvis_hl_tag{position:absolute;left:-2px;bottom:100%;margin-bottom:5px;font:600 11px/18px "Segoe UI",system-ui,sans-serif;' +
      'letter-spacing:.05em;background:#3dff8f;color:#02140a;padding:0 7px;border-radius:4px;white-space:nowrap}' +
      '@keyframes __jarvis_pulse{0%,100%{box-shadow:0 0 0 3px rgba(61,255,143,.12),0 0 18px rgba(61,255,143,.45)}' +
      '50%{box-shadow:0 0 0 6px rgba(61,255,143,.2),0 0 32px rgba(61,255,143,.75)}}';
    head.appendChild(st);
  }
  if (hasHL && !doc.getElementById('__jarvis_hlrule_' + hlName)) {
    var rule = doc.createElement('style');
    rule.id = '__jarvis_hlrule_' + hlName;
    rule.textContent = '::highlight(' + hlName + '){background-color:rgba(61,255,143,.38);color:inherit}';
    head.appendChild(rule);
  }

  // Flatten the visible text to lower case with single spaces and typographic quotes
  // straightened, remembering where every character came from
  var subs: any = { '‘': "'", '’': "'", '“': '"', '”': '"',
                    '–': '-', '—': '-', ' ': ' ' };
  var walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  var nodes: any[] = [], text = '', map: number[] = [], space = true, node: any;
  while ((node = walker.nextNode())) {
    var parent = node.parentElement;
    if (!parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
    if (parent.closest && parent.closest('.__jarvis_hl_box')) continue;
    var v = node.nodeValue || '';
    var ni = nodes.push(node) - 1;
    for (var i = 0; i < v.length; i++) {
      var c = subs[v.charAt(i)] || v.charAt(i);
      if (/\s/.test(c)) {
        if (space) continue;
        c = ' ';
        space = true;
      } else {
        space = false;
      }
      text += c.toLowerCase();
      map.push(ni, i);
    }
  }
  var q = '', qs = true;
  for (var j = 0; j < quote.length; j++) {
    var d = subs[quote.charAt(j)] || quote.charAt(j);
    if (/\s/.test(d)) {
      if (qs) continue;
      d = ' ';
      qs = true;
    } else {
      qs = false;
    }
    q += d.toLowerCase();
  }
  q = q.replace(/\s+$/, '');
  var at = text.indexOf(q), len = q.length;
  if (at < 0 && q.length > 48) { at = text.indexOf(q.slice(0, 48)); len = 48; }        // start of the quote
  if (at < 0 && q.length > 48) { at = text.indexOf(q.slice(-48)); len = 48; }          // or its end
  if (at < 0 || len === 0) return false;

  var s = at * 2, e = (at + len - 1) * 2;
  var range = doc.createRange();
  range.setStart(nodes[map[s]], map[s + 1]);
  range.setEnd(nodes[map[e]], map[e + 1] + 1);
  if (hasHL) win.CSS.highlights.set(hlName, new win.Highlight(range));

  var box = doc.createElement('div');
  box.className = '__jarvis_hl_box';
  var host: any = rootSelector ? root : doc.body;
  var r: any, top = 0;
  // Position the box over the text, in the container's scroll coordinates. Re-run when the
  // container resizes: text reflows when a panel is resized or the layout changes.
  var place = function () {
    r = range.getBoundingClientRect();
    var left: number;
    if (rootSelector) {
      var rr = root.getBoundingClientRect();
      left = r.left - rr.left + root.scrollLeft;
      top = r.top - rr.top + root.scrollTop;
    } else {
      left = r.left + win.pageXOffset;
      top = r.top + win.pageYOffset;
    }
    box.style.left = (left - 6) + 'px';
    box.style.top = (top - 4) + 'px';
    box.style.width = (r.width + 12) + 'px';
    box.style.height = (r.height + 8) + 'px';
  };
  place();
  if (label) {
    var tag = doc.createElement('div');
    tag.className = '__jarvis_hl_tag';
    tag.textContent = label;
    box.appendChild(tag);
  }
  host.appendChild(box);

  var created = Date.now();
  var scrollToMark = function (smooth: boolean) {
    if (rootSelector) {
      // scroll only this panel — scrollIntoView would also scroll the dashboard's grid
      root.scrollTo({ top: Math.max(0, top - root.clientHeight / 2 + r.height / 2), behavior: smooth ? 'smooth' : 'auto' });
    } else if (range.startContainer.parentElement) {
      range.startContainer.parentElement.scrollIntoView({ block: 'center', behavior: smooth ? 'smooth' : 'auto' });
    }
  };
  scrollToMark(true);
  var ro = win.ResizeObserver ? new win.ResizeObserver(function () {
    place();
    if (Date.now() - created < 2000) scrollToMark(false);   // the layout was still settling
  }) : null;
  if (ro) ro.observe(root);
  store[key] = { box: box, ro: ro };
  return true;
}

/** Where the current mark for `key` is, in the page's viewport coordinates (or null). */
export function jarvisHighlightRect(key: string): { x: number; y: number; w: number; h: number } | null {
  var win: any = window;
  var h = win.__jarvisHL && win.__jarvisHL[key];
  if (!h || !h.box || !h.box.isConnected) return null;
  var r = h.box.getBoundingClientRect();
  if (r.bottom < 0 || r.top > win.innerHeight) return null;     // scrolled out of view
  return { x: r.left, y: r.top, w: r.width, h: r.height };
}

/** Readable text of the live page, for Jarvis to summarize (JS-rendered sites). */
export function jarvisExtract(): { title: string; url: string; paragraphs: string[] } {
  var root: any = document.querySelector('article') || document.querySelector('main') || document.body;
  var els = root.querySelectorAll('h1,h2,h3,p,li,blockquote,pre');
  var out: string[] = [];
  for (var i = 0; i < els.length && out.length < 400; i++) {
    var t = String(els[i].innerText || '').replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
    if (t.length > 30 || (/^H/.test(els[i].tagName) && t)) out.push(t);
  }
  return { title: document.title, url: window.location.href, paragraphs: out };
}
