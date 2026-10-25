from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from playwright.async_api import async_playwright

from app.browser_scripts import INTERACTABLES_SCRIPT
from app.utils import UTC


async def test_real_chromium_devtools_reads_stay_in_the_employee_tab_and_eval_is_projected() -> None:
    from app.browser.tab_view import bind_tab, set_tab_owner
    from app.browser_manager import BrowserManager, BrowserSession
    from app.config import Settings
    from app.middleware.tab_scope import TabScopeMiddleware
    from app.routes.session_diagnostics import create_session_diagnostics_router
    from app.routes.sessions import create_sessions_router

    with tempfile.TemporaryDirectory(prefix="devtools-eval-") as folder:
        root = Path(folder)
        settings = Settings(
            _env_file=None,
            WITNESS_ENABLED=False,
            ENABLE_TRACING=False,
            **{
                name: str(root / name.lower())
                for name in (
                    "ARTIFACT_ROOT",
                    "AUTH_ROOT",
                    "UPLOAD_ROOT",
                    "APPROVAL_ROOT",
                    "AUDIT_ROOT",
                    "WITNESS_ROOT",
                    "SESSION_STORE_ROOT",
                )
            },
        )
        manager = BrowserManager(settings)
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - environment dependent
                pytest.skip(f"chromium cannot launch here: {exc}")
            try:
                context = await browser.new_context()
                owner_page = await context.new_page()
                await owner_page.goto(
                    "data:text/html,<title>Owner secret tab</title><p>owner-only-value</p>"
                )
                employee_page = await context.new_page()
                await employee_page.goto(
                    "data:text/html,<title>Employee tab</title><p>employee-visible</p>"
                )
                await employee_page.evaluate("""() => {
                    const video = document.createElement('video');
                    video.id = 'rendered-preview';
                    video.dataset.operatorId = 'op-media123';
                    video.controls = true;
                    Object.defineProperty(video, 'duration', {value: Infinity});
                    Object.defineProperty(video, 'currentTime', {value: 1.25});
                    document.body.append(video, document.createElement('audio'));
                }""")
                artifact_dir = root / "artifacts" / "real-eval"
                artifact_dir.mkdir(parents=True)
                session = BrowserSession(
                    id="real-eval",
                    name="real-eval",
                    created_at=datetime.now(UTC),
                    context=context,
                    page=owner_page,
                    browser=browser,
                    artifact_dir=artifact_dir,
                    auth_dir=root / "auth",
                    upload_dir=root / "uploads",
                    takeover_url="",
                    trace_path=artifact_dir / "trace.zip",
                )
                session.driver_epoch = manager._driver_epoch
                manager.sessions[session.id] = session
                employee_tab = "t-0123456789ab"
                bind_tab(session, employee_page, employee_tab)
                set_tab_owner(session, employee_tab, "emad")
                manager._attach_page_listeners(owner_page, session)
                manager._attach_page_listeners(employee_page, session)

                app = FastAPI()
                app.add_middleware(TabScopeMiddleware)
                app.include_router(create_sessions_router(manager=manager))
                app.include_router(create_session_diagnostics_router(manager=manager, settings=settings))
                headers = {"X-Tab-Id": employee_tab}
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://controller"
                ) as client:
                    snapshot = await client.get(f"/sessions/{session.id}/snapshot", headers=headers)
                    assert snapshot.status_code == 200
                    assert snapshot.json()["url"] == employee_page.url
                    assert "employee-visible" in snapshot.json()["text"]
                    assert "owner-only-value" not in str(snapshot.json())
                    assert snapshot.json()["media"] == [
                        {
                            "kind": "video", "element_id": "op-media123", "id": "rendered-preview",
                            "paused": True, "ended": False, "current_time": 1.25,
                            "duration": None, "ready_state": 0, "video_width": 0,
                            "video_height": 0, "controls": True,
                        },
                        {
                            "kind": "audio", "element_id": None, "id": None,
                            "paused": True, "ended": False, "current_time": 0,
                            "duration": None, "ready_state": 0, "video_width": None,
                            "video_height": None, "controls": False,
                        },
                    ]
                    assert "media" not in str(snapshot.json()["controls"])

                    evaluated = await client.post(
                        f"/sessions/{session.id}/actions/evaluate-script",
                        headers=headers,
                        json={"expression": "document.readyState"},
                    )
                    assert evaluated.status_code == 200
                    assert evaluated.json() == "complete"

                    refused = await client.post(
                        f"/sessions/{session.id}/actions/evaluate-script",
                        headers=headers,
                        json={"expression": "document.querySelector('p').textContent.slice(0, 3)"},
                    )
                    assert refused.status_code == 400
                    assert "trusted browser diagnostic projections" in refused.json()["detail"]

                    waited = await client.post(
                        f"/sessions/{session.id}/actions/wait-for",
                        headers=headers,
                        json={"text": "employee-visible", "timeout_ms": 1000},
                    )
                    assert waited.status_code == 200
                    assert waited.json()["url"] == employee_page.url

                    class SyntheticRequest:
                        method = "GET"
                        resource_type = "fetch"

                        def __init__(self, url: str) -> None:
                            self.url = url

                    network_urls = [
                        ("https://user:FixtureOnly@example.test/items?code=FixtureOnly#fragment",
                         "https://example.test/items"),
                        ("http://user:FixtureOnly@example.test:8080/items?code=FixtureOnly",
                         "http://example.test:8080/items"),
                        ("https://[2001:db8::1]/items#FixtureOnly", "https://[2001:db8::1]/items"),
                        ("data:text/html,FixtureOnly", ""),
                        ("file:///FixtureOnly", ""),
                        ("https://example.test:invalid/FixtureOnly", ""),
                        ("https://exa mple.test/FixtureOnly", ""),
                        ("https://[invalid/FixtureOnly", ""),
                    ]
                    for url, _expected in network_urls:
                        request = SyntheticRequest(url)
                        manager.diagnostics._on_request(session, employee_page, request)
                        await manager.diagnostics._on_response(
                            session, employee_page,
                            SimpleNamespace(request=request, url=url, status=204),
                            manager.diagnostics._navigation_ids[employee_page],
                            manager.diagnostics._request_scopes[request][2],
                        )
                    for order in ((0, 1), (1, 0)):
                        requests = [SyntheticRequest(f"https://example.test/concurrent?code=FixtureOnly{index}")
                                    for index in range(2)]
                        for request in requests:
                            manager.diagnostics._on_request(session, employee_page, request)
                        pending = []
                        with patch("app.browser.services.diagnostics.spawn_background_task", side_effect=pending.append):
                            for index in order:
                                request = requests[index]
                                manager.diagnostics._schedule_response(
                                    session, employee_page,
                                    SimpleNamespace(request=request, url=request.url, status=(200, 503)[index], headers={}),
                                )
                                manager.diagnostics._request_scopes.pop(request)
                        for response_task in pending:
                            await response_task
                    network = await client.get(
                        f"/sessions/{session.id}/network-log", headers=headers,
                    )
                    assert network.status_code == 200
                    assert network.json()["session"]["current_url"] == employee_page.url
                    assert "owner-only-value" not in str(network.json())
                    entries = network.json()["entries"]
                    assert [(entry["url"], entry["status"]) for entry in entries[:4]] == [
                        ("https://example.test/concurrent", 503), ("https://example.test/concurrent", 200),
                    ] * 2
                    assert [entry["url"] for entry in entries[4:]] == [expected for _url, expected in reversed(network_urls)]
                    assert all(entry["status"] == 204 for entry in entries[4:])
                    assert "FixtureOnly" not in str(entries)

            finally:
                await browser.close()


async def test_real_chromium_snapshot_projects_navigation_and_effective_form_submit() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''
                <a href="https://example.test/account">Account link</a>
                <div role="tab">Details tab</div>
                <button type="button" aria-haspopup="menu" aria-expanded="false">Account menu</button>
                <form id="settings"><button aria-expanded="false">Save settings</button></form>
                <button form="settings" aria-haspopup="listbox">External submit</button>
                <button id="changing" aria-expanded="false">Disclosure</button>
            ''')
            first = (await page.evaluate(INTERACTABLES_SCRIPT, 0))["interactables"]
            controls = {row["label"]: row for row in first}
            assert controls["Account link"]["href"] == "https://example.test/account"
            assert controls["Details tab"]["role"] == "tab"
            assert controls["Account menu"]["aria_haspopup"] == "menu"
            assert controls["Account menu"]["aria_expanded"] == "false"
            assert controls["Account menu"]["form_submit"] is False
            assert controls["Save settings"]["type"] is None
            assert controls["Save settings"]["form_submit"] is True
            assert controls["External submit"]["form_submit"] is True
            before = controls["Disclosure"]
            assert before["form_submit"] is False
            await page.evaluate("""() => {
                const button = document.getElementById('changing');
                button.setAttribute('aria-expanded', 'true');
                document.getElementById('settings').append(button);
            }""")
            after = next(row for row in (await page.evaluate(INTERACTABLES_SCRIPT, 0))["interactables"]
                         if row["element_id"] == before["element_id"])
            assert after["label"] == before["label"] and after["type"] is None
            assert after["aria_expanded"] == "true" and after["form_submit"] is True
        finally:
            await browser.close()
