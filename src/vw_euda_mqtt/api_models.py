"""Shared API models and exceptions."""

from __future__ import annotations

from dataclasses import dataclass

from .api_constants import BASE_URL

class ApiError(Exception):
    """Generic API failure."""


class AuthError(ApiError):
    """Authentication failed or session expired."""


@dataclass(frozen=True)
class PortalConfig:
    email: str
    password: str
    brand: str = "AUDI"
    country: str = "de"
    language: str = "de"
    base_url: str = BASE_URL

    @property
    def oidc_state(self) -> str:
        return f"{self.country}__{self.language}__{self.brand}"
