"""Browser-side JavaScript constants used by BrowserManager for page observation."""

from __future__ import annotations

# Injected before every page load via add_init_script().
# Removes automation signals, mocks realistic browser properties.
STEALTH_INIT_SCRIPT = r"""
() => {
  // Remove webdriver flag
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined, configurable: true,
    });
  } catch (_) {}

  // Chrome runtime object (many sites check for this)
  if (!window.chrome) {
    window.chrome = {
      runtime: {
        onMessage: { addListener: () => {}, removeListener: () => {} },
        connect: () => ({ onDisconnect: { addListener: () => {} }, postMessage: () => {} }),
        sendMessage: () => {},
        id: undefined,
      },
      loadTimes: () => ({}),
      csi: () => ({}),
      app: { isInstalled: false },
    };
  }

  // Realistic navigator.plugins (headless has none by default)
  try {
    const plugins = [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ];
    const arr = { length: plugins.length, refresh: () => {}, item: (i) => plugins[i], namedItem: (n) => plugins.find(p => p.name === n) || null };
    plugins.forEach((p, i) => { arr[i] = p; });
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(navigator, 'plugins', { get: () => arr });
  } catch (_) {}

  // Languages
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (_) {}

  // Permissions API patch
  try {
    const origQuery = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(params) {
      if (params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery.call(this, params);
    };
  } catch (_) {}

  // Canvas fingerprint noise
  try {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
      const ctx = this.getContext('2d');
      if (ctx && this.width > 0 && this.height > 0) {
        try {
          const img = ctx.getImageData(0, 0, 1, 1);
          img.data[0] ^= 1;
          ctx.putImageData(img, 0, 0);
          const result = origToDataURL.call(this, type, quality);
          img.data[0] ^= 1;
          ctx.putImageData(img, 0, 0);
          return result;
        } catch (_) {}
      }
      return origToDataURL.call(this, type, quality);
    };
  } catch (_) {}

  // WebGL vendor / renderer — realistic Intel values
  try {
    const patchGL = (proto) => {
      const orig = proto.getParameter;
      proto.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return orig.call(this, p);
      };
    };
    patchGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchGL(WebGL2RenderingContext.prototype);
  } catch (_) {}

  // Realistic hardware values
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch (_) {}
  try { Object.defineProperty(screen, 'colorDepth', { get: () => 24 }); } catch (_) {}
}
"""

INTERACTABLES_SCRIPT = r"""
(limit) => {
  // A zero limit is the DevTools-style snapshot mode: return the complete
  // accessibility/action surface, including controls below the fold. Ordinary
  // observations keep their existing bounded, viewport-only behaviour.
  const fullSnapshot = limit === 0;
  function isPainted(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && (fullSnapshot || (
        rect.right > 0 && rect.bottom > 0
        && rect.left < window.innerWidth && rect.top < window.innerHeight
      ))
      && style.visibility !== 'hidden' && style.display !== 'none'
      && style.opacity !== '0' && !el.closest('[aria-hidden="true"]');
  }

  function isHitTestable(el) {
    if (!isPainted(el)) return false;
    if (fullSnapshot) return true;
    const rect = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && (hit === el || el.contains(hit)));
  }

  function activeDialog() {
    const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')]
      .filter(isHitTestable);
    if (!dialogs.length) return null;
    const leaves = dialogs.filter((candidate) =>
      !dialogs.some((other) => other !== candidate && candidate.contains(other))
    );
    const candidates = leaves.length ? leaves : dialogs;
    const depth = (el) => {
      let value = 0;
      for (let node = el; node && node !== document.body; node = node.parentElement) value += 1;
      return value;
    };
    candidates.sort((left, right) => {
      const depthDelta = depth(left) - depth(right);
      if (depthDelta) return depthDelta;
      const leftZ = Number.parseInt(window.getComputedStyle(left).zIndex, 10) || 0;
      const rightZ = Number.parseInt(window.getComputedStyle(right).zIndex, 10) || 0;
      if (leftZ !== rightZ) return leftZ - rightZ;
      return left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
    return candidates[candidates.length - 1];
  }

  // An input's live value is never a label: for a password / one-time-code / any text
  // field it is exactly what the page's user typed. Only a button-like input's value is
  // its visible caption.
  function buttonValue(el) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    return el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(type) ? el.value : '';
  }

  function getLabel(el) {
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id)?.innerText || document.getElementById(id)?.textContent || '')
      .join(' ');
    const associatedLabels = el.labels
      ? [...el.labels].map((label) => label.innerText || label.textContent || '').join(' ')
      : '';
    const raw = el.getAttribute('aria-label')
      || labelledBy
      || associatedLabels
      || el.getAttribute('placeholder')
      || el.innerText
      || buttonValue(el)
      || el.getAttribute('name')
      || el.id
      || el.href
      || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  function activePanelContext(el) {
    // Bind an editor to the semantic panel and header it actually belongs to.  This is a
    // structural projection, not a page-wide text search: a matching name in a sidebar or
    // another list cannot become the identity of the active editor's conversation.
    const panel = el.closest('#main, main, [role="main"]');
    if (!panel) return null;
    const header = panel.querySelector(':scope > header') || panel.querySelector('header');
    if (!header || !isPainted(header)) return null;
    const key = (node) => ({
      tag: node.tagName.toLowerCase(),
      id: node.id || null,
      role: node.getAttribute('role'),
      testid: node.getAttribute('data-testid'),
    });
    const labels = new Set();
    const stableIds = new Set();
    for (const node of [header, ...header.querySelectorAll('[title], [aria-label], [data-id]')]) {
      for (const attr of ['title', 'aria-label']) {
        const value = String(node.getAttribute(attr) || '').replace(/\s+/g, ' ').trim();
        if (value && value.length <= 160) labels.add(value);
      }
      for (const attr of ['data-id']) {
        const value = String(node.getAttribute(attr) || '').trim();
        if (value && value.length <= 300) stableIds.add(value);
      }
    }
    return {
      panel: key(panel),
      header: key(header),
      header_labels: [...labels].slice(0, 20),
      stable_ids: [...stableIds].slice(0, 20),
    };
  }

  const selector = [
    'a',
    'button',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="textbox"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[contenteditable="true"]',
    '[tabindex]'
  ].join(',');

  const out = [];
  const seen = new Set();
  const dialog = activeDialog();
  const root = dialog || document;
  const controls = [
    ...(root.matches?.(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ];
  for (const el of controls) {
    if (!isHitTestable(el)) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    seen.add(el);
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: el.dataset.operatorId,
      selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label: getLabel(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      checked: ['checkbox', 'radio'].includes((el.getAttribute('type') || '').toLowerCase())
        ? Boolean(el.checked)
        : (el.hasAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : null),
      href: el.href || null,
      aria_haspopup: el.getAttribute('aria-haspopup'),
      aria_expanded: el.getAttribute('aria-expanded'),
      form_submit: Boolean(el.form && (
        (el.tagName === 'BUTTON' && el.type === 'submit')
        || (el.tagName === 'INPUT' && ['submit', 'image'].includes(el.type))
      )),
      active_panel_context: activePanelContext(el),
      in_active_dialog: Boolean(dialog),
      dialog_label: dialog ? getLabel(dialog) : null,
      bbox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
    if (!fullSnapshot && out.length >= limit) break;
  }

  // Custom dropdown/listbox/menu options: divs or spans with a click handler
  // that never match the `selector` list above (no role, no tabindex) --
  // rendered by a portal or a virtualized list, invisible to every click/type
  // target resolver even though a screenshot shows them plainly. Detect the
  // container (an explicit ARIA role, an open in-page dialog that commonly
  // carries its own search+results, or a portal panel appended straight to
  // <body>) and list its item children, even plain divs, capped.
  function isOptionLike(el) {
    if (seen.has(el)) return false;
    const role = el.getAttribute('role');
    if (role && ['option', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'listitem'].includes(role)) return true;
    if (el.tagName === 'LI') return true;
    let cursor = '';
    try { cursor = window.getComputedStyle(el).cursor; } catch (_) {}
    if (cursor !== 'pointer' && !el.hasAttribute('onclick')) return false;
    // A leaf-ish clickable row, not a wrapper around several rows of its own.
    if (el.querySelector('button, a, input, [role="option"], [role="menuitem"], li')) return false;
    return true;
  }

  function optionContainers() {
    const containers = new Set();
    for (const el of document.querySelectorAll('[role="listbox"], [role="menu"], [role="tree"], [role="grid"]')) {
      if (isPainted(el) && (!dialog || dialog.contains(el) || isHitTestable(el))) containers.add(el);
    }
    if (dialog) containers.add(dialog);
    // A portal-rendered dropdown: a fixed/absolute panel appended as a direct
    // child of <body>, holding several pointer-cursor rows, with no ARIA
    // role of its own.
    for (const el of document.body.children) {
      if (containers.has(el) || !isPainted(el)) continue;
      let position = '';
      try { position = window.getComputedStyle(el).position; } catch (_) {}
      if (position !== 'fixed' && position !== 'absolute') continue;
      const rows = [...el.querySelectorAll('*')].filter(isOptionLike);
      if (rows.length >= 2) containers.add(el);
    }
    return containers;
  }

  const OPTION_CAP = fullSnapshot ? Number.POSITIVE_INFINITY : 60;
  let optionCount = 0;
  outer:
  for (const container of optionContainers()) {
    const items = container.querySelectorAll(
      '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="listitem"], li'
    );
    const candidates = items.length ? [...items] : [...container.querySelectorAll('*')].filter(isOptionLike);
    for (const el of candidates) {
      if (!fullSnapshot && out.length >= limit) break outer;
      if (seen.has(el) || !isHitTestable(el)) continue;
      seen.add(el);
      if (!el.dataset.operatorId) {
        el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
      }
      const rect = el.getBoundingClientRect();
      out.push({
        element_id: el.dataset.operatorId,
        selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
        tag: el.tagName.toLowerCase(),
        type: null,
        role: el.getAttribute('role') || 'option',
        label: getLabel(el),
        disabled: Boolean(el.getAttribute('aria-disabled') === 'true'),
        checked: el.hasAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : null,
        href: null,
        aria_haspopup: el.getAttribute('aria-haspopup'),
        aria_expanded: el.getAttribute('aria-expanded'),
        form_submit: false,
        in_active_dialog: Boolean(dialog && dialog.contains(el)),
        dialog_label: dialog ? getLabel(dialog) : null,
        bbox: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        },
        list_option: true
      });
      optionCount += 1;
      if (optionCount >= OPTION_CAP || (!fullSnapshot && out.length >= limit)) break outer;
    }
  }

  let dialogSummary = null;
  if (dialog) {
    if (!dialog.dataset.operatorId) {
      dialog.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = dialog.getBoundingClientRect();
    dialogSummary = {
      element_id: dialog.dataset.operatorId,
      label: getLabel(dialog),
      role: dialog.getAttribute('role') || 'dialog',
      bbox: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    };
  }
  // Playback facts are read only and carry no source URLs or media contents.
  // Keep this separate from controls: a media node is not a click target.
  const media = fullSnapshot ? [...document.querySelectorAll('video, audio')].slice(0, 20).map((el) => {
    const finite = (value) => Number.isFinite(value) ? value : null;
    return {
      kind: el.tagName.toLowerCase(),
      element_id: el.dataset.operatorId || null,
      id: el.id ? el.id.slice(0, 160) : null,
      paused: Boolean(el.paused),
      ended: Boolean(el.ended),
      current_time: finite(el.currentTime),
      duration: finite(el.duration),
      ready_state: el.readyState,
      video_width: el.tagName === 'VIDEO' ? el.videoWidth : null,
      video_height: el.tagName === 'VIDEO' ? el.videoHeight : null,
      controls: Boolean(el.controls),
    };
  }) : [];
  return {interactables: out, active_dialog: dialogSummary, media};
}
"""

# A scoped (or whole-page, when scopeSelector is null/absent) scan for visible
# elements carrying a text label -- their own direct text, an aria-label, or a
# placeholder -- used as the click/type text-fallback's candidate pool when a
# selector matches nothing (see resolve_candidate_locator's text fallback in
# actions.py). For each candidate, walks up to the nearest element that is
# actually clickable (a native control, an ARIA interactive role, an
# onclick/tabindex, or a computed cursor:pointer) so a click lands on the row
# a person would actually press, not on an inert text node inside it.
TEXT_CANDIDATES_SCRIPT = r"""
(scopeSelector) => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const isClickable = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (['a', 'button', 'input', 'select', 'textarea', 'label'].includes(tag)) return true;
    const role = el.getAttribute('role');
    if (role && [
      'button', 'option', 'menuitem', 'menuitemradio', 'menuitemcheckbox',
      'link', 'tab', 'checkbox', 'radio', 'listitem'
    ].includes(role)) return true;
    if (el.hasAttribute('onclick') || el.hasAttribute('tabindex')) return true;
    try { return window.getComputedStyle(el).cursor === 'pointer'; } catch (_) { return false; }
  };
  const clickableAncestor = (el) => {
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i++) {
      if (isClickable(node)) return node;
      node = node.parentElement;
    }
    return el;
  };

  const root = scopeSelector ? document.querySelector(scopeSelector) : document.body;
  if (!root) return [];

  const out = [];
  const seen = new Set();
  for (const el of root.querySelectorAll('*')) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    const own = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
    const placeholder = el.getAttribute('placeholder') || '';
    const ariaLabel = el.getAttribute('aria-label') || '';
    const label = own || ariaLabel || placeholder;
    if (!label) continue;
    const target = clickableAncestor(el);
    if (seen.has(target)) continue;
    seen.add(target);
    if (!target.dataset.operatorId) {
      target.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    out.push({
      selector_hint: `[data-operator-id="${target.dataset.operatorId}"]`,
      text: own.slice(0, 200),
      placeholder: placeholder || null,
      aria_label: ariaLabel || null
    });
    if (out.length >= 500) break;
  }
  return out;
}
"""

ACTIVE_ELEMENT_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName.toLowerCase(),
    element_id: el.dataset?.operatorId || null,
    name: el.getAttribute('name'),
    id: el.id || null,
    label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes((el.getAttribute('type') || '').toLowerCase()) ? el.value : '') || '').toString().replace(/\s+/g, ' ').trim().slice(0, 120)
  };
}
"""

PAGE_SUMMARY_SCRIPT = r"""
(textLimit) => {
  const squash = (value, maxLength = textLimit) =>
    String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);
  // Match interactables: only button-like inputs expose their value as a visible caption.
  // Hidden tokens, passwords and other editable values are never field labels.
  const buttonValue = (field) => field.tagName === 'INPUT'
    && ['button', 'submit', 'reset'].includes((field.getAttribute('type') || '').toLowerCase())
    ? field.value : '';
  const visibleCaption = (field) => ['BUTTON', 'SELECT'].includes(field.tagName)
    ? field.innerText : '';

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .slice(0, 8)
    .map((el) => ({
      level: el.tagName.toLowerCase(),
      text: squash(el.innerText, 160)
    }))
    .filter((item) => item.text);

  const forms = Array.from(document.forms)
    .slice(0, 3)
    .map((form) => ({
      action: form.getAttribute('action') || null,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
        .slice(0, 8)
        .map((field) => ({
          tag: field.tagName.toLowerCase(),
          type: field.getAttribute('type') || null,
          name: field.getAttribute('name') || null,
          label: squash(
            field.getAttribute('aria-label')
              || field.getAttribute('placeholder')
              || visibleCaption(field)
              || buttonValue(field)
              || field.getAttribute('name')
              || field.id,
            80
          ),
          disabled: Boolean(field.disabled || field.getAttribute('aria-disabled') === 'true')
        }))
    }));

  return {
    scroll_position: {x: window.scrollX, y: window.scrollY},
    text_excerpt: squash(document.body?.innerText || '', textLimit),
    dom_outline: {
      headings,
      forms,
      counts: {
        links: document.querySelectorAll('a').length,
        buttons: document.querySelectorAll('button, [role="button"]').length,
        inputs: document.querySelectorAll('input, textarea, select').length,
        forms: document.forms.length
      }
    }
  };
}
"""

# Feed/profile extraction helpers
EXTRACT_POSTS_SCRIPT = r"""
(limit) => {
  const candidates = [];
  const seen = new Set();
  const els = document.querySelectorAll(
    'article, [role="article"], [data-testid*="tweet"], ' +
    '[data-testid*="post"], [class*="post-content"], ' +
    '[class*="feed-item"], [class*="timeline-item"]'
  );
  for (const el of els) {
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (text.length < 20 || seen.has(text.slice(0, 100))) continue;
    seen.add(text.slice(0, 100));
    const rect = el.getBoundingClientRect();
    if (rect.width === 0) continue;
    const links = [...el.querySelectorAll('a[href]')]
      .map(a => a.href).filter(h => h && !h.startsWith('javascript'));
    const imgs = [...el.querySelectorAll('img[src]')].map(i => i.src).slice(0, 3);
    candidates.push({
      text: text.slice(0, 600),
      links: links.slice(0, 5),
      images: imgs,
      tag: el.tagName.toLowerCase(),
      y_position: Math.round(rect.top + window.scrollY),
    });
    if (candidates.length >= limit) break;
  }
  return candidates;
}
"""

EXTRACT_PROFILE_SCRIPT = r"""
() => {
  function first(selectors) {
    for (const s of selectors) {
      const el = document.querySelector(s);
      const text = el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : null;
      if (text && text.length > 0) return text.slice(0, 300);
    }
    return null;
  }
  function findCount(keyword) {
    for (const el of document.querySelectorAll('a, span, div')) {
      const text = (el.innerText || '').toLowerCase();
      if (text.includes(keyword) && /[\d,]+/.test(text)) {
        const m = text.match(/([\d,.]+\s*[kmb]?)/i);
        return m ? m[0].trim() : null;
      }
    }
    return null;
  }
  return {
    page_title: document.title,
    url: window.location.href,
    username: first(['[data-testid="UserName"]', '.username', 'h1', '[class*="username"]']),
    display_name: first(['[data-testid="UserName"] span', '[class*="display-name"]', '[class*="displayName"]']),
    bio: first(['[data-testid="UserDescription"]', '[class*="bio"]', '[class*="about"]', '[class*="description"]']),
    followers: findCount('follower'),
    following: findCount('following'),
    post_count: findCount('post') || findCount('tweet') || findCount('video'),
    avatar_url: document.querySelector(
      'img[src*="profile_image"], img[src*="avatar"], img[class*="avatar"], img[class*="profile"]'
    )?.src || null,
  };
}
"""

SMOOTH_SCROLL_SCRIPT = r"""
(deltaY) => {
  return new Promise((resolve) => {
    const direction = Math.sign(deltaY) || 1;
    const target = Math.abs(deltaY);
    const overshoot = target > 240 ? Math.round(target * (0.04 + Math.random() * 0.08)) : 0;
    let travelled = 0;
    let correcting = false;

    function nextDelay() {
      return 14 + Math.random() * 22;
    }

    function tick() {
      const remaining = correcting ? travelled : (target + overshoot - travelled);
      if (remaining <= 0) {
        if (!correcting && overshoot > 0) {
          correcting = true;
          travelled = overshoot;
          setTimeout(tick, nextDelay());
          return;
        }
        resolve();
        return;
      }

      const baseStep = 45 + Math.random() * 65;
      const amount = Math.min(remaining, baseStep);
      const signed = correcting ? (-direction * amount) : (direction * amount);
      window.scrollBy({ top: signed, behavior: 'auto' });
      travelled += amount;
      setTimeout(tick, nextDelay());
    }
    tick();
  });
}
"""

# Social interaction scripts — find and click engagement buttons
FIND_LIKE_BUTTON_SCRIPT = r"""
(postIndex) => {
  const likeSelectors = [
    '[data-testid="like"]',
    '[aria-label*="like" i]:not([aria-pressed="true"])',
    '[aria-label*="heart" i]:not([aria-pressed="true"])',
    '[aria-label*="love" i]:not([aria-pressed="true"])',
    'button[class*="like"]:not([class*="liked"])',
    'button[class*="heart"]',
    '[role="button"][class*="like"]',
  ];
  const allButtons = [];
  for (const sel of likeSelectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        allButtons.push({
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
        });
      }
    }
  }
  const target = allButtons[postIndex] || allButtons[0] || null;
  return target;
}
"""

FIND_FOLLOW_BUTTON_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="followButton"]',
    '[aria-label*="follow" i]:not([aria-label*="unfollow" i])',
    'button:not([class*="unfollow"]):not([class*="following"])',
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
      if (!text.includes('follow')) continue;
      if (text.includes('unfollow') || text.includes('following')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        return {
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80),
        };
      }
    }
  }
  return null;
}
"""

FIND_UNFOLLOW_BUTTON_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="unfollow"]',
    '[aria-label*="following" i]',
    '[aria-label*="unfollow" i]',
    'button[class*="following"]',
    'button[class*="unfollow"]',
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
      if (!text.includes('following') && !text.includes('unfollow')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        return {
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80),
        };
      }
    }
  }
  return null;
}
"""

FIND_REPLY_BUTTON_SCRIPT = r"""
(postIndex) => {
  const selectors = [
    '[data-testid="reply"]',
    '[data-testid="comment"]',
    '[aria-label*="reply" i]',
    '[aria-label*="comment" i]',
    'button[class*="reply"]',
    'button[class*="comment"]',
  ];
  const buttons = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      buttons.push({
        selector: sel,
        x: Math.round(rect.x + rect.width / 2),
        y: Math.round(rect.y + rect.height / 2),
        label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
      });
    }
  }
  return buttons[postIndex] || buttons[0] || null;
}
"""

FIND_REPOST_BUTTON_SCRIPT = r"""
(postIndex) => {
  const selectors = [
    '[data-testid="retweet"]',
    '[data-testid="repost"]',
    '[aria-label*="retweet" i]',
    '[aria-label*="repost" i]',
    '[aria-label*="share post" i]',
    'button[class*="retweet"]',
    'button[class*="repost"]',
  ];
  const buttons = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      buttons.push({
        selector: sel,
        x: Math.round(rect.x + rect.width / 2),
        y: Math.round(rect.y + rect.height / 2),
        label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
      });
    }
  }
  return buttons[postIndex] || buttons[0] || null;
}
"""

FIND_SEARCH_INPUT_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="SearchBox_Search_Input"]',
    'input[type="search"]',
    'input[name="q"]',
    '[aria-label*="search" i]',
    '[placeholder*="search" i]',
    'input[role="searchbox"]',
    '[role="searchbox"]',
    'input[type="text"][name*="search"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0) return sel;
    }
  }
  return null;
}
"""

EXTRACT_COMMENTS_SCRIPT = r"""
(limit) => {
  const out = [];
  const seen = new Set();
  const selectors = [
    'article',
    '[role="article"]',
    '[data-testid*="reply"]',
    '[data-testid*="comment"]',
    '[class*="comment"]',
    '[class*="reply"]',
    'li'
  ];
  for (const el of document.querySelectorAll(selectors.join(','))) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (text.length < 12) continue;
    const signature = text.slice(0, 120);
    if (seen.has(signature)) continue;
    seen.add(signature);
    const author = el.querySelector('[data-testid="User-Name"], [class*="author"], strong, h3, h4')?.textContent || null;
    out.push({
      author: author ? author.replace(/\s+/g, ' ').trim().slice(0, 120) : null,
      text: text.slice(0, 800),
      y_position: Math.round(rect.top + window.scrollY),
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

FIND_ACCESSIBLE_TARGET_SCRIPT = r"""
(options) => {
  const keywords = (options?.keywords || []).map((item) => String(item).toLowerCase());
  const excludeKeywords = (options?.excludeKeywords || []).map((item) => String(item).toLowerCase());
  const index = Number.isInteger(options?.index) ? options.index : 0;
  const requireTextbox = Boolean(options?.requireTextbox);
  const selector = options?.selector || [
    'button',
    'a',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
  ].join(',');

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };

  const isTextbox = (el) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    return (
      el.isContentEditable ||
      el.getAttribute('contenteditable') === 'true' ||
      el.tagName === 'TEXTAREA' ||
      (el.tagName === 'INPUT' && !['checkbox', 'radio', 'submit', 'button'].includes((el.type || '').toLowerCase())) ||
      role === 'textbox'
    );
  };

  const describe = (el) => {
    return [
      el.getAttribute('aria-label'),
      el.getAttribute('title'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.innerText,
      el.textContent,
      el.value,
    ]
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
  };

  const matches = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    if (requireTextbox && !isTextbox(el)) continue;
    const description = describe(el);
    if (keywords.length && !keywords.some((keyword) => description.includes(keyword))) continue;
    if (excludeKeywords.some((keyword) => description.includes(keyword))) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    matches.push({
      selector: `[data-operator-id="${el.dataset.operatorId}"]`,
      element_id: el.dataset.operatorId,
      x: Math.round(rect.x + rect.width / 2),
      y: Math.round(rect.y + rect.height / 2),
      label: description.slice(0, 160),
      tag: el.tagName.toLowerCase(),
    });
  }
  return matches[index] || matches[0] || null;
}
"""


# Visible form-validation state -- what GET /sessions/{id}/diagnostics reports
# alongside console errors and failed requests. Deliberately narrow: aria-invalid
# fields, role=alert banners, error-shaped classes (only when they actually hold
# text and are visible -- most sites keep an empty error <div> in the DOM at all
# times), and native :invalid fields with their browser-computed validationMessage.
VALIDATION_MESSAGES_SCRIPT = r"""
(limit) => {
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function text(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  }
  const out = [];
  const seen = new Set();
  function push(kind, el, message) {
    if (seen.has(el) || out.length >= limit) return;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    out.push({
      kind,
      tag: el.tagName.toLowerCase(),
      message: message || text(el),
      name: el.getAttribute('name') || el.id || null,
      visible: isVisible(el),
      bbox: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height),
      },
    });
  }

  for (const el of document.querySelectorAll('[aria-invalid="true"]')) {
    if (out.length >= limit) break;
    push('aria-invalid', el, null);
  }
  for (const el of document.querySelectorAll('[role="alert"]')) {
    if (out.length >= limit) break;
    const t = text(el);
    if (t) push('role-alert', el, t);
  }
  const errorClassRe = /(^|[-_ ])(error|invalid)([-_ ]|$)|invalid-feedback|field-error|form-error|validation-message/i;
  for (const el of document.querySelectorAll('[class]')) {
    if (out.length >= limit) break;
    if (typeof el.className !== 'string' || !errorClassRe.test(el.className)) continue;
    if (!isVisible(el)) continue;
    const t = text(el);
    if (t) push('error-class', el, t);
  }
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (out.length >= limit) break;
    try {
      if (el.matches(':invalid') && el.validationMessage) push('invalid-field', el, el.validationMessage);
    } catch (e) { /* :invalid unsupported on this element -- skip it */ }
  }
  return out;
}
"""

# Read-only CSS/text query for browser_dom_query -- the CSS selector and the
# text filter are plain strings passed as arguments, never interpolated into
# the script, so an employee can point at any element without ever getting a
# JS-eval channel into the page.
DOM_QUERY_SCRIPT = r"""
(args) => {
  const css = args.css || null;
  const needle = (args.text || '').toLowerCase();
  const limit = args.limit || 50;
  const editorTextLimit = Number.isInteger(args.editor_text_limit)
    ? Math.max(1, Math.min(args.editor_text_limit, 4096))
    : null;
  function isPainted(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && rect.right > 0 && rect.bottom > 0
      && rect.left < window.innerWidth && rect.top < window.innerHeight
      && style.visibility !== 'hidden' && style.display !== 'none'
      && style.opacity !== '0' && !el.closest('[aria-hidden="true"]');
  }
  function isVisible(el) {
    if (!isPainted(el)) return false;
    const rect = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && (hit === el || el.contains(hit)));
  }
  function activeDialog() {
    const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')]
      .filter(isVisible);
    if (!dialogs.length) return null;
    const leaves = dialogs.filter((candidate) =>
      !dialogs.some((other) => other !== candidate && candidate.contains(other))
    );
    const candidates = leaves.length ? leaves : dialogs;
    const depth = (el) => {
      let value = 0;
      for (let node = el; node && node !== document.body; node = node.parentElement) value += 1;
      return value;
    };
    candidates.sort((left, right) => {
      const depthDelta = depth(left) - depth(right);
      if (depthDelta) return depthDelta;
      const leftZ = Number.parseInt(window.getComputedStyle(left).zIndex, 10) || 0;
      const rightZ = Number.parseInt(window.getComputedStyle(right).zIndex, 10) || 0;
      if (leftZ !== rightZ) return leftZ - rightZ;
      return left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
    return candidates[candidates.length - 1];
  }
  function buttonValue(el) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    return el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(type) ? el.value : '';
  }
  function accessibleName(el) {
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id)?.innerText || document.getElementById(id)?.textContent || '')
      .join(' ');
    const associatedLabels = el.labels
      ? [...el.labels].map((label) => label.innerText || label.textContent || '').join(' ')
      : '';
    const raw = el.getAttribute('aria-label') || labelledBy || associatedLabels
      || el.getAttribute('placeholder') || el.innerText || buttonValue(el)
      || el.getAttribute('name') || el.id || el.href || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  }
  function sensitiveEditor(el) {
    const inputType = (el.getAttribute('type') || '').trim().toLowerCase();
    const autocomplete = (el.getAttribute('autocomplete') || '').trim().toLowerCase();
    if (inputType === 'password') return true;
    if (['current-password', 'new-password', 'one-time-code'].includes(autocomplete)) return true;
    const attributes = [
      inputType,
      autocomplete,
      el.getAttribute('name'),
      el.id,
      el.getAttribute('placeholder'),
      el.getAttribute('aria-label'),
    ].filter(Boolean).join(' ').toLowerCase();
    return /password|passcode|otp|one[- ]time|verification|token|secret|2fa|mfa/.test(attributes);
  }
  function trustedEditorText(el, visible) {
    if (editorTextLimit === null) return null;
    const base = {
      available: false,
      text: null,
      character_count: null,
      max_chars: editorTextLimit,
      complete: false,
      truncated: false,
    };
    if (!visible || el.closest('[aria-hidden="true"]')) {
      return {...base, reason: 'not_visible'};
    }
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role !== 'textbox' || !el.isContentEditable) {
      return {...base, reason: 'not_trusted_editor'};
    }
    if (sensitiveEditor(el)) {
      return {...base, reason: 'sensitive_editor'};
    }
    const raw = String(el.innerText ?? el.textContent ?? '');
    // Array.from counts Unicode code points and never cuts a surrogate pair in
    // half, unlike String.length/slice.  This matches the controller's bounded
    // character contract for Arabic text and emoji as well as ASCII.
    const characters = Array.from(raw);
    const truncated = characters.length > editorTextLimit;
    return {
      available: true,
      text: characters.slice(0, editorTextLimit).join(''),
      character_count: characters.length,
      max_chars: editorTextLimit,
      complete: !truncated,
      truncated,
    };
  }
  let nodes;
  try {
    nodes = Array.from(document.querySelectorAll(css || '*'));
  } catch (e) {
    return { error: 'invalid_selector', items: [], total_matched: 0 };
  }
  const total_matched = nodes.length;
  const dialog = activeDialog();
  // A huge page must not be scanned node-by-node in full -- cap the scan,
  // not just the result, before the text filter runs.
  nodes = nodes.slice(0, 2000);
  const out = [];
  for (const el of nodes) {
    if (out.length >= limit) break;
    // The opt-in editor read must not accidentally carry a password/OTP value
    // through the legacy summary field beside the protected editor_text field.
    const hideSensitiveEditorText = editorTextLimit !== null && sensitiveEditor(el);
    const t = hideSensitiveEditorText
      ? ''
      : (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (needle && !t.toLowerCase().includes(needle)) continue;
    const rect = el.getBoundingClientRect();
    const visible = isVisible(el);
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const item = {
      element_id: el.dataset.operatorId,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || null,
      label: accessibleName(el),
      text: t.slice(0, 200),
      id: el.id || null,
      name: el.getAttribute('name') || null,
      class_name: typeof el.className === 'string' ? el.className.slice(0, 200) : null,
      type: el.getAttribute('type') || null,
      href: el.getAttribute('href') || null,
      aria_label: el.getAttribute('aria-label') || null,
      visible,
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      checked: ['checkbox', 'radio'].includes((el.getAttribute('type') || '').toLowerCase())
        ? Boolean(el.checked)
        : (el.hasAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : null),
      in_active_dialog: Boolean(dialog && (dialog === el || dialog.contains(el))),
      dialog_label: dialog ? accessibleName(dialog) : null,
      bbox: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height),
      },
    };
    const editorText = trustedEditorText(el, visible);
    if (editorText !== null) item.editor_text = editorText;
    out.push(item);
  }
  return { error: null, items: out, total_matched };
}
"""


async def apply_stealth(page: object) -> None:
    """Inject stealth init script into a page before any navigation."""
    add_init_script = getattr(page, "add_init_script", None)
    if callable(add_init_script):
        await add_init_script(STEALTH_INIT_SCRIPT)

