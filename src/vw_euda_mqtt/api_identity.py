"""Identity portal HTML parsing and consent payload helpers."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from .api_constants import OIDC_CLIENT_ID

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
