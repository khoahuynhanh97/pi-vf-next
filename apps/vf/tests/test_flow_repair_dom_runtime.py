"""Execute the trusted DOM extractor in credential-free offline Chrome.

Every page request is fulfilled from these fixtures. A dead local proxy and
disabled background networking prevent an accidental remote fallback. No user
profile, provider operation, model, or production adapter is involved.
"""
import json

import pytest

from video_factory.maintenance import browser


URL = "https://flow.google.com/project/11111111-1111-1111-1111-111111111111"
COMMON = """
<style>body {font-family:sans-serif} section,nav,main,flow-controls {display:block;padding:8px}
button,input,select {min-width:100px;min-height:30px}</style>
<title>private-account@example.com confidential-title</title>
<input aria-label="Action prompt" value="CONFIDENTIAL_INPUT_BYTES">
<button aria-label="Profile private-account@example.com token=PRIVATE_TOKEN https://assets.test/a?signed=PRIVATE_SIGNATURE#PRIVATE_FRAGMENT">Account</button>
<div hidden><button>Hidden ancestor control</button></div>
<button style="display:none">Settings</button>
<button aria-hidden="true">Hidden aria control</button>
<button style="visibility:hidden">Hidden visibility control</button>
"""
LAYOUTS = [
    ("toolbar-selects", """<!doctype html><html><body>""" + COMMON + """
    <main><section role="toolbar" aria-label="Creation controls">
      <button onclick="this.dataset.opened='yes'">Settings</button>
      <button>Settings and more</button>
    </section><section role="group" aria-label="Generation settings">
      <label for="model">Model</label><select id="model"><option>Veo 3</option></select>
      <label for="duration">Duration</label><select id="duration"><option>8 seconds</option></select>
      <label for="aspect">Aspect ratio</label><select id="aspect"><option>9:16</option></select>
    </section></main></body></html>""", "toolbar", "Settings", "combobox"),
    ("navigation-shadow-buttons", """<!doctype html><html><body>""" + COMMON + """
    <nav aria-label="Project navigation"><flow-controls></flow-controls></nav>
    <script>
    const host = document.querySelector('flow-controls');
    host.attachShadow({mode:'open'}).innerHTML = `
      <style>:host{display:block}button{min-width:120px;min-height:30px}</style>
      <button aria-label="Generation settings" onclick="this.dataset.opened='yes'">Configure</button>
      <section role="group" aria-label="Generation settings">
        <span id="model-label">Model</span><button aria-labelledby="model-label">Veo 3</button>
        <span id="duration-label">Duration</span><button aria-labelledby="duration-label">8 seconds</button>
        <button aria-label="Aspect ratio 9:16">Portrait</button>
      </section>`;
    </script></body></html>""", "navigation", "Generation settings", "button"),
]


@pytest.fixture(scope="module")
def offline_chrome():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        try:
            chrome = playwright.chromium.launch(
                channel="chrome", headless=True,
                proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                args=["--disable-background-networking", "--disable-extensions",
                      "--disable-component-update", "--disable-sync", "--no-first-run",
                      "--disable-default-apps", "--disable-domain-reliability",
                      "--metrics-recording-only"],
            )
        except playwright_api.Error as error:
            pytest.skip("Local headless Chrome unavailable: " + str(error).splitlines()[0])
        try:
            yield chrome
        finally:
            chrome.close()


@pytest.mark.parametrize("name,html,container_role,settings_name,model_role", LAYOUTS,
                         ids=[entry[0] for entry in LAYOUTS])
def test_actual_dom_script_extracts_two_local_layouts(
        offline_chrome, name, html, container_role, settings_name, model_role):
    context = offline_chrome.new_context(service_workers="block", accept_downloads=False,
                                         permissions=[], viewport={"width": 1000, "height": 800})
    requests = []

    def local_fixture(route):
        requests.append((route.request.method, route.request.url))
        if route.request.url == URL and route.request.method == "GET":
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.fulfill(status=204, body="")

    context.route("**/*", local_fixture)
    context.route_web_socket("**/*", lambda socket: socket.close())
    try:
        page = context.new_page()
        page.goto(URL, wait_until="load")
        receipt = browser.capture_page(page)
        assert receipt["status"] == "observed"
        assert receipt["route_type"] == "project"
        assert len(receipt["fingerprint"]) == 64
        controls = receipt["visible_controls"]
        assert any(control["role"] == model_role and control["name"] == "Model" for control in controls)
        assert any(control["name"] == "Duration" for control in controls)
        assert any(control["name"] == settings_name and control["role"] == "button" for control in controls)
        assert any(node["role"] == container_role for node in _nodes(receipt["dom"]))
        if name == "toolbar-selects":
            assert next(control for control in controls if control["name"] == "Model")["selected"] == "Veo 3"
            assert next(control for control in controls if control["name"] == "Duration")["selected"] == "8 seconds"
        serialized = json.dumps(receipt)
        for private in ("CONFIDENTIAL_INPUT_BYTES", "private-account@example.com", "PRIVATE_TOKEN",
                        "PRIVATE_SIGNATURE", "PRIVATE_FRAGMENT", "confidential-title",
                        "Hidden ancestor control", "Hidden aria control", "Hidden visibility control"):
            assert private not in serialized
        assert "[EMAIL]" in serialized
        assert "[REDACTED]" in serialized
        assert receipt["contract_verified"] is False
        assert receipt["capabilities"]["duration"]["status"] == "unverified"
        assert receipt["capabilities"]["start_binding"]["status"] == "unverified"
        assert receipt["recipe"]["steps"] == []
        action = browser._open_settings(page)
        assert action["status"] == "observed"
        assert page.locator('button[data-opened="yes"]').count() == 1
        assert requests and all(method == "GET" and url == URL for method, url in requests)
    finally:
        context.close()


def _nodes(node):
    yield node
    for child in node.get("children", []):
        yield from _nodes(child)
