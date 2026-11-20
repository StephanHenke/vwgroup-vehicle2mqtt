"""Synthetic login transport and API regressions; no real account data."""

import json
import io
import unittest
import zipfile
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from yarl import URL

from vw_euda_mqtt.api import EudaApiClient, BASE_URL, IDENTITY_BASE, USER_AGENT, VEHICLES_PATH
from vw_euda_mqtt.api_login import MAX_REDIRECTS
from vw_euda_mqtt.api_constants import LEGACY_BASE_URL
from vw_euda_mqtt.api import ApiError, AuthError, PortalConfig


CALLBACK = f"{BASE_URL}/services/callbacklogin"
AUTHORIZE = f"{IDENTITY_BASE}/oidc/v1/authorize"
SIGNIN = f"{IDENTITY_BASE}/signin-service/test"
IDENTIFIER = f"{IDENTITY_BASE}/identifier"
PASSWORD = f"{IDENTITY_BASE}/authenticate"
USER_PAGE = f"{BASE_URL}/content/euda/se/en/user.html"
SESSION_CHECK = f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT"


def form(action):
    return f'<form action="{action}"><input name="hmac" value="test"><input name="_csrf" value="test"></form>'


class Response:
    def __init__(self, url, status=200, html="", *, location=None, cookie=None, payload=None, raw=b""):
        self.url = URL(url)
        self.status = status
        self.html = html
        self.headers = {"Location": location} if location is not None else {}
        self.cookies = SimpleCookie()
        if cookie is not None:
            self.cookies.load(cookie)
        self.released = False
        self.payload = [] if payload is None else payload
        self.raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.released = True

    async def text(self):
        if isinstance(self.html, Exception):
            raise self.html
        return self.html

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def read(self):
        return self.raw


class Session:
    def __init__(self, responses, *, portal_base_url=BASE_URL):
        self.responses = list(responses)
        self.cookie_jar = aiohttp.CookieJar()
        self.calls = []
        self.cms_status = 406
        self.session_check_url = f"{portal_base_url}{VEHICLES_PATH}?viewPosition=FRONT_LEFT"
        self.session_check = Response(self.session_check_url)

    def _request(self, method, url, **kwargs):
        self.calls.append((method, str(url), kwargs))
        if str(url) == USER_PAGE:
            return Response(USER_PAGE, self.cms_status)
        if str(url) == self.session_check_url:
            return self.session_check
        if not self.responses:
            raise AssertionError(f"Unexpected request to {urlparse(str(url)).path}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        # Query values vary with the brand; every other request must match exactly.
        if str(url).split("?", 1)[0] != AUTHORIZE and str(url) != str(response.url):
            raise AssertionError(f"Unexpected request to {urlparse(str(url)).path}")
        self.cookie_jar.update_cookies(response.cookies, response_url=response.url)
        return response

    def get(self, url, **kwargs):
        return GetRequest(self._request("GET", url, **kwargs))

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)


class GetRequest:
    """aiohttp GET can be awaited or used as an async context manager."""
    def __init__(self, response):
        self.response = response

    def __await__(self):
        async def result():
            return self.response
        return result().__await__()

    async def __aenter__(self):
        return await self.response.__aenter__()

    async def __aexit__(self, *args):
        return await self.response.__aexit__(*args)


def callback(status=302, *, cookie="access_token=test-token; Path=/; Secure; HttpOnly", location=USER_PAGE):
    return Response(CALLBACK, status, location=location, cookie=cookie)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *tail, config=None):
        responses = [
            Response(AUTHORIZE, 302, location=SIGNIN),
            Response(SIGNIN, html=form("/identifier")),
            Response(IDENTIFIER, html=form("/authenticate")),
            *tail,
        ]
        config = config or PortalConfig("user@example.com", "test-password")
        session = Session(responses, portal_base_url=config.base_url)
        client = EudaApiClient(session, config)
        return client, session, responses

    async def test_callback_cookie_completes_login_without_fetching_cms_or_priming(self):
        for cms_status in (401, 403, 404, 406, 410, 429, 500, 502, 503, 504):
            with self.subTest(cms_status=cms_status):
                client, session, responses = self.client(Response(PASSWORD, 302, location=CALLBACK), callback())
                session.cms_status = cms_status
                await client.async_login()
                self.assertTrue(client._logged_in)
                self.assertFalse(session.responses)
                self.assertTrue(all(r.released for r in responses))
                self.assertEqual([urlparse(url).path for _, url, _ in session.calls], [
                    "/oidc/v1/authorize", "/signin-service/test", "/identifier", "/authenticate", "/services/callbacklogin",
                    VEHICLES_PATH,
                ])
                self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))
                self.assertEqual(session.calls[3][2]["data"]["password"], "test-password")

    async def test_cookie_must_be_new_nonempty_and_usable_by_portal(self):
        for cookie in (None, "access_token=; Path=/", "access_token=token; Path=/elsewhere",
                       "access_token=token; Path=/services", "other=token; Path=/"):
            with self.subTest(cookie=cookie):
                client, session, _ = self.client(Response(PASSWORD, 302, location=CALLBACK), callback(cookie=cookie))
                session.cookie_jar.update_cookies({"access_token": "old-token"}, response_url=URL(BASE_URL))
                client._logged_in = True
                with self.assertRaisesRegex(AuthError, "cookie missing"):
                    await client.async_login()
                self.assertFalse(client._logged_in)

    async def test_verified_callback_needs_neither_body_nor_followup_page(self):
        for location in (None, USER_PAGE, f"{BASE_URL}/maintenance.html", "https://example.org/unused"):
            with self.subTest(location=location):
                response = callback(location=location)
                response.html = ValueError("callback body must not be decoded")
                client, session, _ = self.client(Response(PASSWORD, 302, location=CALLBACK), response)
                await client.async_login()
                self.assertTrue(client._logged_in)
                self.assertEqual(session.calls[-1][1], SESSION_CHECK)

    async def test_callback_errors_are_not_accepted_even_with_cookie(self):
        for status in range(400, 600):
            with self.subTest(status=status):
                client, _, _ = self.client(Response(PASSWORD, 302, location=CALLBACK), callback(status))
                with self.assertRaisesRegex(AuthError, f"HTTP {status}"):
                    await client.async_login()
                self.assertFalse(client._logged_in)

    async def test_direct_user_page_without_callback_is_rejected_without_fetch(self):
        client, session, _ = self.client(Response(PASSWORD, 302, location=USER_PAGE))
        with self.assertRaisesRegex(AuthError, "without an authenticated portal callback"):
            await client.async_login()
        self.assertEqual(len(session.calls), 4)

    async def test_untrusted_redirects_and_form_actions_are_rejected_before_request(self):
        for target in (
            "http://identity.vwgroup.io/callback", "https://example.org/callback",
            "https://identity.vwgroup.io.example.org/callback", "https://user@identity.vwgroup.io/callback",
            "https://identity.vwgroup.io:444/callback", f"{BASE_URL}/services/callbacklogin/extra",
        ):
            with self.subTest(target=target):
                tail = [Response(PASSWORD, 302, location=target)]
                if target.endswith("/extra"):
                    tail.append(Response(target))
                client, _, _ = self.client(*tail)
                with self.assertRaises(AuthError):
                    await client.async_login()
        client, session, _ = self.client()
        session.responses[1].html = form("https://example.org/steal")
        with self.assertRaises(AuthError):
            await client.async_login()
        self.assertEqual(len(session.calls), 2)

    async def test_redirect_loop_and_missing_location_fail(self):
        client, session, _ = self.client(*[
            Response(PASSWORD, 302, location=PASSWORD) for _ in range(MAX_REDIRECTS + 1)
        ])
        with self.assertRaisesRegex(AuthError, "exceeded"):
            await client.async_login()
        self.assertEqual(len(session.calls), 3 + MAX_REDIRECTS + 1)
        client, _, _ = self.client(Response(PASSWORD, 302))
        with self.assertRaisesRegex(AuthError, "Location"):
            await client.async_login()

    async def test_relative_redirects_and_307_308_get_redirects(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                portal_login = f"{BASE_URL}/login"
                client, _, _ = self.client(
                    Response(PASSWORD, 303, location=portal_login),
                    Response(portal_login, status, location="/services/callbacklogin"), callback(),
                )
                await client.async_login()
                self.assertTrue(client._logged_in)

    async def test_post_preserving_redirect_cannot_send_password_to_portal(self):
        for status in (307, 308):
            client, session, _ = self.client(Response(PASSWORD, status, location=CALLBACK))
            with self.assertRaisesRegex(AuthError, "form data outside"):
                await client.async_login()
            self.assertEqual(len(session.calls), 4)

    async def test_known_legacy_portal_redirect_reaches_current_callback_and_api(self):
        legacy_login = f"{LEGACY_BASE_URL}/login?code=example&state=de__de__AUDI"
        current_login = f"{BASE_URL}/login?code=example&state=de__de__AUDI"
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                client, session, _ = self.client(
                    Response(PASSWORD, 302, location=legacy_login),
                    Response(legacy_login, status, location=current_login),
                    Response(current_login, 302, location=CALLBACK), callback(),
                )
                await client.async_login()
                self.assertTrue(client._logged_in)
                self.assertEqual(session.calls[-1][1], SESSION_CHECK)
                self.assertTrue(all(method == "GET" for method, url, _ in session.calls if url.startswith(LEGACY_BASE_URL)))

    async def test_legacy_cookie_cannot_authenticate_current_portal(self):
        client, session, _ = self.client(
            Response(PASSWORD, 302, location=CALLBACK),
            callback(cookie="access_token=test-token; Domain=drivesomethinggreater.com; Path=/; Secure"),
        )
        session.cookie_jar.update_cookies({"access_token": "old-token"}, response_url=URL(LEGACY_BASE_URL))
        with self.assertRaisesRegex(AuthError, "cookie missing"):
            await client.async_login()
        self.assertFalse(client._logged_in)

    async def test_unknown_future_redirect_is_actionable_and_redacted(self):
        target = "https://next-portal.example/login?code=secret-code&state=private-state#private-fragment"
        client, session, _ = self.client(Response(PASSWORD, 302, location=target))
        with self.assertRaises(AuthError) as caught:
            await client.async_login()
        message = str(caught.exception)
        self.assertIn("next-portal.example", message)
        self.assertIn("VW_EUDA_PORTAL_BASE_URL", message)
        for secret in ("secret-code", "private-state", "private-fragment", "test-password"):
            self.assertNotIn(secret, message)
        self.assertEqual(len(session.calls), 4)

    async def test_configured_portal_is_used_for_oauth_cookie_check_and_every_api_url(self):
        from vw_euda_mqtt.api_constants import METADATA_PATH, RELATION_PATH, LIST_PATH, DOWNLOAD_PATH
        base = "https://next-portal.example"
        config = PortalConfig("user@example.com", "test-password", base_url=base)
        custom_callback = base + "/services/callbacklogin"
        client, session, _ = self.client(
            Response(PASSWORD, 302, location=custom_callback),
            Response(custom_callback, 302, cookie="access_token=test-token; Path=/; Secure"), config=config,
        )
        await client.async_login()
        authorize_query = parse_qs(urlparse(session.calls[0][1]).query)
        self.assertEqual(authorize_query["redirect_uri"], [base + "/login"])
        self.assertEqual(session.calls[-1][1], base + VEHICLES_PATH + "?viewPosition=FRONT_LEFT")
        vin, identifier = "TESTVIN1234567890", "test-stream"
        metadata = base + METADATA_PATH.format(vin=vin)
        relation = base + RELATION_PATH.format(vin=vin)
        listing = base + LIST_PATH.format(vin=vin, identifier=identifier)
        download = base + DOWNLOAD_PATH.format(vin=vin, identifier=identifier)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("sample.json", '{"Data": []}')
        session.responses.extend([
            Response(metadata, html='{"Identifier":"test-stream"}'),
            Response(relation, html='{}'),
            Response(listing, html='[{"name":"sample.zip"}]'),
            Response(download, raw=raw.getvalue()),
        ])
        self.assertEqual((await client.async_get_metadata(vin))["Identifier"], identifier)
        self.assertEqual(await client.async_get_relation(vin), {})
        self.assertEqual(await client.async_list_datasets(vin, identifier), [{"name": "sample.zip"}])
        self.assertEqual((await client.async_download_dataset(vin, identifier, "sample.zip")).payload, {"Data": []})
        self.assertEqual([url for _, url, _ in session.calls[-4:]], [metadata, relation, listing, download])
        self.assertFalse(any(url.startswith(LEGACY_BASE_URL) for _, url, _ in session.calls))

    async def test_configured_portal_cannot_receive_post_preserved_password(self):
        base = "https://next-portal.example"
        for status in (307, 308):
            with self.subTest(status=status):
                client, session, _ = self.client(
                    Response(PASSWORD, status, location=base + "/services/callbacklogin"),
                    config=PortalConfig("user@example.com", "test-password", base_url=base),
                )
                with self.assertRaisesRegex(AuthError, "form data outside"):
                    await client.async_login()
                self.assertEqual(len(session.calls), 4)

    async def test_marketing_callback_skips_page_and_reencodes_query(self):
        resume = f"{IDENTITY_BASE}/oidc/v1/oauth/client/callback/success?scopes=openid+cars"
        marketing = f"{IDENTITY_BASE}/signin-service/consent/marketing/test?" + urlencode({
            "callback": resume.replace("openid+cars", "openid cars"),
        })
        client, session, _ = self.client(
            Response(PASSWORD, 302, location=marketing), Response(resume, 302, location=CALLBACK), callback(),
        )
        await client.async_login()
        self.assertTrue(client._logged_in)
        self.assertNotIn(marketing, [url for _, url, _ in session.calls])
        self.assertEqual(len([c for c in session.calls if c[0] == "POST"]), 2)

    async def test_invalid_marketing_callbacks_fail_without_following(self):
        for target in ("https://example.org/callback", f"{BASE_URL}/services/callbacklogin", f"{IDENTITY_BASE}/wrong"):
            marketing = f"{IDENTITY_BASE}/consent/marketing/test?" + urlencode({"callback": target})
            client, session, _ = self.client(Response(PASSWORD, 302, location=marketing))
            with self.assertRaises(AuthError):
                await client.async_login()
            self.assertEqual(len(session.calls), 4)

    async def test_existing_terms_and_client_consent_paths_resume_at_callback(self):
        for path in ("/signin-service/terms-and-conditions", "/signin-service/consent/users/test"):
            with self.subTest(path=path):
                landing = IDENTITY_BASE + path
                action = f"{IDENTITY_BASE}/signin-service/v1/test-client/terms-and-conditions" if "terms" in path else landing
                client, _, _ = self.client(
                    Response(PASSWORD, 302, location=landing),
                    Response(landing, html=form("") + 'templateModel={"clientLegalEntityModel":{"clientId":"test-client"}};'),
                    Response(action, 302, location=CALLBACK), callback(),
                )
                await client.async_login()
                self.assertTrue(client._logged_in)

    async def test_email_verification_empty_consent_and_bad_credentials_fail(self):
        for path, html in (
            ("/verification/email-sent", ""), ("/signin-service/consent/users/test", ""),
            ("/signin-service/test", 'templateModel={"errorCode":"BadCredentials"};'),
        ):
            landing = IDENTITY_BASE + path
            client, _, _ = self.client(Response(PASSWORD, 302, location=landing), Response(landing, html=html))
            with self.assertRaises(AuthError):
                await client.async_login()
            self.assertFalse(client._logged_in)

    async def test_bad_http_status_at_identity_is_rejected(self):
        for status in (400, 401, 403, 404, 406, 408, 410, 422, 429, 451, 500, 502, 503, 504):
            with self.subTest(status=status):
                client, _, _ = self.client(Response(PASSWORD, status))
                with self.assertRaisesRegex(AuthError, f"HTTP {status}"):
                    await client.async_login()

    async def test_session_check_rejects_all_errors_and_does_not_follow_redirects(self):
        for status in (301, 302, 303, 307, 308, *range(400, 600)):
            with self.subTest(status=status):
                client, session, _ = self.client(Response(PASSWORD, 302, location=CALLBACK), callback())
                session.session_check = Response(SESSION_CHECK, status, location=SIGNIN)
                with self.assertRaisesRegex(AuthError, f"session check returned HTTP {status}"):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertEqual(session.calls[-1][1], SESSION_CHECK)
                self.assertFalse(session.calls[-1][2]["allow_redirects"])

    async def test_cookie_with_invalid_session_json_is_not_a_successful_login(self):
        for payload in (ValueError("html"), aiohttp.ContentTypeError(None, ()),
                        "signin", False, {"error": "expired"}, {"errorCode": "denied"},
                        {"statusCode": 401}, {"status": 401}):
            with self.subTest(payload=type(payload).__name__):
                client, session, _ = self.client(Response(PASSWORD, 302, location=CALLBACK), callback())
                session.session_check = Response(SESSION_CHECK, payload=payload)
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)

    async def test_network_error_does_not_leave_client_logged_in(self):
        client, _, _ = self.client(aiohttp.ClientConnectionError("connection closed"))
        client._logged_in = True
        with self.assertRaises(ApiError):
            await client.async_login()
        self.assertFalse(client._logged_in)

    async def test_product_user_agent_locale_and_brand_specific_authorize(self):
        for brand, prefix in (("AUDI", "cc29b87a"), ("VOLKSWAGEN", "9b58543e"), ("SKODA", "3ea88bf9")):
            client, session, _ = self.client(
                Response(PASSWORD, 302, location=CALLBACK), callback(),
                config=PortalConfig("user@example.com", "test-password", brand=brand),
            )
            await client.async_login()
            query = parse_qs(urlparse(session.calls[0][1]).query)
            self.assertTrue(query["client_id"][0].startswith(prefix))
            state_brand = "VOLKSWAGEN_PASSENGER_CARS" if brand == "VOLKSWAGEN" else brand
            self.assertEqual(query["state"], [f"de__de__{state_brand}"])
            for _, _, kwargs in session.calls:
                self.assertEqual(kwargs["headers"]["User-Agent"], USER_AGENT)
                if "Accept-Language" in kwargs["headers"]:
                    self.assertIn("de-DE", kwargs["headers"]["Accept-Language"])
            self.assertRegex(USER_AGENT, r"^vwgroup-vehicle2mqtt/[0-9.]+$")

    async def test_api_401_reauthenticates_once_and_preserves_api_failures(self):
        url = f"{BASE_URL}{VEHICLES_PATH}"
        client, session, responses = self.client(Response(PASSWORD, 302, location=CALLBACK), callback())
        session.responses.insert(0, Response(url, 401))
        session.responses.append(Response(url, html=json.dumps({"vehicles": []})))
        self.assertEqual(await client._get_json(url), {"vehicles": []})
        self.assertTrue(client._logged_in)
        session.responses = [Response(url, 403), *responses, Response(url, 403)]
        with self.assertRaises(ApiError):
            await client._get_json(url)
