from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.registration import BrowserRegistrationAdapter, BrowserRegistrationFlow, RegistrationContext, RegistrationResult
import core.registration.flows as flows_module
from core.registration.helpers import build_otp_callback


def test_browser_flow_wires_phone_callback_and_runs_cleanup(monkeypatch):
    events = []

    def fake_build_phone_callbacks(ctx, *, service=None):
        events.append(("build", service))
        return (lambda: "18885551234", lambda: events.append(("cleanup", service)))

    monkeypatch.setattr(flows_module, "build_phone_callbacks", fake_build_phone_callbacks)

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
        ),
        config=RegisterConfig(executor_type="headless", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    def build_worker(ctx, artifacts):
        assert callable(artifacts.phone_callback)
        return SimpleNamespace(phone_callback=artifacts.phone_callback)

    def run_worker(worker, ctx, artifacts):
        events.append(("callback", worker.phone_callback()))
        return {"email": ctx.identity.email, "password": ctx.password}

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        browser_worker_builder=build_worker,
        browser_register_runner=run_worker,
    )

    result = BrowserRegistrationFlow(adapter).run(ctx)

    assert result.email == "user@example.com"
    assert ("build", "chatgpt") in events
    assert ("callback", "18885551234") in events
    assert ("cleanup", "chatgpt") in events


def test_chatgpt_browser_worker_receives_keep_browser_open_on_failure_flag():
    from platforms.chatgpt.plugin import ChatGPTPlatform

    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_browser_registration_adapter(platform)
    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform,
        identity=SimpleNamespace(email="user@example.com", identity_provider="mailbox"),
        config=RegisterConfig(
            executor_type="headed",
            extra={"keep_browser_open_on_failure": True},
        ),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )
    artifacts = SimpleNamespace(otp_callback=None, phone_callback=None)

    worker = adapter.browser_worker_builder(ctx, artifacts)

    assert worker.keep_browser_open_on_failure is True


def test_build_otp_callback_resends_after_short_timeout_window():
    events = []

    class FakeMailbox:
        def __init__(self):
            self.calls = 0

        def wait_for_code(self, account, **kwargs):
            self.calls += 1
            events.append(("wait", kwargs.get("timeout"), kwargs.get("before_ids")))
            if self.calls == 1:
                raise TimeoutError("otp timeout")
            return "654321"

        def get_current_ids(self, account):
            return {"msg-after-resend"}

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=FakeMailbox()),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
            mailbox_account=SimpleNamespace(email="user@example.com"),
            before_ids={"msg-before"},
        ),
        config=RegisterConfig(executor_type="headed", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: events.append(("log", message)),
    )

    otp_callback = build_otp_callback(
        ctx,
        timeout=90,
        resend_after=30,
        wait_message="等待验证码...",
        success_label="验证码",
    )
    otp_callback.set_resend_callback(lambda: events.append(("resend", None)))

    code = otp_callback()

    assert code == "654321"
    assert ("wait", 30, {"msg-before"}) in events
    assert ("resend", None) in events
    assert ("wait", 60, {"msg-after-resend"}) in events
