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


def bosta_packaging_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.netloc == "business.bosta.co"
                and parts.path == "/shop/packaging" and not parts.query and not parts.fragment)
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
  const allowed = /^(?:api integration|api key|api keys|show|show api key|reveal|reveal api key|copy|copy api key|create|generate|create api key|generate api key|generate new api key|readwrite|read write|read only|readonly|request otp|send otp|resend otp|verify|settings|integrations|close|cancel|confirm|continue|login|log in|sign in|sign up|signup|register|next|back|save|save address|add address|create new address|add pickup location|create account|business information|pickup locations|addresses|submit|إنشاء حساب|تسجيل الدخول|التالي|السابق|حفظ|تأكيد|إلغاء|إضافة عنوان|نسخ|تم|قراءة و كتابة|إنشاء|انشاء|متابعة|تحقق|إعدادات|عنوان جديد|إرسال رمز التحقق|طلب رمز التحقق|طلب OTP|إضافة مكان جديد|إضافة موقع الاستلام|إنشاء عنوان جديد|تعديل المكان|تحديد الموقع|المحافظة والمنطقة|أقل من 100 شحنة|الشحن فقط|ابدأ شحن الان!|مصر)$/i;
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
    ['اسم شركتك','Company name'], ['رابط الشركة','Website'], ['اسم العنوان','Address name'],
    ['رقم الموبايل','Phone'], ['رقم التليفون','Phone'], ['الموقع الإلكتروني','Website'],
    ['العنوان بالتفصيل','Address'], ['رقم المبنى','Building number'], ['الشارع','Street'], ['اسم الشارع','Street'],
    ['اسم/رقم المبنى','Building number'], ['رقم الدور','Floor'], ['رقم الشقة','Apartment'],
    ['الدور','Floor'], ['الشقة','Apartment'], ['علامة مميزة','Landmark'],
    ['أقل من 100 شحنة','أقل من 100 شحنة'], ['الشحن فقط','الشحن فقط'], ['مصر','مصر'],
    ['كلمة المرور','Password'], ['كود التحقق','Verification code'], ['رمز التحقق','Verification code']
  ]);
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
    && !el.closest('[aria-hidden="true"]');
  const normalize = text => (text || '').replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/[_:*]/g, ' ').replace(/اختياري\s*$/g, ' ')
    .replace(/\s+/g, ' ').trim().toLowerCase();
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
  const regionDialogs = apiPage ? [] : [...document.querySelectorAll('[role="dialog"],dialog[open],[aria-modal="true"]')]
    .filter(dialog => visible(dialog) && (
      dialog.querySelector('.ant-modal-title,[role="heading"],h1,h2,h3,h4,h5,h6')?.textContent ||
      (dialog.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(id => document.getElementById(id)?.textContent || '').join(' ')
    ).replace(/\s+/g, ' ').trim() === 'اختار المنطقة');
  const regionDialog = regionDialogs.length === 1 ? regionDialogs[0] : null;
  const selector = 'button,a,input,textarea,select,[role="button"],[role="combobox"],[role="radio"],[role="checkbox"],[role="option"],.ant-select-item-option,.br-cities-collapse__item-header,.br-cities-collapse__district-item';
  return [...(regionDialog || document).querySelectorAll(selector)].flatMap(el => {
    if (!visible(el)) return [];
    const regionHeader = !!regionDialog && el.matches('.br-cities-collapse__item-header') && !!el.closest('[role="list"]');
    const regionLeaf = !!regionDialog && el.matches('.br-cities-collapse__district-items-container [role="list"] > div.br-cities-collapse__district-item')
      && el.firstElementChild?.tagName === 'SPAN';
    const regionSearch = !!regionDialog && el.matches('input[type="text"]')
      && /^اكتب منطقتك\s+["“«]مدينة نصر["”»]$/.test((el.getAttribute('placeholder') || '').trim());
    const districtButton = !apiPage && el.tagName === 'BUTTON'
      && el.id === 'createEditBusinessLocationForm_address_districtId'
      && normalize(el.closest('.ant-form-item')?.querySelector('label')?.textContent) === 'المحافظة والمنطقة';
    const field = el.matches('input,textarea,select,[role="combobox"]');
    const option = regionLeaf || el.matches('[role="option"],.ant-select-item-option');
    let label = districtButton ? 'المحافظة والمنطقة' : regionSearch ? 'ابحث عن المنطقة'
      : regionLeaf ? optionText(el.firstElementChild) : regionHeader ? optionText(el)
      : field ? labelFor(el) : (el.getAttribute('aria-label') || el.innerText || '').trim();
    if ((regionHeader || regionLeaf) && !safeOption(label)) return [];
    if (option && !apiPage && !regionLeaf) {
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
    } else if (!regionHeader && !(option && !apiPage) && !allowed.test(label)) return [];
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
      role: link ? 'link' : (option ? 'option' : el.getAttribute('role') || (field ? (el.tagName === 'SELECT' ? 'combobox' : ['radio','checkbox'].includes(el.type) ? el.type : 'textbox') : 'button')),
      type: field ? (label === 'Password' ? 'password' : el.type || 'text') : (link ? '' : (['button','submit','reset'].includes(el.type) ? el.type : 'button')),
      checked: ['radio','checkbox'].includes(el.type) ? el.checked === true : null,
      options: el.tagName === 'SELECT' ? [...el.options].map(optionText).filter(safeOption) : [],
      href, visible: true, disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true'
        || (regionLeaf && el.classList.contains('br-cities-collapse__district-item-disabled'))}];
  }).slice(0, 50);
}"""

# A permission choice is public metadata, unlike credential input values. This
# inspects only the provider's observed creation-form control and emits an enum.
BOSTA_PACKAGING_SCRIPT = r"""() => {
  if (location.href !== 'https://business.bosta.co/shop/packaging') return null;
  const visible = el => !!el && el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[aria-hidden="true"]');
  const text = el => (el?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const read = (root, selector) => {
    const nodes = [...root.querySelectorAll(selector)].filter(visible);
    return nodes.length === 1 ? text(nodes[0]) : '';
  };
  const drawers = [...document.querySelectorAll('.ant-drawer')].filter(drawer => {
    const body = drawer.querySelector('.ant-drawer-body');
    if (!visible(body)) return false;
    const rect = body.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0
      && rect.left < innerWidth && rect.top < innerHeight;
  });
  if (drawers.length > 1 || (drawers.length === 1 && read(drawers[0], '.ant-drawer-title') !== 'عربة التسوق')) return null;
  const drawer = drawers[0] || null;
  const modals = [...document.querySelectorAll('[role="dialog"],dialog[open],[aria-modal="true"]')].filter(visible);
  if (modals.some(modal => !drawer || !drawer.contains(modal))) return null;
  if ([...document.querySelectorAll('input[type="password"],input[autocomplete="one-time-code"],input[name*="otp" i],input[id*="otp" i],input[aria-label*="api key" i]')].some(visible)) return null;
  const controls = [];
  const control = (el, label, role = 'button', disabled = false) => {
    if (!visible(el) || el.closest('.br-flyers-shop__checkout-button')) return null;
    if (!/^op-[A-Za-z0-9]{4,40}$/.test(el.dataset.operatorId || ''))
      el.dataset.operatorId = 'op-' + Math.random().toString(36).slice(2, 12);
    let href = '';
    if (el.tagName === 'A') {
      let destination;
      try { destination = new URL(el.getAttribute('href') || '', location.href); } catch (_) { return null; }
      if (destination.origin !== 'https://business.bosta.co' || destination.search || destination.hash
          || !/^\/(?:[a-z][a-z-]*\/?)*$/.test(destination.pathname)) return null;
      href = destination.href;
    }
    controls.push({element_id:el.dataset.operatorId, uid:el.dataset.operatorId,
      selector_hint:'[data-operator-id="' + el.dataset.operatorId + '"]', tag:el.tagName.toLowerCase(),
      label, role:el.tagName === 'A' ? 'link' : role, type:'button', href, visible:true,
      disabled:disabled || el.disabled === true || el.getAttribute('aria-disabled') === 'true'});
    return el.dataset.operatorId;
  };
  const catalog = [];
  for (const card of document.querySelectorAll('div.br-item-card__item-container')) {
    const contents = [...card.querySelectorAll(':scope > div.br-item-card__item-content')].filter(visible);
    if (contents.length !== 1) continue;
    const content = contents[0];
    const name = read(content, '.br-item-card__item-info > .br-item-card__item-details > .br-item-card__item-details-title > .br-item-card__item-details-title-section > span');
    const price = read(content, '.br-item-card__item-pricing > span.body-medium');
    if (!name || !price) continue;
    const item = {name, price_text:price, size:read(content,'.br-item-card__item-size'),
      limit:read(content,'.br-item-card__item-limit.caption'),
      pack_unit:read(content,'.br-item-card__item-pricing > span.br-item-card__item-action-info-pack.caption')};
    if (!drawer) {
      if (content.classList.contains('br-item-card__item-content-clickable'))
        item.uid = control(content, 'عرض مواد التغليف: ' + name + ' — ' + price);
      const adds = [...card.querySelectorAll(':scope > div.br-item-card__item-action > div.br-item-card__icon-changer > button.br-list-item__action')].filter(visible);
      if (adds.length === 1) item.add_uid = control(adds[0], 'إضافة إلى السلة: ' + name + ' — ' + price);
    }
    catalog.push(item);
  }
  const tabs = new Set(['المتجر','تتبع الطلبات','الكل','فلايرات','صناديق','ملصقات','أخرى']);
  let cartSummary = '';
  if (!drawer) for (const el of document.querySelectorAll('button,a,[role="button"],[role="tab"]')) {
    if (!visible(el)) continue;
    const label = text(el);
    if (tabs.has(label)) control(el,label,el.getAttribute('role') === 'tab' ? 'tab' : 'button');
    else if (/^عرض العربة\s*\([0-9٠-٩]+\)\s*[0-9٠-٩.,]+\s*ج\.?\s*م\.?$/.test(label)) {
      cartSummary = label;
      control(el,label);
    }
  }
  let cart = {state:'closed', visible_summary:cartSummary || null, shipping_text:null, final_total_text:null};
  if (drawer) {
    const closes = [...drawer.querySelectorAll('.ant-drawer-header > .ant-drawer-extra span.anticon.anticon-close[role="img"][aria-label="close"]')].filter(visible);
    if (closes.length === 1) control(closes[0],'إغلاق السلة');
    const items = [];
    for (const row of drawer.querySelectorAll('.ant-drawer-body .br-basket-item__container')) {
      if (!visible(row)) continue;
      const descriptions = [...row.querySelectorAll('.br-basket-item__description-section')].filter(visible);
      if (descriptions.length !== 1) continue;
      const nameLeaf = [descriptions[0], ...descriptions[0].querySelectorAll('span,p,div,h1,h2,h3,h4,h5,h6')]
        .find(el => visible(el) && el.childElementCount === 0 && text(el));
      const name = text(nameLeaf);
      const price = read(row,'.br-basket-item__action-price');
      if (!name || !price) continue;
      const item = {name, price_text:price, size:read(row,'.br-basket-item__size'), limit:read(row,'.br-basket-item__limit')};
      const controllers = [...row.querySelectorAll('.br-basket-item__action-controller')].filter(visible);
      if (controllers.length === 1) {
        const buttons = [...controllers[0].querySelectorAll('button')].filter(visible);
        const inputs = [...controllers[0].querySelectorAll('input.ant-input-number-input')].filter(visible);
        if (buttons.length === 2 && inputs.length === 1 && /^[0-9]{1,6}$/.test(inputs[0].value || '')) {
          item.quantity = Number(inputs[0].value);
          item.minus_uid = control(buttons[0],'تقليل الكمية: ' + name,'button',buttons[0].classList.contains('button-disabled'));
          item.plus_uid = control(buttons[1],'زيادة الكمية: ' + name,'button',buttons[1].classList.contains('button-disabled'));
          // Quantity is read as bounded numeric metadata. Keyboard editing is
          // not offered until the checkout/form-submit relationship is proven.
        }
      }
      const removes = [...row.querySelectorAll('.br-basket-item__action-delete')].filter(visible);
      if (removes.length === 1) item.remove_uid = control(removes[0],'حذف من السلة: ' + name);
      items.push(item);
    }
    const costs = [...drawer.querySelectorAll('.br-flyers-shop__cart-checkout__amount-cost')].filter(visible);
    let shipping = null;
    if (costs.length === 1) {
      const clone = costs[0].cloneNode(true);
      clone.querySelectorAll('.br-flyers-shop__cart-checkout__amount-cost-currency').forEach(el => el.remove());
      shipping = text(clone) || null;
    }
    cart = {state:'open', items, subtotal_text:read(drawer,'.br-flyers-shop__cart-checkout__amount-cost-currency') || null,
      shipping_text:shipping, final_total_text:read(drawer,'.br-flyers-shop__total_price-checkout-title') || null};
  }
  if (!catalog.length && !drawer) return null;
  const focused = document.activeElement;
  let active_element = null;
  if (focused?.closest('.br-flyers-shop__checkout-button')) {
    // No checkout UID is offered. This fixed semantic lets the existing
    // payment guard refuse keyboard activation of a focused purchase button.
    active_element = {role:'button',type:'button',label:'Pay now'};
  } else {
    const known = controls.find(item => item.element_id === focused?.dataset?.operatorId);
    if (known) active_element = {...known};
  }
  return {controls, catalog, cart, active_element, text:JSON.stringify({catalog,cart})};
}"""

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
