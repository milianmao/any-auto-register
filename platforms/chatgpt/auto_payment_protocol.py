"""
ChatGPT Plus 自动支付 — 协议模式（纯 HTTP，完全模拟浏览器）
流程: Stripe hosted 链接 → 选 PayPal → 填地址提交 → 跟踪重定向到 PayPal → 填表单 → 完成
使用 curl_cffi Session 保持 cookie/TLS 指纹一致性，完全模拟 Chrome 浏览器行为
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.parse
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


def _navigate_headers(referer: str = "") -> dict:
    """模拟浏览器导航请求头"""
    h = {
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        "Sec-Fetch-User": "?1",
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
# HTML 解析辅助（避免引入 BeautifulSoup 依赖）
# ============================================================

def _extract_hidden_inputs(html: str) -> Dict[str, str]:
    """从 HTML 中提取所有 hidden input 的 name/value"""
    inputs = {}
    pattern = r'<input[^>]*type=["\']hidden["\'][^>]*>'
    for match in re.finditer(pattern, html, re.IGNORECASE):
        tag = match.group(0)
        name_m = re.search(r'name=["\']([^"\']*)["\']', tag)
        value_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            inputs[name_m.group(1)] = value_m.group(1) if value_m else ""
    return inputs


def _extract_form_action(html: str, form_id: str = "") -> str:
    """提取表单的 action URL"""
    if form_id:
        pattern = rf'<form[^>]*id=["\']{ re.escape(form_id) }["\'][^>]*action=["\']([^"\']*)["\']'
    else:
        pattern = r'<form[^>]*action=["\']([^"\']*)["\']'
    m = re.search(pattern, html, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_meta_redirect(html: str) -> str:
    """提取 meta refresh 重定向 URL"""
    m = re.search(r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\'"\s>]+)', html, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'<meta[^>]*content=["\'][^"\']*url=([^"\'"\s>]+)["\'][^>]*http-equiv=["\']refresh["\']', html, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_js_redirect(html: str) -> str:
    """提取 JS window.location 重定向"""
    patterns = [
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.replace\s*\(\s*["\']([^"\']+)["\']',
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def _extract_stripe_page_id(url: str) -> str:
    """从 Stripe hosted URL 提取 payment page ID (cs_live_xxx)"""
    m = re.search(r'/(cs_(?:live|test)_[A-Za-z0-9]+)', url)
    return m.group(1) if m else ""


def _extract_select_options(html: str, select_id: str) -> Dict[str, str]:
    """提取 select 元素的所有 option (value -> text)"""
    pattern = rf'<select[^>]*id=["\']{ re.escape(select_id) }["\'][^>]*>(.*?)</select>'
    m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        return {}
    options_html = m.group(1)
    options = {}
    for opt in re.finditer(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>(.*?)</option>', options_html, re.DOTALL):
        options[opt.group(1)] = opt.group(2).strip()
    return options


def _find_select_value(options: Dict[str, str], text: str) -> str:
    """根据文本匹配 select option 的 value"""
    text_lower = text.lower()
    for val, label in options.items():
        if text_lower in label.lower() or text_lower in val.lower():
            return val
    return ""


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


def _stripe_extract_session_data(html: str) -> dict:
    """
    从 Stripe checkout 页面的内联 JS 中提取会话数据
    Stripe 在页面中嵌入了 JSON 配置，包含 payment_page_id、session_id 等
    """
    patterns = [
        r'window\.__STRIPE_CHECKOUT_SESSION__\s*=\s*({.*?});',
        r'"paymentPageId"\s*:\s*"([^"]+)"',
        r'"sessionId"\s*:\s*"([^"]+)"',
        r'"publishableKey"\s*:\s*"(pk_(?:live|test)_[^"]+)"',
        r'data-session-id="([^"]+)"',
        r'data-publishable-key="([^"]+)"',
    ]
    data = {}

    # 尝试提取完整的 JSON 配置
    json_pattern = r'<script[^>]*>\s*window\.__(?:STRIPE_CHECKOUT|NEXT_DATA)__\s*=\s*({.*?})\s*;?\s*</script>'
    m = re.search(json_pattern, html, re.DOTALL)
    if m:
        try:
            data["__config__"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 提取各个关键字段
    for key, pattern in [
        ("payment_page_id", r'"paymentPageId"\s*:\s*"([^"]+)"'),
        ("session_id", r'"sessionId"\s*:\s*"([^"]+)"'),
        ("publishable_key", r'"publishableKey"\s*:\s*"(pk_[^"]+)"'),
        ("api_key", r'"apiKey"\s*:\s*"(pk_[^"]+)"'),
        ("merchant_id", r'"merchantId"\s*:\s*"(acct_[^"]+)"'),
        ("payment_intent", r'"paymentIntent"\s*:\s*"(pi_[^"]+)"'),
        ("stripe_js_id", r'"stripeJsId"\s*:\s*"([^"]+)"'),
        ("auth_token", r'"authenticity_token"\s*:\s*"([^"]+)"'),
        ("csrf_token", r'name="csrf-token"\s+content="([^"]+)"'),
    ]:
        m = re.search(pattern, html)
        if m:
            data[key] = m.group(1)

    # 从 data attributes 提取
    for attr_key, attr_name in [
        ("session_id", "data-session-id"),
        ("publishable_key", "data-publishable-key"),
        ("stripe_account", "data-stripe-account"),
    ]:
        if attr_key not in data:
            m = re.search(rf'{attr_name}="([^"]+)"', html)
            if m:
                data[attr_key] = m.group(1)

    return data


def _stripe_confirm_paypal(
    session: Session,
    hosted_url: str,
    page_html: str,
    identity,
    config: ProtocolPaymentConfig,
    log_fn,
) -> str:
    """
    在 Stripe checkout 页面选择 PayPal 并提交，返回 PayPal redirect URL
    模拟浏览器提交表单 → Stripe confirm API → 跟踪重定向到 PayPal
    """
    stripe_data = _stripe_extract_session_data(page_html)
    page_id = _extract_stripe_page_id(hosted_url)

    log_fn(f"[协议] Stripe session 数据: page_id={page_id}, keys={list(stripe_data.keys())}")

    state_full = _resolve_state(identity.state, getattr(identity, 'state_abbr', ''))

    # 查找 state 对应的 Stripe select value
    state_options = _extract_select_options(page_html, "billingAdministrativeArea")
    state_value = _find_select_value(state_options, state_full) if state_options else state_full

    pk = stripe_data.get("publishable_key") or stripe_data.get("api_key", "")
    merchant_id = stripe_data.get("merchant_id", "")

    confirm_url = f"https://api.stripe.com/v1/payment_pages/{page_id}/confirm"

    # 构建完全模拟浏览器的 confirm 请求
    form_data = {
        "eid": f"NA-{secrets.token_hex(16)}",
        "payment_method": "paypal",
        "billing_address[country]": config.country or "US",
        "billing_address[line1]": identity.street,
        "billing_address[line2]": "",
        "billing_address[city]": identity.city,
        "billing_address[state]": state_value or state_full,
        "billing_address[postal_code]": identity.zipcode,
        "terms_of_service_consent[accepted]": "true",
        "key": pk,
    }

    if merchant_id:
        form_data["stripe_account"] = merchant_id

    log_fn("[协议] 提交 Stripe confirm (选择 PayPal)...")

    stripe_origin = "https://pay.openai.com"
    stripe_referer = hosted_url

    resp = session.post(
        confirm_url,
        data=form_data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": stripe_origin,
            "Referer": stripe_referer,
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
            redirect_url = (
                data.get("redirect_url")
                or data.get("next_action", {}).get("redirect_to_url", {}).get("url", "")
                or data.get("url", "")
            )
            if redirect_url:
                log_fn(f"[协议] 获取到重定向 URL: {redirect_url[:80]}...")
                return _follow_stripe_redirect(session, redirect_url, stripe_referer, log_fn)
            log_fn(f"[协议] confirm 响应内容: {json.dumps(data, ensure_ascii=False)[:500]}")
        except Exception as e:
            log_fn(f"[协议] 解析 confirm 响应失败: {e}")

    elif resp.status_code in (302, 303, 307):
        redirect_url = resp.headers.get("Location", "")
        if redirect_url:
            log_fn(f"[协议] 302 重定向到: {redirect_url[:80]}...")
            return _follow_stripe_redirect(session, redirect_url, stripe_referer, log_fn)

    raise ValueError(f"Stripe confirm 未返回 PayPal 重定向 (status={resp.status_code})")


def _follow_stripe_redirect(session: Session, url: str, referer: str, log_fn) -> str:
    """
    跟踪 Stripe → PayPal 的重定向链
    pm-redirects.stripe.com → paypal.com/agreements/approve?ba_token=xxx
    """
    current_url = url
    max_redirects = 10

    for i in range(max_redirects):
        log_fn(f"[协议] 跟踪重定向 [{i+1}]: {current_url[:80]}...")

        resp = session.get(
            current_url,
            headers=_navigate_headers(referer),
            allow_redirects=False,
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            next_url = resp.headers.get("Location", "")
            if not next_url:
                break
            if not next_url.startswith("http"):
                parsed = urllib.parse.urlparse(current_url)
                next_url = f"{parsed.scheme}://{parsed.netloc}{next_url}"
            referer = current_url
            current_url = next_url

            if "paypal.com" in current_url:
                log_fn(f"[协议] 到达 PayPal: {current_url[:100]}...")
                return current_url
            continue

        if resp.status_code == 200:
            html = resp.text
            # 检查 meta refresh 或 JS 重定向
            meta_url = _extract_meta_redirect(html)
            if meta_url:
                referer = current_url
                current_url = meta_url
                if "paypal.com" in current_url:
                    return current_url
                continue

            js_url = _extract_js_redirect(html)
            if js_url:
                referer = current_url
                current_url = js_url
                if "paypal.com" in current_url:
                    return current_url
                continue

            if "paypal.com" in current_url:
                return current_url

        break

    raise ValueError(f"重定向链未到达 PayPal (最终: {current_url[:100]})")


# ============================================================
# PayPal 协议流程
# ============================================================

def _paypal_load_page(session: Session, paypal_url: str, referer: str, log_fn) -> Tuple[str, str]:
    """
    加载 PayPal 页面，完全模拟浏览器导航
    返回 (html, final_url)
    """
    log_fn(f"[协议] 加载 PayPal 页面: {paypal_url[:80]}...")

    resp = session.get(
        paypal_url,
        headers=_navigate_headers(referer),
        allow_redirects=True,
    )

    log_fn(f"[协议] PayPal 页面状态: {resp.status_code}, URL: {str(resp.url)[:80]}...")

    if resp.status_code == 403:
        log_fn("[协议] PayPal 返回 403，可能触发了 DataDome 检测")
        raise ValueError("PayPal 403 Forbidden - DataDome 反爬检测")

    resp.raise_for_status()
    return resp.text, str(resp.url)


def _paypal_extract_csrf(html: str) -> str:
    """提取 PayPal 页面中的 CSRF token"""
    patterns = [
        r'"_csrf"\s*:\s*"([^"]+)"',
        r'name="_csrf"\s+(?:value|content)="([^"]+)"',
        r'"token"\s*:\s*"([^"]+)"',
        r'name="token"\s+value="([^"]+)"',
        r'"xsrfTokenValue"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def _paypal_extract_flow_id(html: str) -> str:
    """提取 PayPal flow ID"""
    patterns = [
        r'"flowId"\s*:\s*"([^"]+)"',
        r'name="flowId"\s+value="([^"]+)"',
        r'"flow_id"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def _paypal_extract_session_token(html: str) -> str:
    """提取 PayPal session token"""
    patterns = [
        r'"sessionToken"\s*:\s*"([^"]+)"',
        r'"sessionID"\s*:\s*"([^"]+)"',
        r'"session_id"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def _paypal_handle_agreements_page(
    session: Session,
    page_html: str,
    page_url: str,
    log_fn,
) -> Tuple[str, str]:
    """
    处理 PayPal /agreements/approve 页面
    这个页面通常会重定向到 /pay 或 /checkoutweb
    返回 (next_page_html, next_page_url)
    """
    log_fn("[协议] 处理 PayPal agreements 页面...")

    # 检查页面是否直接包含重定向
    meta_url = _extract_meta_redirect(page_html)
    if meta_url:
        log_fn(f"[协议] agreements meta 重定向到: {meta_url[:80]}...")
        return _paypal_load_page(session, meta_url, page_url, log_fn)

    js_url = _extract_js_redirect(page_html)
    if js_url:
        if not js_url.startswith("http"):
            parsed = urllib.parse.urlparse(page_url)
            js_url = f"{parsed.scheme}://{parsed.netloc}{js_url}"
        log_fn(f"[协议] agreements JS 重定向到: {js_url[:80]}...")
        return _paypal_load_page(session, js_url, page_url, log_fn)

    # 检查表单提交
    form_action = _extract_form_action(page_html)
    if form_action:
        hidden_inputs = _extract_hidden_inputs(page_html)
        if not form_action.startswith("http"):
            parsed = urllib.parse.urlparse(page_url)
            form_action = f"{parsed.scheme}://{parsed.netloc}{form_action}"
        log_fn(f"[协议] agreements 表单提交到: {form_action[:80]}...")
        resp = session.post(
            form_action,
            data=hidden_inputs,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"https://{urllib.parse.urlparse(page_url).netloc}",
                "Referer": page_url,
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text, str(resp.url)

    return page_html, page_url


def _paypal_submit_email(
    session: Session,
    page_html: str,
    page_url: str,
    email: str,
    log_fn,
) -> Tuple[str, str]:
    """
    PayPal /pay 页面 — 提交邮箱
    完全模拟浏览器表单提交
    """
    log_fn(f"[协议] PayPal /pay 页面，提交邮箱: {email}")

    csrf = _paypal_extract_csrf(page_html)
    flow_id = _paypal_extract_flow_id(page_html)
    hidden_inputs = _extract_hidden_inputs(page_html)
    form_action = _extract_form_action(page_html)

    if not form_action:
        # PayPal /pay 页面通常用 AJAX 提交
        # 尝试找到 API endpoint
        api_patterns = [
            r'"submitUrl"\s*:\s*"([^"]+)"',
            r'"actionUrl"\s*:\s*"([^"]+)"',
        ]
        for p in api_patterns:
            m = re.search(p, page_html)
            if m:
                form_action = m.group(1)
                break

    paypal_origin = f"https://{urllib.parse.urlparse(page_url).netloc}"

    if not form_action:
        form_action = page_url

    if not form_action.startswith("http"):
        form_action = paypal_origin + form_action

    post_data = {**hidden_inputs}
    post_data["email"] = email
    if csrf:
        post_data["_csrf"] = csrf
    if flow_id:
        post_data["flowId"] = flow_id

    log_fn(f"[协议] 提交邮箱到: {form_action[:80]}...")

    resp = session.post(
        form_action,
        data=post_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": paypal_origin,
            "Referer": page_url,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        },
        allow_redirects=True,
    )

    log_fn(f"[协议] 邮箱提交响应: {resp.status_code}, URL: {str(resp.url)[:80]}...")
    resp.raise_for_status()
    return resp.text, str(resp.url)


def _paypal_submit_checkout(
    session: Session,
    page_html: str,
    page_url: str,
    identity,
    config: ProtocolPaymentConfig,
    email: str,
    password: str,
    log_fn,
    sms_controller=None,
) -> Tuple[str, str]:
    """
    PayPal /checkoutweb 页面 — 提交完整表单（注册+支付）
    完全模拟浏览器表单提交
    """
    log_fn("[协议] PayPal checkout 页面，构建完整表单...")

    csrf = _paypal_extract_csrf(page_html)
    flow_id = _paypal_extract_flow_id(page_html)
    session_token = _paypal_extract_session_token(page_html)
    hidden_inputs = _extract_hidden_inputs(page_html)
    form_action = _extract_form_action(page_html)

    paypal_origin = f"https://{urllib.parse.urlparse(page_url).netloc}"

    if not form_action:
        api_patterns = [
            r'"submitUrl"\s*:\s*"([^"]+)"',
            r'"createAccountUrl"\s*:\s*"([^"]+)"',
            r'"actionUrl"\s*:\s*"([^"]+)"',
        ]
        for p in api_patterns:
            m = re.search(p, page_html)
            if m:
                form_action = m.group(1)
                break

    if not form_action:
        form_action = page_url

    if not form_action.startswith("http"):
        form_action = paypal_origin + form_action

    state_full = _resolve_state(identity.state, getattr(identity, 'state_abbr', ''))

    # 尝试匹配 state select value
    state_options = _extract_select_options(page_html, "billingState")
    state_value = _find_select_value(state_options, state_full) if state_options else state_full

    # 手机号：优先使用接码平台
    if sms_controller:
        log_fn("[协议] 使用接码平台获取手机号...")
        try:
            sms_phone = str(sms_controller() or "").strip()
            if sms_phone:
                log_fn(f"[协议] 接码平台手机号: {sms_phone}")
                phone = sms_phone
            else:
                log_fn("[协议] 接码平台未返回手机号，使用备用号码")
                phone = config.phone or getattr(identity, 'phone', '') or ""
        except Exception as e:
            log_fn(f"[协议] 接码获取手机号失败: {e}，使用备用号码")
            phone = config.phone or getattr(identity, 'phone', '') or ""
    else:
        phone = config.phone or getattr(identity, 'phone', '') or ""

    card_number = config.card_number or getattr(identity, 'card_number', '') or ""
    card_expiry = config.card_expiry or getattr(identity, 'card_expiry', '') or ""
    card_cvv = config.card_cvv or getattr(identity, 'card_cvv', '') or ""

    post_data = {**hidden_inputs}
    post_data.update({
        "email": email,
        "password": password,
        "phone": phone,
        "phoneCode": "US+1",
        "cardNumber": card_number,
        "cardExpiry": card_expiry,
        "cardCvv": card_cvv,
        "firstName": getattr(identity, 'first_name', '') or "James",
        "lastName": getattr(identity, 'last_name', '') or "Smith",
        "billingLine1": identity.street,
        "billingLine2": "",
        "billingCity": identity.city,
        "billingState": state_value or state_full,
        "billingPostalCode": identity.zipcode,
        "country": "US",
    })

    if csrf:
        post_data["_csrf"] = csrf
    if flow_id:
        post_data["flowId"] = flow_id
    if session_token:
        post_data["sessionToken"] = session_token

    log_fn(f"[协议] 提交 checkout 表单到: {form_action[:80]}...")
    log_fn(f"[协议] 表单字段: email={email}, phone={phone}, "
           f"name={identity.first_name} {identity.last_name}, city={identity.city}")

    resp = session.post(
        form_action,
        data=post_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": paypal_origin,
            "Referer": page_url,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        },
        allow_redirects=True,
    )

    log_fn(f"[协议] checkout 提交响应: {resp.status_code}, URL: {str(resp.url)[:80]}...")
    resp.raise_for_status()

    result_html = resp.text
    result_url = str(resp.url)

    # 检测是否需要短信验证
    if sms_controller:
        result_html, result_url = _handle_sms_verification_protocol(
            session, result_html, result_url, sms_controller, log_fn,
        )

    return result_html, result_url


# ============================================================
# 短信验证（协议模式）
# ============================================================

def _detect_otp_form(html: str) -> bool:
    """检测 HTML 中是否包含验证码输入表单"""
    patterns = [
        r'name=["\'](?:code|otp|verify|verification)',
        r'id=["\'](?:code|otp|verify|verification)',
        r'autocomplete=["\']one-time-code',
        r'(?:verification|verify|确认).{0,100}<input',
        r'<input.{0,100}(?:verification|verify|code|otp)',
    ]
    for p in patterns:
        if re.search(p, html, re.IGNORECASE):
            return True
    return False


def _submit_otp_form(
    session: Session,
    page_html: str,
    page_url: str,
    code: str,
    log_fn,
) -> Tuple[str, str]:
    """提交验证码表单"""
    hidden_inputs = _extract_hidden_inputs(page_html)
    form_action = _extract_form_action(page_html)
    csrf = _paypal_extract_csrf(page_html)

    paypal_origin = f"https://{urllib.parse.urlparse(page_url).netloc}"

    if not form_action:
        form_action = page_url
    if not form_action.startswith("http"):
        form_action = paypal_origin + form_action

    post_data = {**hidden_inputs}
    # 尝试多种验证码字段名
    for field_name in ("code", "otp", "verificationCode", "smsCode"):
        post_data[field_name] = code
    if csrf:
        post_data["_csrf"] = csrf

    log_fn(f"[协议] 提交验证码到: {form_action[:80]}...")

    resp = session.post(
        form_action,
        data=post_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": paypal_origin,
            "Referer": page_url,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        },
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text, str(resp.url)


def _handle_sms_verification_protocol(
    session: Session,
    page_html: str,
    page_url: str,
    sms_controller,
    log_fn,
) -> Tuple[str, str]:
    """检测并处理协议模式下的短信验证"""
    if not _detect_otp_form(page_html):
        log_fn("[协议] 未检测到短信验证码表单，跳过")
        return page_html, page_url

    log_fn("[协议] 检测到短信验证码表单，等待接收验证码...")
    try:
        sms_code = str(sms_controller() or "").strip()
        if not sms_code:
            log_fn("[协议] 未收到短信验证码")
            return page_html, page_url

        log_fn(f"[协议] 收到验证码: {sms_code}")
        result_html, result_url = _submit_otp_form(
            session, page_html, page_url, sms_code, log_fn,
        )
        log_fn(f"[协议] 验证码提交后 URL: {result_url[:80]}...")

        if hasattr(sms_controller, "report_success"):
            sms_controller.report_success()

        return result_html, result_url

    except Exception as e:
        log_fn(f"[协议] 短信验证流程异常: {e}")
        return page_html, page_url


# ============================================================
# 检测支付结果
# ============================================================

def _check_payment_success(html: str, url: str) -> bool:
    """检查是否支付成功"""
    if "chatgpt.com" in url and "success" in url:
        return True
    if "chatgpt.com" in url and "checkout" not in url and "pricing" not in url:
        return True
    success_texts = ["success", "thank you", "you are now subscribed", "payment confirmed"]
    body_lower = html.lower()
    return any(t in body_lower for t in success_texts)


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
    """协议模式完整支付流程"""

    # === Step 1: 加载 Stripe checkout 页面 ===
    stripe_html, stripe_url = _stripe_load_checkout(session, hosted_url, log_fn)

    # === Step 2: 提交 Stripe confirm（选 PayPal + 填地址）→ 获取 PayPal URL ===
    paypal_url = _stripe_confirm_paypal(
        session, hosted_url, stripe_html, identity, config, log_fn,
    )

    # === Step 3: 加载 PayPal 页面 ===
    paypal_html, paypal_final_url = _paypal_load_page(session, paypal_url, stripe_url, log_fn)

    # === Step 4: 根据 PayPal 页面路径分发处理 ===
    if "/agreements/approve" in paypal_final_url:
        log_fn("[协议] 处理 agreements/approve 页面...")
        paypal_html, paypal_final_url = _paypal_handle_agreements_page(
            session, paypal_html, paypal_final_url, log_fn,
        )

    if "/pay" in paypal_final_url and "/checkoutweb" not in paypal_final_url:
        log_fn("[协议] 进入 PayPal /pay 邮箱页面...")
        paypal_html, paypal_final_url = _paypal_submit_email(
            session, paypal_html, paypal_final_url, paypal_email, log_fn,
        )

    if "/checkoutweb" in paypal_final_url:
        log_fn("[协议] 进入 PayPal /checkoutweb 注册+支付页面...")
        final_html, final_url = _paypal_submit_checkout(
            session, paypal_html, paypal_final_url,
            identity, config, paypal_email, paypal_password, log_fn,
            sms_controller=sms_controller,
        )
    else:
        log_fn(f"[协议] 当前 PayPal URL 不在预期路径: {paypal_final_url[:100]}")
        log_fn("[协议] 尝试直接在当前页面提交 checkout 表单...")
        final_html, final_url = _paypal_submit_checkout(
            session, paypal_html, paypal_final_url,
            identity, config, paypal_email, paypal_password, log_fn,
            sms_controller=sms_controller,
        )

    # === Step 5: 跟踪最终重定向，检测支付结果 ===
    log_fn(f"[协议] 最终页面 URL: {final_url[:100]}...")

    if _check_payment_success(final_html, final_url):
        result.success = True
        result.subscription_status = "plus"
        log_fn("[协议] 支付成功！ChatGPT Plus 已激活")
    else:
        # 检查是否有回调重定向到 ChatGPT
        redirect_url = _extract_meta_redirect(final_html) or _extract_js_redirect(final_html)
        if redirect_url:
            log_fn(f"[协议] 跟踪最终重定向: {redirect_url[:80]}...")
            try:
                resp = session.get(
                    redirect_url,
                    headers=_navigate_headers(final_url),
                    allow_redirects=True,
                )
                if _check_payment_success(resp.text, str(resp.url)):
                    result.success = True
                    result.subscription_status = "plus"
                    log_fn("[协议] 支付成功！ChatGPT Plus 已激活")
                else:
                    result.error = f"支付结果未确认 (URL: {str(resp.url)[:80]})"
                    log_fn(f"[协议] {result.error}")
            except Exception as e:
                result.error = f"跟踪最终重定向失败: {e}"
                log_fn(f"[协议] {result.error}")
        else:
            result.error = f"支付结果未确认 (URL: {final_url[:80]})"
            log_fn(f"[协议] {result.error}")
            log_fn(f"[协议] 页面内容片段: {final_html[:500]}...")

    return result
