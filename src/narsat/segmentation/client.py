"""Client of the burn-scar inference service.

The model runs in its own process (``services/inference``) because its dependency
stack (PyTorch, TerraTorch) is large and versioned independently of the data
pipeline. The contract is deliberately small: a float32 reflectance cube in, a
float32 probability raster out, both as NumPy ``.npy`` bytes.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import numpy as np
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class InferenceError(RuntimeError):
    """The inference service refused the request or returned something unusable."""


class InferenceUnavailableError(InferenceError):
    """The service could not be reached (worth retrying)."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    version: str
    engine: str
    bands: list[str]
    resolution_m: float
    device: str


def _to_npy(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def _from_npy(data: bytes) -> np.ndarray:
    loaded: np.ndarray = np.load(io.BytesIO(data), allow_pickle=False)
    return loaded


class InferenceClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 600.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type(InferenceUnavailableError),
        wait=wait_exponential(multiplier=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise InferenceUnavailableError(f"inference service unreachable: {exc}") from exc
        if response.status_code in (502, 503, 504):
            raise InferenceUnavailableError(f"inference service HTTP {response.status_code}")
        if response.status_code != 200:
            raise InferenceError(
                f"inference service HTTP {response.status_code}: {response.text[:300]}"
            )
        return response

    def model_info(self) -> ModelInfo:
        payload = self._request("GET", "/v1/model").json()
        return ModelInfo(
            name=payload["name"],
            version=payload["version"],
            engine=payload["engine"],
            bands=list(payload["bands"]),
            resolution_m=float(payload["resolution_m"]),
            device=payload["device"],
        )

    def segment(self, reflectance: np.ndarray) -> np.ndarray:
        """Burn probability for a ``(6, height, width)`` reflectance cube (0-1 units).

        Missing values must be passed as NaN; the service replaces them itself, and
        the caller masks the corresponding outputs.
        """
        if reflectance.ndim != 3:
            raise ValueError("expected an array of shape (bands, height, width)")
        response = self._request(
            "POST",
            "/v1/segment",
            content=_to_npy(reflectance.astype("float32")),
            headers={"Content-Type": "application/x-npy"},
        )
        probability = _from_npy(response.content)
        if probability.shape != reflectance.shape[1:]:
            raise InferenceError(
                f"probability shape {probability.shape} != input {reflectance.shape[1:]}"
            )
        return probability.astype("float32")
