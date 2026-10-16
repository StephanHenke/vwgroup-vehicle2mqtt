"""Client for the VW EU Data Act portal.

Adapted from https://github.com/mikrohard/hass-vw-eu-data-act (MIT).
"""

from __future__ import annotations

import io
import json
import logging
import hashlib
import uuid
import zipfile
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree import ElementTree

import aiohttp

from .api_constants import (
    BASE_URL, BRAND_CLIENT_IDS, BRAND_STATE_ALIASES, IDENTITY_BASE,
    OIDC_AUTHORIZE_URL, OIDC_CLIENT_ID, OIDC_SCOPE, OIDC_REDIRECT_URI,
    VEHICLES_PATH, RELATION_PATH, METADATA_PATH, LIST_PATH, DOWNLOAD_PATH,
    NO_CONTENT_SUFFIX, USER_AGENT,
)
from .api_identity import (
    _FormParser, _parse_form, _extract_template_model, _extract_csrf,
    _safe_url_for_log, _redact_sensitive_path_parts, _login_fields,
    _login_error, _terms_payload, _terms_action_url, _form_payload,
    _marketing_payload, _marketing_skip_url,
)
from .api_models import ApiError, AuthError, PortalConfig
from .api_login import PortalLogin

LOG = logging.getLogger(__name__)

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
        authorize_url = await self._get_authorize_url()
        await PortalLogin(self._session, self._config).login(authorize_url)

    def _build_authorize_url(self) -> str:
        brand = self._config.brand.upper()
        state_brand = BRAND_STATE_ALIASES.get(brand, brand)
        params = {
            "client_id": BRAND_CLIENT_IDS.get(brand, OIDC_CLIENT_ID),
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": f"{self._config.country}__{self._config.language}__{state_brand}",
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    async def _get_authorize_url(self) -> str:
        brand = self._config.brand.upper()
        if brand in BRAND_CLIENT_IDS:
            return self._build_authorize_url()
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
