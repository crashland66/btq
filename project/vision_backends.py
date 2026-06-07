from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import tempfile
from pathlib import Path
from urllib import request
from urllib.error import URLError
from urllib.parse import urlsplit


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_BACKEND = "mlx"
# Production runs the MLX backend with Qwen2.5-VL-7B (4-bit). Keep these defaults in
# sync with the cat-capture demo (your-org cat_pipeline/vision_runner.py).
DEFAULT_MLX_MODEL = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
DEFAULT_OLLAMA_MODEL = "qwen2.5vl:7b"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MLX_MAX_TOKENS = 512
# Images larger than this on their longest edge are downscaled before MLX inference.
# Full-res WhatsApp photos (1–3 MB) load as 37+ GB tensors; 1280 px gives the model
# sufficient detail at a fraction of the memory cost.
MAX_MLX_IMAGE_LONG_EDGE = 1280
LOCAL_OLLAMA_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_VISION_JSON_ATTEMPTS = 3
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_TAILSCALE_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_MLX_TEXT_PLACEHOLDER_IMAGE = Path("/tmp/btq_mlx_text_placeholder.png")
_MLX_TEXT_PLACEHOLDER_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAJklEQVR42u3NMQ0AAAwDoPo33arYsQQMkB6LQCAQCAQCgUAg+BIMi1X0ptsIcT0AAAAASUVORK5CYII="
)

logger = logging.getLogger(__name__)


class VisionModelTimeoutError(RuntimeError):
    pass


class VisionModelConnectionError(RuntimeError):
    """Raised when the Ollama endpoint can't be reached at all (vs taking too
    long to respond). Treated as retryable: a transient network blip or a
    rebooting remote inference host should auto-heal on the next cycle.
    """
    pass


class OllamaVisionClient:
    provider = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        validate_local_ollama_url(ollama_url)
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.engine_name = f"ollama:{model}"

    def describe(self, image_path: Path, prompt: str) -> dict:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        for attempt in range(1, _MAX_VISION_JSON_ATTEMPTS + 1):
            raw = self._request_response(prompt, image_b64)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                # format=json constrains Ollama's grammar, but gemma-class models
                # still emit invalid JSON on rare draws; the model is stochastic
                # so a fresh request almost always parses.
                if attempt >= _MAX_VISION_JSON_ATTEMPTS:
                    raise
                logger.warning(
                    "vision model returned malformed JSON attempt=%s/%s error=%s; retrying",
                    attempt,
                    _MAX_VISION_JSON_ATTEMPTS,
                    exc,
                )
                continue
            if not isinstance(parsed, dict):
                raise ValueError("Vision model response was not a JSON object")
            return parsed
        raise RuntimeError("vision retry loop exited without a result")

    def _request_response(self, prompt: str, image_b64: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [image_b64],
            "format": "json",
            "stream": False,
            # Lower temperature so the model commits to specific observations
            # instead of hedging into vague "dark-colored object" output.
            # Larger context so the prompt isn't truncated when the vision
            # encoder spends 1k+ tokens on the image itself.
            "options": {
                "temperature": 0.3,
                "num_ctx": 8192,
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.ollama_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise VisionModelTimeoutError(f"local Ollama vision request timed out after {self.timeout_seconds:g} seconds") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc).lower():
                raise VisionModelTimeoutError(f"local Ollama vision request timed out after {self.timeout_seconds:g} seconds") from exc
            # Distinguish "endpoint unreachable" (Dell rebooting, network blip)
            # from genuine errors so the pipeline auto-retries instead of giving
            # up. ConnectionError covers ConnectionRefused/Reset/Aborted; OSError
            # catches the "[Errno 51] Network is unreachable" / "No route to
            # host" family.
            if isinstance(exc.reason, (ConnectionError, OSError)):
                raise VisionModelConnectionError(f"local Ollama vision endpoint unreachable: {exc}") from exc
            raise RuntimeError(f"local Ollama vision request failed: {exc}") from exc
        except ConnectionError as exc:
            raise VisionModelConnectionError(f"local Ollama vision endpoint unreachable: {exc}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"local Ollama vision request failed: {exc}") from exc
        raw = response_payload.get("response")
        if not isinstance(raw, str):
            raise ValueError("Ollama response did not contain a JSON response string")
        return raw


class OllamaTextClient:
    provider = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        keep_alive: str = "10m",
        disable_thinking: bool = False,
    ) -> None:
        if not model.strip():
            raise ValueError("Ollama text model is required")
        validate_local_ollama_url(base_url)
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive
        self.disable_thinking = disable_thinking

    @property
    def engine_name(self) -> str:
        return f"ollama:{self.model}"

    def generate_json(self, prompt: str) -> dict:
        content = f"{prompt}\n/no_think\n" if self.disable_thinking else prompt
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "format": "json",
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": 8192,
            },
        }
        if self.disable_thinking:
            payload["think"] = False
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise VisionModelTimeoutError(f"local Ollama text request timed out after {self.timeout_seconds:g} seconds") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc).lower():
                raise VisionModelTimeoutError(f"local Ollama text request timed out after {self.timeout_seconds:g} seconds") from exc
            if isinstance(exc.reason, (ConnectionError, OSError)):
                raise VisionModelConnectionError(f"local Ollama text endpoint unreachable: {exc}") from exc
            raise RuntimeError(f"local Ollama text request failed: {exc}") from exc
        except ConnectionError as exc:
            raise VisionModelConnectionError(f"local Ollama text endpoint unreachable: {exc}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"local Ollama text request failed: {exc}") from exc

        message = response_payload.get("message")
        if not isinstance(message, dict):
            raise ValueError("Ollama chat response did not contain a message object")
        raw = message.get("content")
        if not isinstance(raw, str):
            raise ValueError("Ollama chat response did not contain message content")
        candidate = extract_json_from_model_output(raw)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ollama text response was not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Ollama text response was not a JSON object")
        return parsed


def extract_json_from_model_output(raw: str) -> str:
    """Strip optional markdown code fences that some MLX models emit."""
    m = _JSON_BLOCK_RE.search(raw)
    if m:
        return m.group(1).strip()
    # Find the first { … } span as a fallback
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start:end + 1]
    return raw.strip()


class MlxVisionClient:
    # Local inference only: mlx-vlm loads a HuggingFace model onto Apple
    # Silicon and performs inference in-process. No network calls are made
    # to external AI APIs. This is approved as a local-only alternative to
    # the Ollama path while on-premises GPU capacity is being provisioned.
    """Run a HuggingFace vision model locally via mlx-vlm (Apple Silicon only)."""

    provider = "mlx"

    def __init__(self, model: str, max_tokens: int = DEFAULT_MLX_MAX_TOKENS) -> None:
        try:
            from mlx_vlm import load, generate as mlx_generate  # noqa: F401
            from mlx_vlm.prompt_utils import apply_chat_template  # noqa: F401
            from mlx_vlm.utils import load_config  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "mlx-vlm is not installed; run: pip install 'bt-pipeline[vision-mlx]'"
            ) from exc
        self.model = model
        self.model_path = model
        self.max_tokens = max_tokens
        self.engine_name = f"mlx:{model}"
        logger.info("mlx: loading model %s", model)
        self._model, self._processor = load(model, processor_config={"use_fast": False})
        self._config = load_config(model)
        self._apply_chat_template = apply_chat_template
        self._generate = mlx_generate
        logger.info("mlx: model loaded")

    def describe(self, image_path: Path, prompt: str) -> dict:
        inference_path, was_resized = _resize_image_for_mlx(image_path)
        try:
            for attempt in range(1, _MAX_VISION_JSON_ATTEMPTS + 1):
                raw = self._generate_response(prompt, str(inference_path))
                candidate = extract_json_from_model_output(raw)
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    if attempt >= _MAX_VISION_JSON_ATTEMPTS:
                        raise
                    logger.warning(
                        "mlx vision model returned malformed JSON attempt=%s/%s error=%s; retrying",
                        attempt,
                        _MAX_VISION_JSON_ATTEMPTS,
                        exc,
                    )
                    continue
                if not isinstance(parsed, dict):
                    raise ValueError("MLX vision model response was not a JSON object")
                return parsed
        finally:
            if was_resized:
                inference_path.unlink(missing_ok=True)
        raise RuntimeError("mlx vision retry loop exited without a result")

    def _generate_response(self, prompt: str, image_path: str) -> str:
        formatted = self._apply_chat_template(
            self._processor, self._config, prompt, num_images=1
        )
        return self._generate(
            self._model,
            self._processor,
            formatted,
            [image_path],
            max_tokens=self.max_tokens,
            verbose=False,
        )


class MlxTextClient:
    """Run text-only JSON generation through the same mlx-vlm stack as vision."""

    provider = "mlx"

    def __init__(self, model: str = DEFAULT_MLX_MODEL, max_tokens: int = DEFAULT_MLX_MAX_TOKENS) -> None:
        try:
            from mlx_vlm import load, stream_generate  # noqa: F401
            from mlx_vlm.prompt_utils import apply_chat_template  # noqa: F401
            from mlx_vlm.utils import load_config  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "mlx-vlm is not installed; run: pip install 'bt-pipeline[vision-mlx]'"
            ) from exc
        self.model = model
        self.max_tokens = max_tokens
        logger.info("mlx-vlm text: loading model %s", model)
        self._model, self._processor = load(model, processor_config={"use_fast": False})
        self._config = load_config(model)
        self._apply_chat_template = apply_chat_template
        self._stream_generate = stream_generate
        logger.info("mlx-vlm text: model loaded")

    def generate_json(self, prompt: str) -> dict:
        for attempt in range(1, _MAX_VISION_JSON_ATTEMPTS + 1):
            raw = self._generate_response(prompt)
            candidate = extract_json_from_model_output(raw)
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                if attempt >= _MAX_VISION_JSON_ATTEMPTS:
                    raise
                logger.warning(
                    "mlx-vlm text model returned malformed JSON attempt=%s/%s error=%s; retrying",
                    attempt,
                    _MAX_VISION_JSON_ATTEMPTS,
                    exc,
                )
                continue
            if not isinstance(parsed, dict):
                raise ValueError("MLX text model response was not a JSON object")
            return parsed
        raise RuntimeError("mlx-vlm text retry loop exited without a result")

    def _generate_response(self, prompt: str) -> str:
        image_path = mlx_text_placeholder_image_path()
        formatted = self._apply_chat_template(
            self._processor, self._config, prompt, num_images=1
        )
        text = ""
        for response in self._stream_generate(
            self._model,
            self._processor,
            formatted,
            [str(image_path)],
            max_tokens=self.max_tokens,
        ):
            text += getattr(response, "text", str(response))
            candidate = extract_json_from_model_output(text)
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return text
        return text


def _resize_image_for_mlx(image_path: Path) -> tuple[Path, bool]:
    """Downscale image to MAX_MLX_IMAGE_LONG_EDGE on its longest edge if needed.

    Returns (path_to_use, was_resized). Caller must unlink the returned path when
    was_resized is True. Falls back to the original path if Pillow is unavailable.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; skipping image resize before MLX inference")
        return image_path, False
    with Image.open(image_path) as img:
        w, h = img.size
        long_edge = max(w, h)
        if long_edge <= MAX_MLX_IMAGE_LONG_EDGE:
            return image_path, False
        scale = MAX_MLX_IMAGE_LONG_EDGE / long_edge
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        logger.info("mlx: resizing image %s from %dx%d to %dx%d before inference", image_path.name, w, h, *new_size)
        resized = img.resize(new_size, Image.LANCZOS)
        suffix = image_path.suffix or ".jpg"
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=suffix)
        tmp_path = Path(tmp_name)
        try:
            import os
            os.close(tmp_fd)
            resized.save(tmp_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    return tmp_path, True


def mlx_text_placeholder_image_path() -> Path:
    image_bytes = base64.b64decode(_MLX_TEXT_PLACEHOLDER_PNG_B64)
    if not _MLX_TEXT_PLACEHOLDER_IMAGE.exists() or _MLX_TEXT_PLACEHOLDER_IMAGE.read_bytes() != image_bytes:
        _MLX_TEXT_PLACEHOLDER_IMAGE.write_bytes(image_bytes)
    return _MLX_TEXT_PLACEHOLDER_IMAGE


def validate_local_ollama_url(ollama_url: str) -> None:
    parsed = urlsplit(ollama_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("BTQ field-capture vision requires a local Ollama http(s) URL")
    if not _is_local_ollama_host(parsed.hostname):
        raise ValueError("BTQ field-capture vision only supports local Ollama endpoints; cloud vision APIs are not allowed")


def _is_local_ollama_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname in LOCAL_OLLAMA_HOSTS:
        return True
    # Accept any private/loopback/link-local address so the Pro can point its
    # watcher at an inference helper on the LAN (e.g. Dell at 10.0.0.10).
    # ipaddress.is_private covers 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16.
    # Tailscale uses 100.64.0.0/10 (RFC 6598 CGNAT) which is_private does NOT
    # include, so check that block explicitly. DNS names other than "localhost"
    # are still rejected — operators should use the IP.
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False
        # Policy: accept loopback (127.0.0.1, ::1, localhost), RFC-1918 private
        # addresses, link-local, and Tailscale CGNAT (100.64.0.0/10). All of
        # these are local-network inference nodes -- no external API calls are
        # permitted. LOCAL_OLLAMA_HOSTS names receive first priority; the
        # broader check covers the Dell inference node (10.0.0.10) and similar
        # on-premises hosts reached over Tailscale.
    return addr.is_private or addr in _TAILSCALE_CGNAT_NETWORK


def build_vision_client(
    backend: str,
    *,
    model: str | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MLX_MAX_TOKENS,
) -> MlxVisionClient | OllamaVisionClient:
    if backend == "mlx":
        return MlxVisionClient(model or DEFAULT_MLX_MODEL, max_tokens=max_tokens)
    return OllamaVisionClient(
        model or DEFAULT_OLLAMA_MODEL,
        ollama_url=ollama_url,
        timeout_seconds=timeout_seconds,
    )
