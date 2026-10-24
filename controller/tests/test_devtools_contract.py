from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.browser_manager import BrowserManager, BrowserSession, _scrub_script_result
from app.config import Settings
from app.utils import UTC


def test_script_results_remove_actual_runtime_secret_values() -> None:
    result = _scrub_script_result(
        {
            "cookie": "session=raw-cookie-value; ok=true",
            "nested": {"password": "raw-password"},
            "safe": 42,
        },
        ("raw-cookie-value", "raw-password"),
    )
    assert "raw-cookie-value" not in result["cookie"]
    assert result["nested"]["password"] == "[REDACTED]"
    assert result["safe"] == 42


def test_script_result_redaction_is_data_based_not_expression_keyword_based() -> None:
    result = _scrub_script_result(
        {"title": "Public dashboard", "count": 3},
        ("actual-runtime-secret",),
    )
    assert result == {"title": "Public dashboard", "count": 3}


async def test_real_chromium_evaluate_is_read_only_and_redacts_runtime_values() -> None:
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
                page = await context.new_page()
                await page.set_content(
                    '<title>Public dashboard</title>'
                    '<input aria-label="account" value="actual-runtime-secret">'
                    '<script>window.sideEffectCount = 0</script>'
                )
                artifact_dir = root / "artifacts" / "real-eval"
                artifact_dir.mkdir(parents=True)
                session = BrowserSession(
                    id="real-eval",
                    name="real-eval",
                    created_at=datetime.now(UTC),
                    context=context,
                    page=page,
                    browser=browser,
                    artifact_dir=artifact_dir,
                    auth_dir=root / "auth",
                    upload_dir=root / "uploads",
                    takeover_url="",
                    trace_path=artifact_dir / "trace.zip",
                )
                session.driver_epoch = manager._driver_epoch
                manager.sessions[session.id] = session

                result = await manager.evaluate_script(
                    session.id,
                    "({title: document.title, value: document.querySelector('input').value})",
                )
                assert result == {"title": "Public dashboard", "value": "[REDACTED]"}

                with pytest.raises(ValueError, match="side effects"):
                    await manager.evaluate_script(session.id, "window.sideEffectCount = 1")
                assert await page.evaluate("window.sideEffectCount") == 0
            finally:
                await browser.close()
