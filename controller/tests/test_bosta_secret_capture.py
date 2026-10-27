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
