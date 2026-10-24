"""JavaScript dialogs and popups on the owner's live browser.

Playwright dismisses every dialog on a page with no "dialog" listener. Nothing
listened, so every confirm/prompt a site showed the owner while he browsed by
hand was cancelled before he could see it. Now a listener is always attached,
the owner's dialogs are left open for him, and only dialogs caused by an agent
action are answered automatically -- and only when harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import tempfile
import threading
import time
import unittest
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.action_errors import BrowserActionError
from app.browser.services.diagnostics import BrowserDiagnosticsService
from app.browser.services.dialogs import BrowserDialogService, decide
from app.browser.tab_scope import current_tab_id
from app.browser.tab_view import tab_id_for
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC


class FakeDialog:
    def __init__(self, type_: str, message: str, default_value: str = "") -> None:
        self.type = type_
        self.message = message
        self.default_value = default_value
        self.accepted: list[str | None] = []
        self.dismissed = False

    async def accept(self, prompt_text: str | None = None) -> None:
        self.accepted.append(prompt_text)

    async def dismiss(self) -> None:
        self.dismissed = True


class FakePage:
    def __init__(self, url: str = "https://site.example/", *, blocked: bool = False) -> None:
        self.url = url
        self.blocked = blocked
        self.closed = False
        self.handlers: dict[str, list] = {}
        self.fronted = False

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def evaluate(self, script: str, *args):
        if self.blocked:
            await asyncio.sleep(10)
        return 1

    def is_closed(self) -> bool:
        return self.closed

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        return None

    async def bring_to_front(self) -> None:
        self.fronted = True


def _session(page: FakePage, *, agent: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        page=page,
        lock=asyncio.Lock(),
        agent_action_depth=1 if agent else 0,
        agent_dialog_grace_until=0.0,
        open_dialogs=weakref.WeakKeyDictionary(),
        dialog_log=[],
        popup_openers=weakref.WeakKeyDictionary(),
        pending_popup=None,
        attached_pages=weakref.WeakSet(),
    )


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append(self, **kwargs) -> None:
        self.events.append(kwargs)


class FakeManager:
    def __init__(self, session: SimpleNamespace, pages: list[FakePage]) -> None:
        self.session = session
        self.audit = FakeAudit()
        self.attached: list[FakePage] = []
        self.tabs = SimpleNamespace(pages=lambda _session: list(pages))
        self.dialogs = BrowserDialogService(self)

    async def get_session(self, session_id: str):
        return self.session

    def _attach_page_listeners(self, page, session) -> None:
        self.attached.append(page)

    async def _settle(self, page) -> None:
        return None


class DecideTests(unittest.TestCase):
    def test_owner_dialogs_are_never_answered_for_him(self) -> None:
        for dialog_type in ("alert", "confirm", "prompt", "beforeunload"):
            with self.subTest(dialog_type=dialog_type):
                self.assertEqual(decide(dialog_type, "Continue?", agent_flow=False), "leave_open")

    def test_agent_flow_accepts_benign_dialogs(self) -> None:
        self.assertEqual(decide("alert", "Saved!", agent_flow=True), "accept")
        self.assertEqual(decide("beforeunload", "", agent_flow=True), "accept")
        self.assertEqual(decide("confirm", "Continue with Google?", agent_flow=True), "accept")
        self.assertEqual(decide("confirm", "Subscribe to the free plan?", agent_flow=True), "accept")

    def test_agent_flow_leaves_risky_confirms_and_prompts_for_the_agent(self) -> None:
        for message in (
            "Are you sure you want to delete this page?",
            "Cancel your subscription?",
            "Confirm payment of $20",
            "Subscribe for $20 per month?",
            "هل تريد حذف الحساب؟",
        ):
            with self.subTest(message=message):
                self.assertEqual(decide("confirm", message, agent_flow=True), "leave_open")
        self.assertEqual(decide("prompt", "Your name?", agent_flow=True), "leave_open")


class DialogListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_is_attached_so_playwright_stops_auto_dismissing(self) -> None:
        page = FakePage()
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        diagnostics = BrowserDiagnosticsService(manager, pii_scrubber=None, download_capture=None)
        diagnostics.attach_page_listeners(page, session)
        self.assertIn("dialog", page.handlers)
        self.assertIn("popup", page.handlers)

    async def test_a_dialog_during_the_owners_own_browsing_stays_open(self) -> None:
        page = FakePage(blocked=True)
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        dialog = FakeDialog("confirm", "Leave this page to verify your identity?")
        await manager.dialogs._on_dialog(session, page, dialog)
        self.assertEqual(dialog.accepted, [])
        self.assertFalse(dialog.dismissed)
        record = await manager.dialogs.open_dialog(session)
        self.assertIsNotNone(record)
        self.assertEqual(record["message"], "Leave this page to verify your identity?")
        self.assertEqual(record["opened_during"], "owner_or_site")

    async def test_owner_answering_by_hand_clears_the_record(self) -> None:
        page = FakePage(blocked=True)
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        await manager.dialogs._on_dialog(session, page, FakeDialog("alert", "Code sent"))
        page.blocked = False  # the owner clicked OK in the live view
        self.assertIsNone(await manager.dialogs.open_dialog(session))
        self.assertEqual(session.dialog_log[-1]["outcome"], "closed")

    async def test_agent_action_dialogs_are_answered_and_logged(self) -> None:
        page = FakePage()
        session = _session(page, agent=True)
        manager = FakeManager(session, [page])
        alert = FakeDialog("alert", "Welcome!")
        await manager.dialogs._on_dialog(session, page, alert)
        self.assertEqual(alert.accepted, [None])
        self.assertEqual(session.dialog_log[-1]["outcome"], "auto_accepted")

        risky = FakeDialog("confirm", "Delete this draft?")
        page.blocked = True
        await manager.dialogs._on_dialog(session, page, risky)
        self.assertEqual(risky.accepted, [])
        with self.assertRaises(BrowserActionError) as raised:
            await manager.dialogs.raise_if_open(session, "click")
        self.assertEqual(raised.exception.code, "dialog_open")
        self.assertEqual(raised.exception.status_code, 423)
        self.assertEqual(raised.exception.details["dialog"]["message"], "Delete this draft?")

    async def test_grace_window_after_an_action_still_counts_as_the_agents(self) -> None:
        page = FakePage()
        session = _session(page, agent=False)
        session.agent_dialog_grace_until = time.monotonic() + 5
        manager = FakeManager(session, [page])
        dialog = FakeDialog("confirm", "Continue to the next step?")
        await manager.dialogs._on_dialog(session, page, dialog)
        self.assertEqual(dialog.accepted, [None])

    async def test_agent_answers_a_prompt_with_its_own_text(self) -> None:
        page = FakePage(blocked=True)
        session = _session(page, agent=True)
        manager = FakeManager(session, [page])
        dialog = FakeDialog("prompt", "Name this board")
        await manager.dialogs._on_dialog(session, page, dialog)
        result = await manager.dialogs.handle("s1", accept=True, prompt_text="Stone launch")
        self.assertTrue(result["handled"])
        self.assertEqual(dialog.accepted, ["Stone launch"])
        self.assertEqual(result["dialog"]["outcome"], "accepted")
        self.assertEqual(manager.audit.events[-1]["action"], "dialog")

    async def test_dismiss_and_nothing_open(self) -> None:
        page = FakePage(blocked=True)
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        self.assertEqual((await manager.dialogs.handle("s1", accept=False))["reason"], "no_open_dialog")
        dialog = FakeDialog("confirm", "Leave site?")
        await manager.dialogs._on_dialog(session, page, dialog)
        result = await manager.dialogs.handle("s1", accept=False)
        self.assertTrue(dialog.dismissed)
        self.assertEqual(result["dialog"]["outcome"], "dismissed")


class PopupTests(unittest.IsolatedAsyncioTestCase):
    async def test_popup_from_an_agent_click_becomes_active_then_hands_back(self) -> None:
        site = FakePage("https://www.canva.com/signup")
        chooser = FakePage("https://accounts.google.com/o/oauth2/auth")
        session = _session(site, agent=True)
        manager = FakeManager(session, [site, chooser])

        manager.dialogs._on_popup(session, site, chooser)  # "Continue with Google" opened it
        followed = await manager.dialogs.follow_popup(session)
        self.assertEqual(followed, {"followed_popup": True, "url": chooser.url})
        self.assertIs(session.page, chooser)
        self.assertTrue(chooser.fronted)

        chooser.closed = True  # the chooser closes itself after the account is picked
        self.assertTrue(manager.dialogs.heal_active_page(session))
        self.assertIs(session.page, site)

    async def test_popup_the_owner_opens_is_not_followed(self) -> None:
        site = FakePage()
        other = FakePage("https://other.example/")
        session = _session(site, agent=False)
        manager = FakeManager(session, [site, other])
        manager.dialogs._on_popup(session, site, other)
        self.assertIsNone(await manager.dialogs.follow_popup(session))
        self.assertIs(session.page, site)

    async def test_open_active_tab_is_left_alone(self) -> None:
        site = FakePage()
        session = _session(site, agent=True)
        manager = FakeManager(session, [site])
        self.assertFalse(manager.dialogs.heal_active_page(session))
        self.assertIs(session.page, site)


if __name__ == "__main__":
    unittest.main()


class PipelineDialogGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_action_on_a_tab_blocked_by_a_dialog_fails_fast_and_unmarks(self) -> None:
        from app.actions import ActionRunContext
        from app.actions.pipeline import BrowserActionPipeline

        page = FakePage(blocked=True)
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        manager.settings = SimpleNamespace(agent_dialog_grace_seconds=3.0)
        await manager.dialogs._on_dialog(session, page, FakeDialog("confirm", "Leave page?"))

        async def operation() -> None:
            raise AssertionError("must not run while a dialog blocks the tab")

        context = ActionRunContext(
            manager=manager,
            session=session,
            action_name="click",
            target={"mode": "selector"},
            operation=operation,
        )
        with self.assertRaises(BrowserActionError) as raised:
            await BrowserActionPipeline().run(context)
        self.assertEqual(raised.exception.code, "dialog_open")
        self.assertEqual(session.agent_action_depth, 0)
        self.assertGreater(session.agent_dialog_grace_until, time.monotonic())

    async def test_the_owners_type_here_bridge_is_not_an_agent_action(self) -> None:
        from app.actions import ActionRunContext
        from app.actions.pipeline import BrowserActionPipeline

        page = FakePage(blocked=True)
        session = _session(page, agent=False)
        manager = FakeManager(session, [page])
        manager.settings = SimpleNamespace(agent_dialog_grace_seconds=3.0)
        await manager.dialogs._on_dialog(session, page, FakeDialog("alert", "Hi"))

        async def operation() -> None:
            return None

        context = ActionRunContext(
            manager=manager,
            session=session,
            action_name="type",
            target={"mode": "focused"},
            operation=operation,
        )
        with self.assertRaises(BrowserActionError):
            await BrowserActionPipeline().run(context)
        self.assertEqual(session.agent_dialog_grace_until, 0.0)


class PersistentPromptDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_clients_keep_prompt_and_release_action_for_normal_answer(self) -> None:
        """The primary persistent client retains the prompt; CDP dispatch must return."""
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="dialog-dispatch-") as folder:
            root = Path(folder)
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text(
                '<title>persistent-prompt</title><button id="prompt" onclick="'
                'window.clicks=(window.clicks||0)+1;'
                "document.querySelector('#result').textContent=prompt('Your name?', '');"
                '">prompt happen</button><p id="result"></p>', encoding="utf-8",
            )

            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_args) -> None:
                    pass

            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(Handler, directory=str(site)),
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                async with async_playwright() as playwright:
                    profile = root / "profile"
                    primary = await playwright.chromium.launch_persistent_context(
                        str(profile), headless=True, args=["--remote-debugging-port=0"],
                    )
                    primary.on("dialog", lambda _dialog: None)  # browser-node's actual ownership rule
                    endpoint = (profile / "DevToolsActivePort").read_text().splitlines()
                    browser = await playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{endpoint[0]}", timeout=5000,
                    )
                    manager = BrowserManager(Settings(
                        _env_file=None, ALLOWED_HOSTS="127.0.0.1", WITNESS_ENABLED=False,
                        ENABLE_TRACING=False, PERCEPTION_PRESET_DEFAULT="text",
                        BROWSER_ACTION_TIMEOUT_SECONDS=4, BROWSER_CALL_TIMEOUT_SECONDS=2,
                        SESSION_WATCHDOG_INTERVAL_SECONDS=0.05,
                        **{name: str(root / name.lower()) for name in (
                            "ARTIFACT_ROOT", "AUTH_ROOT", "UPLOAD_ROOT", "APPROVAL_ROOT",
                            "AUDIT_ROOT", "WITNESS_ROOT", "SESSION_STORE_ROOT",
                        )},
                    ))
                    manager.playwright = playwright
                    await manager.audit.startup()
                    await manager.witness.startup()
                    context = browser.contexts[0]
                    owner_page = context.pages[0]
                    page = await context.new_page()
                    await page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                    artifact_dir = root / "artifacts" / "prompt"
                    artifact_dir.mkdir(parents=True)
                    session = BrowserSession(
                        id="prompt", name="prompt", created_at=datetime.now(UTC),
                        context=context, page=owner_page, browser=browser,
                        artifact_dir=artifact_dir, auth_dir=root / "auth", upload_dir=root / "uploads",
                        takeover_url="", trace_path=artifact_dir / "trace.zip",
                    )
                    session.driver_epoch = manager._driver_epoch
                    manager.sessions[session.id] = session
                    manager._attach_page_listeners(owner_page, session)
                    manager._attach_page_listeners(page, session)
                    token = current_tab_id.set(tab_id_for(session, page))
                    watchdog = asyncio.create_task(manager._session_watchdog_loop())
                    try:
                        # Cache metadata through the ordinary summary before the modal.
                        view = await manager.get_session(session.id)
                        self.assertEqual((await manager._session_summary(view))["title"], "persistent-prompt")
                        started = time.monotonic()
                        result = await asyncio.wait_for(
                            manager.click(session.id, selector="#prompt", pace="fast"), timeout=3,
                        )
                        self.assertLess(time.monotonic() - started, 3)
                        self.assertEqual(result["status"], "dialog_open")
                        self.assertFalse(result["verification"]["verified"])
                        self.assertFalse(result["retryable"])
                        self.assertTrue(result["action_may_have_executed"])
                        self.assertEqual(result["after"]["open_dialog"]["type"], "prompt")
                        self.assertFalse(view.lock.locked())
                        self.assertIs(session.page, owner_page)

                        # Pending-dialog reads must not send renderer work at all.
                        with patch.object(page, "evaluate", new=AsyncMock(side_effect=AssertionError("DOM read"))), \
                             patch.object(page, "title", new=AsyncMock(side_effect=AssertionError("title read"))), \
                             patch.object(page, "screenshot", new=AsyncMock(side_effect=AssertionError("screenshot"))):
                            observed = await asyncio.wait_for(manager.observe(session.id, preset="text"), 0.5)
                            self.assertEqual(observed["open_dialog"]["message"], "Your name?")
                            self.assertEqual(observed["session"]["title"], "persistent-prompt")
                            # A recorded modal timeout remains a dialog, never a dead link.
                            with self.assertRaises(BrowserActionError) as raised:
                                await manager.session_lifecycle.guarded(
                                    view, asyncio.sleep(1), what="observe", timeout=0.01,
                                )
                            self.assertEqual(raised.exception.code, "dialog_open")
                            await asyncio.sleep(0.1)
                            self.assertIsNone(session.unresponsive_reason)
                            self.assertIs(manager.sessions[session.id], session)
                            self.assertEqual(session.reattach_count, 0)
                        handled = await asyncio.wait_for(
                            manager.handle_dialog(session.id, accept=True, prompt_text="اختبار فريق"), 2,
                        )
                        self.assertTrue(handled["handled"])
                        observed = await manager.observe(session.id, preset="text")
                        self.assertIsNone(observed["open_dialog"])
                        self.assertIn("اختبار فريق", observed["text_excerpt"])
                        self.assertEqual(await page.evaluate("window.clicks"), 1)
                        events = [json.loads(line) for line in
                                  (artifact_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
                        self.assertEqual([(event["action"], event["status"]) for event in events], [("click", "pending")])
                    finally:
                        current_tab_id.reset(token)
                        watchdog.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await watchdog
                        await browser.close()  # CDP client only
                        await primary.close()  # disposable fixture profile only
            finally:
                server.shutdown()
                server.server_close()
