"""Client for the VW EU Data Act portal.

Adapted from https://github.com/mikrohard/hass-vw-eu-data-act (MIT).
"""

from __future__ import annotations

import io
import json
import logging
import re
import hashlib
import uuid
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from xml.etree import ElementTree

import aiohttp

LOG = logging.getLogger(__name__)

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_CLIENT_ID = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"

VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

NO_CONTENT_SUFFIX = "_no_content_found.zip"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


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

    @property
    def oidc_state(self) -> str:
        return f"{self.country}__{self.language}__{self.brand}"


@dataclass(frozen=True)
class DatasetFile:
    name: str
    media_type: str
    content: str
    sha256: str
    xml_json: dict | None = None


@dataclass(frozen=True)
class DatasetDownload:
    name: str
    payload: dict
    files: list[DatasetFile]
    raw: bytes = b""


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self.field_items: list[tuple[str, str]] = []
        self._in_form = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        attr = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attr.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form:
            name = attr.get("name")
            if name:
                value = attr.get("value") or ""
                self.fields[name] = value
                self.field_items.append((name, value))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


def _parse_form(html: str) -> _FormParser:
    parser = _FormParser()
    parser.feed(html)
    return parser


def _extract_template_model(html: str) -> dict:
    idx = html.find("templateModel")
    if idx == -1:
        return {}
    brace = html.find("{", idx)
    if brace == -1:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        char = html[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except ValueError:
                    return {}
    return {}


def _extract_csrf(html: str) -> str | None:
    match = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return match.group(1) if match else None


def _safe_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    path = _redact_sensitive_path_parts(parsed.path)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return path or url


def _redact_sensitive_path_parts(path: str) -> str:
    path = re.sub(r"(/datadelivery/vehicles/)[^/]+/[^/]+", r"\1<redacted>/<redacted>", path)
    path = re.sub(r"(/datarequest/vehicles/)[^/]+", r"\1<redacted>", path)
    return re.sub(r"(/relations/)[^/]+", r"\1<redacted>", path)


def _login_fields(html: str) -> tuple[dict[str, str], str | None]:
    form = _parse_form(html)
    fields: dict[str, str] = dict(form.fields)
    model = _extract_template_model(html)
    if model:
        for key in ("hmac", "relayState"):
            if model.get(key):
                fields[key] = model[key]
        email = (model.get("emailPasswordForm") or {}).get("email")
        if email:
            fields.setdefault("email", email)
    csrf = _extract_csrf(html)
    if csrf:
        fields.setdefault("_csrf", csrf)
    return fields, form.action


def _login_error(html: str) -> str | None:
    model = _extract_template_model(html)
    err = model.get("error") or model.get("errorCode")
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or str(err)
    return str(err) if err else None


def _terms_payload(html: str) -> dict[str, str]:
    model = _extract_template_model(html)
    data: dict[str, str] = {}
    for idx, doc in enumerate(model.get("legalDocuments") or []):
        prefix = f"legalDocuments[{idx}]"
        for key in ("name", "language", "version", "countryCode"):
            data[f"{prefix}.{key}"] = str(doc.get(key) or "")
        data[f"{prefix}.updated"] = "yes" if doc.get("updated") else "no"
        data[f"{prefix}.skippable"] = "yes" if doc.get("skippable") else "no"
        data[f"{prefix}.declinable"] = "yes" if doc.get("declinable") else "no"
    if model.get("relayState"):
        data["relayState"] = str(model["relayState"])
    if model.get("hmac"):
        data["hmac"] = str(model["hmac"])
    if csrf := _extract_csrf(html):
        data["_csrf"] = csrf
    return data


def _terms_action_url(landing: str, html: str) -> str:
    model = _extract_template_model(html)
    client_id = (
        (model.get("clientLegalEntityModel") or {}).get("clientId")
        or OIDC_CLIENT_ID
    )
    parsed = urlparse(landing)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/signin-service/v1/{client_id}/terms-and-conditions"


def _form_payload(html: str) -> list[tuple[str, str]]:
    return _parse_form(html).field_items


def _marketing_payload(html: str) -> list[tuple[str, str]]:
    model = _extract_template_model(html)
    data: list[tuple[str, str]] = []
    csrf = model.get("csrf") or {}
    if isinstance(csrf, dict) and csrf.get("parameterName") and csrf.get("token"):
        data.append((str(csrf["parameterName"]), str(csrf["token"])))
    elif token := _extract_csrf(html):
        data.append(("_csrf", token))
    for model_key, field_name in (
        ("documentKey", "documentKey"),
        ("relayStateToken", "relayState"),
        ("hmac", "hmac"),
        ("countryOfJurisdiction", "countryOfJurisdiction"),
        ("language", "language"),
        ("callback", "callback"),
    ):
        if model.get(model_key) is not None:
            data.append((field_name, str(model[model_key])))
    selected_channels = {
        item.get("channelId")
        for item in model.get("marketChannels") or []
        if item.get("channelType") == "BOUND_TO_BASIC_AGREEMENT"
    }
    for item in model.get("marketChannels") or []:
        channel_id = item.get("channelId")
        if channel_id:
            value = "true" if channel_id in selected_channels else "false"
            data.append((f"channel{channel_id}", value))
    return data


def _marketing_skip_url(landing: str, html: str) -> str:
    model = _extract_template_model(html)
    parsed = urlparse(landing)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return (
        f"{origin}/signin-service/v1/consent/marketing/"
        f"{model['userId']}/{model['clientId']}/{model['step']}/skip"
    )


def _extract_vins(payload) -> list[dict]:
    vins: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            vin = node.get("vin") or node.get("vehicleIdentificationNumber")
            if isinstance(vin, str) and len(vin) == 17:
                vins.setdefault(vin, {"vin": vin})
                nick = node.get("vehicleNickname") or node.get("nickname") or node.get("modelName")
                if nick and not vins[vin].get("nickname"):
                    vins[vin]["nickname"] = nick
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(vins.values())


class EudaApiClient:
    """Authenticated client for the EU Data Act portal."""

    def __init__(self, session: aiohttp.ClientSession, config: PortalConfig) -> None:
        self._session = session
        self._config = config
        self._logged_in = False

    async def _get(
        self,
        url: str,
        *,
        headers: dict | None = None,
        allow_redirects: bool = True,
    ):
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        return await self._session.get(
            url,
            headers=request_headers,
            allow_redirects=allow_redirects,
        )

    async def async_login(self) -> None:
        self._logged_in = False
        try:
            await self._do_login()
        except aiohttp.ClientError as err:
            raise ApiError(f"Network error during login: {err}") from err
        self._logged_in = True

    async def _do_login(self) -> None:
        try:
            async with await self._get(f"{BASE_URL}/") as resp:
                await resp.read()
        except aiohttp.ClientError as err:
            LOG.debug("Portal priming failed, continuing: %s", err)

        authorize_url = await self._get_authorize_url()
        LOG.debug("OIDC authorize URL: %s", authorize_url)
        async with await self._get(authorize_url) as resp:
            signin_url = str(resp.url)
            signin_html = await resp.text()

        fields, action = _login_fields(signin_html)
        if "hmac" not in fields or "_csrf" not in fields:
            raise AuthError(f"Could not parse sign-in form, found fields: {sorted(fields)}")
        fields["email"] = self._config.email
        identifier_action = urljoin(signin_url, action or "")
        async with self._session.post(
            identifier_action,
            data=fields,
            headers={"User-Agent": USER_AGENT, "Referer": signin_url},
        ) as resp:
            authenticate_url = str(resp.url)
            authenticate_html = await resp.text()

        fields2, action2 = _login_fields(authenticate_html)
        if "hmac" not in fields2 or "_csrf" not in fields2:
            err = _login_error(authenticate_html)
            raise AuthError(
                err
                or "Identity portal did not return the password form. "
                "Check the email address or login flow."
            )
        fields2["email"] = self._config.email
        fields2["password"] = self._config.password
        authenticate_action = (
            urljoin(authenticate_url, action2)
            if action2
            else authenticate_url.split("?", 1)[0]
        )

        async with self._session.post(
            authenticate_action,
            data=fields2,
            headers={"User-Agent": USER_AGENT, "Referer": authenticate_url},
        ) as resp:
            landing = str(resp.url)
            safe_landing = _safe_url_for_log(landing)
            LOG.debug("Identity login landed at %s", safe_landing)
            landing_html = await resp.text()
            if resp.status >= 400 and not await self._accept_missing_login_landing(resp):
                err = _login_error(landing_html)
                raise AuthError(err or f"Login rejected with HTTP {resp.status}")

        portal_host = urlparse(BASE_URL).netloc
        if "terms-and-conditions" in landing:
            LOG.info("Accepting required VW identity terms at %s", safe_landing)
            terms_action = _terms_action_url(landing, landing_html)
            terms_origin = urlparse(terms_action)
            async with self._session.post(
                terms_action,
                data=_terms_payload(landing_html),
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": landing,
                    "Origin": f"{terms_origin.scheme}://{terms_origin.netloc}",
                },
            ) as resp:
                landing = str(resp.url)
                safe_landing = _safe_url_for_log(landing)
                LOG.debug("Terms confirmation landed at %s", safe_landing)
                landing_html = await resp.text()
                if resp.status >= 400 and not await self._accept_missing_login_landing(resp):
                    err = _login_error(landing_html)
                    raise AuthError(
                        err
                        or "Could not confirm VW identity terms automatically. "
                        f"Login landed at {safe_landing}"
                    )
        if "/consent/users/" in landing:
            LOG.info("Granting required VW identity client consent at %s", safe_landing)
            consent_payload = _form_payload(landing_html)
            if not consent_payload:
                raise AuthError(
                    "VW identity client requires consent, but the consent form could not be parsed. "
                    f"Login landed at {safe_landing}"
                )
            consent_origin = urlparse(landing)
            async with self._session.post(
                landing,
                data=consent_payload,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": landing,
                    "Origin": f"{consent_origin.scheme}://{consent_origin.netloc}",
                },
            ) as resp:
                landing = str(resp.url)
                safe_landing = _safe_url_for_log(landing)
                LOG.debug("Consent confirmation landed at %s", safe_landing)
                landing_html = await resp.text()
                if resp.status >= 400 and not await self._accept_missing_login_landing(resp):
                    err = _login_error(landing_html)
                    raise AuthError(
                        err
                        or "Could not grant VW identity client consent automatically. "
                        f"Login landed at {safe_landing}"
                    )
        if "/consent/marketing/" in landing:
            LOG.info("Skipping optional VW identity marketing consent at %s", safe_landing)
            marketing_skip = _marketing_skip_url(landing, landing_html)
            marketing_origin = urlparse(marketing_skip)
            async with self._session.post(
                marketing_skip,
                data=_marketing_payload(landing_html),
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": landing,
                    "Origin": f"{marketing_origin.scheme}://{marketing_origin.netloc}",
                },
            ) as resp:
                landing = str(resp.url)
                safe_landing = _safe_url_for_log(landing)
                LOG.debug("Marketing consent skip landed at %s", safe_landing)
                landing_html = await resp.text()
                if resp.status >= 400 and not await self._accept_missing_login_landing(resp):
                    err = _login_error(landing_html)
                    raise AuthError(
                        err
                        or "Could not skip optional VW identity marketing consent automatically. "
                        f"Login landed at {safe_landing}"
                    )
        if "verification/email-sent" in landing:
            raise AuthError(
                "The VW identity client requires email verification for this account/client. "
                "Check the account mailbox, open the Volkswagen verification link, then run "
                f"the service again. Login landed at {safe_landing}"
            )
        if "signin-service" in landing or "/error" in landing:
            raise AuthError("Login failed. Check email/password or complete browser login first.")
        if urlparse(landing).netloc != portal_host:
            raise AuthError(f"Login did not complete, ended at {safe_landing}")

    async def _accept_missing_login_landing(self, response: aiohttp.ClientResponse) -> bool:
        landing = urlparse(str(response.url))
        portal = urlparse(BASE_URL)
        if (
            response.status != 404
            or (landing.scheme, landing.netloc) != (portal.scheme, portal.netloc)
            or not re.fullmatch(r"/(?:content/euda/)?[a-z]{2}/[a-z]{2}/user\.html", landing.path)
        ):
            return False
        callback_seen = any(
            item.status in (301, 302, 303, 307, 308)
            and (urlparse(str(item.url)).scheme, urlparse(str(item.url)).netloc)
            == (portal.scheme, portal.netloc)
            and urlparse(str(item.url)).path == "/services/callbacklogin"
            for item in response.history
        )
        if not callback_seen:
            return False

        # The localized page can be missing even with a valid session. Probe the
        # authenticated API directly, without redirects or recursive login retries.
        async with await self._get(
            f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT",
            allow_redirects=False,
        ) as probe:
            if probe.status != 200:
                raise AuthError(
                    "Portal landing page returned HTTP 404 after callback, but the "
                    f"authenticated session check returned HTTP {probe.status}. "
                    "Complete browser login and any required consent, then retry."
                )
            try:
                payload = await probe.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise AuthError("Portal session check did not return vehicle API JSON") from err
            if not isinstance(payload, (dict, list)) or (
                isinstance(payload, dict) and any(key in payload for key in ("error", "errorCode", "statusCode"))
            ):
                raise AuthError("Portal session check returned an unexpected vehicle API response")
        LOG.warning(
            "Portal landing page %s returned HTTP 404 after callback; authenticated API session verified",
            _safe_url_for_log(str(response.url)),
        )
        return True

    def _build_authorize_url(self) -> str:
        params = {
            "client_id": OIDC_CLIENT_ID,
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": self._config.oidc_state,
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    async def _get_authorize_url(self) -> str:
        brand = self._config.brand.upper()
        redirect_url = f"{BASE_URL}/services/redirect/authentication?{urlencode({'brand': brand, 'method': 'login'})}"
        try:
            async with await self._get(
                redirect_url,
                headers={"Referer": f"{BASE_URL}/{self._config.country}/{self._config.language}/login.html"},
            ) as resp:
                parsed = urlparse(str(resp.url))
                query = parse_qs(parsed.query)
                redirect = (query.get("redirect") or [""])[0]
                if redirect:
                    LOG.debug(
                        "Using %s brand authorize URL from portal redirect",
                        brand,
                    )
                    return redirect
        except aiohttp.ClientError as err:
            LOG.debug("Could not fetch brand authorize URL, using fallback: %s", err)
        return self._build_authorize_url()

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict | None = None,
        _retry: bool = True,
    ):
        try:
            async with await self._get(url, headers=headers) as resp:
                if resp.status in (401, 403) and _retry:
                    self._logged_in = False
                    await self.async_login()
                    return await self._get_json(url, headers=headers, _retry=False)
                if resp.status >= 400:
                    raise ApiError(f"GET {_safe_url_for_log(url)} -> HTTP {resp.status}")
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise ApiError(f"Connection error for {_safe_url_for_log(url)}: {err}") from err
        try:
            return json.loads(text)
        except ValueError as err:
            raise ApiError(f"Invalid JSON from {_safe_url_for_log(url)}: {err}") from err

    async def async_ensure_login(self) -> None:
        if not self._logged_in:
            await self.async_login()

    async def async_list_vehicles(self) -> list[dict]:
        await self.async_ensure_login()
        payload = await self._get_json(f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT")
        vehicles = _extract_vins(payload)
        for vehicle in vehicles:
            try:
                relation = await self.async_get_relation(vehicle["vin"])
                nickname = (relation.get("relation") or {}).get("vehicleNickname")
                if nickname:
                    vehicle["nickname"] = nickname
            except ApiError as err:
                LOG.debug("Could not fetch vehicle nickname for %s: %s", vehicle["vin"], err)
        return vehicles

    async def async_get_relation(self, vin: str) -> dict:
        await self.async_ensure_login()
        headers = {"traceid": f"vehicle-relation-fetch-{uuid.uuid4()}"}
        return await self._get_json(
            f"{BASE_URL}{RELATION_PATH.format(vin=vin)}",
            headers=headers,
        )

    async def async_get_metadata(self, vin: str) -> dict:
        await self.async_ensure_login()
        return await self._get_json(f"{BASE_URL}{METADATA_PATH.format(vin=vin)}")

    async def async_list_datasets(self, vin: str, identifier: str) -> list[dict]:
        await self.async_ensure_login()
        url = f"{BASE_URL}{LIST_PATH.format(vin=vin, identifier=identifier)}"
        data = await self._get_json(url, headers={"type": "partial"})
        return data if isinstance(data, list) else data.get("files", [])

    async def async_download_dataset(self, vin: str, identifier: str, name: str) -> DatasetDownload:
        await self.async_ensure_login()
        if name.endswith(NO_CONTENT_SUFFIX):
            raise ApiError(f"{name} contains no content")
        url = f"{BASE_URL}{DOWNLOAD_PATH.format(vin=vin, identifier=identifier)}"
        headers = {"filename": name, "type": "partial"}
        try:
            async with await self._get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    self._logged_in = False
                    await self.async_login()
                    async with await self._get(url, headers=headers) as resp2:
                        if resp2.status >= 400:
                            raise ApiError(f"Download {name} -> HTTP {resp2.status}")
                        raw = await resp2.read()
                elif resp.status >= 400:
                    raise ApiError(f"Download {name} -> HTTP {resp.status}")
                else:
                    raw = await resp.read()
        except aiohttp.ClientError as err:
            raise ApiError(f"Connection error downloading {name}: {err}") from err
        return self._unzip_dataset(raw, name)

    @staticmethod
    def _unzip_json(raw: bytes, name: str) -> dict:
        return EudaApiClient._unzip_dataset(raw, name).payload

    @staticmethod
    def _unzip_dataset(raw: bytes, name: str) -> DatasetDownload:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                payload: dict | None = None
                files: list[DatasetFile] = []
                for item in zf.infolist():
                    if item.is_dir():
                        continue
                    raw_content = zf.read(item)
                    content = raw_content.decode("utf-8-sig", errors="replace")
                    if item.filename.lower().endswith(".json") and payload is None:
                        payload = json.loads(content)
                    files.append(
                        DatasetFile(
                            name=item.filename,
                            media_type=_media_type_for_name(item.filename),
                            content=content,
                            sha256=hashlib.sha256(raw_content).hexdigest(),
                            xml_json=_xml_to_json(content) if item.filename.lower().endswith(".xml") else None,
                        )
                    )
                if payload is None:
                    raise ApiError(f"No JSON inside {name}")
                return DatasetDownload(name=name, payload=payload, files=files, raw=raw)
        except (zipfile.BadZipFile, ValueError) as err:
            raise ApiError(f"Could not read {name}: {err}") from err


def _media_type_for_name(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".xml"):
        return "application/xml"
    if lower.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _xml_to_json(content: str) -> dict | None:
    try:
        return _xml_element_to_json(ElementTree.fromstring(content))
    except ElementTree.ParseError:
        return None


def _xml_element_to_json(element: ElementTree.Element) -> dict:
    children = [_xml_element_to_json(child) for child in list(element)]
    result: dict[str, object] = {"tag": _strip_xml_namespace(element.tag)}
    if element.attrib:
        result["attributes"] = dict(element.attrib)
    text = (element.text or "").strip()
    if text:
        result["text"] = text
    if children:
        result["children"] = children
    return result


def _strip_xml_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag
