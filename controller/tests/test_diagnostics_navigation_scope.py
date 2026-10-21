"""Current-document diagnostics, including events racing navigation/rebinding."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.browser.services.diagnostics import BrowserDiagnosticsService
from app.browser.tab_view import bind_tab, tab_id_for


class Page:
    def __init__(self):
        self.url = "https://fixture.test/form"
        self.main_frame = object()
        self.handlers = {}
        self.title_hook = None

    def on(self, name, callback):
        self.handlers[name] = callback

    async def title(self):
        if self.title_hook:
            await self.title_hook()
        return "fixture"

    async def evaluate(self, *_args):
        return [{"message": "current validation"}]


class Request:
    url = "https://cdn.other.test/failed"
    method = "GET"
    failure = "failed"
    resource_type = "fetch"


class NavigationScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = Page()
        self.session = SimpleNamespace(
            page=self.page, attached_pages=set(), tab_diagnostics={},
            console_messages=[], page_errors=[], request_failures=[],
        )
        self.service = BrowserDiagnosticsService(
            SimpleNamespace(), SimpleNamespace(console_enabled=False), None,
        )
        self.service.attach_page_listeners(self.page, self.session)

    def console(self, text="old error"):
        self.page.handlers["console"](SimpleNamespace(type="error", text=text, location={"url": "https://cdn.other.test"}))

    def navigate(self):
        self.page.handlers["framenavigated"](self.page.main_frame)

    async def digest(self):
        return await self.service.build_digest(self.session)

    async def test_same_url_navigation_clears_current_digest_but_preserves_legacy_history(self):
        self.console()
        self.page.handlers["pageerror"](ValueError("old page error"))
        request = Request()
        self.page.handlers["request"](request)
        self.page.handlers["requestfailed"](request)
        before = await self.digest()
        self.navigate()
        after = await self.digest()
        self.assertNotEqual(before["navigation_id"], after["navigation_id"])
        for key in ("console_errors", "page_errors", "failed_requests"):
            self.assertTrue(before[key])
            self.assertEqual(after[key], [])
        self.assertEqual(len(self.session.console_messages), 1)
        self.assertEqual(len(self.session.page_errors), 1)
        self.assertEqual(len(self.session.request_failures), 1)

    async def test_iframe_navigation_keeps_current_cross_origin_events(self):
        self.console("current CDN error")
        request = Request()
        self.page.handlers["request"](request)
        self.page.handlers["framenavigated"](object())
        self.page.handlers["requestfailed"](request)
        result = await self.digest()
        self.assertEqual(result["console_errors"][0]["text"], "current CDN error")
        self.assertEqual(result["failed_requests"][0]["url"], request.url)

    async def test_old_or_unobserved_request_failure_never_enters_new_document(self):
        old = Request()
        self.page.handlers["request"](old)
        self.navigate()
        self.page.handlers["requestfailed"](old)
        self.page.handlers["requestfailed"](Request())
        self.assertEqual((await self.digest())["failed_requests"], [])
        self.assertEqual(len(self.session.request_failures), 2)

    async def test_response_capture_precedes_requestfinished_and_drops_body_after_navigation(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def body():
            started.set()
            await finish.wait()
            return b'old response'
        request = Request()
        response = SimpleNamespace(request=request, status=422, headers={"content-type": "text/plain"},
                                   url=request.url, body=body)
        self.page.handlers["request"](request)
        tasks = []
        with patch("app.browser.services.diagnostics.spawn_background_task", side_effect=lambda coro: tasks.append(asyncio.create_task(coro))):
            self.page.handlers["response"](response)
            self.page.handlers["requestfinished"](request)
        await started.wait()
        self.navigate()
        finish.set()
        await tasks[0]
        self.assertEqual((await self.digest())["failed_requests"], [])
        self.assertEqual(len(self.service._request_scopes), 0)

    async def test_current_response_survives_finished_event_before_task_starts(self):
        async def body():
            return b'current response'
        request = Request()
        response = SimpleNamespace(request=request, status=422, headers={"content-type": "text/plain"},
                                   url=request.url, body=body)
        self.page.handlers["request"](request)
        tasks = []
        with patch("app.browser.services.diagnostics.spawn_background_task", side_effect=lambda coro: tasks.append(asyncio.create_task(coro))):
            self.page.handlers["response"](response)
            self.page.handlers["requestfinished"](request)
        await tasks[0]
        self.assertEqual((await self.digest())["failed_requests"][0]["body"], "current response")

    async def test_rebound_tab_does_not_reuse_previous_page_buffer(self):
        self.console()
        tab_id = tab_id_for(self.session, self.page)
        replacement = Page()
        self.service.attach_page_listeners(replacement, self.session)
        bind_tab(self.session, replacement, tab_id)
        self.session.page = replacement
        result = await self.digest()
        self.assertEqual(result["tab_id"], tab_id)
        self.assertEqual(result["console_errors"], [])

    async def test_navigation_while_reading_digest_fails_closed(self):
        self.console()
        async def navigate_during_title():
            self.navigate()
        self.page.title_hook = navigate_during_title
        result = await self.digest()
        self.assertEqual(result["scope_status"], "changed_or_unobserved")
        self.assertIsNone(result["navigation_id"])
        for key in ("url", "title"):
            self.assertEqual(result[key], "")
        for key in ("console_errors", "page_errors", "failed_requests", "validation_messages"):
            self.assertEqual(result[key], [])
