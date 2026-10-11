"""Client for the UniFi Network controller's API (the one its web interface uses).

Two flavors of the same API:

- **UniFi OS** consoles (UDM, UDR, UCG, Cloud Key Gen2+, UNVR...): sign in with
  ``POST /api/auth/login`` (a ``TOKEN`` cookie plus an ``x-csrf-token`` header),
  then the Network application answers under ``/proxy/network/api/s/<site>/...``.
- The **self-hosted** UniFi Network Application: ``POST /api/login`` (a
  ``unifises`` cookie plus a ``csrf_token`` one), endpoints under ``/api/s/<site>/...``.

The flavor is detected once (``GET /`` answers 200 on UniFi OS, a redirect to
``/manage`` on the self-hosted application) and the session (flavor, cookies and
CSRF token) is kept in the plugin's state folder, so the controller does not see
a new login on every collection. Collecting sends only GET requests, besides
the login; ``put`` (a device's settings) is called only by ``apply``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from omini_sdk import PluginError, log

UNIFI_OS = "unifi_os"
CLASSIC = "classic"

LOGIN_PATHS = {UNIFI_OS: "/api/auth/login", CLASSIC: "/api/login"}
PREFIXES = {UNIFI_OS: "/proxy/network", CLASSIC: ""}
NAMES = {UNIFI_OS: "UniFi OS", CLASSIC: "UniFi Network Application"}

LOCAL_ACCOUNT = "use a local account (see the plugin's README)"

# What a View Only account is told when it tries a change.
CHANGE_ROLE = (
    "the account can only view: changes need an admin whose role in the Network "
    "application is Full Management or Site Admin, not View Only (see the plugin's README)"
)


class Forbidden(Exception):
    """The account's role cannot read this endpoint."""


class NotFound(Exception):
    """The endpoint does not exist on this controller version."""


class ApiError(Exception):
    """The controller answered with ``meta.rc = "error"``."""


class _LoginRequired(Exception):
    pass


class _WrongFlavor(Exception):
    pass


class Client:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        site: str = "default",
        verify_tls: bool = False,
        state_dir: Path | None = None,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ):
        url = url.strip().rstrip("/")
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        self.base = url
        self.site = site.strip() or "default"
        self.username = username
        self.password = password
        self.state_dir = state_dir
        self.fingerprint = _fingerprint(url, username, password)
        self.http = httpx.Client(
            base_url=url,
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "omini-plugin-unifi"},
            transport=transport,
        )
        self.flavor: str | None = None
        self.csrf: str | None = None
        saved = self._load()
        if saved.get("for") == self.fingerprint and saved.get("flavor") in PREFIXES:
            self.flavor = saved["flavor"]
            self.csrf = saved.get("csrf")
            for name, value in (saved.get("cookies") or {}).items():
                self.http.cookies.set(name, value)
        self.logins = 0

    def close(self) -> None:
        self.http.close()

    @property
    def kind(self) -> str:
        return NAMES.get(self.flavor or "", "UniFi")

    # --- state ------------------------------------------------------------------

    def _state_file(self) -> Path | None:
        return self.state_dir / "session.json" if self.state_dir else None

    def _load(self) -> dict[str, Any]:
        f = self._state_file()
        try:
            return json.loads(f.read_text()) if f and f.exists() else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        f = self._state_file()
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data))
            os.chmod(f, 0o600)
        except OSError:
            log.debug("could not save the session")

    def _save(self) -> None:
        self._write(
            {
                "for": self.fingerprint,
                "flavor": self.flavor,
                "csrf": self.csrf,
                "cookies": {c.name: c.value for c in self.http.cookies.jar},
            }
        )

    # --- login ------------------------------------------------------------------

    # After the controller refuses these credentials, wait before signing in
    # again: UniFi OS locks an account out after repeated failed logins.
    LOGIN_BACKOFF_S = 10 * 60

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self.http.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}: {e}") from e
        except httpx.TimeoutException as e:
            raise PluginError(f"{self.base} did not answer in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed: {e}") from e

    def _detect(self) -> list[str]:
        """Flavors to try, the likely one first."""
        r = self._send("GET", "/", follow_redirects=False)
        return [UNIFI_OS, CLASSIC] if r.status_code == 200 else [CLASSIC, UNIFI_OS]

    def login(self) -> None:
        saved = self._load()
        refused = saved.get("refused") or {}
        if (
            refused.get("for") == self.fingerprint
            and time.time() - refused.get("at", 0) < self.LOGIN_BACKOFF_S
        ):
            raise PluginError(
                f"{refused.get('error')} (waiting a few minutes before signing in again)"
            )
        self.http.cookies.clear()
        self.csrf = None
        flavors = [self.flavor] if self.flavor else self._detect()
        for flavor in flavors:
            try:
                self._login(flavor)
            except _WrongFlavor:
                continue
            except PluginError as e:
                if getattr(e, "refused", False):
                    self._write(
                        {"refused": {"for": self.fingerprint, "at": time.time(), "error": str(e)}}
                    )
                raise
            self.flavor = flavor
            self.logins += 1
            self._save()
            return
        if self.flavor:  # a saved flavor that no longer answers: detect again
            self.flavor = None
            return self.login()
        raise PluginError(f"{self.base} does not look like a UniFi OS console or UniFi Network")

    def _login(self, flavor: str) -> None:
        body = {"username": self.username, "password": self.password, "remember": True}
        headers = {"Content-Type": "application/json"}
        r = self._send("POST", LOGIN_PATHS[flavor], json=body, headers=headers)
        msg = _meta_msg(r)
        if r.status_code in (404, 405) or (r.status_code >= 300 and r.status_code < 400):
            raise _WrongFlavor
        if r.status_code == 429:
            raise PluginError("the controller refused the login: too many attempts, try later")
        mfa = "2fa" in msg.lower() or "mfa" in r.text[:500].lower()
        if r.status_code == 499 or (r.status_code >= 400 and mfa):
            raise _refused(f"the account asks for two-factor authentication: {LOCAL_ACCOUNT}")
        if r.status_code in (400, 401, 403):
            raise _refused(f"the controller rejected the username or password ({LOCAL_ACCOUNT})")
        if r.status_code >= 400:
            raise PluginError(f"the controller answered {r.status_code} to the login")
        if not self.http.cookies.jar:
            # Something else answered the login path with a page.
            raise _WrongFlavor
        if flavor == UNIFI_OS:
            self.csrf = r.headers.get("x-csrf-token") or r.headers.get("x-updated-csrf-token")
        else:
            self.csrf = self.http.cookies.get("csrf_token")

    # --- requests ---------------------------------------------------------------

    def get(self, endpoint: str) -> list[dict[str, Any]]:
        """``GET /api/s/<site>/<endpoint>``: the ``data`` list of the answer,
        signing in when there is no session or the saved one has expired."""
        return self._call("GET", endpoint)

    def put(self, endpoint: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """``PUT /api/s/<site>/<endpoint>`` (a change: only ``apply`` calls it).
        Whether it took effect is checked by reading again, never assumed from
        the answer. An account that may only view gets a PluginError naming the
        role it needs."""
        try:
            return self._call("PUT", endpoint, body)
        except Forbidden as e:
            raise PluginError(CHANGE_ROLE) from e

    def _call(
        self, method: str, endpoint: str, body: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if not self.flavor or not self.http.cookies.jar:
            self.login()
        try:
            return self._request(method, endpoint, body)
        except _LoginRequired:
            self.login()
            try:
                return self._request(method, endpoint, body)
            except _LoginRequired as e:
                raise PluginError("the controller rejected the session right after login") from e

    def _request(
        self, method: str, endpoint: str, body: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        path = f"{PREFIXES[self.flavor or CLASSIC]}/api/s/{quote(self.site, safe='')}/{endpoint}"
        headers = {"x-csrf-token": self.csrf} if self.csrf else {}
        r = self._send(method, path, headers=headers, json=body)
        if r.headers.get("x-updated-csrf-token"):
            self.csrf = r.headers["x-updated-csrf-token"]
        msg = _meta_msg(r)
        if r.status_code == 401 or msg == "api.err.LoginRequired":
            raise _LoginRequired
        if msg == "api.err.NoSiteContext":
            raise PluginError(f'there is no site "{self.site}" on this controller')
        if r.status_code == 403 or msg == "api.err.NoPermission":
            raise Forbidden(endpoint)
        if r.status_code == 404:
            if self.flavor == UNIFI_OS and "stat/device" in endpoint:
                raise PluginError(
                    "the console answered, but not its Network application: is it installed?"
                )
            raise NotFound(endpoint)
        try:
            answer = r.json()
        except ValueError as e:
            raise PluginError(f"{self.base} did not answer {endpoint} with JSON") from e
        if not isinstance(answer, dict):
            raise ApiError(endpoint)
        if (answer.get("meta") or {}).get("rc") == "error":
            raise ApiError(f"{endpoint}: {msg}")
        if r.status_code >= 400:
            raise ApiError(f"{endpoint}: HTTP {r.status_code}")
        data = answer.get("data")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _meta_msg(r: httpx.Response) -> str:
    try:
        meta = r.json().get("meta") or {}
    except (ValueError, AttributeError):
        return ""
    return str(meta.get("msg") or "")


def _refused(message: str) -> PluginError:
    e = PluginError(message)
    e.refused = True  # type: ignore[attr-defined]
    return e


def _fingerprint(url: str, username: str, password: str) -> str:
    return hashlib.sha256(f"{url}\0{username}\0{password}".encode()).hexdigest()
