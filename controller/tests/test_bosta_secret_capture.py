"""Real Chromium fixture: unknown key shape never reaches public browser surfaces."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from app.artifacts import SessionArtifactService
from app.browser.secret_pages import BOSTA_URL, protect_secret_page
from app.browser.services.observation import BrowserObservationService

KEY = "synthetic_" + "z8Q2-" * 12


@pytest.fixture
async def surface(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.tracing.start(screenshots=True, snapshots=True)
        page = await context.new_page()
        await page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
        await page.goto(BOSTA_URL)
        session = SimpleNamespace(page=page, context=context, lock=asyncio.Lock(), trace_recording=True,
                                  trace_path=tmp_path / "trace.zip", id="bosta-test", artifact_dir=tmp_path)
        manager = SimpleNamespace(get_session=AsyncMock(return_value=session),
                                  _session_summary=AsyncMock(return_value={"id": session.id}))
        service = BrowserObservationService(manager)
        yield session, service
        await context.close()
        await browser.close()


async def test_unique_labelled_input_and_code_are_captured_privately(surface):
    session, service = surface
    for html in (f'<label for="api">API Key</label><input id="api" value="{KEY}">',
                 f'<section><h3>API Key</h3><code>{KEY}</code></section>'):
        await session.page.set_content(html)
        result = await service.find_api_keys("s", "bosta")
        assert result["keys"] == [KEY] and result["privacy"] == "protected"
        assert not session.trace_recording
        assert not session.trace_path.exists()
    prepared = await service.find_api_keys("s", "bosta", prepare_only=True)
    assert prepared["status"] == "ready" and prepared["keys"] == []
    assert KEY not in json.dumps(prepared)


@pytest.mark.parametrize("html", [
    f'<p>API Key {KEY}</p>',
    f'<input name="password" value="{KEY}">',
    f"<iframe srcdoc='<input aria-label=\"API key\" value=\"{KEY}\">'></iframe>",
    f'<input aria-label="API key" value="{KEY}"><code aria-label="API key">{KEY}</code>',
    '<input aria-label="API key" value="**********************">',
])
async def test_unlabelled_iframe_masked_or_ambiguous_source_never_captured(surface, html):
    session, service = surface
    await session.page.set_content(html)
    assert (await service.find_api_keys("s", "bosta"))["keys"] == []


@pytest.mark.parametrize("url", ["http://business.bosta.co/settings/api-integration",
    "https://business.bosta.co.attacker.test/settings/api-integration",
    "https://business.bosta.co/settings/other", BOSTA_URL + "?key=hidden", BOSTA_URL + "#key"])
async def test_source_is_exact_and_never_read_on_mismatch(surface, url):
    session, service = surface
    await session.page.goto(url)
    await session.page.set_content(f'<input aria-label="API key" value="{KEY}">')
    result = await service.find_api_keys("s", "bosta")
    assert result["status"] == "source_refused" and result["keys"] == []


async def test_public_surfaces_exclude_unknown_secret_and_remain_usable_after_reveal(surface):
    session, service = surface
    await protect_secret_page(session)
    await session.page.set_content(f'<h1>{KEY}</h1><input aria-label="API Key" value="{KEY}">'
                                   f'<button>Show API key</button><button>{KEY}</button>')
    for payload in (await service._snapshot_payload(session), await service.observation_payload(session),
                    await service.light_snapshot(session, label="after-click"),
                    await service._dom_query_locked(session, css="*", text=None, limit=50, editor_text_limit=None),
                    await service.capture_screenshot("s")):
        assert KEY not in json.dumps(payload)
    snapshot = await service._snapshot_payload(session)
    assert [c["label"] for c in snapshot["controls"]] == ["show api key"]
    artifacts = SessionArtifactService(session.artifact_dir)
    assert await artifacts.capture_screenshot(session, "secret") == {"path": None, "url": None}
    assert not list(Path(session.artifact_dir).glob("*.png"))
    assert not artifacts.trace_payload(session)["trace_exists"]


async def test_trace_stop_failure_prevents_read_or_action():
    context = SimpleNamespace(pages=[], tracing=SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError(KEY))))
    page = SimpleNamespace(url=BOSTA_URL, evaluate=AsyncMock())
    session = SimpleNamespace(context=context, page=page, trace_recording=True)
    with pytest.raises(RuntimeError, match="^secret_page_trace_protection_failed$"):
        await protect_secret_page(session)
    page.evaluate.assert_not_called()
    # Failed preparation cannot be bypassed by the sticky privacy flag.
    with pytest.raises(RuntimeError, match="^secret_page_trace_protection_failed$"):
        await protect_secret_page(session)
    assert context.tracing.stop.await_count == 1


async def test_actual_reveal_click_returns_sanitized_success_and_records_no_secret(surface):
    from app.actions.pipeline import BrowserActionPipeline
    from app.browser.services.actions import BrowserActionService

    session, service = surface
    await session.page.set_content(f'<input aria-label="API Key" value="{KEY}" type="password">'
        '<button id="reveal" onclick="document.querySelector(\'input\').type=\'text\'">Show API key</button>'
        '<a href="https://business.bosta.co/settings">Settings</a>'
        f'<a href="{BOSTA_URL}?key={KEY}">API Key</a>'
        f'<a href="https://example.com/{KEY}">API Integration</a>')
    artifacts = SessionArtifactService(session.artifact_dir)
    manager = service.manager
    manager._consume_witness_context = lambda session: {}
    manager._witness_action_class = lambda *a, **kw: "write"
    manager._witness_session_context = lambda session: {}
    manager._build_witness_action_context = lambda **kw: {}
    manager.witness_policy = SimpleNamespace(evaluate_action=lambda **kw: SimpleNamespace(should_block=False, require_approval=False))
    manager._light_snapshot = service.light_snapshot
    manager._observation_payload = service.observation_payload
    manager._ensure_witness_remote_ready = AsyncMock()
    manager._assert_runtime_url_allowed = lambda url: None
    manager._action_class = lambda action: "write"
    manager._action_verification = lambda *args: {"verified": False, "signals": []}
    manager._append_jsonl = artifacts.append_jsonl
    manager._record_witness_receipt = AsyncMock()
    manager._persist_session = AsyncMock()
    manager.audit = SimpleNamespace(append=AsyncMock())
    snapshot = await service._snapshot_payload(session)
    assert [c["href"] for c in snapshot["controls"]] == ["", "https://business.bosta.co/settings"]
    assert KEY not in json.dumps(snapshot)
    control = snapshot["controls"][0]
    uid = control["element_id"]
    assert re.fullmatch(r"op-[A-Za-z0-9]{4,40}", uid)
    assert control["role"] == "button" and control["type"] in {"button", "submit"}
    assert control["href"] == "" and control["visible"] is True and control["disabled"] is False
    # Locally exercise the actual unchanged Social consumer in a fresh process;
    # no duplicate UID resolver or fingerprint implementation in this test.
    contract_root = os.environ.get("SOCIAL_OPERATOR_CONTRACT_ROOT")
    if contract_root:
        contract_python = os.environ["SOCIAL_OPERATOR_CONTRACT_PYTHON"]
        accepted = subprocess.run([contract_python, "-c", """
import json,sys
from app.operator.agent import tools
from app.operator.agent.tools_browser import _trusted_named_control
from app.browser.client import operator_id_of, resolve_uid, control_identity
observation=json.load(sys.stdin)
uid=observation['interactables'][0]['element_id']
assert operator_id_of(uid)==uid
assert resolve_uid(observation,uid)==uid
assert control_identity(observation,uid,tool='browser.click',exact=True)['role']=='button'
assert _trusted_named_control(observation,uid,tool='browser.click',exact=True) is not None
print('social-control-contract:accepted')
"""], input=json.dumps(snapshot), text=True, capture_output=True, cwd=contract_root, timeout=30)
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.strip() == "social-control-contract:accepted"
    manager.action_pipeline = BrowserActionPipeline()
    manager._settle = AsyncMock()
    actions = BrowserActionService(manager)
    manager._run_action = actions.run_action
    # Exercise the real controller UID action path, including pointer dispatch.
    result = await actions.click("s", element_id=uid, pace="fast")
    assert await session.page.locator("input").get_attribute("type") == "text"
    assert result["action"] == "click" and result["after"]["privacy"] == "protected"
    assert KEY not in json.dumps(result)
    assert KEY not in (session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8")
    assert KEY not in str(manager.audit.append.call_args_list) + str(manager._record_witness_receipt.call_args_list)
    assert not session.trace_path.exists() and not list(session.artifact_dir.glob("*.png"))


async def test_diagnostics_and_network_never_collect_unknown_secret(surface):
    from app.browser.services.diagnostics import BrowserDiagnosticsService
    from app.network_inspector import NetworkInspector

    session, service = surface
    await protect_secret_page(session)
    diagnostics = BrowserDiagnosticsService(service.manager, None, None)
    session.console_messages, session.page_errors, session.request_failures = [], [], []
    diagnostics._on_console(session, session.page, SimpleNamespace(type="error", text=KEY, location={}))
    diagnostics._on_pageerror(session, session.page, RuntimeError(KEY))
    diagnostics._on_request_failed(session, session.page, SimpleNamespace(url=KEY))
    assert not session.console_messages and not session.page_errors and not session.request_failures
    for result in (await diagnostics.get_diagnostics("s"), await diagnostics.get_network_log("s"),
                   await diagnostics.get_console_messages("s"), await diagnostics.get_page_errors("s"),
                   await diagnostics.get_request_failures("s"), await diagnostics.screenshot_diff("s")):
        assert result["status"] == "sensitive_page" and KEY not in json.dumps(result)
    inspector = NetworkInspector("s")
    inspector._page = session.page
    await inspector._handle_request(SimpleNamespace(url=KEY, post_data=KEY))
    assert inspector.entries() == [] and not inspector._pending


async def test_other_sites_keep_normal_navigation_while_context_trace_stays_discarded(surface):
    session, service = surface
    await protect_secret_page(session)
    await session.page.goto("https://example.com/ordinary")
    await session.page.set_content("<p>ordinary site text</p>")
    snapshot = await service._snapshot_payload(session)
    assert snapshot["text"] == "ordinary site text"
    assert "privacy" not in snapshot
    assert not session.trace_recording and not session.trace_path.exists()


async def test_signup_address_fields_and_choices_keep_actual_url_without_values(surface):
    session, service = surface
    for path in ("/signup", "/settings/addresses"):
        await session.page.goto("https://business.bosta.co" + path)
        await session.page.set_content(
            f'<label for="name">Full name</label><input id="name" value="{KEY}">'
            f'<label for="password">Password</label><input id="password" type="password" value="{KEY}">'
            f'<label for="otp">Verification code</label><input id="otp" value="{KEY}">'
            '<label for="address">Address</label><input id="address">'
            '<label for="city">City</label><select id="city"><option>Cairo</option><option>Giza</option></select>'
            f'<input aria-label="API key" value="{KEY}"><p>{KEY}</p><button>Save address</button>'
        )
        snapshot = await service._snapshot_payload(session)
        assert snapshot["url"] == "https://business.bosta.co" + path
        controls = {item["label"]: item for item in snapshot["controls"]}
        assert {"full name", "password", "verification code", "address", "city", "save address"} <= controls.keys()
        assert controls["password"]["type"] == "password"
        assert controls["city"]["options"] == ["Cairo", "Giza"]
        assert all(re.fullmatch(r"op-[A-Za-z0-9]{4,40}", row["element_id"]) for row in controls.values())
        assert KEY not in json.dumps(snapshot)
        assert not session.trace_recording and not session.trace_path.exists()


async def test_actual_key_form_metadata_and_arabic_readwrite_choice_are_visible(surface):
    session, service = surface
    await session.page.set_content(
        '<label for="createEditKeyForm_name">اسم المفتاح</label><input id="createEditKeyForm_name">'
        '<label for="createEditKeyForm_apiGroupName">الصلاحية</label>'
        '<input id="createEditKeyForm_apiGroupName" role="combobox" type="search">'
        '<div role="option">قراءة و كتابة</div><div role="option">Full</div>'
        '<button>إنشاء</button><button>طلب رمز التحقق</button>'
    )
    snapshot = await service._snapshot_payload(session)
    labels = {item["label"] for item in snapshot["controls"]}
    assert {"key name", "permission", "قراءة و كتابة", "إنشاء", "طلب رمز التحقق"} <= labels
    assert "full" not in labels


async def test_creation_preflight_refuses_full_scope_without_reading_key(surface):
    session, service = surface
    for choice, expected in (("Full", "scope_required"), ("قراءة و كتابة", "ready")):
        await session.page.set_content('<div role="dialog"><input id="createEditKeyForm_name"><div class="ant-select">'
            f'<span class="ant-select-selection-item">{choice}</span>'
            '<input id="createEditKeyForm_apiGroupName" role="combobox" type="search"></div><button>إنشاء</button></div>'
            f'<input aria-label="API key" value="{KEY}">')
        snapshot = await service._snapshot_payload(session)
        assert snapshot["bosta_key_creation"]["scope"] == ("full" if choice == "Full" else "read_write")
        assert len(snapshot["bosta_key_creation"]["confirm_uids"]) == 1
        result = await service.find_api_keys("s", "bosta", prepare_only=True)
        assert result["status"] == expected and result["keys"] == []
        assert KEY not in json.dumps(result)


@pytest.mark.parametrize("invalid", [False, True], ids=["observed-dialog", "extra-field"])
async def test_one_time_dialog_capture_has_exact_structural_provenance(surface, invalid):
    session, service = surface
    await session.page.set_content(
        '<div role="dialog"><button class="ant-modal-close"></button>'
        f'<input type="text" value="{KEY}"><button>نسخ</button><button>تم</button>'
        + ('<input type="text">' if invalid else '') + '</div>'
    )
    prepared = await service.find_api_keys("s", "bosta", prepare_only=True)
    assert prepared["status"] == ("scope_required" if invalid else "ready") and prepared["keys"] == []
    result = await service.find_api_keys("s", "bosta")
    if invalid:
        assert result["keys"] == []
    else:
        assert result["keys"] == [KEY] and result["source"] == "bosta_one_time_key_dialog"
    assert KEY not in json.dumps(await service._snapshot_payload(session))


async def test_unknown_visible_api_modal_is_unverified_without_secret_payload(surface):
    session, service = surface
    await session.page.set_content(
        '<input id="createEditKeyForm_apiGroupName" type="hidden">'
        f'<div role="dialog"><label for="otp">Verification code</label><input id="otp" value="{KEY}">'
        f'<p>{KEY}</p><button>Verify</button></div>'
    )
    snapshot = await service._snapshot_payload(session)
    assert snapshot["bosta_key_creation"] == {"state": "unverified", "scope": None, "confirm_uids": []}
    prepared = await service.find_api_keys("s", "bosta", prepare_only=True)
    assert prepared["status"] == "scope_required" and prepared["keys"] == []
    assert KEY not in json.dumps(snapshot) + json.dumps(prepared)


async def test_observed_arabic_onboarding_labels_keep_uids_without_input_values(surface):
    session, service = surface
    await session.page.goto("https://business.bosta.co/settings/addresses")
    fields = [
        ("اسم العنوان", "address name"), ("العنوان بالتفصيل *", "address"),
        ("اسم/رقم المبنى\nاختياري", "building number"), ("رقم الدور\nاختياري", "floor"),
        ("رقم الشقة\nاختياري", "apartment"), ("علامة مميزة\nاختياري", "landmark"),
        ("الاسم بالكامل", "full name"), ("رقم التليفون", "phone"),
        ("اسم شركتك", "company name"), ("رابط الشركة", "website"),
    ]
    buttons = ["إضافة مكان جديد", "إضافة موقع الاستلام", "إنشاء عنوان جديد", "تعديل المكان",
               "تحديد الموقع", "المحافظة والمنطقة", "ابدأ شحن الان!", "طلب OTP"]
    html = ''
    for i, (label, _) in enumerate(fields):
        field_id = f"createEditBusinessLocationForm_address_field{i}"
        # The provider renders the optional decoration in its own visual line;
        # textContent concatenates it directly to the field label.
        rendered_label = label.replace('\nاختياري', '<span style="display:block">اختياري</span>')
        html += f'<div class="ant-form-item"><label for="{field_id}">{rendered_label}</label>'
        html += f'<input id="{field_id}" value="{KEY}"></div>'
    html += ''.join(f'<button>{label}</button>' for label in buttons)
    html += (f'<label><input type="radio" name="volume" value="{KEY}" checked>أقل من 100 شحنة</label>'
             f'<label><input type="radio" name="service" value="{KEY}">الشحن فقط</label>'
             '<label for="country">Country</label><select id="country"><option>مصر</option></select>'
             '<div class="ant-form-item"><label>المحافظة والمنطقة</label>'
             f'<button id="createEditBusinessLocationForm_address_districtId">{KEY}</button></div>'
             f'<input aria-label="API key" value="{KEY}"><button>{KEY}</button>')
    await session.page.set_content(html)
    snapshot = await service._snapshot_payload(session)
    controls = {item["label"]: item for item in snapshot["controls"]}
    assert {canonical for _, canonical in fields} <= controls.keys()
    assert {label.lower() for label in buttons} <= controls.keys()
    assert controls["أقل من 100 شحنة"]["role"] == "radio"
    assert controls["أقل من 100 شحنة"]["checked"] is True
    assert controls["الشحن فقط"]["role"] == "radio"
    assert controls["country"]["options"] == ["مصر"]
    assert controls["المحافظة والمنطقة"]["tag"] == "button"
    assert all(re.fullmatch(r"op-[A-Za-z0-9]{4,40}", item["element_id"]) for item in controls.values())
    assert KEY not in json.dumps(snapshot)


async def test_observed_region_dialog_projects_component_choices_and_disabled_districts(surface):
    session, service = surface
    await session.page.goto("https://business.bosta.co/settings/addresses")
    region = (
        '<div role="dialog"><h2 class="ant-modal-title">اختار المنطقة</h2>'
        f'<input type="text" placeholder="اكتب منطقتك “مدينة نصر”" value="{KEY}">'
        '<div role="list"><div class="br-cities-collapse__item-header"><span>القاهرة</span></div>'
        '<div class="br-cities-collapse__district-items-container"><div role="list">'
        '<div class="br-cities-collapse__district-item"><span>مدينة نصر</span></div>'
        '<div class="br-cities-collapse__district-item br-cities-collapse__district-item-disabled">'
        '<span>منطقة بعيدة</span><div class="br-cities-collapse__district-item-tags">خارج التغطية</div></div>'
        '</div></div></div></div>'
    )
    await session.page.set_content(region + f'<div class="br-cities-collapse__district-item"><span>{KEY}</span></div>')
    snapshot = await service._snapshot_payload(session)
    controls = {item["label"]: item for item in snapshot["controls"]}
    assert controls["ابحث عن المنطقة"]["role"] == "textbox"
    assert controls["القاهرة"]["role"] == "button"
    assert controls["مدينة نصر"]["role"] == "option" and controls["مدينة نصر"]["disabled"] is False
    assert controls["منطقة بعيدة"]["role"] == "option" and controls["منطقة بعيدة"]["disabled"] is True
    assert not any("خارج التغطية" in label for label in controls)
    assert KEY not in json.dumps(snapshot)
    for url, html in (("https://business.bosta.co/settings/addresses", region.replace("اختار المنطقة", "Other modal")),
                      (BOSTA_URL, region)):
        await session.page.goto(url)
        await session.page.set_content(html)
        labels = {item["label"] for item in (await service._snapshot_payload(session))["controls"]}
        assert "القاهرة" not in labels and "مدينة نصر" not in labels and "منطقة بعيدة" not in labels


def _packaging_catalog():
    return (
        f'<p>{KEY}</p><input type="hidden" value="{KEY}">'
        '<button role="tab">المتجر</button><button role="tab">صناديق</button>'
        '<button>عرض العربة (0) 0 ج.م</button>'
        '<div class="br-item-card__item-container">'
        '<div class="br-item-card__item-content br-item-card__item-content-clickable">'
        '<div class="br-item-card__item-info"><div class="br-item-card__item-details">'
        '<div class="br-item-card__item-details-title"><div class="br-item-card__item-details-title-section">'
        '<span>صندوق صغير</span></div></div><div class="br-item-card__item-size">20 × 10 سم</div>'
        '<div class="br-item-card__item-limit caption">حد الطلب 10 قطع</div></div></div>'
        '<div class="br-item-card__item-pricing"><span class="body-medium">3.5 ج.م</span>'
        '<span class="br-item-card__item-action-info-pack caption">10 قطعة</span></div></div>'
        '<div class="br-item-card__item-action"><div class="br-item-card__icon-changer">'
        '<button class="br-list-item__action"></button></div></div></div>'
    )


async def test_packaging_catalog_exposes_real_card_and_cart_only_controls_privately(surface):
    session, service = surface
    await session.page.goto("https://business.bosta.co/shop/packaging")
    await session.page.set_content(_packaging_catalog())
    await session.page.locator('.br-item-card__item-container').evaluate("""card => {
      const flyer = card.cloneNode(true);
      flyer.querySelector('.br-item-card__item-content').classList.remove('br-item-card__item-content-clickable');
      flyer.querySelector('.br-item-card__item-details-title-section > span').textContent = 'فلاير 25x30';
      card.after(flyer);
    }""")
    snapshot = await service._snapshot_payload(session)
    catalog = snapshot["bosta_packaging"]["catalog"]
    assert len(catalog) == 2
    assert catalog[0]["name"] == "صندوق صغير" and catalog[0]["price_text"] == "3.5 ج.م"
    assert catalog[0]["pack_unit"] == "10 قطعة" and catalog[0]["limit"] == "حد الطلب 10 قطع"
    assert await session.page.locator(f'[data-operator-id="{catalog[0]["uid"]}"]').get_attribute("class") == "br-item-card__item-content br-item-card__item-content-clickable"
    assert await session.page.locator(f'[data-operator-id="{catalog[0]["add_uid"]}"]').get_attribute("class") == "br-list-item__action"
    assert catalog[1]["name"] == "فلاير 25x30" and catalog[1]["price_text"] == "3.5 ج.م"
    assert "uid" not in catalog[1] and catalog[1]["add_uid"]
    assert await session.page.locator(f'[data-operator-id="{catalog[1]["add_uid"]}"]').get_attribute("class") == "br-list-item__action"
    await session.page.locator(f'[data-operator-id="{catalog[0]["add_uid"]}"]').focus()
    focused = await service._snapshot_payload(session)
    assert focused["active_element"]["uid"] == catalog[0]["add_uid"]
    assert any(row["label"] == "صناديق" for row in snapshot["controls"])
    assert snapshot["bosta_packaging"]["cart"]["shipping_text"] is None
    assert snapshot["bosta_packaging"]["cart"]["final_total_text"] is None
    assert KEY not in json.dumps(snapshot)
    assert snapshot["privacy"] == "protected" and snapshot["screenshot_path"] is None
    assert not session.trace_recording and not session.trace_path.exists()


async def test_packaging_cart_reads_shipping_total_and_omits_checkout_and_ambiguous_quantity(surface):
    session, service = surface
    await session.page.goto("https://business.bosta.co/shop/packaging")
    cart = (
        '<div class="ant-drawer"><div role="dialog"><div class="ant-drawer-header">'
        '<div class="ant-drawer-title">عربة التسوق</div><div class="ant-drawer-extra">'
        '<span class="anticon anticon-close" role="img" aria-label="close" style="display:inline-block;width:12px;height:12px"></span>'
        '</div></div>'
        '<div class="ant-drawer-body"><div class="br-basket-item__container">'
        '<div class="br-basket-item__description-section"><span>صندوق صغير</span></div>'
        '<span class="br-basket-item__size">20 × 10 سم</span><span class="br-basket-item__limit">10 قطعة</span>'
        '<span class="br-basket-item__action-price">3.5 ج.م</span>'
        '<div class="br-basket-item__action-controller"><button class="button-disabled"></button>'
        '<input class="ant-input-number-input" value="1"><button></button></div>'
        '<button class="br-basket-item__action-delete"></button></div>'
        '<div class="br-flyers-shop__cart-checkout__amount-cost">'
        '<span class="br-flyers-shop__cart-checkout__amount-cost-currency">3.5 ج.م</span> +55 ج.م شحن</div>'
        '<div class="br-flyers-shop__total_price-checkout-title">58.5 ج.م</div>'
        '<button class="br-flyers-shop__checkout-button">تأكيدالأوردر</button></div></div></div>'
    )
    await session.page.set_content(_packaging_catalog() + cart)
    snapshot = await service._snapshot_payload(session)
    result = snapshot["bosta_packaging"]["cart"]
    assert result["subtotal_text"] == "3.5 ج.م"
    assert result["shipping_text"] == "+55 ج.م شحن" and result["final_total_text"] == "58.5 ج.م"
    item = result["items"][0]
    assert item["name"] == "صندوق صغير" and item["quantity"] == 1
    controls = {row["uid"]: row for row in snapshot["controls"]}
    closes = [row for row in controls.values() if row["label"] == "إغلاق السلة"]
    assert len(closes) == 1 and closes[0]["role"] == "button" and closes[0]["tag"] == "span"
    assert await session.page.locator(f'[data-operator-id="{closes[0]["uid"]}"]').get_attribute("aria-label") == "close"
    assert controls[item["minus_uid"]]["disabled"] is True
    assert controls[item["plus_uid"]]["disabled"] is False
    assert item["remove_uid"] in controls
    assert await session.page.locator('.br-flyers-shop__checkout-button').get_attribute('data-operator-id') is None
    assert await session.page.locator('input.ant-input-number-input').get_attribute('data-operator-id') is None
    assert KEY not in json.dumps(snapshot)
    await session.page.locator('.br-flyers-shop__checkout-button').focus()
    checkout = await service._snapshot_payload(session)
    assert checkout["active_element"] == {"role": "button", "type": "button", "label": "Pay now"}
    assert "uid" not in checkout["active_element"] and "element_id" not in checkout["active_element"]
    await session.page.locator('input.ant-input-number-input').focus()
    assert (await service._snapshot_payload(session))["active_element"] is None
    await session.page.locator('.br-basket-item__action-controller').evaluate("el => el.append(document.createElement('button'))")
    ambiguous = (await service._snapshot_payload(session))["bosta_packaging"]["cart"]["items"][0]
    assert "plus_uid" not in ambiguous and "minus_uid" not in ambiguous


async def test_packaging_projection_never_falls_back_to_body_on_other_routes_or_unknown_modals(surface):
    session, service = surface
    for url in (BOSTA_URL, 'https://business.bosta.co/signup', 'https://business.bosta.co/shop/packaging?token=hidden'):
        await session.page.goto(url)
        await session.page.set_content(_packaging_catalog())
        snapshot = await service._snapshot_payload(session)
        assert snapshot["bosta_packaging"] is None and snapshot["text"] == ""
        assert KEY not in json.dumps(snapshot)
    await session.page.goto('https://business.bosta.co/shop/packaging')
    for extra in (f'<div role="dialog"><h2>Unknown</h2><input value="{KEY}"></div>',
                  f'<input type="password" value="{KEY}">'):
        await session.page.set_content(_packaging_catalog() + extra)
        snapshot = await service._snapshot_payload(session)
        assert snapshot["bosta_packaging"] is None and snapshot["text"] == ""
        assert KEY not in json.dumps(snapshot)
