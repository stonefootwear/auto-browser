from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.browser.services.observation import BrowserObservationService
from app.browser_manager import BrowserManager
from app.browser_scripts import ACTIVE_ELEMENT_SCRIPT, INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT


class ActionVerificationTests(unittest.TestCase):
    def test_click_verification_uses_page_change_signals(self) -> None:
        before = {
            "url": "https://example.com",
            "title": "Before",
            "active_element": {"tag": "body"},
            "text_excerpt": "before text",
            "dom_outline": {"counts": {"links": 1, "buttons": 1, "inputs": 0, "forms": 0}},
        }
        after = {
            "url": "https://example.com/next",
            "title": "After",
            "active_element": {"tag": "button", "element_id": "op-1"},
            "text_excerpt": "after text",
            "dom_outline": {"counts": {"links": 2, "buttons": 1, "inputs": 0, "forms": 0}},
            "interactables": [],
        }

        verification = BrowserManager._action_verification(
            "click",
            {"element_id": "op-1", "selector": '[data-operator-id="op-1"]'},
            before,
            after,
        )

        self.assertTrue(verification["verified"])
        self.assertIn("url_changed", verification["signals"])
        self.assertIn("target_no_longer_visible", verification["signals"])

    def test_navigate_requires_real_navigation_signal(self) -> None:
        before = {
            "url": "https://example.com",
            "title": "Example",
            "active_element": None,
            "text_excerpt": "same",
            "dom_outline": {"counts": {"links": 1}},
        }
        after = {
            "url": "https://example.com",
            "title": "Example",
            "active_element": None,
            "text_excerpt": "same",
            "dom_outline": {"counts": {"links": 1}},
            "interactables": [],
        }

        verification = BrowserManager._action_verification("navigate", {"url": "https://example.com"}, before, after)

        self.assertFalse(verification["verified"])
        self.assertEqual(verification["signals"], [])


    def test_scroll_requires_observed_document_movement(self) -> None:
        for before_position, after_position, moved in (
            ({"x": 0, "y": 0}, {"x": 0, "y": 600}, True),
            ({"x": 0, "y": 0}, {"x": 25, "y": 0}, True),
            ({"x": 0, "y": 600}, {"x": 0, "y": 600}, False),
            (None, {"x": 0, "y": 600}, False),
            ({"x": 0, "y": 0}, None, False),
            ({"x": 0}, {"x": 0, "y": 600}, False),
            ({"x": False, "y": 0}, {"x": 0, "y": 600}, False),
            ({"x": 0, "y": 0}, {"x": 0, "y": float("nan")}, False),
        ):
            with self.subTest(before=before_position, after=after_position):
                verification = BrowserManager._action_verification(
                    "scroll", {"delta_x": 0, "delta_y": 600},
                    {"scroll_position": before_position, "text_excerpt": "before"},
                    {"scroll_position": after_position, "text_excerpt": "after"},
                )
                self.assertIs(verification["verified"], moved)
                self.assertEqual("scroll_position_changed" in verification["signals"], moved)
                self.assertIsNone(verification["target_seen_after"])


class ScrollObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_observation_captures_document_coordinates(self) -> None:
        manager = MagicMock()
        manager._session_summary = AsyncMock(return_value={})
        manager.tabs.summaries = AsyncMock(return_value=[])
        page = SimpleNamespace(
            url="https://example.com", title=AsyncMock(return_value="Example"),
            evaluate=AsyncMock(return_value={"x": 0, "y": 600}),
        )
        session = SimpleNamespace(
            page=page, console_messages=[], page_errors=[], request_failures=[], downloads=[],
        )
        observation = BrowserObservationService(manager)
        observation._capture_screenshot_redacted = AsyncMock(return_value={"path": "test", "url": "test"})
        result = await observation._observation_payload(session, preset="fast")
        self.assertEqual(result["scroll_position"], {"x": 0, "y": 600})
        page.evaluate.assert_awaited_once_with("() => ({x: window.scrollX, y: window.scrollY})")


    async def test_action_before_and_after_capture_feed_scroll_verification(self) -> None:
        for positions, moved in (
            (({"x": 0, "y": 0}, {"x": 0, "y": 600}), True),
            (({"x": 0, "y": 600}, {"x": 0, "y": 600}), False),
            ((None, {"x": 0, "y": 600}), False),
            (({"x": 0, "y": 0}, None), False),
        ):
            with self.subTest(positions=positions):
                manager = MagicMock()
                manager._session_summary = AsyncMock(return_value={})
                manager.tabs.summaries = AsyncMock(return_value=[])
                snapshots = iter(positions)

                async def evaluate(script, *args):
                    if script == PAGE_SUMMARY_SCRIPT:
                        self.assertIn("scroll_position: {x: window.scrollX, y: window.scrollY}", script)
                        position = next(snapshots)
                        return {"scroll_position": position, "text_excerpt": "unchanged"}
                    if script == ACTIVE_ELEMENT_SCRIPT:
                        return None
                    self.assertEqual(script, INTERACTABLES_SCRIPT)
                    return {"interactables": [], "active_dialog": None}

                page = SimpleNamespace(
                    url="https://example.com", title=AsyncMock(return_value="Example"),
                    evaluate=AsyncMock(side_effect=evaluate),
                )
                session = SimpleNamespace(
                    page=page, console_messages=[], page_errors=[], request_failures=[], downloads=[],
                )
                observation = BrowserObservationService(manager)
                observation._capture_screenshot_redacted = AsyncMock(return_value={"path": "test", "url": "test"})
                # These are the actual helpers used by the action pipeline, not invented snapshots.
                before = await observation.light_snapshot(session, label="before-scroll")
                after = await observation._observation_payload(session, preset="text")
                verification = BrowserManager._action_verification("scroll", {"delta_y": 600}, before, after)
                self.assertIs(verification["verified"], moved)
                self.assertEqual("scroll_position_changed" in verification["signals"], moved)
                # No extra position-only evaluation on either DOM-summary capture path.
                self.assertEqual(page.evaluate.await_count, 5)
