"""
ChatGPT Plus 自动支付流程（基于真实页面选择器）
流程: Stripe hosted 链接 → 选 PayPal → 填地址 → PayPal /pay 填邮箱 → /checkoutweb 填表单 → 完成
选择器来源: 油猴脚本 + Chrome DevTools 实际抓取
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PaymentConfig:
    """支付配置"""
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
class PaymentResult:
    """支付结果"""
    success: bool = False
    hosted_url: str = ""
    paypal_email: str = ""
    paypal_password: str = ""
    error: str = ""
    subscription_status: str = ""


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
    import secrets
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(chars) for _ in range(16)) + "@gmail.com"


def _rand_password() -> str:
    import secrets
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


def _create_sms_controller(config: PaymentConfig, log_fn):
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


# ============================================================
# 核心：通过 Playwright evaluate 注入 JS 填写表单
# 与油猴脚本同样的方式，绕过 React 受控组件
# ============================================================

_JS_FILL_BY_ID = """
(args) => {
    const [id, val] = args;
    const el = document.getElementById(id);
    if (!el) return false;
    const ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    ns.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
}
"""

_JS_FILL_BY_SELECTOR = """
(args) => {
    const [sel, val] = args;
    const el = document.querySelector(sel);
    if (!el) return false;
    const ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    ns.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
}
"""

_JS_FILL_SELECT = """
(args) => {
    const [id, text] = args;
    const el = document.getElementById(id);
    if (!el) return false;
    for (let i = 0; i < el.options.length; i++) {
        if (el.options[i].text.toLowerCase().includes(text.toLowerCase()) ||
            el.options[i].value.toLowerCase().includes(text.toLowerCase())) {
            el.value = el.options[i].value;
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
    }
    return false;
}
"""

_JS_CLICK_SUBMIT = """
() => {
    const selectors = [
        'button[data-testid="submit-button"]',
        'button[data-testid="hosted-payment-submit-button"]',
        'button[data-atomic-wait-intent="Submit_Email"]',
        'button.SubmitButton--complete',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.getBoundingClientRect().height > 0) {
            btn.click();
            return sel;
        }
    }
    const texts = ['Next', '下一页', 'Subscribe', 'Pay', 'Continue', 'Agree'];
    const all = document.querySelectorAll('button');
    for (const btn of all) {
        const t = btn.textContent.trim();
        if (texts.some(x => t === x) && !btn.disabled && btn.getBoundingClientRect().height > 0) {
            btn.click();
            return 'text:' + t;
        }
    }
    return null;
}
"""

_JS_HIDE_CAPTCHA = """
() => {
    const st = document.createElement('style');
    st.textContent = '#captcha-standalone,.captcha-overlay,.captcha-container,.AddressAutocomplete-results{display:none!important;height:0!important;overflow:hidden!important}';
    document.head.appendChild(st);
}
"""

_JS_DETECT_OTP_INPUT = """
() => {
    const selectors = [
        'input[name*="code"]', 'input[name*="otp"]', 'input[name*="verify"]',
        'input[id*="code"]', 'input[id*="otp"]', 'input[id*="verify"]',
        'input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
        'input[type="tel"][maxlength]',
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.getBoundingClientRect().height > 0) return sel;
    }
    const labels = document.querySelectorAll('label, span, p, div');
    for (const l of labels) {
        const t = l.textContent.toLowerCase();
        if ((t.includes('verification') || t.includes('code') || t.includes('验证码'))
            && !t.includes('country')) {
            const input = l.querySelector('input') || l.nextElementSibling?.querySelector('input');
            if (input) return 'found_near_label';
        }
    }
    return null;
}
"""

_JS_FILL_OTP = """
(args) => {
    const [code] = args;
    const selectors = [
        'input[name*="code"]', 'input[name*="otp"]', 'input[name*="verify"]',
        'input[id*="code"]', 'input[id*="otp"]', 'input[id*="verify"]',
        'input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
        'input[type="tel"][maxlength]',
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.getBoundingClientRect().height > 0) {
            const ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            ns.call(el, code);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
    }
    return false;
}
"""


def _js_fill(page, field_id: str, value: str, log_fn, label: str = "") -> bool:
    if not value:
        return False
    ok = page.evaluate(_JS_FILL_BY_ID, [field_id, value])
    if ok and label:
        log_fn(f"[支付] 已填写 {label}: {value[:20]}{'...' if len(value)>20 else ''}")
    return ok


def _js_fill_sel(page, selector: str, value: str, log_fn, label: str = "") -> bool:
    if not value:
        return False
    ok = page.evaluate(_JS_FILL_BY_SELECTOR, [selector, value])
    if ok and label:
        log_fn(f"[支付] 已填写 {label}")
    return ok


def _js_fill_select(page, select_id: str, text: str, log_fn, label: str = "") -> bool:
    if not text:
        return False
    ok = page.evaluate(_JS_FILL_SELECT, [select_id, text])
    if ok and label:
        log_fn(f"[支付] 已选择 {label}: {text}")
    return ok


def _js_click_submit(page, log_fn) -> bool:
    result = page.evaluate(_JS_CLICK_SUBMIT)
    if result:
        log_fn(f"[支付] 已点击按钮: {result}")
        return True
    return False


# ============================================================
# 主入口
# ============================================================

def auto_pay_plus(
    account,
    config: PaymentConfig,
    log_fn: Callable[[str], None] = print,
) -> PaymentResult:
    result = PaymentResult()

    # 1. 获取虚拟身份
    log_fn("[支付] 正在获取虚拟身份信息...")
    try:
        from providers.address.meiguodizhi import fetch_virtual_identity
        identity = fetch_virtual_identity(proxy=config.proxy)
        log_fn(f"[支付] 虚拟身份: {identity.full_name}, {identity.city}, {identity.state}")
    except Exception as e:
        result.error = f"获取虚拟身份失败: {e}"
        log_fn(f"[支付] {result.error}")
        return result

    # 2. 生成 Stripe hosted 链接
    log_fn("[支付] 正在生成 Stripe hosted 链接...")
    try:
        from platforms.chatgpt.payment import generate_plus_hosted_link
        hosted_url = generate_plus_hosted_link(account, proxy=config.proxy, country=config.country)
        result.hosted_url = hosted_url
        log_fn(f"[支付] Stripe 链接: {hosted_url[:80]}...")
    except Exception as e:
        result.error = f"生成支付链接失败: {e}"
        log_fn(f"[支付] {result.error}")
        return result

    # 3. 浏览器自动化
    log_fn("[支付] 启动浏览器...")
    try:
        result = _browser_flow(hosted_url, identity, config, log_fn, result)
    except Exception as e:
        result.error = f"浏览器流程异常: {e}"
        log_fn(f"[支付] {result.error}")

    return result


def _browser_flow(
    hosted_url: str,
    identity,
    config: PaymentConfig,
    log_fn: Callable[[str], None],
    result: PaymentResult,
) -> PaymentResult:
    from playwright.sync_api import sync_playwright

    paypal_email = _rand_email()
    paypal_password = _rand_password()
    result.paypal_email = paypal_email
    result.paypal_password = paypal_password

    sms_controller = _create_sms_controller(config, log_fn)

    with sync_playwright() as p:
        launch_args = {
            "headless": config.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if config.proxy:
            launch_args["proxy"] = {"server": config.proxy}

        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            # === Step 1: Stripe Checkout ===
            log_fn("[支付] 打开 Stripe checkout...")
            page.goto(hosted_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.evaluate(_JS_HIDE_CAPTCHA)
            log_fn("[支付] Stripe 页面加载完成")

            _handle_stripe_page(page, identity, config, log_fn)

            # === Step 2: 等待跳转到 PayPal ===
            log_fn("[支付] 等待 PayPal 页面...")
            paypal_page = _wait_for_paypal(page, context, log_fn, timeout=30)
            if not paypal_page:
                result.error = "未检测到 PayPal 页面"
                return result

            paypal_url = paypal_page.url
            log_fn(f"[支付] PayPal 页面: {paypal_url[:80]}...")
            paypal_page.evaluate(_JS_HIDE_CAPTCHA)

            # === Step 3: PayPal 流程（根据路径分发） ===
            if "/pay" in paypal_url and "/checkoutweb" not in paypal_url:
                # PayPal 登录/邮箱页
                _handle_paypal_pay_page(paypal_page, paypal_email, log_fn)

                # 等待跳转到 /checkoutweb
                log_fn("[支付] 等待 PayPal checkout 页面...")
                checkout_page = _wait_for_paypal_checkout(paypal_page, context, log_fn, timeout=30)
                if checkout_page:
                    checkout_page.evaluate(_JS_HIDE_CAPTCHA)
                    _handle_paypal_checkout_page(
                        checkout_page, identity, config,
                        paypal_email, paypal_password, log_fn,
                        sms_controller=sms_controller,
                    )
                else:
                    log_fn("[支付] 未跳转到 checkout 页面，尝试在当前页面操作")

            elif "/checkoutweb" in paypal_url:
                # 直接到了 checkout 页面
                _handle_paypal_checkout_page(
                    paypal_page, identity, config,
                    paypal_email, paypal_password, log_fn,
                    sms_controller=sms_controller,
                )

            # === Step 4: 等待支付结果 ===
            log_fn("[支付] 等待支付结果...")
            if _wait_for_success(context, timeout=config.payment_timeout):
                result.success = True
                result.subscription_status = "plus"
                log_fn("[支付] 支付成功！ChatGPT Plus 已激活")
            else:
                result.error = "支付确认超时"
                log_fn(f"[支付] {result.error}")

        except Exception as e:
            result.error = f"支付流程异常: {e}"
            log_fn(f"[支付] {result.error}")
        finally:
            if sms_controller:
                sms_controller.cleanup()
            browser.close()

    return result


# ============================================================
# Step 1: Stripe Checkout (pay.openai.com)
# ============================================================

def _handle_stripe_page(page, identity, config, log_fn):
    """Stripe 页面：选 PayPal → 填地址 → 勾选条款 → Subscribe"""

    # 点击 PayPal 按钮（油猴脚本: data-testid="paypal-accordion-item-button"）
    log_fn("[支付] 选择 PayPal...")
    clicked = page.evaluate("""
    () => {
        let btn = document.querySelector('[data-testid="paypal-accordion-item-button"]')
            || document.querySelector('.paypal-accordion-item button');
        if (btn) { btn.click(); return true; }
        // fallback: 找 PayPal radio
        const radios = document.querySelectorAll('input[type="radio"]');
        for (const r of radios) {
            if (r.closest('label')?.textContent?.includes('PayPal') || r.value === 'paypal') {
                r.click(); return true;
            }
        }
        const labels = document.querySelectorAll('label, div[role="radio"]');
        for (const l of labels) {
            if (l.textContent.includes('PayPal')) { l.click(); return true; }
        }
        return false;
    }
    """)
    if clicked:
        log_fn("[支付] 已选择 PayPal")
    else:
        log_fn("[支付] ⚠ 未找到 PayPal 按钮")
    page.wait_for_timeout(2000)

    # 选择国家
    log_fn("[支付] 填写账单地址...")
    _js_fill_select(page, "billingCountry", "United States", log_fn, "国家")
    page.wait_for_timeout(500)

    # 先点 "Enter address manually" 避免触发 Google 自动补全下拉
    page.evaluate("""
    () => {
        const els = document.querySelectorAll('a, button, span');
        for (const el of els) {
            if (el.textContent.trim() === 'Enter address manually') {
                el.click(); return true;
            }
        }
        return false;
    }
    """)
    page.wait_for_timeout(500)

    # 填写地址字段
    _js_fill_sel(page, "#billingAddressLine1", identity.street, log_fn, "地址")
    _js_fill_sel(page, "#billingLocality", identity.city, log_fn, "城市")
    _js_fill_sel(page, "#billingPostalCode", identity.zipcode, log_fn, "邮编")
    state_full = _resolve_state(identity.state, identity.state_abbr)
    _js_fill_select(page, "billingAdministrativeArea", state_full, log_fn, "州")

    # 勾选条款
    page.evaluate("""
    () => {
        const cb = document.getElementById('termsOfServiceConsentCheckbox');
        if (cb && !cb.checked) { cb.click(); return true; }
        // fallback
        const boxes = document.querySelectorAll('input[type="checkbox"]');
        for (const b of boxes) {
            const label = b.closest('label')?.textContent || '';
            if (label.includes('Terms') || label.includes('charged')) {
                if (!b.checked) b.click();
                return true;
            }
        }
        return false;
    }
    """)
    log_fn("[支付] 已勾选条款")
    page.wait_for_timeout(1000)

    # 点击 Subscribe
    _js_click_submit(page, log_fn)
    page.wait_for_timeout(3000)


# ============================================================
# Step 2: PayPal /pay 页面（输入邮箱，点击 Next）
# ============================================================

def _handle_paypal_pay_page(page, email: str, log_fn):
    """PayPal /pay 登录页：填邮箱 → 点 Next"""
    log_fn("[支付] PayPal 登录页，填写邮箱...")
    page.wait_for_timeout(2000)
    _js_fill(page, "email", email, log_fn, "PayPal 邮箱")
    page.wait_for_timeout(1000)
    _js_click_submit(page, log_fn)
    page.wait_for_timeout(3000)


# ============================================================
# Step 3: PayPal /checkoutweb 页面（综合表单）
# ============================================================

def _handle_paypal_checkout_page(page, identity, config, email, password, log_fn, sms_controller=None):
    """PayPal checkout 综合表单：邮箱+密码+卡+姓名+地址，可选短信验证"""
    log_fn("[支付] PayPal checkout 页面，填写表单...")
    page.wait_for_timeout(2000)

    # 切换国家为 US（如果需要）
    country_switched = page.evaluate("""
    () => {
        const c = document.getElementById('country');
        if (c && c.value !== 'US') {
            c.value = 'US';
            c.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
        return false;
    }
    """)
    if country_switched:
        log_fn("[支付] 国家已切换为 US")
        page.wait_for_timeout(3000)

    # 填写所有字段
    _js_fill(page, "email", email, log_fn, "邮箱")
    _js_fill(page, "password", password, log_fn, "密码")

    # 手机号：优先使用接码平台，否则用配置或虚拟身份的号码
    if sms_controller:
        log_fn("[支付] 使用接码平台获取手机号...")
        try:
            sms_phone = str(sms_controller() or "").strip()
            if sms_phone:
                log_fn(f"[支付] 接码平台手机号: {sms_phone}")
                _js_fill(page, "phone", sms_phone, log_fn, "电话(接码)")
            else:
                log_fn("[支付] 接码平台未返回手机号，使用备用号码")
                phone = config.phone or identity.phone or ""
                _js_fill(page, "phone", phone, log_fn, "电话")
        except Exception as e:
            log_fn(f"[支付] 接码获取手机号失败: {e}，使用备用号码")
            phone = config.phone or identity.phone or ""
            _js_fill(page, "phone", phone, log_fn, "电话")
    else:
        phone = config.phone or identity.phone or ""
        _js_fill(page, "phone", phone, log_fn, "电话")

    card_number = config.card_number or identity.card_number or ""
    card_expiry = config.card_expiry or identity.card_expiry or ""
    card_cvv = config.card_cvv or identity.card_cvv or ""

    _js_fill(page, "cardNumber", card_number, log_fn, "卡号")
    _js_fill(page, "cardExpiry", card_expiry, log_fn, "有效期")
    _js_fill(page, "cardCvv", card_cvv, log_fn, "CVV")

    _js_fill(page, "firstName", identity.first_name or "James", log_fn, "名")
    _js_fill(page, "lastName", identity.last_name or "Smith", log_fn, "姓")

    _js_fill(page, "billingLine1", identity.street, log_fn, "地址")
    _js_fill(page, "billingCity", identity.city, log_fn, "城市")
    _js_fill(page, "billingPostalCode", identity.zipcode, log_fn, "邮编")

    state_full = _resolve_state(identity.state, identity.state_abbr)
    _js_fill_select(page, "billingState", state_full, log_fn, "州")

    page.wait_for_timeout(500)
    _js_click_submit(page, log_fn)
    log_fn("[支付] PayPal 表单已提交")
    page.wait_for_timeout(5000)

    # 检测是否出现短信验证码输入框
    if sms_controller:
        _handle_sms_verification(page, sms_controller, log_fn)


# ============================================================
# Step 4: 短信验证（提交表单后可能出现）
# ============================================================

def _handle_sms_verification(page, sms_controller, log_fn):
    """检测并处理 PayPal 短信验证码步骤"""
    log_fn("[支付] 检测是否需要短信验证...")

    max_wait = 15
    found = False
    for _ in range(max_wait):
        otp_sel = page.evaluate(_JS_DETECT_OTP_INPUT)
        if otp_sel:
            found = True
            break
        page.wait_for_timeout(1000)

    if not found:
        log_fn("[支付] 未检测到短信验证码输入框，跳过")
        return

    log_fn("[支付] 检测到短信验证码输入框，等待接收验证码...")
    try:
        sms_code = str(sms_controller() or "").strip()
        if not sms_code:
            log_fn("[支付] 未收到短信验证码")
            return

        log_fn(f"[支付] 收到验证码: {sms_code}")
        filled = page.evaluate(_JS_FILL_OTP, [sms_code])
        if filled:
            log_fn("[支付] 已填入验证码")
        else:
            log_fn("[支付] 填入验证码失败")
            return

        page.wait_for_timeout(500)
        _js_click_submit(page, log_fn)
        log_fn("[支付] 验证码已提交")
        page.wait_for_timeout(3000)

        if hasattr(sms_controller, "report_success"):
            sms_controller.report_success()

    except Exception as e:
        log_fn(f"[支付] 短信验证流程异常: {e}")


# ============================================================
# 等待/检测辅助
# ============================================================

def _wait_for_paypal(page, context, log_fn, timeout: int = 30):
    """等待跳转到 PayPal（新标签或当前页跳转）"""
    start = time.time()
    while time.time() - start < timeout:
        for p in context.pages:
            if "paypal.com" in p.url:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                return p
        if "paypal.com" in page.url:
            return page
        time.sleep(2)
    return None


def _wait_for_paypal_checkout(page, context, log_fn, timeout: int = 30):
    """等待 PayPal /checkoutweb 页面出现"""
    start = time.time()
    while time.time() - start < timeout:
        for p in context.pages:
            if "paypal.com" in p.url and "/checkoutweb" in p.url:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                return p
        if "paypal.com" in page.url and "/checkoutweb" in page.url:
            return page
        time.sleep(2)
    return None


def _wait_for_success(context, timeout: int = 60) -> bool:
    """等待支付成功（页面跳转回 ChatGPT 或显示成功）"""
    start = time.time()
    while time.time() - start < timeout:
        for p in context.pages:
            url = p.url
            if "chatgpt.com" in url and "success" in url:
                return True
            if "chatgpt.com" in url and "checkout" not in url and "pricing" not in url:
                return True
            try:
                hit = p.evaluate("""
                () => {
                    const texts = ['success', 'Thank you', 'You are now subscribed', 'Payment confirmed'];
                    const body = document.body?.innerText || '';
                    return texts.some(t => body.toLowerCase().includes(t.toLowerCase()));
                }
                """)
                if hit:
                    return True
            except Exception:
                pass
        time.sleep(2)
    return False
