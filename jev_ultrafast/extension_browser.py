"""Jev browser adapter backed by the local Chrome extension bridge."""

from __future__ import annotations

import hashlib
import json
import re
import time

from extension_client import call

from .browser import StalePage


class ObservationUnavailable(RuntimeError):
    """A read failed; unlike StalePage, this says nothing about mutation outcome."""

    def __init__(self, message: str, *, retryable: bool):
        self.retryable = retryable
        super().__init__(message)


def fingerprint(state: dict) -> str:
    content = {key: state[key] for key in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class ExtensionBrowser:
    owns_tab = False
    settlements = {
        "navigation-complete",
        "visible-change",
        "no-visible-change",
        "navigation-timeout",
        "frozen",
        "observation-unavailable",
        "tab-unavailable",
        "busy-timeout",
        "dom-change-inconclusive",
    }

    def __init__(self, url: str, target_id: str, *, deadline: float | None = None):
        self.deadline = deadline
        tab = call("get_tab", {"tabId": str(target_id)}, timeout=self._timeout())
        if tab.get("url") != url:
            raise ValueError("Selected existing tab changed; list tabs again")
        self.target = str(target_id)

    def _timeout(self) -> float:
        if self.deadline is None:
            return 20
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Run deadline reached before the browser call.")
        return min(20, remaining)

    def observe(self, screenshot: bool = False) -> dict:
        timeout = self._timeout()
        try:
            state = call("observe", {"tabId": self.target}, timeout=timeout)
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            retryable = not any(
                marker in lowered
                for marker in (
                    "not connected",
                    "disconnected",
                    "connection refused",
                    "connection reset",
                    "timed out",
                    "timeout",
                )
            )
            raise ObservationUnavailable(message, retryable=retryable) from exc
        state["fingerprint"] = fingerprint(state)
        if screenshot:
            state["screenshot"] = call("capture", {"tabId": self.target}, timeout=self._timeout())["data"]
        return state

    def fresh(self, page: dict, action: dict | None = None) -> bool:
        current = self.observe(screenshot=False)
        if action is not None and isinstance(action.get("node"), int):
            node = action["node"]
            return current.get("page_key") == page.get("page_key") and current.get("guards", {}).get(
                str(node)
            ) == page.get("guards", {}).get(str(node))
        return current.get("marker") == page.get("marker")

    def act(self, action: dict, page: dict, text: str | None = None) -> dict:
        try:
            receipt = call(
                "act",
                {
                    "tabId": self.target,
                    "action": action,
                    "pageKey": page.get("page_key"),
                    "guard": page.get("guards", {}).get(str(action.get("node"))),
                    "url": page["url"],
                    "text": text,
                },
                timeout=self._timeout(),
            )
        except RuntimeError as exc:
            if str(exc).strip() == "STALE_PAGE":
                raise StalePage("Page changed since this decision. Observe again.") from None
            raise
        if not isinstance(receipt, dict) or receipt.get("executed") != action.get("id"):
            raise RuntimeError("Browser action outcome is unknown; inspect the tab before retrying.")
        settlement = receipt.get("settlement")
        if "settled" in receipt:
            if not isinstance(receipt["settled"], bool):
                raise RuntimeError("Browser action returned an invalid settlement status; inspect before retrying.")
            if not isinstance(settlement, str) or settlement not in self.settlements:
                raise RuntimeError("Browser action returned an invalid settlement reason; inspect before retrying.")
        elif settlement is not None:
            raise RuntimeError("Browser action returned a settlement reason without a status; inspect before retrying.")
        compact = {"executed": receipt["executed"]}
        if isinstance(receipt.get("settled"), bool):
            compact["settled"] = receipt["settled"]
        if settlement is not None:
            compact["settlement"] = settlement
        for key in ("baseline", "immediate", "baselineHash", "immediateHash"):
            value = receipt.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                compact[key] = value
        return compact

    def call(self, method: str, **_params) -> dict:
        if method != "Page.captureScreenshot":
            raise ValueError(f"Unsupported extension browser call: {method}")
        return call("capture", {"tabId": self.target}, timeout=self._timeout())

    def close(self) -> None:
        self.target = None
