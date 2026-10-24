from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from playwright.async_api import async_playwright

from app.browser.tab_view import bind_tab, set_tab_owner
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.middleware.tab_scope import TabScopeMiddleware
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.routes.sessions import create_sessions_router
from app.utils import UTC


async def test_real_chromium_devtools_reads_stay_in_the_employee_tab_and_eval_is_projected() -> None:
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

                    network = await client.get(
                        f"/sessions/{session.id}/network-log", headers=headers,
                    )
                    assert network.status_code == 200
                    assert network.json()["session"]["current_url"] == employee_page.url
                    assert "owner-only-value" not in str(network.json())

            finally:
                await browser.close()
