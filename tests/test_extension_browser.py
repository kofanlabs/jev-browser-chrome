from unittest.mock import patch

import pytest

from jev_ultrafast.browser import StalePage
from jev_ultrafast.extension_browser import ExtensionBrowser


def page_state(value=""):
    return {
        "url": "https://example.com/",
        "title": "Example",
        "text": "Form",
        "actions": [{"id": "e1", "kind": "fill", "node": 7, "label": "Name", "value": value}],
        "scroll": {"y": 0, "height": 800},
        "marker": [1, value],
        "page_key": [1, "https://example.com/", 0, 0, 1000, 800, [[7, value]]],
        "guards": {"7": [7, "textbox", "Name", value]},
    }


def test_extension_browser_observes_and_executes_selected_tab():
    observed = page_state()
    calls = []

    def bridge(method, params, timeout=20):
        calls.append((method, params))
        if method == "get_tab":
            return {"tabId": "12", "url": "https://example.com/", "title": "Example"}
        if method == "observe":
            return dict(observed)
        if method == "act":
            return {"executed": "e1"}
        raise AssertionError(method)

    with patch("jev_ultrafast.extension_browser.call", side_effect=bridge):
        browser = ExtensionBrowser("https://example.com/", "12")
        page = browser.observe()
        browser.act(page["actions"][0], page, text="Ada")

    assert calls[-1][0] == "act"
    assert calls[-1][1]["text"] == "Ada"
    assert calls[-1][1]["guard"] == observed["guards"]["7"]


def test_extension_browser_maps_atomic_stale_rejection():
    def bridge(method, _params, timeout=20):
        if method == "get_tab":
            return {"tabId": "12", "url": "https://example.com/"}
        if method == "act":
            raise RuntimeError("STALE_PAGE")
        raise AssertionError(method)

    with patch("jev_ultrafast.extension_browser.call", side_effect=bridge):
        browser = ExtensionBrowser("https://example.com/", "12")
        page = page_state()
        with pytest.raises(StalePage):
            browser.act(page["actions"][0], page, text="Ada")
