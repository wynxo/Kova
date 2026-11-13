"""ChatGPT PKCE login and explicitly consented, read-only local harness reuse.

Owned OAuth tokens live only in the 0600 config. Borrowed harness tokens are
read afresh, never copied, refreshed, revoked or written back by Kova.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from filelock import FileLock

from kova.config import ProviderEntry, load_config, save_config
from kova.providers import ProviderError

ISSUER = 'https://auth.openai.com'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'


def codex_auth_path() -> Path:
    # Only the standard, local file. No network discovery or arbitrary uploads.
    return Path.home() / '.codex' / 'auth.json'


def detect_harness() -> bool:
    return codex_auth_path().is_file()


def reuse_codex(provider: ProviderEntry, *, consent: bool) -> None:
    if not consent:
        raise ProviderError('Reusing an installed login requires explicit consent.')
    provider.auth = {'source': 'codex'}
    credentials(provider)


def _expiry(token: str) -> float:
    try:
        # Expiry is only a refresh hint, never proof of identity or authorization.
        raw = token.split('.')[1]
        return float(json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))).get('exp', 0))
    except (ValueError, IndexError, TypeError):
        return 0


def credentials(provider: ProviderEntry, *, transport=None) -> dict:
    if provider.auth.get('source') == 'codex':
        try:
            data = json.loads(codex_auth_path().read_text())
            tokens = data.get('tokens') or {}
            token = tokens.get('access_token')
            if not isinstance(token, str) or not token:
                raise ValueError()
            expiry = _expiry(token)
            if expiry and expiry <= time.time() + 30:
                raise ProviderError('Installed login has expired. Sign in again with codex login, then retry.')
            return {'access_token': token, 'account_id': tokens.get('account_id', '')}
        except (OSError, ValueError, AttributeError):
            raise ProviderError('No reusable ChatGPT login in ~/.codex/auth.json. Run codex login first.') from None
    if not provider.auth.get('access_token'):
        if provider.kind in ('chatgpt', 'codex'):
            raise ProviderError('Provider is signed out. Please sign in again.')
        return {}
    if float(provider.auth.get('expires_at', 0)) > time.time() + 60:
        return provider.auth
    if not provider._home:
        raise ProviderError('Sign in again to renew the provider session.')
    with FileLock(str(provider._home / '.provider-auth.lock'), timeout=30):
        cfg = load_config(provider._home)
        saved = next((p for p in cfg.providers if p.id == provider.id), None)
        if saved is None or not saved.auth.get('refresh_token'):
            raise ProviderError('Provider is signed out. Please sign in again.')
        if float(saved.auth.get('expires_at', 0)) <= time.time() + 60:
            updated = _exchange({'grant_type': 'refresh_token', 'client_id': saved.auth['client_id'],
                                 'refresh_token': saved.auth['refresh_token'], 'resource': RESOURCE}, transport)
            saved.auth.update(updated)
            save_config(cfg)
        provider.auth = dict(saved.auth)
    return provider.auth


def _exchange(payload: dict, transport=None) -> dict:
    try:
        with httpx.Client(timeout=20, transport=transport, trust_env=False) as client:
            response = client.post(TOKEN_URL, data=payload)
        if response.status_code != 200:
            raise ProviderError('Could not renew or complete sign-in. Please sign in again.')
        result = response.json()
        if not result.get('access_token'):
            raise ValueError()
        result['expires_at'] = time.time() + float(result.get('expires_in', 3600))
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        raise ProviderError('Could not complete sign-in. Check the connection and try again.') from None


class LoginAttempt:
    """One bounded loopback callback, opened before returning a browser URL."""

    def __init__(self, provider: ProviderEntry):
        self.provider = provider
        self.state, self.nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.result: dict | None = None
        self.error = ''
        self.done = threading.Event()
        self.deadline = time.monotonic() + 300
        attempt = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # callbacks carry authorization codes, never log URLs

            def do_GET(self):
                parsed = urlsplit(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path != '/auth/callback' or not secrets.compare_digest(params.get('state', ''), attempt.state):
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'Invalid sign-in callback.')
                    return
                try:
                    attempt.complete(params)
                    message = b'Signed in. You can return to Kova.'
                except Exception:
                    attempt.error = 'Sign-in was declined or could not be verified. Please retry.'
                    message = b'Sign-in could not be completed. Return to Kova and retry.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(message)
                attempt.done.set()

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.timeout = .5
        self.redirect_uri = f'http://127.0.0.1:{self.server.server_port}/auth/callback'
        previous = provider.auth
        self.client_id = previous.get('client_id', 'dynamic_agent_client')
        self.host_id = previous.get('ext_agent_host_id', 'urn:uuid:' + str(uuid.uuid4()))
        params = {'client_id': self.client_id, 'ext_agent_host_id': self.host_id, 'response_type': 'code',
                  'redirect_uri': self.redirect_uri, 'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
                  'resource': RESOURCE, 'state': self.state, 'nonce': self.nonce,
                  'code_challenge_method': 'S256',
                  'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')}
        if self.client_id == 'dynamic_agent_client':
            params['agent_name_hint'] = 'Kova'
        self.url = ISSUER + '/api/accounts/authorize?' + urlencode(params)
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        try:
            while not self.done.is_set() and time.monotonic() < self.deadline:
                self.server.handle_request()
        finally:
            self.server.server_close()
            if not self.done.is_set():
                self.error = 'Sign-in timed out. Please retry.'
                self.done.set()

    def cancel(self):
        self.done.set()

    def complete(self, params: dict):
        if params.get('error') or not params.get('code'):
            raise ProviderError('Sign-in declined.')
        client_id = params.get('client_id', self.client_id)
        if client_id == 'dynamic_agent_client' or (self.client_id != 'dynamic_agent_client' and client_id != self.client_id):
            raise ProviderError('Sign-in registration did not match.')
        tokens = _exchange({'grant_type': 'authorization_code', 'client_id': client_id, 'code': params['code'],
                            'code_verifier': self.verifier, 'redirect_uri': self.redirect_uri, 'resource': RESOURCE})
        # Validate signature, issuer, audience, expiry and nonce before saving any token.
        with httpx.Client(timeout=10, trust_env=False) as client:
            discovery = client.get(ISSUER + '/.well-known/openid-configuration')
            discovery.raise_for_status()
            jwks_uri = discovery.json()['jwks_uri']
            if not jwks_uri.startswith(ISSUER + '/'):
                raise ProviderError('Unexpected sign-in verification endpoint.')
            jwks = client.get(jwks_uri)
            jwks.raise_for_status()
        keyset = jwt.PyJWKSet.from_dict(jwks.json())
        header = jwt.get_unverified_header(tokens['id_token'])
        key = next(k for k in keyset.keys if k.key_id == header.get('kid'))
        claims = jwt.decode(tokens['id_token'], key.key, algorithms=['RS256'], audience=client_id, issuer=ISSUER,
                            options={'require': ['exp', 'sub', 'nonce', 'iss', 'aud']})
        if not secrets.compare_digest(claims['nonce'], self.nonce) or 'chatgpt.tokens.use.direct' not in tokens.get('scope', '').split():
            raise ProviderError('ChatGPT plan access was not granted.')
        if self.provider.auth.get('subject') and claims['sub'] != self.provider.auth['subject']:
            raise ProviderError('Signed in to a different account. Add a separate provider instead.')
        self.result = {**tokens, 'client_id': client_id, 'ext_agent_host_id': self.host_id,
                       'subject': claims['sub'], 'email': claims.get('email', '')}


def login(provider: ProviderEntry) -> None:
    try:
        attempt = LoginAttempt(provider)
    except OSError:
        raise ProviderError('Cannot open the local sign-in callback. Check loopback access, then retry kova setup.') from None
    # No tokens or authorization-code URLs printed.
    try:
        try:
            opened = webbrowser.open(attempt.url)
        except webbrowser.Error:
            opened = False
        if not opened:
            raise ProviderError('Could not open a browser. Configure a default browser, then retry kova setup.')
        if not attempt.done.wait(300) or attempt.error or not attempt.result:
            raise ProviderError(attempt.error or 'Sign-in timed out. Please retry.')
        provider.auth = attempt.result
    finally:
        attempt.cancel()


def logout(provider: ProviderEntry) -> bool:
    """Forget local access; borrowed sessions must never be revoked."""
    confirmed = True
    if provider.kind == 'chatgpt' and provider.auth.get('refresh_token'):
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                discovery = client.get(ISSUER + '/.well-known/openid-configuration')
                endpoint = discovery.json()['revocation_endpoint']
                if not endpoint.startswith(ISSUER + '/'):
                    raise ValueError()
                response = client.post(endpoint, data={'token': provider.auth['refresh_token'],
                    'token_type_hint': 'refresh_token', 'client_id': provider.auth['client_id']})
                confirmed = response.status_code == 200
        except (httpx.HTTPError, ValueError, KeyError):
            confirmed = False
    provider.auth = {k: v for k, v in provider.auth.items()
                     if k in ('client_id', 'ext_agent_host_id', 'subject', 'email')}
    provider.api_key = None
    provider.headers.clear()
    return confirmed
