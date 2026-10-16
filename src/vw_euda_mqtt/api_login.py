"""Bounded Identity redirects, stopping at the authenticated portal callback."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import aiohttp
from yarl import URL

from .api_constants import BASE_URL, IDENTITY_BASE, USER_AGENT, VEHICLES_PATH
from .api_identity import (
    _form_payload,
    _login_error,
    _login_fields,
    _marketing_payload,
    _marketing_skip_url,
    _terms_action_url,
    _terms_payload,
)
from .api_models import AuthError, PortalConfig

LOG = logging.getLogger(__name__)
CALLBACK_PATH = "/services/callbacklogin"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 10


def _validate_login_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in (urlparse(BASE_URL).netloc, urlparse(IDENTITY_BASE).netloc)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AuthError("Login redirected to an unexpected location")


def _is_callback(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() == urlparse(BASE_URL).netloc and parsed.path == CALLBACK_PATH


def _is_user_page(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() == urlparse(BASE_URL).netloc and bool(
        re.fullmatch(r"/(?:content/euda/)?[a-z]{2}/[a-z]{2}/user\.html", parsed.path)
    )


def _marketing_callback(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != urlparse(IDENTITY_BASE).netloc or "/consent/marketing/" not in parsed.path:
        return None
    callback = (parse_qs(parsed.query).get("callback") or [""])[0]
    if not callback:
        return None
    _validate_login_url(callback)
    target = urlparse(callback)
    if target.netloc.lower() != urlparse(IDENTITY_BASE).netloc or target.path not in (
        "/oidc/v1/oauth/client/callback", "/oidc/v1/oauth/client/callback/success",
    ):
        raise AuthError("Unexpected marketing consent callback URL")
    return target._replace(query=urlencode(parse_qsl(target.query, keep_blank_values=True))).geturl()


@dataclass(frozen=True)
class _LoginResponse:
    url: str
    status: int
    html: str
    session_created: bool = False


class PortalLogin:
    def __init__(self, session: aiohttp.ClientSession, config: PortalConfig) -> None:
        self.session = session
        self.config = config

    async def _request(self, url: str, *, data=None, referer: str | None = None) -> _LoginResponse:
        method = "POST" if data is not None else "GET"
        language = self.config.language.lower()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{language}-{self.config.country.upper()},{language};q=0.9,en;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        for hop in range(MAX_REDIRECTS + 1):
            _validate_login_url(url)
            if _is_user_page(url):
                raise AuthError("Login reached a user page without an authenticated portal callback")
            if callback := _marketing_callback(url):
                LOG.info("Skipping optional marketing consent through its Identity callback")
                url = callback
                method, data = "GET", None
            if method == "POST" and urlparse(url).netloc.lower() != urlparse(IDENTITY_BASE).netloc:
                raise AuthError("Refusing to send Identity form data outside the Identity host")
            request_headers = dict(headers)
            if method == "POST":
                request_headers["Origin"] = IDENTITY_BASE
            request = (
                self.session.post(url, data=data, headers=request_headers, allow_redirects=False)
                if method == "POST"
                else self.session.get(url, headers=request_headers, allow_redirects=False)
            )
            async with request as response:
                current = str(response.url)
                if _is_callback(current):
                    cookie = response.cookies.get("access_token")
                    # Require a newly issued, usable cookie, never an old jar entry.
                    jar_cookie = self.session.cookie_jar.filter_cookies(URL(f"{BASE_URL}{VEHICLES_PATH}")).get("access_token")
                    established = bool(cookie and cookie.value and jar_cookie and jar_cookie.value == cookie.value)
                    return _LoginResponse(current, response.status, "", established)
                html = await response.text()
                if response.status not in REDIRECT_STATUSES:
                    return _LoginResponse(current, response.status, html)
                location = response.headers.get("Location")
                if not location:
                    raise AuthError("Login redirect is missing its Location header")
                target = urljoin(current, location)
                _validate_login_url(target)
                if hop == MAX_REDIRECTS:
                    raise AuthError(f"Login exceeded {MAX_REDIRECTS} redirects")
                if response.status in (301, 302, 303):
                    method, data = "GET", None
                headers["Referer"] = current
                url = target
        raise AuthError("Login redirect chain did not complete")  # pragma: no cover

    async def login(self, authorize_url: str) -> None:
        signin = await self._request(authorize_url)
        self._check_status(signin)
        fields, action = _login_fields(signin.html)
        if "hmac" not in fields or "_csrf" not in fields:
            raise AuthError(f"Could not parse sign-in form, found fields: {sorted(fields)}")
        fields["email"] = self.config.email
        password_form = await self._request(
            urljoin(signin.url, action or ""), data=fields, referer=signin.url,
        )
        self._check_status(password_form)
        fields, action = _login_fields(password_form.html)
        if "hmac" not in fields or "_csrf" not in fields:
            raise AuthError(_login_error(password_form.html) or "Identity portal did not return the password form")
        fields.update(email=self.config.email, password=self.config.password)
        landing = await self._request(
            urljoin(password_form.url, action) if action else password_form.url.split("?", 1)[0],
            data=fields, referer=password_form.url,
        )
        completed_steps: set[str] = set()
        while True:
            self._check_status(landing)
            if _is_callback(landing.url):
                if not landing.session_created:
                    raise AuthError("Portal callback did not establish a session (access_token cookie missing)")
                await self._verify_session()
                LOG.debug("Portal callback and authenticated vehicle API confirmed the session")
                return
            path = urlparse(landing.url).path
            if "verification/email-sent" in path:
                raise AuthError("VW identity requires email verification. Open the verification email, then retry.")
            if "terms-and-conditions" in path:
                step = "terms"
                action = _terms_action_url(landing.url, landing.html)
                data = _terms_payload(landing.html)
            elif "/consent/users/" in path:
                step = "client consent"
                action = landing.url
                data = _form_payload(landing.html)
                if not data:
                    raise AuthError("Identity client requires consent. Complete browser login, then retry.")
            elif "/consent/marketing/" in path:
                step = "marketing skip"
                try:
                    action = _marketing_skip_url(landing.url, landing.html)
                except KeyError as err:
                    raise AuthError("Optional marketing consent could not be skipped. Complete browser login.") from err
                data = _marketing_payload(landing.html)
            else:
                raise AuthError("Login did not reach the portal callback. Check credentials or complete browser login.")
            if step in completed_steps:
                raise AuthError(f"Identity {step} did not complete. Complete browser login, then retry.")
            completed_steps.add(step)
            landing = await self._request(action, data=data, referer=landing.url)

    async def _verify_session(self) -> None:
        # Probe directly: an expired/rejected cookie must not start another login
        # or follow a redirect into the browser/CMS flow.
        async with self.session.get(
            f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise AuthError(f"Authenticated portal session check returned HTTP {response.status}")
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise AuthError("Authenticated portal session check did not return vehicle API JSON") from err
            if not isinstance(payload, (dict, list)) or (
                isinstance(payload, dict)
                and (
                    any(key in payload for key in ("error", "errorCode", "statusCode"))
                    or isinstance(payload.get("status"), int) and payload["status"] >= 400
                )
            ):
                raise AuthError("Authenticated portal session check returned an unexpected vehicle API response")

    @staticmethod
    def _check_status(response: _LoginResponse) -> None:
        if response.status >= 400:
            raise AuthError(_login_error(response.html) or f"Login rejected with HTTP {response.status}")
