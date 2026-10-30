"""Offline regression coverage for persisted Robinhood OAuth token expiry."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs

from relay.broker import BrokerError, RobinhoodMCP, OAUTH_TOKEN_EXPIRY_FIELD, _OAuthStorage, _oauth_client_provider, is_auth_required


@unittest.skipUnless(importlib.util.find_spec("mcp") and importlib.util.find_spec("httpx"),
                     "Optional Robinhood SDK is not installed")
class RobinhoodOAuthExpiryChecks(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_tokens_with_refresh_token_refresh_before_resource_request(self):
        import httpx
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            storage = _OAuthStorage(path)
            client_info = OAuthClientInformationFull(
                client_id="synthetic-client",
                redirect_uris=["http://127.0.0.1:8766/callback"],
                token_endpoint_auth_method="none",
            )
            await storage.set_client_info(client_info)
            data = storage._read()
            data["tokens"] = {
                "access_token": "synthetic-expired-access",
                "token_type": "Bearer",
                "refresh_token": "synthetic-refresh",
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            path.chmod(0o600)

            seen = []

            async def handle(request):
                seen.append((request.url.path, request.headers.get("Authorization")))
                if request.url.path == "/token":
                    form = parse_qs(request.content.decode("ascii"))
                    self.assertEqual(form["grant_type"], ["refresh_token"])
                    self.assertEqual(form["refresh_token"], ["synthetic-refresh"])
                    return httpx.Response(200, json={
                        "access_token": "synthetic-new-access",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "synthetic-new-refresh",
                    })
                return httpx.Response(200, text="ok")

            provider = _oauth_client_provider(
                server_url="https://agent.robinhood.com/mcp/trading",
                client_metadata=OAuthClientMetadata(
                    client_name="Offline expiry regression",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope="internal",
                ),
                storage=storage,
                redirect_handler=None,
                callback_handler=None,
            )
            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(handle)) as client:
                response = await client.get("https://agent.robinhood.com/mcp/trading")
                second_response = await client.get("https://agent.robinhood.com/mcp/trading")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(second_response.status_code, 200)
            self.assertEqual(seen, [
                ("/token", None),
                ("/mcp/trading", "Bearer synthetic-new-access"),
                ("/mcp/trading", "Bearer synthetic-new-access"),
            ])
            saved = storage._read()
            self.assertEqual(saved["client_info"]["client_id"], "synthetic-client")
            self.assertEqual(saved["tokens"]["access_token"], "synthetic-new-access")
            self.assertGreater(saved[OAUTH_TOKEN_EXPIRY_FIELD], time.time())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

            # A fresh provider process restores the absolute expiry instead of
            # assuming the newly loaded access token is valid indefinitely.
            restarted_storage = _OAuthStorage(path)
            restarted_requests = []

            async def restarted_handle(request):
                restarted_requests.append((request.url.path, request.headers.get("Authorization")))
                return httpx.Response(200, text="ok")

            restarted_provider = _oauth_client_provider(
                server_url="https://agent.robinhood.com/mcp/trading",
                client_metadata=OAuthClientMetadata(
                    client_name="Offline expiry restart regression",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope="internal",
                ),
                storage=restarted_storage,
                redirect_handler=None,
                callback_handler=None,
            )
            async with httpx.AsyncClient(
                auth=restarted_provider,
                transport=httpx.MockTransport(restarted_handle),
            ) as client:
                response = await client.get("https://agent.robinhood.com/mcp/trading")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(restarted_requests, [
                ("/mcp/trading", "Bearer synthetic-new-access"),
            ])

    async def test_rejected_refresh_does_not_send_stale_access_token(self):
        import httpx
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            storage = _OAuthStorage(path)
            await storage.set_client_info(OAuthClientInformationFull(
                client_id="synthetic-client",
                redirect_uris=["http://127.0.0.1:8766/callback"],
                token_endpoint_auth_method="none",
            ))
            data = storage._read()
            data["tokens"] = {
                "access_token": "synthetic-revoked-access",
                "token_type": "Bearer",
                "refresh_token": "synthetic-revoked-refresh",
            }
            data[OAUTH_TOKEN_EXPIRY_FIELD] = 0.0
            path.write_text(json.dumps(data), encoding="utf-8")
            path.chmod(0o600)

            seen = []

            async def handle(request):
                seen.append((request.url.path, request.headers.get("Authorization")))
                if request.url.path == "/token":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(500, text="synthetic upstream unavailable")

            provider = _oauth_client_provider(
                server_url="https://agent.robinhood.com/mcp/trading",
                client_metadata=OAuthClientMetadata(
                    client_name="Offline rejected refresh regression",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope="internal",
                ),
                storage=storage,
                redirect_handler=None,
                callback_handler=None,
            )
            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(handle)) as client:
                response = await client.get("https://agent.robinhood.com/mcp/trading")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(seen, [
                ("/token", None),
                ("/mcp/trading", None),
            ])
            self.assertNotIn("synthetic-revoked-access", repr(seen))

    async def test_unauthorized_after_rejected_refresh_requires_user_authorization(self):
        import httpx
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            storage = _OAuthStorage(path)
            await storage.set_client_info(OAuthClientInformationFull(
                client_id="synthetic-client", redirect_uris=["http://127.0.0.1:8766/callback"],
                token_endpoint_auth_method="none"))
            await storage.set_tokens(OAuthToken(access_token="synthetic-revoked-access",
                token_type="Bearer", refresh_token="synthetic-revoked-refresh", expires_in=0))
            original = path.read_bytes()
            seen = []

            async def handle(request):
                seen.append((request.url.path, request.headers.get("Authorization")))
                if request.url.path == "/token":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                if request.url.path.startswith("/.well-known/oauth-protected-resource"):
                    return httpx.Response(200, json={"resource": "https://agent.robinhood.com/mcp/trading",
                        "authorization_servers": ["https://agent.robinhood.com"]})
                if request.url.path.startswith("/.well-known/oauth-authorization-server"):
                    return httpx.Response(200, json={"issuer": "https://agent.robinhood.com",
                        "authorization_endpoint": "https://robinhood.com/oauth/authorize",
                        "token_endpoint": "https://agent.robinhood.com/token",
                        "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"]})
                return httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})

            async def unused_callback():
                self.fail("A noninteractive broker must not wait for a login callback")

            provider = _oauth_client_provider(server_url="https://agent.robinhood.com/mcp/trading",
                client_metadata=OAuthClientMetadata(client_name="Offline authorization rejection",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                    token_endpoint_auth_method="none", scope="internal"),
                storage=storage, redirect_handler=RobinhoodMCP({})._redirect, callback_handler=unused_callback)
            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(handle)) as client:
                for _ in range(2):
                    with self.assertRaises(BrokerError) as failure:
                        await client.get("https://agent.robinhood.com/mcp/trading")
                    self.assertTrue(is_auth_required(failure.exception))
            self.assertEqual(sum(path == "/token" for path, _ in seen), 2)
            self.assertTrue(all(authorization is None for _, authorization in seen))
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
