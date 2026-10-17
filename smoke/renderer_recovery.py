"""Focused real-Chromium fault injection against a disposable browser server.

Run inside the controller image on a temporary internal Docker network. Supply
only that disposable server's WS URL, never an owner's live browser endpoint.
Uses synthetic cookies, isolated contexts and temporary controller state.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC
from playwright.async_api import async_playwright


async def run(endpoint: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect(endpoint, timeout=10000)
        try:
            for with_healthy_tab in (True, False):
                with tempfile.TemporaryDirectory(prefix="renderer-recovery-") as temporary:
                    root = Path(temporary)
                    settings = Settings(
                        _env_file=None,
                        ARTIFACT_ROOT=str(root / "artifacts"),
                        UPLOAD_ROOT=str(root / "uploads"),
                        AUTH_ROOT=str(root / "auth"),
                        APPROVAL_ROOT=str(root / "approvals"),
                        AUDIT_ROOT=str(root / "audit"),
                        WITNESS_ROOT=str(root / "witness"),
                        SESSION_STORE_ROOT=str(root / "sessions"),
                        STATE_DB_PATH=str(root / "state.db"),
                        AUTO_PERSIST_INTERVAL_SECONDS=0,
                        SESSION_WATCHDOG_INTERVAL_SECONDS=0,
                    )
                    manager = BrowserManager(settings)
                    manager.playwright = playwright
                    context = await browser.new_context()
                    crash_task = None
                    try:
                        await context.add_cookies([
                            {"name": "probe", "value": "preserved", "domain": "example.test", "path": "/"},
                        ])
                        crashed = await context.new_page()
                        healthy = await context.new_page() if with_healthy_tab else None
                        session = BrowserSession(
                            id="disposable-crash-probe", name="disposable-crash-probe",
                            created_at=datetime.now(UTC), context=context, page=crashed,
                            artifact_dir=root / "artifacts", auth_dir=root / "auth",
                            upload_dir=root / "uploads", takeover_url="", trace_path=root / "trace.zip",
                            browser=browser,
                        )
                        manager.sessions[session.id] = session
                        manager._attach_page_listeners(crashed, session)
                        if healthy:
                            manager._attach_page_listeners(healthy, session)
                        cdp = await context.new_cdp_session(crashed)
                        observed = asyncio.Event()
                        crashed.on("crash", lambda *_: observed.set())
                        crash_task = asyncio.create_task(cdp.send("Page.crash"))
                        await asyncio.wait_for(observed.wait(), timeout=5)
                        for _ in range(100):
                            await manager.session_lifecycle.reap_dead_sessions()
                            if session.renderer_recovery_count:
                                break
                            await asyncio.sleep(0.05)
                        assert manager.sessions.get(session.id) is session, "Authorized session was lost"
                        assert session.renderer_recovery_count == 1, "Renderer was not recovered exactly once"
                        assert session.context is context, "Browser context changed"
                        assert session.page is not crashed, "Crashed target stayed active"
                        assert crashed not in manager.tabs.pages(session), "Crashed target stayed selectable"
                        if healthy:
                            assert session.page is healthy, "Healthy tab was unnecessarily replaced"
                        await session.page.evaluate("1 + 1")
                        assert any(c["name"] == "probe" and c["value"] == "preserved"
                                   for c in await context.cookies("https://example.test/"))
                        manager.sessions.pop(session.id)
                        before = len(context.pages)
                        assert not manager.session_lifecycle.note_page_crash(
                            session, session.page, source="late event after revocation",
                        )
                        assert not await manager.session_lifecycle.recover_crashed_page(
                            session, session.page, reason="late event after revocation",
                        )
                        assert len(context.pages) == before, "Revoked session created a page"
                        print(f"PASS real renderer crash: healthy_tab={with_healthy_tab}; "
                              "same session/context, cookie retained, revoked session stays closed", flush=True)
                    finally:
                        manager.sessions.clear()
                        await context.close()
                        if crash_task is not None:
                            crash_task.cancel()
                            with contextlib.suppress(Exception, asyncio.CancelledError):
                                await crash_task
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
