"""
ChatGPT Plus 自动支付 — 协议模式（纯 HTTP，完全模拟浏览器）
流程: Stripe hosted 链接 → 选 PayPal → 填地址提交 → 跟踪重定向到 PayPal → 填表单 → 完成
使用 curl_cffi Session 保持 cookie/TLS 指纹一致性，完全模拟 Chrome 浏览器行为
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session

logger = logging.getLogger(__name__)


@dataclass
class ProtocolPaymentConfig:
    """协议模式支付配置"""
    country: str = "US"
    proxy: Optional[str] = None
    headless: bool = True
    payment_timeout: int = 300
    phone: str = ""
    card_number: str = ""
    card_expiry: str = ""
    card_cvv: str = ""
    # 接码配置
    sms_provider: str = ""
    sms_api_key: str = ""
    sms_country: str = ""
    sms_service: str = "pp"
    sms_max_price: float = -1
    sms_proxy: str = ""
    uukg_codes: str = ""


@dataclass
class ProtocolPaymentResult:
    """协议模式支付结果"""
    success: bool = False
    hosted_url: str = ""
    paypal_email: str = ""
    paypal_password: str = ""
    error: str = ""
    subscription_status: str = ""
    debug_log: List[str] = field(default_factory=list)


_US_STATE_ABBR_TO_FULL = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _rand_email() -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(chars) for _ in range(16)) + "@gmail.com"


def _rand_password() -> str:
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    digits = "0123456789"
    specials = "!@#$%^"
    pool = upper + lower + digits + specials
    required = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(specials),
    ]
    required.extend(secrets.choice(pool) for _ in range(10))
    lst = list(required)
    secrets.SystemRandom().shuffle(lst)
    return "".join(lst)


def _resolve_state(state: str, state_abbr: str) -> str:
    abbr = (state_abbr or state or "").strip().upper()
    if abbr in _US_STATE_ABBR_TO_FULL:
        return _US_STATE_ABBR_TO_FULL[abbr]
    return state or state_abbr or ""


def _create_sms_controller(config: ProtocolPaymentConfig, log_fn):
    """根据配置创建接码控制器，返回 PhoneCallbackController 或 None"""
    if not config.sms_provider:
        return None
    if config.sms_provider == "uukg":
        if not config.uukg_codes and not config.sms_api_key:
            return None
    elif not config.sms_api_key:
        return None
    from core.base_sms import PhoneCallbackController
    sms_config = {
        f"{config.sms_provider}_api_key": config.sms_api_key,
        "sms_country": config.sms_country,
        "sms_service": config.sms_service or "pp",
        "sms_proxy": config.sms_proxy or config.proxy or "",
        "proxy": config.proxy or "",
        "uukg_codes": config.uukg_codes,
        "uukg_api_key": config.sms_api_key,
    }
    return PhoneCallbackController(
        config.sms_provider,
        sms_config,
        service=config.sms_service or "pp",
        country=config.sms_country,
        log_fn=log_fn,
    )


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


# ============================================================
# 浏览器模拟 Session 工厂
# ============================================================

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _create_browser_session(proxy: Optional[str] = None) -> Session:
    """创建完全模拟 Chrome 的 curl_cffi Session"""
    session = Session(
        impersonate="chrome124",
        proxies=_build_proxies(proxy),
        timeout=30,
    )
    session.headers.update({
        "User-Agent": _CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Chromium";v="125", "Google Chrome";v="125", "Not=A?Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def _human_delay(lo: float = 0.5, hi: float = 2.0):
    """模拟人类操作间隔，避免机器行为特征"""
    import random
    time.sleep(random.uniform(lo, hi))


def _navigate_headers(referer: str = "", target_url: str = "") -> dict:
    """模拟浏览器导航请求头，根据 referer 和 target 自动判断 Sec-Fetch-Site"""
    if not referer:
        site = "none"
    elif referer and target_url:
        ref_host = urllib.parse.urlparse(referer).netloc
        tgt_host = urllib.parse.urlparse(target_url).netloc
        if ref_host == tgt_host:
            site = "same-origin"
        elif ref_host.split(".")[-2:] == tgt_host.split(".")[-2:]:
            site = "same-site"
        else:
            site = "cross-site"
    else:
        site = "cross-site"
    h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": site,
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


def _xhr_headers(origin: str, referer: str) -> dict:
    """模拟浏览器 XHR/fetch 请求头"""
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": origin,
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }



# ============================================================
# Stripe 辅助
# ============================================================

def _extract_stripe_page_id(url: str) -> str:
    """从 Stripe hosted URL 提取 payment page ID (cs_live_xxx)"""
    m = re.search(r'/(cs_(?:live|test)_[A-Za-z0-9]+)', url)
    return m.group(1) if m else ""


def _decode_stripe_fragment(hosted_url: str) -> dict:
    """从 Stripe hosted URL 的 fragment 解码配置（XOR 0x05 + base64）"""
    if "#" not in hosted_url:
        return {}
    fragment = urllib.parse.unquote(hosted_url.split("#", 1)[1])
    try:
        padded = fragment + "=" * (4 - len(fragment) % 4)
        raw = base64.urlsafe_b64decode(padded)
        decoded = bytes(b ^ 0x05 for b in raw).decode("ascii", errors="replace")
        return json.loads(decoded)
    except Exception:
        return {}


# ============================================================
# Stripe 协议流程
# ============================================================

def _stripe_load_checkout(session: Session, hosted_url: str, log_fn) -> Tuple[str, str]:
    """
    加载 Stripe hosted checkout 页面，返回 (html, final_url)
    模拟浏览器直接导航到该 URL
    """
    log_fn("[协议] 加载 Stripe checkout 页面...")
    resp = session.get(
        hosted_url,
        headers=_navigate_headers(),
        allow_redirects=True,
    )
    resp.raise_for_status()
    log_fn(f"[协议] Stripe 页面状态: {resp.status_code}, URL: {resp.url[:80]}...")
    return resp.text, str(resp.url)


def _stripe_init_session(session: Session, hosted_url: str, page_id: str, pk: str, log_fn) -> dict:
    """调用 Stripe init API 获取 checkout session 数据"""
    eid = str(uuid.uuid4())
    resp = session.post(
        f"https://api.stripe.com/v1/payment_pages/{page_id}/init",
        data={"key": pk, "eid": eid, "browser_locale": "en-US"},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://pay.openai.com",
            "Referer": hosted_url,
        },
    )
    if resp.status_code != 200:
        raise ValueError(f"Stripe init 失败: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    data["_eid"] = eid
    return data


def _stripe_create_paypal_pm(session: Session, pk: str, identity, config, hosted_url: str) -> str:
    """创建 PayPal 类型的 PaymentMethod，返回 pm_xxx ID"""
    state = getattr(identity, "state_abbr", "") or getattr(identity, "state", "")
    resp = session.post(
        "https://api.stripe.com/v1/payment_methods",
        data={
            "type": "paypal",
            "key": pk,
            "billing_details[address][country]": config.country or "US",
            "billing_details[address][line1]": identity.street or "100 Main St",
            "billing_details[address][city]": identity.city or "Portland",
            "billing_details[address][state]": state or "OR",
            "billing_details[address][postal_code]": identity.zipcode or "97204",
            "billing_details[name]": identity.full_name or "John Smith",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://pay.openai.com",
            "Referer": hosted_url,
        },
    )
    if resp.status_code != 200:
        raise ValueError(f"创建 PaymentMethod 失败: {resp.status_code} {resp.text[:300]}")
    return resp.json()["id"]


def _stripe_confirm_paypal(
    session: Session,
    hosted_url: str,
    page_html: str,
    identity,
    config: ProtocolPaymentConfig,
    log_fn,
) -> tuple[str, str]:
    """
    Stripe checkout: 解码 fragment 获取 pk → init → 创建 PM → confirm → 提取 PayPal redirect
    返回 (pm_redirect_url, paypal_url)
    """
    page_id = _extract_stripe_page_id(hosted_url)
    if not page_id:
        raise ValueError("无法从 URL 提取 Stripe session ID")

    # 从 fragment XOR 解码获取 publishable key
    frag_data = _decode_stripe_fragment(hosted_url)
    pk = frag_data.get("apiKey", "")
    if not pk:
        log_fn("[协议] fragment 解码未找到 apiKey，尝试从 HTML 提取...")
        m = re.search(r'pk_live_[A-Za-z0-9]+', page_html)
        pk = m.group(0) if m else ""
    if not pk:
        raise ValueError("无法获取 Stripe publishable key")
    log_fn(f"[协议] Stripe PK: {pk[:30]}...")

    # Init session
    log_fn("[协议] 调用 Stripe init...")
    init_data = _stripe_init_session(session, hosted_url, page_id, pk, log_fn)
    init_checksum = init_data.get("init_checksum", "")
    eid = init_data["_eid"]

    invoice = init_data.get("invoice") or {}
    amount_due = invoice.get("amount_due", 2000)
    bca = invoice.get("billing_cycle_anchor")
    log_fn(f"[协议] init 成功: amount={amount_due}, checksum={init_checksum[:16]}...")

    # 创建 PayPal PaymentMethod
    _human_delay(1.0, 2.0)
    log_fn("[协议] 创建 PayPal PaymentMethod...")
    pm_id = _stripe_create_paypal_pm(session, pk, identity, config, hosted_url)
    log_fn(f"[协议] PM: {pm_id}")

    # Confirm
    _human_delay(1.0, 2.5)
    if bca:
        expected_amount = 0
        extra = {"expected_amount_on_bca": str(amount_due)}
    else:
        expected_amount = amount_due
        extra = {}

    confirm_data = {
        "eid": eid,
        "payment_method": pm_id,
        "expected_amount": str(expected_amount),
        "consent[terms_of_service]": "accepted",
        "key": pk,
        "init_checksum": init_checksum,
    }
    confirm_data.update(extra)

    log_fn(f"[协议] 提交 Stripe confirm (PayPal, expected_amount={expected_amount})...")
    resp = session.post(
        f"https://api.stripe.com/v1/payment_pages/{page_id}/confirm",
        data=confirm_data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://pay.openai.com",
            "Referer": hosted_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        },
        allow_redirects=False,
    )

    log_fn(f"[协议] Stripe confirm 响应: {resp.status_code}")

    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except Exception as e:
            raise ValueError(f"解析 Stripe confirm JSON 失败: {e}")

        text = json.dumps(data)
        pm_redirects = re.findall(r'https?://pm-redirects\.stripe\.com/[^"\\]+', text)
        if pm_redirects:
            redirect_url = pm_redirects[0]
            log_fn(f"[协议] 获取到 PM redirect: {redirect_url[:80]}...")
            # 不用 HTTP 消费 redirect，直接返回给浏览器使用
            return redirect_url, ""

        redirect_url = (
            data.get("redirect_url")
            or data.get("next_action", {}).get("redirect_to_url", {}).get("url", "")
        )
        if redirect_url:
            log_fn(f"[协议] 获取到重定向 URL: {redirect_url[:80]}...")
            paypal_url = _follow_stripe_redirect(session, redirect_url, hosted_url, log_fn)
            return redirect_url, paypal_url

        log_fn(f"[协议] confirm 响应无重定向 URL, status={data.get('status')}")

    elif resp.status_code in (302, 303, 307):
        redirect_url = resp.headers.get("Location", "")
        if redirect_url:
            log_fn(f"[协议] 302 重定向到: {redirect_url[:80]}...")
            paypal_url = _follow_stripe_redirect(session, redirect_url, hosted_url, log_fn)
            return redirect_url, paypal_url

    raise ValueError(f"Stripe confirm 未返回 PayPal 重定向 (status={resp.status_code})")


def _follow_stripe_redirect(session: Session, url: str, referer: str, log_fn) -> str:
    """
    跟踪 pm-redirects.stripe.com 的 302 重定向，提取 PayPal URL。
    不加载 PayPal 页面（DataDome 会拦截），只返回 URL 给浏览器使用。
    """
    log_fn(f"[协议] 跟踪重定向: {url[:80]}...")
    _human_delay(0.5, 1.5)

    resp = session.get(
        url,
        headers=_navigate_headers(referer, url),
        allow_redirects=False,
        timeout=30,
    )

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "")
        if location:
            log_fn(f"[协议] 302 重定向到: {location[:100]}")
            return location

    log_fn(f"[协议] pm-redirects 未返回 302 (status={resp.status_code})，尝试 allow_redirects...")
    resp = session.get(
        url,
        headers=_navigate_headers(referer, url),
        allow_redirects=True,
        timeout=30,
    )
    final_url = str(resp.url)
    log_fn(f"[协议] 最终 URL: {final_url[:100]}")
    return final_url




# ============================================================
# 主入口
# ============================================================

def auto_pay_plus_protocol(
    account,
    config: ProtocolPaymentConfig,
    log_fn: Callable[[str], None] = print,
) -> ProtocolPaymentResult:
    """
    协议模式自动支付 ChatGPT Plus
    完全模拟浏览器，使用 curl_cffi 的 TLS 指纹模拟
    """
    result = ProtocolPaymentResult()

    # 1. 获取虚拟身份
    log_fn("[协议] 获取虚拟身份...")
    try:
        from providers.address.meiguodizhi import fetch_virtual_identity
        identity = fetch_virtual_identity(proxy=config.proxy)
        log_fn(f"[协议] 身份: {identity.full_name}, {identity.city}, {identity.state}")
    except Exception as e:
        result.error = f"获取虚拟身份失败: {e}"
        log_fn(f"[协议] {result.error}")
        return result

    # 2. 生成 Stripe hosted 链接
    log_fn("[协议] 生成 Stripe hosted 链接...")
    try:
        from platforms.chatgpt.payment import generate_plus_hosted_link
        hosted_url = generate_plus_hosted_link(account, proxy=config.proxy, country=config.country)
        result.hosted_url = hosted_url
        log_fn(f"[协议] Stripe 链接: {hosted_url[:80]}...")
    except Exception as e:
        result.error = f"生成支付链接失败: {e}"
        log_fn(f"[协议] {result.error}")
        return result

    # 3. 协议模式流程
    paypal_email = _rand_email()
    paypal_password = _rand_password()
    result.paypal_email = paypal_email
    result.paypal_password = paypal_password

    session = _create_browser_session(config.proxy)
    sms_controller = _create_sms_controller(config, log_fn)

    try:
        result = _protocol_flow(session, hosted_url, identity, config, paypal_email, paypal_password, log_fn, result, sms_controller)
    except Exception as e:
        result.error = f"协议流程异常: {e}"
        log_fn(f"[协议] {result.error}")
        import traceback
        log_fn(f"[协议] 堆栈: {traceback.format_exc()}")
    finally:
        if sms_controller:
            sms_controller.cleanup()
        session.close()

    return result


def _protocol_flow(
    session: Session,
    hosted_url: str,
    identity,
    config: ProtocolPaymentConfig,
    paypal_email: str,
    paypal_password: str,
    log_fn: Callable[[str], None],
    result: ProtocolPaymentResult,
    sms_controller=None,
) -> ProtocolPaymentResult:
    """混合模式支付流程：协议模式处理 Stripe，浏览器模式处理 PayPal"""

    # === Step 1: 协议模式 — 加载 Stripe checkout 页面 ===
    stripe_html, stripe_url = _stripe_load_checkout(session, hosted_url, log_fn)

    # === Step 2: 协议模式 — Stripe confirm → 获取 PayPal URL ===
    pm_redirect_url, paypal_url = _stripe_confirm_paypal(
        session, hosted_url, stripe_html, identity, config, log_fn,
    )
    if paypal_url:
        log_fn(f"[协议] PayPal URL: {paypal_url[:100]}")
    log_fn(f"[协议] PM redirect URL: {pm_redirect_url[:80]}...")

    # === Step 3: 浏览器模式 — Playwright 处理 PayPal ===
    log_fn("[协议] 切换到浏览器模式处理 PayPal...")
    success = _browser_paypal_flow(
        pm_redirect_url, paypal_url, identity, config,
        paypal_email, paypal_password, log_fn,
        sms_controller=sms_controller,
    )

    if success:
        result.success = True
        result.subscription_status = "plus"
        log_fn("[协议] 支付成功！ChatGPT Plus 已激活")
    else:
        result.error = "PayPal 支付流程未确认成功"
        log_fn(f"[协议] {result.error}")

    return result


def _browser_paypal_flow(
    pm_redirect_url: str,
    paypal_url: str,
    identity,
    config: ProtocolPaymentConfig,
    paypal_email: str,
    paypal_password: str,
    log_fn: Callable[[str], None],
    sms_controller=None,
) -> bool:
    """使用 Playwright 浏览器处理 PayPal 支付流程，从 pm-redirects 自然跳转绕过 DataDome"""
    from playwright.sync_api import sync_playwright
    from platforms.chatgpt.auto_payment import (
        _handle_paypal_pay_page,
        _handle_paypal_checkout_page,
        _wait_for_paypal_checkout,
        _wait_for_success,
        _handle_sms_verification,
        _JS_HIDE_CAPTCHA,
        PaymentConfig,
    )

    browser_config = PaymentConfig(
        country=config.country,
        proxy=config.proxy,
        headless=config.headless,
        payment_timeout=config.payment_timeout,
        phone=config.phone,
        card_number=config.card_number,
        card_expiry=config.card_expiry,
        card_cvv=config.card_cvv,
    )

    with sync_playwright() as p:
        launch_args = {
            "headless": config.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        }
        if config.proxy:
            launch_args["proxy"] = {"server": config.proxy}

        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_CHROME_UA,
            locale="en-US",
            timezone_id="America/New_York",
        )

        from playwright_stealth import Stealth
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            # 从 pm-redirects.stripe.com 开始导航，让浏览器自然跟随 302 到 PayPal
            # 这样 Referer 是 stripe.com，导航类型是 redirect，更接近真实用户行为
            nav_url = pm_redirect_url or paypal_url
            log_fn(f"[浏览器] 从 Stripe redirect 导航到 PayPal...")
            page.goto(nav_url, timeout=60000, referer="https://pay.openai.com/")
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.evaluate(_JS_HIDE_CAPTCHA)

            current_url = page.url
            log_fn(f"[浏览器] PayPal 页面加载完成: {current_url[:80]}")

            if "/agreements/approve" in current_url:
                log_fn("[浏览器] agreements/approve 页面，等待跳转...")
                for _ in range(15):
                    page.wait_for_timeout(2000)
                    new_url = page.url
                    if new_url != current_url:
                        current_url = new_url
                        log_fn(f"[浏览器] 跳转到: {current_url[:80]}")
                        break
                else:
                    log_fn("[浏览器] agreements 页面未跳转，尝试直接处理")

            if "/pay" in current_url and "/checkoutweb" not in current_url:
                log_fn("[浏览器] PayPal /pay 邮箱页面...")
                _handle_paypal_pay_page(page, paypal_email, log_fn)

                log_fn("[浏览器] 等待 PayPal checkout 页面...")
                checkout_page = _wait_for_paypal_checkout(page, context, log_fn, timeout=30)
                if checkout_page:
                    checkout_page.evaluate(_JS_HIDE_CAPTCHA)
                    _handle_paypal_checkout_page(
                        checkout_page, identity, browser_config,
                        paypal_email, paypal_password, log_fn,
                        sms_controller=sms_controller,
                    )
                else:
                    log_fn("[浏览器] 未跳转到 checkout 页面，尝试当前页面")

            elif "/checkoutweb" in current_url:
                log_fn("[浏览器] PayPal /checkoutweb 页面...")
                _handle_paypal_checkout_page(
                    page, identity, browser_config,
                    paypal_email, paypal_password, log_fn,
                    sms_controller=sms_controller,
                )
            else:
                log_fn(f"[浏览器] 未知 PayPal 页面: {current_url[:100]}")
                _handle_paypal_checkout_page(
                    page, identity, browser_config,
                    paypal_email, paypal_password, log_fn,
                    sms_controller=sms_controller,
                )

            log_fn("[浏览器] 等待支付结果...")
            if _wait_for_success(context, timeout=config.payment_timeout):
                return True
            else:
                log_fn("[浏览器] 支付确认超时")
                return False

        except Exception as e:
            log_fn(f"[浏览器] PayPal 流程异常: {e}")
            import traceback
            log_fn(f"[浏览器] 堆栈: {traceback.format_exc()}")
            return False
        finally:
            browser.close()
