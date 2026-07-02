from __future__ import annotations

from pathlib import Path

from client_telemetry import build_client_block, parse_user_agent


def test_ua_parser_classifies_native_ios_pwa_android_and_junk() -> None:
    cfnetwork = "BTQ Capture/1 CFNetwork/1496.0.7 Darwin/23.5.0"
    android = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP1A.240505.004; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/125.0.0.0 Mobile Safari/537.36"
    )

    assert parse_user_agent(cfnetwork)["kind"] == "native"
    assert parse_user_agent(cfnetwork)["platform"] == "ios"
    assert parse_user_agent(android)["kind"] == "pwa"
    assert parse_user_agent(android)["platform"] == "android"
    assert parse_user_agent("not a useful user agent") == {"kind": "other", "platform": "unknown"}


def test_explicit_client_headers_override_user_agent_parse() -> None:
    client = build_client_block(
        {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/125.0 Mobile Safari/537.36",
            "X-BTQ-Client": "BTQCapture/1.2.0",
            "X-BTQ-Platform": "ios",
            "X-BTQ-OS-Version": "17.5",
            "X-BTQ-App-Version": "1.2.3",
            "X-BTQ-Device-Model": "iPhone15,3",
        }
    )

    assert client["kind"] == "native"
    assert client["platform"] == "ios"
    assert client["os_version"] == "17.5"
    assert client["app_version"] == "1.2.3"
    assert client["device_model"] == "iPhone15,3"


def test_client_telemetry_source_does_not_read_ip_or_geolocation() -> None:
    source = Path("project/client_telemetry.py").read_text(encoding="utf-8").lower()

    disallowed = ["x-forwarded-for", "x-real-ip", "remote_addr", "client_address", "latitude", "longitude", "geolocation", "gps"]
    assert not [token for token in disallowed if token in source]
