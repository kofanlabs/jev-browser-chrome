import http.client
import io
import json
import math
import queue
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import extension_client
import extension_daemon


class FakeWebSocket:
    def __init__(self, token, *, fail_send=False, close_entered=None, close_release=None):
        self.request = SimpleNamespace(path=f"/{token}")
        self.incoming = queue.Queue()
        self.sent = queue.Queue()
        self.fail_send = fail_send
        self.close_calls = []
        self.close_entered = close_entered
        self.close_release = close_release

    def __iter__(self):
        while True:
            message = self.incoming.get()
            if message is None:
                return
            yield json.dumps(message)

    def send(self, payload):
        if self.fail_send:
            raise OSError("offline test socket failure")
        self.sent.put(json.loads(payload))

    def close(self, **kwargs):
        self.close_calls.append(kwargs)
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            self.close_release.wait(timeout=1.0)

    def receive(self, message):
        self.incoming.put(message)

    def finish(self):
        self.incoming.put(None)


def start_socket(hub, websocket):
    thread = threading.Thread(target=hub.handle_socket, args=(websocket,), daemon=True)
    thread.start()
    return thread


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    assert predicate(), "condition did not become true before timeout"


def connect(hub, version="1.0.0"):
    websocket = FakeWebSocket(hub.token)
    thread = start_socket(hub, websocket)
    websocket.receive({"type": "hello", "extensionId": "offline-test", "version": version})
    wait_until(lambda: hub.status().get("extensionVersion") == version)
    return websocket, thread


def test_daemon_import_does_not_initialize_credentials_or_listeners():
    assert extension_daemon.TOKEN is None
    assert extension_daemon.HUB is None


def test_replaced_socket_finally_cannot_fail_or_answer_new_rpc():
    hub = extension_daemon.Hub("offline-test-token")
    old_socket, old_thread = connect(hub, "old")
    new_socket = FakeWebSocket(hub.token)
    new_thread = start_socket(hub, new_socket)
    new_socket.receive({"type": "hello", "extensionId": "offline-test", "version": "new"})
    wait_until(lambda: hub.status().get("extensionVersion") == "new")

    outcome = {}

    def run_rpc():
        try:
            outcome["result"] = hub.call("list_tabs", {}, timeout=1.0)
        except Exception as exc:  # surfaced in the test thread
            outcome["error"] = exc

    rpc_thread = threading.Thread(target=run_rpc)
    rpc_thread.start()
    wait_until(lambda: not new_socket.sent.empty() or bool(outcome))
    assert "error" not in outcome, repr(outcome)
    command = new_socket.sent.get(timeout=1.0)

    # A buffered frame from the superseded socket must not satisfy the new RPC.
    old_socket.receive({"type": "response", "id": command["id"], "result": {"stale": True}})
    old_socket.finish()
    old_thread.join(timeout=1.0)
    assert not old_thread.is_alive()
    with hub.lock:
        pending = hub.pending[command["id"]]
        assert pending.websocket is new_socket
        assert not pending.event.is_set()

    new_socket.receive({"type": "response", "id": command["id"], "result": {"tabs": []}})
    rpc_thread.join(timeout=1.0)
    assert not rpc_thread.is_alive()
    assert outcome == {"result": {"tabs": []}}

    new_socket.finish()
    new_thread.join(timeout=1.0)


def test_disconnect_wakes_only_its_pending_rpc():
    hub = extension_daemon.Hub("offline-test-token")
    websocket, socket_thread = connect(hub)
    outcome = {}

    def run_rpc():
        try:
            hub.call("list_tabs", {}, timeout=1.0)
        except Exception as exc:
            outcome["error"] = exc

    rpc_thread = threading.Thread(target=run_rpc)
    rpc_thread.start()
    wait_until(lambda: not websocket.sent.empty() or bool(outcome))
    assert "error" not in outcome, repr(outcome)
    websocket.sent.get(timeout=1.0)
    websocket.finish()
    socket_thread.join(timeout=1.0)
    rpc_thread.join(timeout=1.0)

    assert not rpc_thread.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "disconnected" in str(outcome["error"])
    assert hub.pending == {}
    assert not hub.connected()


def test_mutation_timeout_sends_once_and_discards_pending_entry():
    hub = extension_daemon.Hub("offline-test-token")
    websocket, socket_thread = connect(hub)

    with pytest.raises(TimeoutError, match="timed out"):
        hub.call("create_tab", {"url": "https://example.invalid"}, timeout=0.02)

    command = websocket.sent.get_nowait()
    assert command["method"] == "create_tab"
    assert websocket.sent.empty()
    assert hub.pending == {}

    # A late response after timeout is harmless and cannot trigger a replay.
    websocket.receive({"type": "response", "id": command["id"], "result": {"tabId": 7}})
    websocket.finish()
    socket_thread.join(timeout=1.0)


def test_rpc_deadline_includes_waiting_for_the_serial_command_lock():
    hub = extension_daemon.Hub("offline-test-token")
    hub.rpc_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="waiting to run"):
            hub.call("create_tab", {"url": "https://example.invalid"}, timeout=0.02)
    finally:
        hub.rpc_lock.release()
    assert time.monotonic() - started < 0.5
    assert hub.pending == {}


def test_rpc_does_not_send_when_lock_reports_acquisition_after_deadline():
    hub = extension_daemon.Hub("offline-test-token")
    websocket, socket_thread = connect(hub)

    class LateLock:
        def acquire(self, timeout):
            time.sleep(timeout + 0.005)
            return True

        def release(self):
            pass

    hub.rpc_lock = LateLock()
    with pytest.raises(TimeoutError, match="waiting to run"):
        hub.call("create_tab", {}, timeout=0.02)
    assert websocket.sent.empty()
    assert hub.pending == {}
    websocket.finish()
    socket_thread.join(timeout=1.0)


def test_malformed_response_result_is_rejected_and_pending_is_cleaned():
    hub = extension_daemon.Hub("offline-test-token")
    websocket, socket_thread = connect(hub)
    outcome = {}

    def run_rpc():
        try:
            hub.call("list_tabs", {}, timeout=1.0)
        except Exception as exc:
            outcome["error"] = exc

    rpc_thread = threading.Thread(target=run_rpc)
    rpc_thread.start()
    wait_until(lambda: not websocket.sent.empty() or bool(outcome))
    assert "error" not in outcome, repr(outcome)
    command = websocket.sent.get(timeout=1.0)
    websocket.receive({"type": "response", "id": command["id"], "result": ["malformed"]})
    rpc_thread.join(timeout=1.0)

    assert not rpc_thread.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "invalid response" in str(outcome["error"])
    assert hub.pending == {}
    websocket.finish()
    socket_thread.join(timeout=1.0)


def test_failed_send_marks_connection_down_without_retry():
    hub = extension_daemon.Hub("offline-test-token")
    close_entered = threading.Event()
    close_release = threading.Event()
    websocket = FakeWebSocket(
        hub.token,
        fail_send=True,
        close_entered=close_entered,
        close_release=close_release,
    )
    socket_thread = start_socket(hub, websocket)
    websocket.receive({"type": "hello", "extensionId": "offline-test", "version": "1.0.0"})
    wait_until(hub.connected)

    outcome = {}

    def run_rpc():
        try:
            hub.call("create_tab", {"url": "https://example.invalid"}, timeout=0.2)
        except Exception as exc:
            outcome["error"] = exc

    rpc_thread = threading.Thread(target=run_rpc)
    rpc_thread.start()
    assert close_entered.wait(timeout=1.0)
    assert hub.status()["connected"] is False
    close_release.set()
    rpc_thread.join(timeout=1.0)

    assert not rpc_thread.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "connection failed" in str(outcome["error"])
    assert websocket.close_calls
    assert hub.pending == {}
    assert not hub.connected()
    websocket.finish()
    socket_thread.join(timeout=1.0)


def test_client_call_uses_call_response_as_liveness_check_and_never_retries(monkeypatch):
    paths = []
    bodies = []
    request_timeouts = []
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", True)

    def successful_request(path, body=None, timeout=20.0):
        paths.append(path)
        bodies.append(body)
        request_timeouts.append(timeout)
        return {"result": {"tabs": []}}

    monkeypatch.setattr(extension_client, "request", successful_request)
    assert extension_client.call("list_tabs", timeout=0.75) == {"tabs": []}
    assert paths == ["/call"]
    assert bodies[0]["method"] == "list_tabs"
    assert bodies[0]["params"] == {}
    assert 0 < bodies[0]["timeout"] <= 0.75
    assert request_timeouts == [bodies[0]["timeout"]]

    paths.clear()
    bodies.clear()

    def ambiguous_request(path, body=None, timeout=20.0):
        paths.append(path)
        raise urllib.error.URLError("offline injected timeout")

    monkeypatch.setattr(extension_client, "request", ambiguous_request)
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", True)
    with pytest.raises(urllib.error.URLError):
        extension_client.call("create_tab", {"url": "https://example.invalid"})
    assert paths == ["/call"]
    assert extension_client._DAEMON_KNOWN is False


def test_client_does_not_dispatch_when_startup_consumes_call_deadline(monkeypatch):
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", False)
    dispatched = []

    def slow_ensure():
        time.sleep(0.03)
        return {"daemon": True}

    monkeypatch.setattr(extension_client, "ensure_daemon", slow_ensure)
    monkeypatch.setattr(extension_client, "request", lambda *args, **kwargs: dispatched.append(args))

    with pytest.raises(TimeoutError, match="before dispatch"):
        extension_client.call("create_tab", {"url": "https://example.invalid"}, timeout=0.01)

    assert dispatched == []


def test_api_forwards_the_validated_timeout_into_hub_call(monkeypatch):
    calls = []
    statuses = []
    body = json.dumps({"method": "list_tabs", "params": {}, "timeout": 0.75}).encode("utf-8")
    fake_hub = SimpleNamespace(
        call=lambda method, params, timeout: calls.append((method, params, timeout)) or {"tabs": []}
    )
    monkeypatch.setattr(extension_daemon, "TOKEN", "offline-test-token")
    monkeypatch.setattr(extension_daemon, "HUB", fake_hub)

    handler = object.__new__(extension_daemon.ApiHandler)
    handler.headers = {
        "Authorization": "Bearer offline-test-token",
        "Content-Length": str(len(body)),
    }
    handler.path = "/call"
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.send_response = statuses.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None

    handler.do_POST()

    assert statuses == [200]
    assert calls == [("list_tabs", {}, 0.75)]
    assert json.loads(handler.wfile.getvalue()) == {"result": {"tabs": []}}


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("offline timeout"),
        ConnectionResetError("offline reset"),
        http.client.RemoteDisconnected("offline disconnect"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_client_invalidates_warm_daemon_cache_after_transport_failure(monkeypatch, failure):
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", True)
    paths = []

    def fail_request(path, *_args, **_kwargs):
        paths.append(path)
        raise failure

    monkeypatch.setattr(extension_client, "request", fail_request)
    with pytest.raises(type(failure)):
        extension_client.call("create_tab", {"url": "https://example.invalid"}, timeout=0.5)
    assert paths == ["/call"]
    assert extension_client._DAEMON_KNOWN is False


@pytest.mark.parametrize("timeout", [0, -1, math.inf, -math.inf, math.nan, 60.1, 10**1000, True, "1"])
def test_client_rejects_invalid_timeout_before_dispatch(monkeypatch, timeout):
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", True)
    dispatched = []
    monkeypatch.setattr(extension_client, "request", lambda *args, **kwargs: dispatched.append(args))
    with pytest.raises(ValueError, match="timeout"):
        extension_client.call("create_tab", timeout=timeout)
    assert dispatched == []


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, 60.1, 10**1000, True, "1"])
def test_hub_rejects_invalid_timeout_without_sending(timeout):
    hub = extension_daemon.Hub("offline-test-token")
    websocket, socket_thread = connect(hub)
    with pytest.raises(ValueError, match="timeout"):
        hub.call("create_tab", {}, timeout=timeout)
    assert websocket.sent.empty()
    assert hub.pending == {}
    websocket.finish()
    socket_thread.join(timeout=1.0)


def test_concurrent_client_startup_launches_one_daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", False)
    monkeypatch.setattr(extension_client, "STATE", tmp_path)
    starts = []
    status_calls = []

    def fake_status():
        status_calls.append(None)
        return {"daemon": bool(starts), "connected": False}

    def fake_popen(*_args, **_kwargs):
        starts.append(None)
        return object()

    monkeypatch.setattr(extension_client, "status", fake_status)
    monkeypatch.setattr(extension_client.subprocess, "Popen", fake_popen)
    results = []
    threads = [threading.Thread(target=lambda: results.append(extension_client.ensure_daemon())) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(starts) == 1
    assert len(results) == 5
    assert all(result.get("daemon") for result in results)
    assert status_calls


def test_explicit_ensure_refreshes_a_stale_warm_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_client, "_DAEMON_KNOWN", True)
    monkeypatch.setattr(extension_client, "STATE", tmp_path)
    starts = []

    def fake_status():
        return {"daemon": bool(starts), "connected": False}

    monkeypatch.setattr(extension_client, "status", fake_status)
    monkeypatch.setattr(extension_client.subprocess, "Popen", lambda *_args, **_kwargs: starts.append(None))

    assert extension_client.ensure_daemon()["daemon"] is True
    assert len(starts) == 1


def test_websocket_bind_failure_does_not_create_cold_start_credentials(monkeypatch):
    credentials_calls = []

    def fail_bind(*_args, **_kwargs):
        raise OSError("offline injected address-in-use failure")

    monkeypatch.setattr(extension_daemon, "serve", fail_bind)
    monkeypatch.setattr(extension_daemon, "TOKEN", None)
    monkeypatch.setattr(extension_daemon, "HUB", None)
    monkeypatch.setattr(extension_daemon, "ensure_credentials", lambda: credentials_calls.append(None))
    with pytest.raises(OSError, match="address-in-use"):
        extension_daemon.main()
    assert credentials_calls == []


def test_pid_cleanup_does_not_remove_a_newer_daemon_pid(monkeypatch, tmp_path):
    pid_file = tmp_path / "extension-daemon.pid"
    monkeypatch.setattr(extension_daemon, "PID_FILE", pid_file)
    extension_daemon._write_pid_file(123)
    assert pid_file.read_text(encoding="ascii") == "123"

    pid_file.write_text("456", encoding="ascii")
    extension_daemon._remove_pid_file(123)
    assert pid_file.read_text(encoding="ascii") == "456"

    extension_daemon._remove_pid_file(456)
    assert not pid_file.exists()


def test_daemon_startup_failure_leaves_no_pid_file(monkeypatch, tmp_path):
    pid_file = tmp_path / "extension-daemon.pid"
    monkeypatch.setattr(extension_daemon, "PID_FILE", pid_file)
    monkeypatch.setattr(extension_daemon, "TOKEN", None)
    monkeypatch.setattr(extension_daemon, "HUB", None)
    monkeypatch.setattr(extension_daemon, "ensure_credentials", lambda: "offline-test-token")

    class FakeWebSocketServer:
        def __init__(self):
            self.stopped = threading.Event()

        def serve_forever(self):
            self.stopped.wait(timeout=1.0)

        def shutdown(self):
            self.stopped.set()

    websocket_server = FakeWebSocketServer()
    monkeypatch.setattr(extension_daemon, "serve", lambda *_args: websocket_server)

    class FailingApiServer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("offline injected bind failure")

    monkeypatch.setattr(extension_daemon, "ThreadingHTTPServer", FailingApiServer)

    with pytest.raises(OSError, match="injected bind failure"):
        extension_daemon.main()

    assert not pid_file.exists()
    assert websocket_server.stopped.is_set()
