"""Exercise the actual page-summary JavaScript using only synthetic form values."""

import json

from playwright.async_api import async_playwright

from app.browser_scripts import PAGE_SUMMARY_SCRIPT


async def test_dom_outline_omits_hidden_password_and_text_values_but_keeps_labels():
    markers = [
        'synthetic-hidden-token',
        'synthetic-password-value',
        'synthetic-visible-text',
        'synthetic-textarea-value',
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<form>
                <input type="hidden" name="crypted_string" value="synthetic-hidden-token">
                <input type="password" name="password" value="synthetic-password-value">
                <input type="password" name="labeled_password" aria-label="Account password"
                       value="synthetic-password-value">
                <input type="text" name="email" value="synthetic-visible-text">
                <input type="text" name="labeled_text" placeholder="Email address"
                       value="synthetic-visible-text">
                <input type="submit" name="submit" value="Visible submit caption">
                <button name="continue">Visible button caption</button>
            </form><form>
                <textarea name="notes">synthetic-textarea-value</textarea>
                <textarea name="labeled_notes" aria-label="Public notes">synthetic-textarea-value</textarea>
                <select name="choice"><option>Visible select caption</option></select>
            </form>''')
            summary = await page.evaluate(PAGE_SUMMARY_SCRIPT, 500)
        finally:
            await browser.close()

    serialized = json.dumps(summary)
    assert all(marker not in serialized for marker in markers)
    fields = {
        field['name']: field
        for form in summary['dom_outline']['forms']
        for field in form['fields']
    }
    assert fields['crypted_string']['type'] == 'hidden'
    assert fields['crypted_string']['label'] == 'crypted_string'
    assert fields['password']['label'] == 'password'
    assert fields['labeled_password']['label'] == 'Account password'
    assert fields['email']['label'] == 'email'
    assert fields['labeled_text']['label'] == 'Email address'
    assert fields['notes']['label'] == 'notes'
    assert fields['labeled_notes']['label'] == 'Public notes'
    assert fields['submit']['label'] == 'Visible submit caption'
    assert fields['continue']['label'] == 'Visible button caption'
    assert fields['choice']['label'] == 'Visible select caption'
