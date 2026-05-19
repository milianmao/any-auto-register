from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.base_platform import RegisterConfig
from platforms.chatgpt import browser_register as browser_register_module
from platforms.chatgpt.plugin import (
    ChatGPTPlatform,
    _assert_complete_oauth_callback,
    _generate_chatgpt_registration_password,
)
from platforms.chatgpt.register import RegistrationEngine


DEFAULT_CHATGPT_PASSWORD = "*Yml1145909208"


def test_assert_complete_oauth_callback_accepts_complete_payload():
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "rt_123",
        "id_token": "id_123",
    })


def test_assert_complete_oauth_callback_accepts_nextauth_payload():
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "",
        "id_token": "",
    })


def test_generate_chatgpt_registration_password_meets_openai_strength_requirements():
    for _ in range(8):
        password = _generate_chatgpt_registration_password()
        assert len(password) >= 12
        assert any(ch.islower() for ch in password)
        assert any(ch.isupper() for ch in password)
        assert any(ch.isdigit() for ch in password)
        assert any(ch in ",._!@#" for ch in password)


def test_chatgpt_platform_preserves_user_supplied_password():
    platform = object.__new__(ChatGPTPlatform)
    assert platform._prepare_registration_password("Secret123!") == "Secret123!"


def test_chatgpt_platform_uses_configured_default_password_when_missing():
    platform = object.__new__(ChatGPTPlatform)
    assert platform._prepare_registration_password(None) == DEFAULT_CHATGPT_PASSWORD


def test_protocol_mailbox_mapper_accepts_nextauth_payload():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(password="Secret123!", proxy=None, log=lambda message: None)
    result = SimpleNamespace(
        email="user@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="at_123",
        refresh_token="",
        id_token="",
        session_token="sess_123",
        workspace_id="",
    )

    mapped = adapter.result_mapper(ctx, result)

    assert mapped.user_id == "acct_123"
    assert mapped.token == "at_123"
    assert mapped.extra["session_token"] == "sess_123"


def test_browser_register_run_returns_session_data_from_registered_browser(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

        def goto(self, url, **kwargs):
            self.url = url

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "chatgpt_home"})
    monkeypatch.setattr(
        browser_register_module,
        "_get_cookies",
        lambda page: {
            "__Secure-next-auth.session-token": "sess_123",
            "__Host-next-auth.csrf-token": "csrf_123",
            "_puid": "puid_123",
        },
    )
    monkeypatch.setattr(
        browser_register_module.ChatGPTBrowserRegister,
        "_collect_registered_session",
        lambda self, page, email, password: {
            "email": email,
            "password": password,
            "account_id": "acct_123",
            "access_token": "at_123",
            "refresh_token": "",
            "id_token": "at_123",
            "session_token": "sess_123",
            "workspace_id": "",
            "cookies": "__Secure-next-auth.session-token=sess_123; __Host-next-auth.csrf-token=csrf_123; _puid=puid_123",
            "profile": {"email": email, "id": "acct_123"},
        },
    )

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert result["account_id"] == "acct_123"
    assert result["access_token"] == "at_123"
    assert result["id_token"] == "at_123"
    assert result["session_token"] == "sess_123"
    assert result["cookies"].startswith("__Secure-next-auth.session-token=sess_123")
    assert result["profile"]["email"] == "user@example.com"


def test_start_browser_signup_via_page_uses_chatgpt_home_signup_entry(monkeypatch):
    events: list[tuple[str, str]] = []

    class FakePage:
        def __init__(self):
            self.url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url
            events.append(("goto", url))

    monkeypatch.setattr(browser_register_module, "_derive_registration_state_from_page", lambda page: {"page_type": ""})
    monkeypatch.setattr(browser_register_module, "_wait_for_any_selector", lambda page, selectors, timeout=12: selectors[0])
    monkeypatch.setattr(browser_register_module, "_fill_input_like_user", lambda page, selector, email: events.append(("fill", selector)) or True)
    monkeypatch.setattr(browser_register_module, "_click_first", lambda page, selectors, timeout=8: events.append(("click", selectors[0])) or selectors[0])
    monkeypatch.setattr(browser_register_module, "_submit_form_with_fallback", lambda page, selector: False)
    monkeypatch.setattr(browser_register_module, "_wait_for_signup_entry_transition", lambda page, log: {"page_type": "create_account_password"})

    state = browser_register_module._start_browser_signup_via_page(FakePage(), "user@example.com", lambda message: None)

    assert state["page_type"] == "create_account_password"
    assert ("goto", "https://chatgpt.com/") in events
    assert any(event[0] == "click" for event in events)
    assert any(event[0] == "fill" for event in events)


def test_collect_registered_session_opens_new_tab_for_session_request(monkeypatch):
    events: list[object] = []

    class FakeResponse:
        status = 200

        def text(self):
            return '{"accessToken":"at_123","user":{"email":"user@example.com","id":"acct_123"}}'

    class FakeSessionPage:
        def __init__(self, context):
            self.context = context
            self.url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url
            events.append(("goto", url))
            return FakeResponse()

        def close(self):
            events.append("close")

    class FakeContext:
        def __init__(self):
            self._cookies = [
                {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
                {"name": "__Host-next-auth.csrf-token", "value": "csrf_123"},
                {"name": "_puid", "value": "puid_123"},
            ]

        def cookies(self):
            return list(self._cookies)

        def new_page(self):
            events.append("new_page")
            return FakeSessionPage(self)

    class FakePage:
        def __init__(self):
            self.context = FakeContext()

    monkeypatch.setattr(
        browser_register_module,
        "_fetch_chatgpt_profile",
        lambda access_token, proxy=None: {"email": "user@example.com", "id": "acct_123"},
    )

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    result = worker._collect_registered_session(FakePage(), "user@example.com", "Secret123!")

    assert "new_page" in events
    assert ("goto", "https://chatgpt.com/api/auth/session") in events
    assert "close" in events
    assert result["account_id"] == "acct_123"
    assert result["access_token"] == "at_123"
    assert result["session_token"] == "sess_123"
    assert result["session_payload"]["accessToken"] == "at_123"
    assert result["session_payload"]["user"]["email"] == "user@example.com"


def test_collect_registered_session_waits_30_seconds_before_reading_cookie(monkeypatch):
    sleep_calls: list[float] = []

    class FakeResponse:
        status = 200

        def text(self):
            return '{"accessToken":"at_123","user":{"email":"user@example.com","id":"acct_123"}}'

    class FakeSessionPage:
        def __init__(self, context):
            self.context = context
            self.url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url
            return FakeResponse()

        def close(self):
            return None

    class FakeContext:
        def __init__(self):
            self.cookie_ready = False

        def cookies(self):
            if not self.cookie_ready:
                return []
            return [{"name": "__Secure-next-auth.session-token", "value": "sess_123"}]

        def new_page(self):
            return FakeSessionPage(self)

    class FakePage:
        def __init__(self):
            self.context = FakeContext()

    fake_page = FakePage()

    def fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        fake_page.context.cookie_ready = True

    monkeypatch.setattr(browser_register_module.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        browser_register_module,
        "_fetch_chatgpt_profile",
        lambda access_token, proxy=None: {"email": "user@example.com", "id": "acct_123"},
    )

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    result = worker._collect_registered_session(fake_page, "user@example.com", "Secret123!")

    assert result is not None
    assert result["session_token"] == "sess_123"
    assert sleep_calls == [30]


def test_map_chatgpt_result_preserves_session_payload():
    platform = object.__new__(ChatGPTPlatform)

    mapped = platform._map_chatgpt_result({
        "email": "user@example.com",
        "password": "Secret123!",
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "",
        "id_token": "at_123",
        "session_token": "sess_123",
        "workspace_id": "",
        "cookies": "__Secure-next-auth.session-token=sess_123",
        "profile": {"email": "user@example.com"},
        "session_payload": {
            "accessToken": "at_123",
            "user": {"email": "user@example.com", "id": "acct_123"},
        },
    })

    assert mapped.extra["session_payload"]["accessToken"] == "at_123"
    assert mapped.extra["session_payload"]["user"]["id"] == "acct_123"


def test_browser_register_run_does_not_reopen_browser_when_session_collection_fails(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "chatgpt_home"})
    monkeypatch.setattr(
        browser_register_module.ChatGPTBrowserRegister,
        "_collect_registered_session",
        lambda self, page, email, password: None,
    )
    monkeypatch.setattr(
        browser_register_module.ChatGPTBrowserRegister,
        "_retry_oauth_fresh_browser",
        lambda self, email, password: (_ for _ in ()).throw(AssertionError("should not reopen browser")),
    )

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    with pytest.raises(RuntimeError, match="session"):
        worker.run(email="user@example.com", password="Secret123!")
def test_registration_engine_logs_proxy_value_before_network_checks(monkeypatch):
    engine = RegistrationEngine(
        email_service=object(),
        proxy_url="http://127.0.0.1:7890",
        callback_logger=lambda message: None,
    )
    monkeypatch.setattr(engine, "_check_ip_location", lambda: (False, "CN"))

    engine.run()

    assert any("注册代理 proxy_url: http://127.0.0.1:7890" in entry for entry in engine.logs)
def test_browser_register_keeps_window_open_on_failure_when_enabled(monkeypatch):
    events: list[tuple[str, object]] = []

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            events.append(("close", None))

    class FakeBrowserContext:
        def __init__(self, browser):
            self.browser = browser

        def __enter__(self):
            events.append(("enter", None))
            return self.browser

        def __exit__(self, exc_type, exc, tb):
            events.append(("exit", exc_type.__name__ if exc_type else None))
            return False

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowserContext(FakeBrowser()))
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=False,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
        keep_browser_open_on_failure=True,
    )

    with pytest.raises(RuntimeError, match="boom"):
        worker.run(email="user@example.com", password="Secret123!")

    assert ("exit", "RuntimeError") not in events


def test_request_openai_email_otp_resend_clicks_visible_resend_button(monkeypatch):
    events: list[tuple[str, object]] = []

    class FakePage:
        pass

    monkeypatch.setattr(
        browser_register_module,
        "_click_first",
        lambda page, selectors, timeout=3: events.append(("click", timeout)) or selectors[0],
    )

    clicked = browser_register_module._request_openai_email_otp_resend(FakePage(), lambda message: events.append(("log", message)))

    assert clicked is True
    assert ("click", 3) in events


def test_submit_otp_via_page_skips_hidden_single_input_candidates(monkeypatch):
    class FakeInput:
        def __init__(self, visible: bool):
            self.visible = visible
            self.value = ""

        @property
        def first(self):
            return self

        def nth(self, _index):
            return self

        def count(self):
            return 1

        def wait_for(self, state="visible", timeout=0):
            if state == "visible" and not self.visible:
                raise RuntimeError("hidden")

        def click(self, timeout=0):
            if not self.visible:
                raise RuntimeError("hidden")

        def fill(self, value):
            if not self.visible:
                raise RuntimeError("hidden")
            self.value = value

        def type(self, value, delay=0):
            if not self.visible:
                raise RuntimeError("hidden")
            self.value += value

        def input_value(self):
            return self.value

        def is_visible(self, timeout=0):
            return self.visible

    class FakeLocatorGroup:
        def __init__(self, inputs):
            self.inputs = inputs

        @property
        def first(self):
            return self.inputs[0]

        def nth(self, index):
            return self.inputs[index]

        def count(self):
            return len(self.inputs)

    class FakePage:
        def __init__(self):
            self.url = "https://auth.openai.com/u/email-otp"
            self.single_inputs = FakeLocatorGroup([FakeInput(False), FakeInput(True)])
            self.digit_inputs = FakeLocatorGroup([])

        def wait_for_load_state(self, state, timeout=0):
            return None

        def locator(self, selector):
            if "input[inputmode='numeric']" in selector:
                return self.digit_inputs
            if selector == "input[autocomplete='one-time-code']":
                return self.single_inputs
            if selector in {"input[name*='code' i]", "input[id*='code' i]", "input[type='text']", "input"}:
                return self.single_inputs
            if selector.startswith("button") or selector.startswith("text="):
                return FakeLocatorGroup([])
            return self.single_inputs

        def get_by_label(self, _pattern):
            return self.single_inputs

        def get_by_role(self, role, name=None):
            if role == "textbox":
                return self.single_inputs
            return FakeLocatorGroup([])

    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(browser_register_module, "_browser_pause", lambda page, headed=None: None)
    monkeypatch.setattr(
        browser_register_module,
        "_click_first",
        lambda page, selectors, timeout=8: setattr(page, "url", "https://chatgpt.com/") or selectors[0],
    )

    result = browser_register_module._submit_otp_via_page(FakePage(), "123456", lambda message: None)

    assert result["ok"] is True
