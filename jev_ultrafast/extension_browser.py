"""Jev browser adapter backed by the local Chrome extension bridge."""

from __future__ import annotations

import hashlib
import json

from extension_client import call

from .browser import StalePage


def fingerprint(state: dict) -> str:
    content = {key: state[key] for key in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class ExtensionBrowser:
    owns_tab = False

    def __init__(self, url: str, target_id: str):
        tab = call("get_tab", {"tabId": str(target_id)})
        if tab.get("url") != url:
            raise ValueError("Selected existing tab changed; list tabs again")
        self.target = str(target_id)

    def observe(self, screenshot: bool = False) -> dict:
        try:
            state = call("observe", {"tabId": self.target}, timeout=20)
        except RuntimeError as exc:
            raise StalePage(str(exc)) from None
        state["fingerprint"] = fingerprint(state)
        if screenshot:
            state["screenshot"] = call("capture", {"tabId": self.target})["data"]
        return state

    def fresh(self, page: dict, action: dict | None = None) -> bool:
        try:
            current = self.observe(screenshot=False)
        except StalePage:
            return False
        if action is not None and isinstance(action.get("node"), int):
            node = action["node"]
            return current.get("page_key") == page.get("page_key") and current.get("guards", {}).get(
                str(node)
            ) == page.get("guards", {}).get(str(node))
        return current.get("marker") == page.get("marker")

    def act(self, action: dict, page: dict, text: str | None = None) -> dict:
        try:
            return call(
                "act",
                {
                    "tabId": self.target,
                    "action": action,
                    "pageKey": page.get("page_key"),
                    "guard": page.get("guards", {}).get(str(action.get("node"))),
                    "url": page["url"],
                    "text": text,
                },
            )
        except RuntimeError as exc:
            if "STALE_PAGE" in str(exc):
                raise StalePage("Page changed since this decision. Observe again.") from None
            raise

    def call(self, method: str, **_params) -> dict:
        if method != "Page.captureScreenshot":
            raise ValueError(f"Unsupported extension browser call: {method}")
        return call("capture", {"tabId": self.target})

    def close(self) -> None:
        self.target = None
