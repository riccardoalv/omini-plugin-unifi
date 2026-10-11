import copy
import json
from functools import partial
from pathlib import Path

import httpx
import pytest
from omini_sdk import Config

import omini_unifi.collect as collect_module
from omini_unifi.client import Client

FIXTURES = Path(__file__).parent / "fixtures"


class FakeController:
    """Answers like a UniFi Network controller, from the JSON files in fixtures/.

    ``flavor`` is "unifi_os" (a console: login at /api/auth/login, API under
    /proxy/network) or "classic" (the self-hosted Network Application)."""

    def __init__(self, flavor: str = "unifi_os"):
        self.flavor = flavor
        self.site = "default"
        self.user, self.password = "omini", "s3cret"
        self.routes = {
            f.stem.replace("__", "/"): json.loads(f.read_text()) for f in FIXTURES.glob("*.json")
        }
        self.forbidden: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.logins = 0
        self.sessions: set[str] = set()
        # Changes: the bodies of PUT rest/device/<id>, as sent.
        self.puts: list[tuple[str, dict]] = []
        self.ignore_changes = False  # answers ok but keeps nothing
        self.switch_lags = False  # keeps the override, the switch never reports it

    @property
    def cookie(self) -> str:
        return "TOKEN" if self.flavor == "unifi_os" else "unifises"

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/":
            if self.flavor == "unifi_os":
                return httpx.Response(200, text="<html>UniFi OS</html>")
            return httpx.Response(302, headers={"location": "/manage"})
        login = "/api/auth/login" if self.flavor == "unifi_os" else "/api/login"
        if path == login and request.method == "POST":
            return self.login(request)
        if path in ("/api/auth/login", "/api/login"):
            return httpx.Response(404)
        prefix = "/proxy/network" if self.flavor == "unifi_os" else ""
        if not path.startswith(prefix + "/api/s/"):
            return httpx.Response(404)
        cookies = dict(
            c.strip().split("=", 1)
            for c in request.headers.get("cookie", "").split(";")
            if "=" in c
        )
        if cookies.get(self.cookie) not in self.sessions:
            if self.flavor == "unifi_os":
                return httpx.Response(401, json={"error": {"code": 401, "message": "Unauthorized"}})
            return httpx.Response(
                401, json={"meta": {"rc": "error", "msg": "api.err.LoginRequired"}, "data": []}
            )
        site, _, endpoint = path[len(prefix + "/api/s/") :].partition("/")
        if site != self.site:
            return httpx.Response(
                400, json={"meta": {"rc": "error", "msg": "api.err.NoSiteContext"}, "data": []}
            )
        if request.method == "PUT":
            return self.put(endpoint, json.loads(request.content), request)
        if endpoint in self.forbidden:
            return httpx.Response(
                403, json={"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
            )
        if endpoint not in self.routes:
            return httpx.Response(
                404, json={"meta": {"rc": "error", "msg": "api.err.NotFound"}, "data": []}
            )
        return httpx.Response(200, json=self.routes[endpoint])

    def device(self, _id: str) -> dict | None:
        return next((d for d in self.routes["stat/device"]["data"] if d.get("_id") == _id), None)

    def put(self, endpoint: str, body: dict, request: httpx.Request) -> httpx.Response:
        """PUT rest/device/<id>: the controller keeps the given fields and
        provisions the switch, which then reports each port's ``enable``."""
        kind, _, _id = endpoint.rpartition("/")
        if kind in self.forbidden or "rest/device" in self.forbidden:
            return httpx.Response(
                403, json={"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
            )
        d = self.device(_id) if kind == "rest/device" else None
        if d is None or request.headers.get("x-csrf-token") is None:
            return httpx.Response(
                400, json={"meta": {"rc": "error", "msg": "api.err.Invalid"}, "data": []}
            )
        self.puts.append((endpoint, copy.deepcopy(body)))
        if not self.ignore_changes:
            d.update(copy.deepcopy(body))
            if not self.switch_lags:
                disabled = {
                    p["_id"]
                    for p in self.routes.get("rest/portconf", {}).get("data", [])
                    if p.get("forward") == "disabled"
                }
                by_idx = {o["port_idx"]: o for o in d.get("port_overrides") or []}
                for p in d.get("port_table") or []:
                    o = by_idx.get(p["port_idx"], {})
                    p["enable"] = (
                        o.get("forward") != "disabled" and o.get("portconf_id") not in disabled
                    )
        return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [d]})

    def login(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (body.get("username"), body.get("password")) != (self.user, self.password):
            if self.flavor == "unifi_os":
                return httpx.Response(
                    401,
                    json={
                        "code": "AUTHENTICATION_FAILED_INVALID_CREDENTIALS",
                        "message": "Invalid username or password",
                    },
                )
            return httpx.Response(
                400, json={"meta": {"rc": "error", "msg": "api.err.Invalid"}, "data": []}
            )
        self.logins += 1
        token = f"session-{self.logins}"
        self.sessions.add(token)
        if self.flavor == "unifi_os":
            return httpx.Response(
                200,
                json={"username": self.user},
                headers={
                    "set-cookie": f"TOKEN={token}; path=/; secure; httponly; samesite=none",
                    "x-csrf-token": "csrf-123",
                },
            )
        return httpx.Response(
            200,
            json={"meta": {"rc": "ok"}, "data": []},
            headers=[
                ("set-cookie", f"unifises={token}; Path=/; Secure; HttpOnly"),
                ("set-cookie", "csrf_token=csrf-456; Path=/; Secure"),
            ],
        )


def _install(monkeypatch, fake):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "POLL_S", 0)
    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def unifi(monkeypatch):
    return _install(monkeypatch, FakeController("unifi_os"))


@pytest.fixture
def classic(monkeypatch):
    return _install(monkeypatch, FakeController("classic"))


@pytest.fixture
def cfg(tmp_path):
    return Config(
        {"url": "https://192.168.1.1", "username": "omini", "password": "s3cret"},
        state_dir=tmp_path / "state",
    )
