import time
from typing import Final
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import WebSocket, WebSocketException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.realtime_endpoints.sideband import (
    authenticate_realtime_sideband,
    bind_realtime_call,
    get_realtime_secret,
    remember_realtime_secret,
)
from litellm.types.realtime import RealtimeUpstreamRoute


@pytest.fixture
def secret_cache() -> UserApiKeyCache:
    return UserApiKeyCache()


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_SALT_KEY", "test-sideband-encryption-key")


async def register_call(
    cache: UserApiKeyCache, token: str = "opaque-minted-token", expires_at: int | None = None
) -> None:
    response: Final = httpx.Response(
        200,
        extensions={
            "litellm_realtime_route": RealtimeUpstreamRoute(
                model="openai/gpt-realtime-2.1",
                api_base="https://api.openai.com/v1",
                api_key="upstream-project-key",
                extra_headers={"OpenAI-Project": "project-a"},
            )
        },
    )
    await remember_realtime_secret(
        token=token,
        model="voice-model",
        expires_at=expires_at or int(time.time()) + 60,
        response=response,
        auth=UserAPIKeyAuth(api_key="hashed-virtual-key", user_id="user-a", team_id="team-a"),
        cache=cache,
    )
    context: Final = await get_realtime_secret(token, cache)
    await bind_realtime_call(
        response=httpx.Response(201, headers={"location": "/v1/realtime/calls/rtc_owned"}),
        context=context,
        cache=cache,
    )


def socket(query: str) -> WebSocket:
    return WebSocket(
        {"type": "websocket", "path": "/v1/realtime", "query_string": query.encode(), "headers": []},
        receive=AsyncMock(),
        send=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_sideband_restores_call_owner_and_exact_upstream(secret_cache: UserApiKeyCache) -> None:
    await register_call(secret_cache)
    websocket: Final = socket("call_id=rtc_owned")
    auth: Final = await authenticate_realtime_sideband(websocket, "opaque-minted-token", secret_cache)
    assert (auth.api_key, auth.user_id, auth.team_id) == ("hashed-virtual-key", "user-a", "team-a")
    assert websocket.state.realtime_sideband.route.api_key == "upstream-project-key"
    assert websocket.state.realtime_sideband.route.extra_headers == {"OpenAI-Project": "project-a"}
    assert websocket.state.realtime_sideband.model == "voice-model"
    stored: Final = await secret_cache.async_get_cache(key="realtime:call:rtc_owned")
    assert isinstance(stored, str)
    assert "upstream-project-key" not in stored
    assert "hashed-virtual-key" not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,token",
    [
        ("call_id=rtc_foreign", "opaque-minted-token"),
        ("call_id=rtc_owned", "different-token"),
        ("call_id=rtc_owned", ""),
        ("call_id=", "opaque-minted-token"),
        ("call_id=../rtc_owned", "opaque-minted-token"),
        ("call_id=rtc_owned&model=another-model", "opaque-minted-token"),
        ("call_id=rtc_owned&intent=transcription", "opaque-minted-token"),
    ],
)
async def test_sideband_rejects_unbound_credentials_and_parameters(
    secret_cache: UserApiKeyCache,
    query: str,
    token: str,
) -> None:
    await register_call(secret_cache)
    with pytest.raises(WebSocketException) as error:
        await authenticate_realtime_sideband(socket(query), token, secret_cache)
    assert error.value.code == 1008


@pytest.mark.asyncio
async def test_sideband_rejects_expired_credential_even_if_cache_retains_it(secret_cache: UserApiKeyCache) -> None:
    await register_call(secret_cache, expires_at=int(time.time()) + 60)
    with patch("litellm.proxy.realtime_endpoints.sideband.time.time", return_value=time.time() + 120):
        with pytest.raises(WebSocketException):
            await authenticate_realtime_sideband(socket("call_id=rtc_owned"), "opaque-minted-token", secret_cache)


@pytest.mark.asyncio
async def test_sideband_fails_closed_after_cache_loss(secret_cache: UserApiKeyCache) -> None:
    await register_call(secret_cache)
    with pytest.raises(WebSocketException):
        await authenticate_realtime_sideband(socket("call_id=rtc_owned"), "opaque-minted-token", UserApiKeyCache())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,location", [(403, "/v1/realtime/calls/rtc_bad"), (201, ""), (201, "/rtc_bad")])
async def test_failed_or_malformed_call_is_never_bound(
    secret_cache: UserApiKeyCache, status: int, location: str
) -> None:
    await register_call(secret_cache)
    context: Final = await get_realtime_secret("opaque-minted-token", secret_cache)
    await bind_realtime_call(
        response=httpx.Response(status, headers={"location": location}), context=context, cache=secret_cache
    )
    with pytest.raises(WebSocketException):
        await authenticate_realtime_sideband(socket("call_id=rtc_bad"), "opaque-minted-token", secret_cache)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["authorization", "api-key", "subprotocol"])
async def test_websocket_auth_accepts_scoped_token_without_proxy_key_lookup(
    secret_cache: UserApiKeyCache, transport: str
) -> None:
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth_websocket

    await register_call(secret_cache)
    headers: Final = {
        "authorization": (b"authorization", b"Bearer opaque-minted-token"),
        "api-key": (b"api-key", b"opaque-minted-token"),
        "subprotocol": (b"sec-websocket-protocol", b"realtime, openai-insecure-api-key.opaque-minted-token"),
    }
    websocket: Final = WebSocket(
        {
            "type": "websocket",
            "scheme": "ws",
            "server": ("testserver", 80),
            "path": "/v1/realtime",
            "query_string": b"call_id=rtc_owned",
            "headers": [headers[transport]],
        },
        receive=AsyncMock(),
        send=AsyncMock(),
    )
    with (
        patch("litellm.proxy.proxy_server.user_api_key_cache", secret_cache),
        patch("litellm.proxy.auth.user_api_key_auth.user_api_key_auth", new_callable=AsyncMock) as normal_auth,
    ):
        auth: Final = await user_api_key_auth_websocket(websocket)
    assert auth.user_id == "user-a"
    normal_auth.assert_not_awaited()


@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer invalid", "Bearer "])
def test_sideband_rejects_bad_auth_with_a_single_websocket_close(authorization: str | None) -> None:
    from litellm.proxy.proxy_server import app

    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(app).websocket_connect(
            "/v1/realtime?call_id=rtc_unknown", headers={"Authorization": authorization} if authorization else {}
        ):
            pytest.fail("Unauthenticated sideband was accepted")
    assert error.value.code == 1008


def test_new_realtime_session_still_requires_model() -> None:
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth_websocket
    from litellm.proxy.proxy_server import app

    app.dependency_overrides[user_api_key_auth_websocket] = lambda: UserAPIKeyAuth()
    try:
        with pytest.raises(WebSocketDisconnect) as error:
            with TestClient(app).websocket_connect("/v1/realtime"):
                pytest.fail("A new session without a model was accepted")
        assert error.value.code == 1008
        assert "model query parameter is required" in error.value.reason
    finally:
        app.dependency_overrides.pop(user_api_key_auth_websocket, None)
