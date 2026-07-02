from __future__ import annotations

import re
from collections.abc import Mapping


_VERSION_RE = re.compile(r"(\d+(?:[._]\d+)*)")
_ANDROID_RE = re.compile(r"Android\s+([^;)\s]+)", re.IGNORECASE)
_ANDROID_MODEL_RE = re.compile(r"Android\s+[^;]+;\s*([^;)]+?)(?:\s+Build/[^;)]*)?[;)]", re.IGNORECASE)
_IOS_RE = re.compile(r"OS\s+(\d+(?:[_\.]\d+)*)\s+like\s+Mac\s+OS\s+X", re.IGNORECASE)
_DARWIN_RE = re.compile(r"Darwin/(\d+(?:\.\d+)*)", re.IGNORECASE)

EXPLICIT_HEADER_KEYS = {
    "client": "X-BTQ-Client",
    "platform": "X-BTQ-Platform",
    "os_version": "X-BTQ-OS-Version",
    "app_version": "X-BTQ-App-Version",
    "device_model": "X-BTQ-Device-Model",
}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _header(headers: Mapping[str, object], name: str) -> str:
    getter = getattr(headers, "get", None)
    if callable(getter):
        return _clean(getter(name, ""))
    return _clean(headers.get(name, ""))


def normalize_platform(value: object) -> str:
    text = _clean(value).lower()
    if text in {"ios", "iphone", "ipad", "ipados"}:
        return "ios"
    if text == "android":
        return "android"
    if text in {"web", "browser", "pwa"}:
        return "web"
    return "unknown"


def parse_user_agent(user_agent: str) -> dict[str, str]:
    ua = _clean(user_agent)
    if not ua:
        return {}

    lower = ua.lower()
    parsed: dict[str, str] = {
        "kind": "other",
        "platform": "unknown",
    }

    if "cfnetwork/" in lower and "darwin/" in lower:
        parsed["kind"] = "native"
        parsed["platform"] = "ios"
        darwin = _DARWIN_RE.search(ua)
        if darwin:
            parsed["os_version"] = f"Darwin {darwin.group(1)}"
    elif "android" in lower:
        parsed["kind"] = "pwa" if "chrome/" in lower or "safari/" in lower else "other"
        parsed["platform"] = "android"
        android = _ANDROID_RE.search(ua)
        if android:
            parsed["os_version"] = f"Android {android.group(1).replace('_', '.')}"
        model = _ANDROID_MODEL_RE.search(ua)
        if model:
            device_model = model.group(1).strip()
            if device_model and not device_model.lower().startswith(("wv", "mobile")):
                parsed["device_model"] = device_model
    elif "iphone" in lower or "ipad" in lower:
        parsed["kind"] = "pwa" if "safari/" in lower or "crios/" in lower else "other"
        parsed["platform"] = "ios"
        ios = _IOS_RE.search(ua)
        if ios:
            parsed["os_version"] = f"iOS {ios.group(1).replace('_', '.')}"
        parsed["device_model"] = "iPad" if "ipad" in lower else "iPhone"
    elif "mozilla/" in lower or "chrome/" in lower or "safari/" in lower or "firefox/" in lower:
        parsed["kind"] = "pwa"
        parsed["platform"] = "web"

    return parsed


def build_client_block(headers: Mapping[str, object]) -> dict[str, str]:
    user_agent = _header(headers, "User-Agent")
    explicit = {
        key: _header(headers, header_name)
        for key, header_name in EXPLICIT_HEADER_KEYS.items()
    }
    if not user_agent and not any(explicit.values()):
        return {}

    parsed = parse_user_agent(user_agent)
    client: dict[str, str] = {}
    if user_agent:
        client["user_agent"] = user_agent

    client_name = explicit["client"]
    explicit_kind = ""
    if client_name:
        client["client"] = client_name
        if "btqcapture" in client_name.replace(" ", "").lower():
            explicit_kind = "native"
        version = _VERSION_RE.search(client_name)
        if version:
            client["app_version"] = version.group(1).replace("_", ".")

    for key in ("kind", "platform", "os_version", "app_version", "device_model"):
        value = parsed.get(key)
        if value:
            client[key] = value

    if explicit_kind:
        client["kind"] = explicit_kind
    if explicit["platform"]:
        client["platform"] = normalize_platform(explicit["platform"])
    if explicit["os_version"]:
        client["os_version"] = explicit["os_version"]
    if explicit["app_version"]:
        client["app_version"] = explicit["app_version"]
    if explicit["device_model"]:
        client["device_model"] = explicit["device_model"]
    if client_name and "kind" not in client:
        client["kind"] = "other"

    return {key: value for key, value in client.items() if value}
