"""Credential pages whose unknown key format requires surface-level privacy.

Privacy is sticky for the context: tracing is context-wide, never restarted/exported.
Only fixed action labels survive observations. Private extraction reads one labelled
credential element on an exact origin/path, never the clipboard or arbitrary text.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .tab_view import unwrap_session

BOSTA_URL = "https://business.bosta.co/settings/api-integration"


def secret_origin(url: str) -> bool:
    try:
        return urlsplit(url).hostname == "business.bosta.co"
    except (ValueError, TypeError):
        return False


def bosta_capture_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.netloc == "business.bosta.co"
                and parts.path == "/settings/api-integration" and not parts.query
                and not parts.fragment)
    except (ValueError, TypeError):
        return False


def private_session(session: Any) -> bool:
    return secret_origin(getattr(session.page, "url", ""))


async def protect_secret_page(session: Any, *, destination: str = "") -> bool:
    real = unwrap_session(session)
    pages = getattr(getattr(real, "context", None), "pages", ())
    if not isinstance(pages, (list, tuple)):
        pages = ()
    needed = (getattr(real, "secret_page_mode", False) is True
              or private_session(session) or secret_origin(destination)
              or any(secret_origin(getattr(page, "url", "")) for page in pages))
    if not needed:
        return False
    # Set synchronously before awaiting: listeners must drop events immediately.
    real.secret_page_mode = True
    task = getattr(real, "secret_trace_stop_task", None)
    if task is None:
        async def discard() -> None:
            # No path => Playwright discards the trace, including DOM/screenshots.
            # Stop even if local metadata is stale after a controller reattach.
            try:
                await real.context.tracing.stop()
            except Exception as exc:
                if "tracing is not started" not in str(exc).lower():
                    raise RuntimeError("secret_page_trace_protection_failed") from None
            real.trace_recording = False
            trace_path = getattr(real, "trace_path", None)
            if trace_path is not None:
                trace_path.unlink(missing_ok=True)
            inspector = getattr(real, "network_inspector", None)
            if inspector is not None:
                inspector._secret_page = True
                inspector.clear()
            for name in ("console_messages", "page_errors", "request_failures", "downloads", "dialog_log"):
                items = getattr(real, name, None)
                if isinstance(items, list):
                    items.clear()
            buffers = getattr(real, "tab_diagnostics", {})
            if isinstance(buffers, dict):
                buffers.clear()
        task = asyncio.create_task(discard())
        real.secret_trace_stop_task = task
    await asyncio.shield(task)
    return private_session(session) or secret_origin(destination)


# Return only a closed vocabulary of labels, never arbitrary DOM text/values.
SAFE_CONTROLS_SCRIPT = r"""() => {
  const allowed = /^(?:api integration|api key|api keys|show|show api key|reveal|reveal api key|copy|copy api key|create api key|generate api key|generate new api key|settings|integrations|close|cancel|confirm|continue|login|log in|sign in|next)$/i;
  return [...document.querySelectorAll('button, a, [role="button"]')].flatMap(el => {
    if (!el.getClientRects().length) return [];
    const label = (el.getAttribute('aria-label') || el.innerText || '').trim();
    if (!allowed.test(label)) return [];
    const link = el.tagName === 'A';
    let href = '';
    if (link) {
      // Keep only known non-secret destinations; no arbitrary path/query/fragment.
      let destination;
      try { destination = new URL(el.getAttribute('href') || '', location.href); } catch (_) { return []; }
      if (!['https://business.bosta.co/settings',
             'https://business.bosta.co/settings/api-integration'].includes(destination.href)) return [];
      href = destination.href;
    }
    if (!/^op-[A-Za-z0-9]{4,40}$/.test(el.dataset.operatorId || ''))
      el.dataset.operatorId = 'op-' + Math.random().toString(36).slice(2, 12);
    return [{element_id: el.dataset.operatorId, uid: el.dataset.operatorId,
      selector_hint: '[data-operator-id="' + el.dataset.operatorId + '"]',
      tag: el.tagName.toLowerCase(), label: label.toLowerCase(),
      role: link ? 'link' : 'button',
      type: link ? '' : (['button','submit','reset'].includes(el.type) ? el.type : 'button'),
      href, visible: true, disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true'}];
  }).slice(0, 50);
}"""

BOSTA_KEY_SCRIPT = r"""() => {
  if (location.href !== 'https://business.bosta.co/settings/api-integration')
    return {status: 'source_refused', keys: []};
  const semantic = value => /^(?:bosta)?apikey$/i.test((value || '').replace(/[\s_:-]/g, ''));
  const labelled = el => {
    if (['name', 'id', 'aria-label', 'placeholder', 'data-testid'].some(a => semantic(el.getAttribute(a)))) return true;
    if ([...(el.labels || [])].some(l => semantic(l.textContent))) return true;
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (ids.some(id => semantic(document.getElementById(id)?.textContent))) return true;
    // A code block can be grouped with a sibling label/heading, never body text.
    return [...(el.parentElement?.children || [])].some(sibling =>
      sibling !== el && sibling.matches('label,legend,h1,h2,h3,h4,h5,h6') && semantic(sibling.textContent));
  };
  const fields = [...document.querySelectorAll('input, textarea, code')]
    .filter(el => el.getClientRects().length && labelled(el));
  if (fields.length !== 1) return {status: fields.length ? 'ambiguous' : 'not_found', keys: []};
  const el = fields[0];
  const key = (el.matches('input,textarea') ? el.value : el.textContent || '').trim();
  // Shape is unknown: trust exact semantic source, reject masked/control/whitespace values.
  if (key.length < 16 || key.length > 4096 || /[\s\x00-\x20\x7f*\u2022\u2026]/.test(key) || key.includes('...'))
    return {status: 'not_found', keys: []};
  return {status: 'found', keys: [key]};
}"""
