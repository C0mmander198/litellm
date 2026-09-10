import hashlib
import hmac
import re
import time
from typing import Final, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import WebSocket, WebSocketException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper, encrypt_value_helper
from litellm.types.realtime import RealtimeUpstreamRoute

_ROUTE_ADAPTER: Final[TypeAdapter[RealtimeUpstreamRoute | None]] = TypeAdapter(RealtimeUpstreamRoute | None)
_STRING_ADAPTER: Final = TypeAdapter(str)
_SESSION_ADAPTER: Final[TypeAdapter[dict[str, JsonValue] | None]] = TypeAdapter(dict[str, JsonValue] | None)


class SidebandCache(Protocol):
    async def async_get_cache(self, key: str) -> object: ...

    async def async_set_cache(self, key: str, value: object, **kwargs: object) -> object: ...


class RealtimeSidebandContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    token_digest: str
    model: str
    expires_at: int
    route: RealtimeUpstreamRoute
    auth_json: str = Field(repr=False)
    session: dict[str, JsonValue] | None = Field(default=None, repr=False)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def remember_realtime_secret(
    *,
    token: str,
    model: str,
    expires_at: int | None,
    response: httpx.Response,
    auth: UserAPIKeyAuth,
    cache: SidebandCache,
) -> None:
    route: Final = _ROUTE_ADAPTER.validate_python(response.extensions.pop("litellm_realtime_route", None))
    if not isinstance(route, RealtimeUpstreamRoute) or expires_at is None or expires_at <= time.time():
        return
    context: Final = RealtimeSidebandContext(
        token_digest=_token_digest(token),
        model=model,
        expires_at=expires_at,
        route=route,
        auth_json=auth.model_dump_json(exclude={"parent_otel_span"}),
        session=_SESSION_ADAPTER.validate_python(response.extensions.pop("litellm_realtime_session", None)),
    )
    await cache.async_set_cache(
        key=f"realtime:secret:{context.token_digest}",
        value=encrypt_value_helper(context.model_dump_json()),
        ttl=max(1, expires_at - int(time.time())),
    )


async def _load_context(key: str, token: str, cache: SidebandCache) -> RealtimeSidebandContext | None:
    encrypted: Final = await cache.async_get_cache(key=key)
    if not isinstance(encrypted, str):
        return None
    decrypted: Final = decrypt_value_helper(encrypted, key="realtime_sideband", exception_type="debug")
    if not isinstance(decrypted, str):
        return None
    try:
        context: Final = RealtimeSidebandContext.model_validate_json(decrypted)
    except ValidationError:
        return None
    if context.expires_at <= time.time() or not hmac.compare_digest(context.token_digest, _token_digest(token)):
        return None
    return context


async def get_realtime_secret(token: str, cache: SidebandCache) -> RealtimeSidebandContext | None:
    return await _load_context(f"realtime:secret:{_token_digest(token)}", token, cache)


async def bind_realtime_call(
    *, response: httpx.Response, context: RealtimeSidebandContext | None, cache: SidebandCache
) -> None:
    if context is None or response.status_code != 201:
        return
    location: Final = _STRING_ADAPTER.validate_python(response.headers.get("location", ""))
    path: Final = urlsplit(location).path
    call_id: Final = path.rsplit("/", 1)[-1]
    if not path.endswith(f"/realtime/calls/{call_id}") or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", call_id):
        return
    await cache.async_set_cache(
        key=f"realtime:call:{call_id}",
        value=encrypt_value_helper(context.model_dump_json()),
        ttl=max(1, context.expires_at - int(time.time())),
    )


async def authenticate_realtime_sideband(websocket: WebSocket, token: str, cache: SidebandCache) -> UserAPIKeyAuth:
    call_id: Final = websocket.query_params.get("call_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", call_id):
        raise WebSocketException(code=1008, reason="Invalid realtime call_id")
    context: Final = await _load_context(f"realtime:call:{call_id}", token, cache)
    if context is None:
        raise WebSocketException(code=1008, reason="Invalid or expired realtime call credential")
    if websocket.query_params.get("model") not in (None, context.model) or "intent" in websocket.query_params:
        raise WebSocketException(code=1008, reason="Sideband parameters do not match the realtime call")
    websocket.state.realtime_sideband = context
    return UserAPIKeyAuth.model_validate_json(context.auth_json)


def sideband_token(websocket: WebSocket) -> str:
    authorization: Final = websocket.headers.get("authorization")
    if authorization is not None:
        if not authorization.startswith("Bearer "):
            raise WebSocketException(code=1008, reason="Invalid realtime Authorization header")
        return authorization.removeprefix("Bearer ").strip()
    header_key: Final = websocket.headers.get("api-key")
    if header_key:
        return header_key
    return next(
        (
            protocol.strip().removeprefix("openai-insecure-api-key.")
            for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if protocol.strip().startswith("openai-insecure-api-key.")
        ),
        "",
    )
