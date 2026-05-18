"""美国地址生成器 - 从 meiguodizhi.com 获取虚拟身份信息（地址+信用卡）"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

API_URL = "https://www.meiguodizhi.com/api/v1/dz"


@dataclass
class VirtualIdentity:
    """虚拟身份信息，包含地址和信用卡"""
    # 基本信息
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    gender: str = ""
    birthday: str = ""

    # 地址
    street: str = ""
    city: str = ""
    state: str = ""
    state_abbr: str = ""
    zipcode: str = ""
    country: str = "US"
    phone: str = ""

    # 信用卡
    card_type: str = ""
    card_number: str = ""
    card_expiry: str = ""
    card_cvv: str = ""

    # 其他
    email: str = ""
    ssn: str = ""
    username: str = ""
    password: str = ""


def fetch_virtual_identity(
    *,
    city: str = "",
    proxy: Optional[str] = None,
) -> VirtualIdentity:
    """从 meiguodizhi.com 获取一组虚拟身份信息。

    Args:
        city: 指定城市（留空随机）
        proxy: 代理地址

    Returns:
        VirtualIdentity 数据对象
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None

    payload = {
        "city": city,
        "path": "/",
        "method": "refresh",
    }

    resp = cffi_requests.post(
        API_URL,
        json=payload,
        proxies=proxies,
        timeout=20,
        impersonate="chrome124",
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success" and "address" not in data:
        raise ValueError(f"meiguodizhi API 返回异常: {data}")

    addr = data.get("address", data)

    identity = VirtualIdentity(
        first_name=str(addr.get("first_name", "") or addr.get("firstName", "")),
        last_name=str(addr.get("last_name", "") or addr.get("lastName", "")),
        full_name=str(addr.get("name", "") or addr.get("full_name", "")),
        gender=str(addr.get("gender", "")),
        birthday=str(addr.get("birthday", "") or addr.get("birth", "")),
        street=str(addr.get("street", "") or addr.get("address", "")),
        city=str(addr.get("city", "")),
        state=str(addr.get("state", "")),
        state_abbr=str(addr.get("state_abbr", "") or addr.get("stateAbbr", "")),
        zipcode=str(addr.get("zip", "") or addr.get("zipcode", "") or addr.get("zip_code", "")),
        phone=str(addr.get("phone", "") or addr.get("telephone", "")),
        card_type=str(addr.get("card_type", "") or addr.get("creditCardType", "")),
        card_number=str(addr.get("card_number", "") or addr.get("creditCardNumber", "")),
        card_expiry=str(addr.get("card_expiry", "") or addr.get("creditCardExpire", "")),
        card_cvv=str(addr.get("card_cvv", "") or addr.get("cvv2", "") or addr.get("CVV2", "")),
        email=str(addr.get("email", "") or addr.get("temp_email", "")),
        ssn=str(addr.get("ssn", "") or addr.get("SSN", "")),
        username=str(addr.get("username", "")),
        password=str(addr.get("password", "")),
    )

    if not identity.full_name and identity.first_name:
        identity.full_name = f"{identity.first_name} {identity.last_name}".strip()

    logger.info(f"获取虚拟身份: {identity.full_name}, {identity.city}, {identity.state}")
    return identity
