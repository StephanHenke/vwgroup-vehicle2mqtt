"""Synthetic redirect-chain regressions for GitHub issue #4; no account data."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from vw_euda_mqtt.api import EudaApiClient, BASE_URL, VEHICLES_PATH, AuthError, PortalConfig


FORM = '<form><input name="hmac" value="test"><input name="_csrf" value="test"></form>'
CALLBACK = f"{BASE_URL}/services/callbacklogin"


class Response:
    def __init__(self, url, status=200, html=FORM, history=(), payload=None):
        self.url = url
        self.status = status
        self.history = [SimpleNamespace(url=url, status=302) for url in history]
        self.html = html
        self.payload = [] if payload is None else payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return self.html

    async def read(self):
        return self.html.encode()

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def client(self, landing, *, probe=None, intermediate=None):
        session = Mock()
        responses = [Response("https://identity.vwgroup.io/password")]
        if intermediate:
            responses.append(intermediate)
        responses.append(landing)
        session.post.side_effect = responses
        client = EudaApiClient(session, PortalConfig("user@example.com", "test-password"))
        client._get_authorize_url = AsyncMock(return_value="https://identity.vwgroup.io/authorize")
        client._get = AsyncMock(side_effect=[
            Response(BASE_URL), Response("https://identity.vwgroup.io/signin"),
            probe or Response(f"{BASE_URL}{VEHICLES_PATH}"),
        ])
        return client

    async def test_issue_4_404_after_callback_verifies_session(self):
        client = self.client(Response(f"{BASE_URL}/se/en/user.html", 404, history=[CALLBACK]))
        await client.async_login()
        self.assertTrue(client._logged_in)
        client._get.assert_awaited_with(
            f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT", allow_redirects=False,
        )

    async def test_normal_login_does_not_need_extra_api_call(self):
        client = self.client(Response(f"{BASE_URL}/de/de/user.html"))
        await client.async_login()
        self.assertTrue(client._logged_in)
        self.assertEqual(client._get.await_count, 2)

    async def test_unrelated_http_errors_and_untrusted_callbacks_still_fail(self):
        cases = [
            (404, f"{BASE_URL}/se/en/user.html", []),
            (401, f"{BASE_URL}/se/en/user.html", [CALLBACK]),
            (403, f"{BASE_URL}/se/en/user.html", [CALLBACK]),
            (500, f"{BASE_URL}/se/en/user.html", [CALLBACK]),
            (404, "https://identity.vwgroup.io/se/en/user.html", [CALLBACK]),
            (404, f"{BASE_URL}/error", [CALLBACK]),
            (404, f"{BASE_URL}/se/en/user.html", ["https://example.org/services/callbacklogin"]),
            (404, f"{BASE_URL}/se/en/user.html", [f"{BASE_URL}/other?next=/services/callbacklogin"]),
        ]
        for status, url, history in cases:
            with self.subTest(status=status, url=url, history=history):
                client = self.client(Response(url, status, history=history))
                client._logged_in = True
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertEqual(client._get.await_count, 2)

    async def test_callback_does_not_prove_authenticated_session(self):
        for status in (302, 401, 403, 500):
            with self.subTest(status=status):
                client = self.client(
                    Response(f"{BASE_URL}/se/en/user.html", 404, history=[CALLBACK]),
                    probe=Response(f"{BASE_URL}{VEHICLES_PATH}", status),
                )
                with self.assertRaisesRegex(AuthError, "session check returned HTTP"):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertEqual(client._get.await_count, 3)

    async def test_html_invalid_json_and_error_payloads_do_not_confirm_session(self):
        for payload in (ValueError("html"), aiohttp.ContentTypeError(None, ()),
                        {"error": "consent_required"}, {"statusCode": 401}, "login"):
            with self.subTest(payload=type(payload).__name__):
                client = self.client(
                    Response(f"{BASE_URL}/se/en/user.html", 404, history=[CALLBACK]),
                    probe=Response(f"{BASE_URL}{VEHICLES_PATH}", payload=payload),
                )
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)

    async def test_missing_page_after_existing_consent_steps(self):
        intermediates = [
            Response("https://identity.vwgroup.io/signin-service/terms-and-conditions"),
            Response("https://identity.vwgroup.io/signin-service/consent/users/test"),
            Response("https://identity.vwgroup.io/signin-service/consent/marketing/test", html=(
                'templateModel = {"userId":"test","clientId":"test","step":"test"};'
            )),
        ]
        for intermediate in intermediates:
            with self.subTest(url=intermediate.url):
                client = self.client(
                    Response(f"{BASE_URL}/se/en/user.html", 404, history=[CALLBACK]),
                    intermediate=intermediate,
                )
                await client.async_login()
                self.assertTrue(client._logged_in)
                self.assertEqual(client._session.post.call_count, 3)

    async def test_unresolved_consent_and_email_verification_still_fail(self):
        for path in ("/signin-service/consent/users/test", "/verification/email-sent"):
            with self.subTest(path=path):
                client = self.client(Response(f"https://identity.vwgroup.io{path}", html="<html></html>"))
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)
