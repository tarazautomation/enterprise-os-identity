import httpx
import pytest

from taraz_auth import ServiceTokenError, ServiceTokenProvider


def provider_with(responses, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return responses.pop(0)
    return ServiceTokenProvider.from_issuer(
        "https://auth.test/realms/taraz", "scribe", "s3cret", transport=httpx.MockTransport(handler)
    )


def test_fetches_once_and_caches():
    calls = []
    p = provider_with([httpx.Response(200, json={"access_token": "t1", "expires_in": 300})], calls)
    assert p.get_token() == "t1" and p.get_token() == "t1"
    assert len(calls) == 1
    assert calls[0] == {"grant_type": "client_credentials", "client_id": "scribe", "client_secret": "s3cret"}
    assert p.headers() == {"Authorization": "Bearer t1"}


def test_renews_shortly_before_expiry():
    calls = []
    p = provider_with([httpx.Response(200, json={"access_token": "t1", "expires_in": 20}),
                       httpx.Response(200, json={"access_token": "t2", "expires_in": 300})], calls)
    assert p.get_token() == "t1"
    assert p.get_token() == "t2"          # 20 s left is inside the 30 s margin, so it renews
    assert len(calls) == 2


def test_refused_secret_raises():
    p = provider_with([httpx.Response(401, json={"error": "unauthorized_client"})], [])
    with pytest.raises(ServiceTokenError, match="HTTP 401"):
        p.get_token()


def test_httpx_auth_adds_token_and_retries_once_on_401():
    calls = []
    p = provider_with([httpx.Response(200, json={"access_token": "t1", "expires_in": 300}),
                       httpx.Response(200, json={"access_token": "t2", "expires_in": 300})], calls)
    seen = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200)

    with httpx.Client(auth=p.httpx_auth(), transport=httpx.MockTransport(service)) as http:
        assert http.get("http://knowledge-base/api/x").status_code == 200
    assert seen == ["Bearer t1", "Bearer t2"]


def test_from_env_requires_issuer_or_token_url(monkeypatch):
    monkeypatch.setenv("TARAZ_AUTH_CLIENT_ID", "scribe")
    monkeypatch.setenv("TARAZ_AUTH_CLIENT_SECRET", "x")
    monkeypatch.delenv("TARAZ_AUTH_ISSUER", raising=False)
    monkeypatch.delenv("TARAZ_AUTH_TOKEN_URL", raising=False)
    with pytest.raises(RuntimeError, match="TOKEN_URL or TARAZ_AUTH_ISSUER"):
        ServiceTokenProvider.from_env()
    monkeypatch.setenv("TARAZ_AUTH_ISSUER", "https://auth.test/realms/taraz")
    assert ServiceTokenProvider.from_env().token_url == "https://auth.test/realms/taraz/protocol/openid-connect/token"
