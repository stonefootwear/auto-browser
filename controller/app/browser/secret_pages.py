"""Credential pages whose unknown key format requires surface-level privacy.

Privacy is sticky for the context: tracing is context-wide, never restarted/exported.
Only fixed action/field labels and non-secret choices survive observations. Private
extraction reads one labelled credential or the observed one-time-dialog structure
on an exact origin/path, never the clipboard or arbitrary text.
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


def safe_bosta_url(url: str) -> str:
    """Expose the current non-secret location, never query/fragment payloads."""
    try:
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.netloc == "business.bosta.co":
            # Bosta's application routes contain names, not credential values.
            import re

            if re.fullmatch(r"/(?:[a-z][a-z-]*/?)*", parts.path):
                return "https://business.bosta.co" + parts.path
    except (ValueError, TypeError):
        pass
    return "https://business.bosta.co/"


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
  const apiPage = location.pathname === '/settings/api-integration';
  const allowed = /^(?:api integration|api key|api keys|show|show api key|reveal|reveal api key|copy|copy api key|create|generate|create api key|generate api key|generate new api key|readwrite|read write|read only|readonly|request otp|send otp|resend otp|verify|settings|integrations|close|cancel|confirm|continue|login|log in|sign in|sign up|signup|register|next|back|save|save address|add address|create new address|add pickup location|create account|business information|pickup locations|addresses|submit|إنشاء حساب|تسجيل الدخول|التالي|السابق|حفظ|تأكيد|إلغاء|إضافة عنوان|نسخ|تم|قراءة و كتابة|إنشاء|انشاء|متابعة|تحقق|إعدادات|عنوان جديد|إرسال رمز التحقق|طلب رمز التحقق)$/i;
  const fields = new Map([
    ['full name','Full name'], ['fullname','Full name'], ['name','Name'],
    ['first name','First name'], ['firstname','First name'], ['last name','Last name'], ['lastname','Last name'],
    ['business name','Business name'], ['businessname','Business name'], ['company name','Company name'], ['companyname','Company name'],
    ['email','Email'], ['email address','Email'], ['phone','Phone'], ['phone number','Phone'], ['mobile','Phone'], ['mobile number','Phone'],
    ['address','Address'], ['address name','Address name'], ['location name','Location name'], ['pickup address','Pickup address'],
    ['street','Street'], ['street name','Street'], ['building','Building'], ['building number','Building number'],
    ['floor','Floor'], ['apartment','Apartment'], ['landmark','Landmark'], ['postal code','Postal code'],
    ['city','City'], ['district','District'], ['zone','Zone'], ['area','Area'], ['region','Region'], ['governorate','Governorate'], ['country','Country'],
    ['business type','Business type'], ['industry','Industry'], ['website','Website'],
    ['password','Password'], ['confirm password','Confirm password'], ['verification code','Verification code'], ['otp','Verification code'],
    ['key name','Key name'], ['api key name','Key name'],
    ['create edit key form name','Key name'], ['create edit key form api group name','Permission'],
    ['اسم المفتاح','Key name'], ['الصلاحية','Permission'],
    ['الاسم','Name'], ['الاسم بالكامل','Full name'], ['اسم الشركة','Company name'], ['البريد الإلكتروني','Email'],
    ['رقم الهاتف','Phone'], ['العنوان','Address'], ['المحافظة','Governorate'], ['المدينة','City'], ['المنطقة','Area'],
    ['اسم النشاط التجاري','Business name'], ['نوع النشاط التجاري','Business type'], ['اسم المتجر','Business name'],
    ['رقم الموبايل','Phone'], ['رقم التليفون','Phone'], ['الموقع الإلكتروني','Website'],
    ['العنوان بالتفصيل','Address'], ['رقم المبنى','Building number'], ['الشارع','Street'], ['اسم الشارع','Street'],
    ['الدور','Floor'], ['الشقة','Apartment'], ['علامة مميزة','Landmark'],
    ['كلمة المرور','Password'], ['كود التحقق','Verification code'], ['رمز التحقق','Verification code']
  ]);
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
    && !el.closest('[aria-hidden="true"]');
  const normalize = text => (text || '').replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/[_:*]/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
  const labelFor = el => {
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)?.textContent || '').join(' ');
    const labels = [...(el.labels || [])].map(node => node.textContent || '').join(' ');
    const nearby = el.closest('.ant-form-item, .form-group, .form-field')?.querySelector('label')?.textContent;
    return [el.getAttribute('aria-label'), labelled, labels, nearby, el.getAttribute('placeholder'), el.name, el.id]
      .map(normalize).map(name => fields.get(name)).find(Boolean) || '';
  };
  const optionText = node => (node.textContent || '').replace(/\s+/g, ' ').trim();
  const safeOption = text => text.length > 0 && text.length <= 100 && /^[\p{L}\p{N} .,'()\/-]+$/u.test(text);
  const selector = 'button,a,input,textarea,select,[role="button"],[role="combobox"],[role="radio"],[role="checkbox"],[role="option"],.ant-select-item-option';
  return [...document.querySelectorAll(selector)].flatMap(el => {
    if (!visible(el)) return [];
    const field = el.matches('input,textarea,select,[role="combobox"]');
    const option = el.matches('[role="option"],.ant-select-item-option');
    let label = field ? labelFor(el) : (el.getAttribute('aria-label') || el.innerText || '').trim();
    if (option && !apiPage) {
      // Only expose choices belonging to an already named non-secret combobox.
      const box = el.closest('[role="listbox"],.ant-select-dropdown');
      const owned = box && [...document.querySelectorAll('[role="combobox"],select')].some(control =>
        labelFor(control) && (control.getAttribute('aria-controls') || control.getAttribute('aria-owns') || '')
          .split(/\s+/).some(id => id && (box.id === id || box.querySelector('[id="' + CSS.escape(id) + '"]'))));
      if (!owned || !safeOption(label)) return [];
    }
    if (field) {
      // Unknown and credential fields have no public surface. The owner's secure
      // handoff sees only a fixed password/code label, never any entered value.
      if (!label || (apiPage && !['Key name','Permission','Password','Verification code'].includes(label))) return [];
    } else if (!(option && !apiPage) && !allowed.test(label)) return [];
    const link = el.tagName === 'A';
    let href = '';
    if (link) {
      // Keep only known non-secret destinations; no arbitrary path/query/fragment.
      let destination;
      try { destination = new URL(el.getAttribute('href') || '', location.href); } catch (_) { return []; }
      if (destination.origin !== 'https://business.bosta.co' || destination.search || destination.hash
          || !/^\/(?:[a-z][a-z-]*\/?)*$/.test(destination.pathname)) return [];
      href = destination.href;
    }
    if (!/^op-[A-Za-z0-9]{4,40}$/.test(el.dataset.operatorId || ''))
      el.dataset.operatorId = 'op-' + Math.random().toString(36).slice(2, 12);
    return [{element_id: el.dataset.operatorId, uid: el.dataset.operatorId,
      selector_hint: '[data-operator-id="' + el.dataset.operatorId + '"]',
      tag: el.tagName.toLowerCase(), label: label.toLowerCase(),
      role: link ? 'link' : (option ? 'option' : el.getAttribute('role') || (field ? (el.tagName === 'SELECT' ? 'combobox' : 'textbox') : 'button')),
      type: field ? (label === 'Password' ? 'password' : el.type || 'text') : (link ? '' : (['button','submit','reset'].includes(el.type) ? el.type : 'button')),
      options: el.tagName === 'SELECT' ? [...el.options].map(optionText).filter(safeOption) : [],
      href, visible: true, disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true'}];
  }).slice(0, 50);
}"""

# A permission choice is public metadata, unlike credential input values. This
# inspects only the provider's observed creation-form control and emits an enum.
BOSTA_CREATION_SCOPE_SCRIPT = r"""() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"],dialog[open],[aria-modal="true"]')]
    .filter(el => el.getClientRects().length);
  const field = document.getElementById('createEditKeyForm_apiGroupName');
  if (!field || !field.getClientRects().length) {
    if (!dialogs.length) return {state:'not_present', scope:null, confirm_uids:[]};
    // The already-created one-time result is safe to prepare without reading
    // its input. Unknown/OTP modals are never mistaken for an ordinary page.
    if (dialogs.length === 1) {
      const dialog = dialogs[0];
      const buttons = [...dialog.querySelectorAll('button,[role="button"]')].filter(el => el.getClientRects().length);
      const inputs = [...dialog.querySelectorAll('input,textarea,[contenteditable="true"]')].filter(el => el.getClientRects().length);
      if (inputs.length === 1 && inputs[0].matches('input[type="text"]') && !inputs[0].id
          && !inputs[0].name && !inputs[0].getAttribute('aria-label')
          && buttons.filter(el => (el.innerText || '').trim() === 'نسخ').length === 1
          && buttons.filter(el => (el.innerText || '').trim() === 'تم').length === 1)
        return {state:'key_result', scope:null, confirm_uids:[]};
    }
    return {state:'unverified', scope:null, confirm_uids:[]};
  }
  if (dialogs.length !== 1 || !dialogs[0].contains(field)
      || !dialogs[0].querySelector('#createEditKeyForm_name'))
    return {state:'unverified', scope:null, confirm_uids:[]};
  const selected = field.closest('.ant-select')?.querySelector('.ant-select-selection-item');
  const text = (selected?.textContent || '').replace(/\s+/g, ' ').trim();
  const scope = /^(?:قراءة و كتابة|readwrite|read write)$/i.test(text) ? 'read_write'
    : /^full$/i.test(text) ? 'full' : 'required';
  const confirms = [...dialogs[0].querySelectorAll('button,[role="button"]')].filter(el =>
    el.getClientRects().length && /^(?:create|generate|confirm|save|create api key|generate api key|generate new api key|إنشاء|انشاء|تأكيد|حفظ|إنشاء مفتاح API)$/i
      .test((el.getAttribute('aria-label') || el.innerText || '').trim()));
  const confirm_uids = confirms.map(el => {
    if (!/^op-[A-Za-z0-9]{4,40}$/.test(el.dataset.operatorId || ''))
      el.dataset.operatorId = 'op-' + Math.random().toString(36).slice(2, 12);
    return el.dataset.operatorId;
  });
  return {state:'creation_form', scope, confirm_uids};
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
  let source = 'semantic_api_key_element';
  // Bosta's one-time creation modal has an unlabelled text input beside Copy.
  // Trust the observed Copy/Done structure only inside one visible dialog; never
  // a page-wide input, clipboard, unrelated dialog or arbitrary body text.
  if (!fields.length) {
    const dialogs = [...document.querySelectorAll('[role="dialog"],dialog[open],[aria-modal="true"]')]
      .filter(el => el.getClientRects().length);
    if (dialogs.length === 1) {
      const dialog = dialogs[0];
      const copy = [...dialog.querySelectorAll('button,[role="button"]')]
        .filter(el => el.getClientRects().length && (el.innerText || '').trim() === 'نسخ');
      const done = [...dialog.querySelectorAll('button,[role="button"]')]
        .filter(el => el.getClientRects().length && (el.innerText || '').trim() === 'تم');
      const inputs = [...dialog.querySelectorAll('input,textarea,[contenteditable="true"]')]
        .filter(el => el.getClientRects().length);
      if (copy.length === 1 && done.length === 1 && inputs.length === 1
          && inputs[0].matches('input[type="text"]') && !inputs[0].id
          && !inputs[0].name && !inputs[0].getAttribute('aria-label')) {
        fields.push(inputs[0]);
        source = 'bosta_one_time_key_dialog';
      }
    }
  }
  if (fields.length !== 1) return {status: fields.length ? 'ambiguous' : 'not_found', keys: []};
  const el = fields[0];
  const key = (el.matches('input,textarea') ? el.value : el.textContent || '').trim();
  // Shape is unknown: trust exact semantic source, reject masked/control/whitespace values.
  if (key.length < 16 || key.length > 4096 || /[\s\x00-\x20\x7f*\u2022\u2026]/.test(key) || key.includes('...'))
    return {status: 'not_found', keys: []};
  return {status: 'found', keys: [key], source};
}"""
