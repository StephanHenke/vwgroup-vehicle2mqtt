"""Synthetic login-to-download regressions; no account data or external requests."""

import io
import json
import unittest
import zipfile
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from vw_euda_mqtt.api import (
    ApiError, AuthError, BASE_URL, IDENTITY_BASE, VEHICLES_PATH, USER_AGENT,
    DOWNLOAD_PATH, LIST_PATH, METADATA_PATH, RELATION_PATH, EudaApiClient, PortalConfig,
)

CALLBACK = f"{BASE_URL}/services/callbacklogin?code=secret"
AUTHORIZE = f"{IDENTITY_BASE}/oidc/v1/authorize"
IDENTIFIER = f"{IDENTITY_BASE}/identifier"
PASSWORD = f"{IDENTITY_BASE}/password"
DISCOVERY = f"{BASE_URL}/services/redirect/authentication?brand=AUDI&method=login"
LOGIN = f"{BASE_URL}/login?{urlencode({'redirect': AUTHORIZE})}"
PROBE = f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT"
LANDING = f"{BASE_URL}/de/de/user.html"
FORM = '<form action="{}"><input name="hmac" value="test"><input name="_csrf" value="test"></form>'
UNSET = object()


class Response:
    def __init__(self, status=200, *, html="", location=None, payload=UNSET, cookie=None, raw=None):
        self.status = status
        self.html = html
        self.headers = {} if location is None else {"Location": location}
        self.payload = [] if payload is UNSET else payload
        self.cookies = SimpleCookie()
        if cookie is not None:
            self.cookies.load(cookie)
        self.raw = raw

    async def __aenter__(self):
        self.jar.update_cookies(self.cookies, URL(self.url))
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return self.html if self.html else json.dumps(self.payload)

    async def read(self):
        return self.raw if self.raw is not None else (await self.text()).encode()

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def callback_response(**kwargs):
    return Response(302, location=LANDING, cookie="access_token=fresh; Path=/; Secure", **kwargs)


def login_steps(*, password=None, callback=None, probe=None):
    return [
        ("GET", DISCOVERY, Response(302, location=LOGIN)),
        ("GET", LOGIN, Response()),
        ("GET", AUTHORIZE, Response(html=FORM.format('/identifier'))),
        ("POST", IDENTIFIER, Response(html=FORM.format('/password'))),
        ("POST", PASSWORD, password or Response(302, location=CALLBACK)),
        ("GET", CALLBACK, callback or callback_response()),
        ("GET", PROBE, probe or Response()),
    ]


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def client(self, steps):
        session = Mock()
        session.cookie_jar = aiohttp.CookieJar()
        self.requests = []
        self.remaining = list(steps)

        def request(method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            self.assertTrue(self.remaining, f"Unexpected request {method} {url}")
            expected_method, expected_url, response = self.remaining.pop(0)
            self.assertEqual((method, str(url)), (expected_method, expected_url))
            if isinstance(response, Exception):
                raise response
            response.url, response.jar = str(url), session.cookie_jar
            return response

        session.get = AsyncMock(side_effect=lambda url, **kw: request("GET", url, **kw))
        session.post = Mock(side_effect=lambda url, **kw: request("POST", url, **kw))
        return EudaApiClient(session, PortalConfig("user@example.com", "test-password"))

    async def test_login_never_fetches_404_or_406_landing_page(self):
        for status in (404, 406):
            for path in ("/de/de/user.html", "/content/euda/se/en/user.html"):
                with self.subTest(status=status, path=path):
                    callback = callback_response()
                    callback.headers['Location'] = BASE_URL + path
                    steps = login_steps(callback=callback)
                    steps.append(("GET", BASE_URL + path, Response(status)))
                    client = self.client(steps)
                    with self.assertLogs("vw_euda_mqtt.api", level="DEBUG") as logs:
                        await client.async_login()
                    self.assertTrue(client._logged_in)
                    self.assertEqual(len(self.remaining), 1)
                    self.assertNotIn("secret", '\n'.join(logs.output))
                    self.assertFalse(any(url == BASE_URL + '/' for _, url, _ in self.requests))
                    self.assertTrue(all(not kw['allow_redirects'] for _, _, kw in self.requests))
                    self.assertTrue(all(kw['headers']['User-Agent'] == USER_AGENT for _, _, kw in self.requests))
                    self.assertNotIn('Mozilla', USER_AGENT)

    async def test_missing_empty_expired_or_wrong_path_callback_cookie_fails(self):
        for cookie in (None, 'access_token=; Path=/', 'access_token=new; Max-Age=0; Path=/',
                       'access_token=new; Path=/other', 'access_token=new; Domain=example.org; Path=/'):
            with self.subTest(cookie=cookie):
                client = self.client(login_steps(callback=Response(302, location=LANDING, cookie=cookie)))
                client._session.cookie_jar.update_cookies({'access_token': 'old'}, URL(BASE_URL))
                client._logged_in = True
                with self.assertRaisesRegex(AuthError, 'usable access_token'):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertFalse(any(url == PROBE for _, url, _ in self.requests))

    async def test_callback_http_errors_are_not_accepted(self):
        for status in (400, 401, 403, 404, 406, 429, 500):
            with self.subTest(status=status):
                client = self.client(login_steps(callback=Response(status, cookie='access_token=new; Path=/')))
                with self.assertRaises(ApiError):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertFalse(any(url == PROBE for _, url, _ in self.requests))

    async def test_probe_requires_successful_json_response(self):
        for payload in (ValueError('invalid'), aiohttp.ContentTypeError(None, ()), None, False, 42, 'login',
                        {'error': 'consent_required'}, {'errorCode': 'denied'}, {'statusCode': 401}):
            with self.subTest(payload=repr(payload)):
                client = self.client(login_steps(probe=Response(payload=payload)))
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertFalse(self.remaining)
        for payload in ([], {'vehicles': []}):
            client = self.client(login_steps(probe=Response(payload=payload)))
            await client.async_login()
            self.assertTrue(client._logged_in)

    async def test_probe_errors_and_outages_do_not_trigger_recursive_login(self):
        for status in (302, 401, 403, 404, 406, 429, 500):
            with self.subTest(status=status):
                client = self.client(login_steps(probe=Response(status)))
                with self.assertRaises(ApiError) as error:
                    await client.async_login()
                self.assertEqual(isinstance(error.exception, AuthError), status not in (429, 500))
                self.assertFalse(client._logged_in)
                self.assertFalse(self.remaining)

    async def test_untrusted_redirects_and_portal_pages_fail_before_request(self):
        for target in ('http://identity.vwgroup.io/password', 'https://example.org/callback',
                       'https://identity.vwgroup.io@example.org/password', LANDING,
                       CALLBACK.replace('/callbacklogin?', '/callbacklogin/extra?')):
            with self.subTest(target=target):
                steps = login_steps(password=Response(302, location=target))[:5]
                # An allowed portal URL with the wrong callback path is fetched but cannot prove login.
                if '/extra?' in target:
                    steps.append(('GET', target, Response()))
                client = self.client(steps)
                with self.assertRaises(AuthError):
                    await client.async_login()
                self.assertFalse(client._logged_in)
                self.assertFalse(self.remaining)

    async def test_form_actions_cannot_send_credentials_to_portal_or_other_hosts(self):
        for target in (BASE_URL + '/login', 'https://example.org/post', 'http://identity.vwgroup.io/post'):
            for form_index in (2, 3):
                with self.subTest(target=target, form_index=form_index):
                    steps = login_steps()[:form_index + 1]
                    method, url, _ = steps[-1]
                    steps[-1] = (method, url, Response(html=FORM.format(target)))
                    client = self.client(steps)
                    with self.assertRaises(AuthError):
                        await client.async_login()
                    self.assertFalse(self.remaining)

    async def test_redirect_method_semantics_and_limit(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                target = IDENTITY_BASE + '/continue'
                method = 'POST' if status in (307, 308) else 'GET'
                steps = login_steps(password=Response(status, location='/continue'))
                steps.insert(5, (method, target, Response(302, location=CALLBACK)))
                client = self.client(steps)
                await client.async_login()
                forwarded = self.requests[5][2]
                self.assertEqual('data' in forwarded, method == 'POST')
                if method == 'POST':
                    self.assertEqual(forwarded['data']['password'], 'test-password')
        for status in (307, 308):
            client = self.client(login_steps(password=Response(status, location=CALLBACK))[:5])
            with self.assertRaises(AuthError):
                await client.async_login()
        loop = IDENTITY_BASE + '/loop'
        client = self.client([('GET', loop, Response(302, location=loop)) for _ in range(11)])
        with self.assertRaisesRegex(AuthError, 'exceeded 10'):
            await client._login_request('GET', loop)

    async def test_missing_location_is_rejected(self):
        client = self.client(login_steps(password=Response(302))[:5])
        with self.assertRaisesRegex(AuthError, 'Location'):
            await client.async_login()

    async def test_credentials_consent_and_verification_failures_remain_explicit(self):
        for status in (200, 401, 403, 404, 406):
            client = self.client(login_steps(password=Response(
                status, html='templateModel = {"errorCode":"BadCredentials"};',
            ))[:5])
            with self.assertRaisesRegex(AuthError, 'BadCredentials'):
                await client.async_login()
        for path, message in (('/verification/email-sent', 'Email verification'),
                              ('/signin-service/consent/users/test', 'Required client consent')):
            target = IDENTITY_BASE + path
            steps = login_steps(password=Response(302, location=target))[:5]
            client = self.client(steps + [('GET', target, Response())])
            with self.assertRaisesRegex(AuthError, message):
                await client.async_login()

    async def test_terms_and_consent_continue_through_callback(self):
        for path in ('/signin-service/test/terms-and-conditions', '/signin-service/consent/users/test'):
            target = IDENTITY_BASE + path
            html = FORM.format('')
            if 'terms' in path:
                html += 'templateModel = {"clientLegalEntityModel":{"clientId":"test"}};'
                post = IDENTITY_BASE + '/signin-service/v1/test/terms-and-conditions'
            else:
                post = target
            steps = login_steps(password=Response(302, location=target))
            steps[5:5] = [('GET', target, Response(html=html)), ('POST', post, Response(302, location=CALLBACK))]
            client = self.client(steps)
            await client.async_login()
            self.assertTrue(client._logged_in)
            self.assertFalse(self.remaining)

    async def test_marketing_uses_only_validated_identity_callback(self):
        target = IDENTITY_BASE + '/oidc/v1/oauth/client/callback?scopes=openid+cars'
        marketing = IDENTITY_BASE + '/signin-service/consent/marketing/test?' + urlencode({'callback': target})
        steps = login_steps(password=Response(302, location=marketing))
        steps[5:5] = [('GET', marketing, Response()), ('GET', target, Response(302, location=CALLBACK))]
        client = self.client(steps)
        await client.async_login()
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.requests), 2)
        for bad in ('https://example.org/oidc/v1/oauth/client/callback', IDENTITY_BASE + '/unexpected'):
            marketing = IDENTITY_BASE + '/signin-service/consent/marketing/test?' + urlencode({'callback': bad})
            client = self.client(login_steps(password=Response(302, location=marketing))[:5]
                                 + [('GET', marketing, Response())])
            with self.assertRaises(AuthError):
                await client.async_login()

    async def test_network_error_is_sanitized_and_does_not_mark_login_successful(self):
        for error in (aiohttp.ClientConnectionError('secret-url'), TimeoutError('secret-url')):
            steps = login_steps()
            steps[-1] = ('GET', PROBE, error)
            client = self.client(steps)
            with self.assertRaises(ApiError) as caught:
                await client.async_login()
            self.assertNotIn('secret-url', str(caught.exception))
            self.assertFalse(client._logged_in)

    def zip_bytes(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('data.json', '{"data": [{"value": 52}]}')
            archive.writestr('report.xml', '<vehicle><soc>52</soc></vehicle>')
        return data.getvalue()

    async def test_login_vehicles_metadata_listing_and_download(self):
        vin, identifier, name = 'TESTVIN1234567890', 'dataset-id', 'dataset.zip'
        raw = self.zip_bytes()
        steps = login_steps() + [
            ('GET', PROBE, Response(payload={'vehicles': [{'vin': vin}]})),
            ('GET', BASE_URL + RELATION_PATH.format(vin=vin), Response(payload={'relation': {}})),
            ('GET', BASE_URL + METADATA_PATH.format(vin=vin), Response(payload={'identifier': identifier})),
            ('GET', BASE_URL + LIST_PATH.format(vin=vin, identifier=identifier), Response(payload={'files': [name]})),
            ('GET', BASE_URL + DOWNLOAD_PATH.format(vin=vin, identifier=identifier), Response(raw=raw)),
        ]
        client = self.client(steps)
        self.assertEqual((await client.async_list_vehicles())[0]['vin'], vin)
        self.assertEqual((await client.async_get_metadata(vin))['identifier'], identifier)
        self.assertEqual(await client.async_list_datasets(vin, identifier), [name])
        result = await client.async_download_dataset(vin, identifier, name)
        self.assertEqual(result.raw, raw)
        self.assertEqual(result.payload, {'data': [{'value': 52}]})
        self.assertEqual([file.name for file in result.files], ['data.json', 'report.xml'])
        self.assertEqual(self.requests[-1][2]['headers']['filename'], name)
        self.assertFalse(self.remaining)

    async def test_expired_session_download_reauthenticates_once(self):
        url = BASE_URL + DOWNLOAD_PATH.format(vin='vin', identifier='id')
        for final_status in (200, 401, 403):
            client = self.client([('GET', url, Response(401))] + login_steps()
                                 + [('GET', url, Response(final_status, raw=self.zip_bytes()))])
            client._logged_in = True
            if final_status == 200:
                result = await client.async_download_dataset('vin', 'id', 'dataset.zip')
                self.assertEqual(result.payload['data'][0]['value'], 52)
            else:
                with self.assertRaises(ApiError):
                    await client.async_download_dataset('vin', 'id', 'dataset.zip')
                self.assertFalse(client._logged_in)
            self.assertFalse(self.remaining)

    async def test_legacy_marketing_skip_and_repeated_consent(self):
        target = IDENTITY_BASE + '/signin-service/consent/marketing/test'
        skip = IDENTITY_BASE + '/signin-service/v1/consent/marketing/u/c/s/skip'
        html = 'templateModel = {"userId":"u","clientId":"c","step":"s"};'
        steps = login_steps(password=Response(302, location=target))
        steps[5:5] = [('GET', target, Response(html=html)), ('POST', skip, Response(302, location=CALLBACK))]
        client = self.client(steps)
        await client.async_login()
        self.assertFalse(self.remaining)
        target = IDENTITY_BASE + '/signin-service/consent/users/test'
        client = self.client(login_steps(password=Response(302, location=target))[:5] + [
            ('GET', target, Response(html=FORM.format(''))),
            ('POST', target, Response(html=FORM.format(''))),
        ])
        with self.assertRaisesRegex(AuthError, 'still requires client consent'):
            await client.async_login()
        self.assertFalse(self.remaining)

    async def test_supported_callback_statuses_with_verified_session(self):
        for status in (200, 301, 302, 303, 307, 308):
            with self.subTest(status=status):
                callback = callback_response()
                callback.status = status
                client = self.client(login_steps(callback=callback))
                await client.async_login()
                self.assertTrue(client._logged_in)
                self.assertFalse(self.remaining)

    async def test_brand_discovery_fallback_and_untrusted_authorize_url(self):
        client = self.client([('GET', DISCOVERY, aiohttp.ClientConnectionError('offline'))])
        self.assertEqual(await client._get_authorize_url(), client._build_authorize_url())
        bad = BASE_URL + '/login?' + urlencode({'redirect': 'https://example.org/authorize'})
        client = self.client([('GET', DISCOVERY, Response(302, location=bad)), ('GET', bad, Response())])
        with self.assertRaises(AuthError):
            await client._get_authorize_url()

    async def test_unavailable_brand_discovery_preserves_authorize_fallback(self):
        for status in (404, 406, 429, 500):
            client = self.client([('GET', DISCOVERY, Response(status))])
            self.assertEqual(await client._get_authorize_url(), client._build_authorize_url())
