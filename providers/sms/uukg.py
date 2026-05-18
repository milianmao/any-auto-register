"""sms.uu.kg 接码平台 — 卡密制：每个卡密对应一次手机号 + 验证码。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from curl_cffi import requests as cffi_requests

from core.base_sms import BaseSmsProvider, SmsActivation

logger = logging.getLogger(__name__)

_DEFAULT_CODES_FILE = "data/sms_codes.txt"
_USED_LOG_FILE = "data/sms_codes_used.jsonl"


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _default_codes_path() -> Path:
    return _project_data_dir() / "sms_codes.txt"


def _used_log_path() -> Path:
    return _project_data_dir() / "sms_codes_used.jsonl"


class CodePool:
    """线程安全的卡密池：从文件/列表加载，使用后记录并移除。"""

    def __init__(self, codes: Optional[List[str]] = None, codes_file: str = ""):
        self._lock = threading.Lock()
        self._codes_file = Path(codes_file) if codes_file else _default_codes_path()
        self._used_log = _used_log_path()

        already_used = self._load_used_codes()

        pool: list[str] = []
        if codes:
            pool.extend(c for c in codes if c and c not in already_used)
        if self._codes_file.exists():
            for line in self._codes_file.read_text(encoding="utf-8").splitlines():
                c = line.strip()
                if c and c not in already_used and c not in pool:
                    pool.append(c)

        self._pool = pool
        logger.info("CodePool 初始化: %d 个可用卡密 (已排除 %d 个已用)", len(self._pool), len(already_used))

    def _load_used_codes(self) -> set[str]:
        used: set[str] = set()
        if not self._used_log.exists():
            return used
        try:
            for line in self._used_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    code = entry.get("code", "")
                    if code:
                        used.add(code)
                except json.JSONDecodeError:
                    pass
        except Exception as exc:
            logger.warning("读取已用卡密日志失败: %s", exc)
        return used

    @property
    def available_count(self) -> int:
        with self._lock:
            return len(self._pool)

    def take(self) -> str:
        with self._lock:
            if not self._pool:
                raise RuntimeError("卡密池已空，没有可用卡密")
            return self._pool.pop(0)

    def mark_used(self, code: str, phone: str, sms_code: str):
        self._write_log(code, phone=phone, sms_code=sms_code, status="used")
        self._remove_from_file(code)

    def mark_failed(self, code: str, reason: str, phone: str = ""):
        self._write_log(code, phone=phone, status="failed", reason=reason)
        self._remove_from_file(code)

    def return_code(self, code: str):
        with self._lock:
            if code not in self._pool:
                self._pool.insert(0, code)

    def _write_log(self, code: str, *, phone: str = "", sms_code: str = "",
                   status: str = "", reason: str = ""):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "code": code,
            "phone": phone,
            "sms_code": sms_code,
            "status": status,
            "reason": reason,
        }
        try:
            self._used_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self._used_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("写入卡密使用日志失败: %s", exc)

    def _remove_from_file(self, code: str):
        try:
            if not self._codes_file.exists():
                return
            lines = self._codes_file.read_text(encoding="utf-8").splitlines()
            remaining = [l for l in lines if l.strip() != code]
            if len(remaining) < len(lines):
                self._codes_file.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        except Exception as exc:
            logger.warning("从卡密文件移除 %s 失败: %s", code, exc)


class UukgSmsProvider(BaseSmsProvider):
    """sms.uu.kg — 卡密制接码。activation_id 即卡密本身。"""

    BASE_URL = "https://sms.uu.kg"
    auto_report_success_on_code = True

    def __init__(
        self,
        api_key: str = "",
        codes: Optional[List[str]] = None,
        codes_file: str = "",
        proxy: Optional[str] = None,
    ):
        self.api_key = api_key
        self.proxy = proxy
        self._code_pool = CodePool(codes=codes, codes_file=codes_file)
        self._phone_map: dict[str, str] = {}

    def _proxies(self) -> Optional[dict]:
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    def _post(self, action: str, payload: dict, timeout: int = 20) -> dict:
        url = f"{self.BASE_URL}/api.php?action={action}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        resp = cffi_requests.post(
            url,
            json=payload,
            headers=headers,
            proxies=self._proxies(),
            timeout=timeout,
            impersonate="chrome124",
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"sms.uu.kg 响应格式异常: {data}")
        return data

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        code = self._code_pool.take()
        try:
            data = self._post("open_get_phone", {"code": code})
        except Exception as exc:
            self._code_pool.mark_failed(code, f"get_phone请求失败: {exc}")
            raise RuntimeError(f"sms.uu.kg 获取手机号失败: {exc}") from exc

        if not data.get("ok"):
            error_msg = data.get("msg") or data.get("message") or str(data)
            if "occupied" in error_msg.lower() or "已被使用" in error_msg or "已占用" in error_msg:
                self._code_pool.mark_failed(code, f"手机号被占用: {error_msg}")
                raise RuntimeError(f"卡密 {code[:8]}... 对应手机号已被占用: {error_msg}")
            self._code_pool.mark_failed(code, error_msg)
            raise RuntimeError(f"sms.uu.kg 获取手机号失败: {error_msg}")

        phone = data.get("phone", "")
        if not phone:
            self._code_pool.mark_failed(code, "API未返回手机号")
            raise RuntimeError("sms.uu.kg 获取手机号成功但未返回号码")

        self._phone_map[code] = phone
        logger.info("sms.uu.kg 获取手机号成功: %s (卡密: %s...)", phone, code[:8])
        return SmsActivation(activation_id=code, phone_number=phone)

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        code_key = activation_id
        phone = self._phone_map.get(code_key, "")
        deadline = time.time() + timeout
        last_data = {}

        while time.time() < deadline:
            try:
                data = self._post("open_get_sms", {"code": code_key})
                last_data = data
            except Exception as exc:
                logger.debug("sms.uu.kg 轮询验证码异常: %s", exc)
                time.sleep(3)
                continue

            if data.get("ok") and data.get("code"):
                sms_code = str(data["code"])
                logger.info("sms.uu.kg 收到验证码: %s (卡密: %s...)", sms_code, code_key[:8])
                self._code_pool.mark_used(code_key, phone, sms_code)
                return sms_code

            if data.get("ok") is False:
                error_msg = data.get("msg") or data.get("message") or ""
                if "expired" in error_msg.lower() or "过期" in error_msg:
                    self._code_pool.mark_failed(code_key, f"卡密过期: {error_msg}", phone=phone)
                    return ""

            time.sleep(2)

        logger.warning("sms.uu.kg 等待验证码超时 (%ds), 卡密: %s...", timeout, code_key[:8])
        self._code_pool.mark_failed(code_key, f"等待验证码超时({timeout}s)", phone=phone)
        return ""

    def cancel(self, activation_id: str) -> bool:
        code_key = activation_id
        phone = self._phone_map.get(code_key, "")
        try:
            data = self._post("open_change_phone", {"code": code_key})
            if data.get("ok"):
                logger.info("sms.uu.kg 已释放手机号 (卡密: %s...)", code_key[:8])
            self._code_pool.mark_failed(code_key, "cancelled", phone=phone)
            return True
        except Exception as exc:
            logger.warning("sms.uu.kg 释放手机号失败: %s", exc)
            self._code_pool.mark_failed(code_key, f"cancel失败: {exc}", phone=phone)
            return False
