#!/usr/bin/env bash

btq_load_ios_device_env_value() {
  local name="$1"
  local current="${!name:-}"
  local line
  local value

  if [[ -n "$current" || ! -f "${BTQ_IOS_DEVICE_ENV_FILE:-}" ]]; then
    return
  fi

  line="$(grep -E "^${name}=" "$BTQ_IOS_DEVICE_ENV_FILE" | tail -1 || true)"
  if [[ -z "$line" ]]; then
    return
  fi

  value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"

  if [[ -n "$value" ]]; then
    export "$name=$value"
  fi
}

btq_load_ios_device_env() {
  BTQ_IOS_DEVICE_ENV_FILE="${BTQ_IOS_DEVICE_ENV_FILE:-$ROOT_DIR/script/ios_device.env}"

  btq_load_ios_device_env_value BTQ_DEVELOPMENT_TEAM
  btq_load_ios_device_env_value BTQ_DEVICE_NAME
  btq_load_ios_device_env_value BTQ_XCODE_DESTINATION
  btq_load_ios_device_env_value BTQ_DEVICE_SELECTOR
}
