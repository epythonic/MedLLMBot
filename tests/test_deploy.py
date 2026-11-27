"""Demo server stack: the parts of docker-compose.yml the admin panel's security relies on."""

import re
from pathlib import Path

import pytest
from uvicorn._types import (  # the types of uvicorn's public ASGI middleware API
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendCallable,
    ASGISendEvent,
    HTTPScope,
    Scope,
)
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

COMPOSE = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")

CLOUDFLARED = "172.18.0.5"  # a container on the compose network: cloudflared or Caddy
VISITOR = "203.0.113.7"


def forwarded_allow_ips() -> str:
    """The proxies the admin container trusts with X-Forwarded-For (uvicorn reads FORWARDED_ALLOW_IPS)."""
    match = re.search(r'FORWARDED_ALLOW_IPS: "([^"]*)"', COMPOSE)
    assert match, "FORWARDED_ALLOW_IPS is not set for the admin in docker-compose.yml"
    return match.group(1)


async def seen_by_admin(peer: str, headers: dict[str, str]) -> tuple[str, str]:
    """The client address and scheme the admin panel gets for a request from `peer` with these headers."""
    seen: list[tuple[str, str]] = []

    async def app(scope: Scope, _receive: ASGIReceiveCallable, _send: ASGISendCallable) -> None:
        assert scope["type"] == "http"
        client = scope["client"]
        assert client is not None
        seen.append((client[0], scope["scheme"]))

    async def receive() -> ASGIReceiveEvent:
        raise AssertionError("the request body is not read")

    async def send(_message: ASGISendEvent) -> None:
        raise AssertionError("nothing is sent")

    scope: HTTPScope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": (peer, 40000),
        "server": ("172.18.0.3", 8000),
        "extensions": {},
    }
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips())
    await middleware(scope, receive, send)
    return seen[0]


async def test_spoofed_forwarded_for_through_cloudflare_is_ignored() -> None:
    # Cloudflare appends the visitor's real IP to the X-Forwarded-For the visitor sent. The login throttle counts
    # failures per client address, so a made-up leftmost address would let one visitor try passwords forever
    client, _ = await seen_by_admin(CLOUDFLARED, {"X-Forwarded-For": f"6.6.6.6, {VISITOR}"})

    assert client == VISITOR


async def test_visitor_address_and_https_come_from_the_proxy() -> None:
    assert await seen_by_admin(CLOUDFLARED, {"X-Forwarded-For": VISITOR, "X-Forwarded-Proto": "https"}) == (
        VISITOR,
        "https",
    )


async def test_headers_from_an_outside_address_are_not_trusted() -> None:
    # If port 8000 were ever published to the internet, a visitor still could not pick their own address
    outsider = "198.51.100.9"

    client, scheme = await seen_by_admin(outsider, {"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Proto": "https"})

    assert (client, scheme) == (outsider, "http")


@pytest.mark.parametrize("service", ["cloudflared", "caddy"])
def test_proxies_are_behind_a_profile(service: str) -> None:
    # A plain "docker compose up" on a developer machine starts only the bot and the admin
    block = re.search(rf"^  {service}:\n((?:    .*\n|\n)*)", COMPOSE, re.MULTILINE)
    assert block, f"{service} is missing from docker-compose.yml"
    assert re.search(r'^    profiles: \["\w+"\]$', block.group(1), re.MULTILINE)


def test_tunnel_container_gets_only_its_token() -> None:
    # cloudflared faces the internet; the bot's keys from .env must not be in its environment
    block = re.search(r"^  cloudflared:\n((?:    .*\n|\n)*)", COMPOSE, re.MULTILINE)
    assert block
    assert "env_file" not in block.group(1)
    assert "<<: *app" not in block.group(1)
