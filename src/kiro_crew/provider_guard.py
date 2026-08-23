"""Provider configuration validation, safe-mode endpoint gating, and the
connectivity probe behind ``kirocrew doctor``.

Three concerns, one module:

* :func:`validate_provider_settings` — pure config inspection. Returns human
  readable problems (empty model on a router path, missing key, plaintext key
  while a keyring exists). Never raises; the caller decides severity.
* :func:`assert_endpoint_allowed` — the ``agent.safe_mode`` guardrail. When
  safe mode is on, the resolved endpoint must be loopback or private/RFC1918
  (plus Tailscale CGNAT and link-local), so a typo can never point the agent
  at a random public router. DNS resolution happens here; callers must keep
  this OFF the event loop.
* :func:`probe_provider_endpoint` — one tiny Anthropic-shaped request used by
  doctor to report reachability + auth verdict + latency.

Blocking I/O lives in the last two functions only; both are documented as
off-loop calls.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 6.0


def _host_is_private(host: str) -> bool:
    """True for loopback, RFC1918, link-local, CGNAT (Tailscale), ULA."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP literal — resolve first
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr in ipaddress.ip_network("100.64.0.0/10")
    )


def classify_endpoint(url: str) -> tuple[str, str]:
    """Split *url* into ``(host, port)`` with defaults applied. Raises
    ValueError on a URL that has no host at all."""
    parsed = urlparse(url if "//" in url else f"http://{url}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"provider_base_url has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def endpoint_is_local(url: str) -> bool | None:
    """Best-effort safe-mode verdict for *url*: True (local/private), False
    (public), None (could not resolve — treat as FAIL-CLOSED upstream)."""
    host, _port = classify_endpoint(url)
    if _host_is_private(host):
        return True
    if host in ("localhost", "*.localhost", "*.local"):
        return True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.warning("safe_mode: cannot resolve %s (%s)", host, exc)
        return None
    for info in infos:
        addr = info[4][0]
        # A single public A/AAAA record makes the HOST public: fail closed.
        if not _host_is_private(addr):
            return False
    return True


def assert_endpoint_allowed(url: str, *, safe_mode: bool) -> None:
    """Raise ValueError when *safe_mode* is on and *url* is not local/private.

    OFF the event loop: resolves DNS synchronously. Called from provider
    factory construction and the doctor command only.
    """
    if not safe_mode:
        return
    verdict = endpoint_is_local(url)
    if verdict is True:
        return
    reason = (
        "resolves to a PUBLIC address"
        if verdict is False
        else "could not be resolved (fail-closed)"
    )
    raise ValueError(
        f"agent.safe_mode is ON and provider_base_url {url!r} {reason}. "
        "Only loopback / RFC1918 / Tailscale (100.64.0.0/10) endpoints are "
        "permitted. Turn agent.safe_mode off to allow public routers."
    )


def validate_provider_settings(agent) -> list[str]:  # noqa: ANN001 - AgentConfig
    """Human-readable problems with the fork's router settings, worst first.

    Pure function over the config object: no network, no imports beyond stdlib.
    ``doctor`` prints these; the provider factory logs them once per boot.
    """
    problems: list[str] = []
    provider = (agent.provider or "").strip()

    if provider == "claude_code":
        base_url = (agent.provider_base_url or "").strip()
        model = (agent.model or "").strip()
        api_key = (agent.provider_api_key or "").strip()
        if not base_url and not api_key:
            problems.append(
                "provider=claude_code with neither provider_base_url nor an API "
                "key: requests will go to api.anthropic.com unauthenticated and "
                "fail with 401."
            )
        if base_url and model in ("", "auto"):
            problems.append(
                "provider=claude_code with a custom router but model='auto': "
                "the router rejects Bedrock-style auto resolution — set a real "
                "model id your router serves."
            )
        if api_key:
            # Imported lazily: secrets.py must stay import-light.
            from kiro_crew.provider_secrets import describe_key_source

            if describe_key_source(api_key).startswith("config.json"):
                problems.append(
                    "provider API key stored plaintext in config.json. Migrate "
                    "to the OS keyring or KIROCREW_PROVIDER_API_KEY."
                )
    elif provider == "opencode":
        if not (agent.provider_base_url or "").strip():
            problems.append(
                "provider=opencode without provider_base_url: the OpenCode ACP "
                "backend will fall back to its own default catalog."
            )

    if getattr(agent, "safe_mode", False):
        base_url = (agent.provider_base_url or "").strip()
        if base_url:
            try:
                classify_endpoint(base_url)
            except ValueError as exc:
                problems.append(f"safe_mode cannot parse provider_base_url: {exc}")

    return problems


def probe_provider_endpoint(base_url: str, api_key: str) -> dict[str, object]:
    """One tiny Anthropic-shaped POST against *base_url*.

    Returns ``{ok, status, latency_ms, verdict}``. BLOCKING (urllib, no deps);
    doctor runs it in a worker thread. A 400 from the backend still means
    reachable+authenticated — the payload is intentionally minimal garbage.
    """
    import json as _json
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/v1/messages"
    body = _json.dumps(
        {
            "model": "probe",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key or "",
            "authorization": f"Bearer {api_key}" if api_key else "",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {
            "ok": False,
            "status": None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "verdict": f"unreachable: {exc}",
        }
    latency = int((time.monotonic() - started) * 1000)

    if status in (200, 400):
        verdict = "reachable + authenticated" if status == 200 else (
            "reachable + authenticated (probe payload rejected as expected)"
        )
        ok = True
    elif status in (401, 403):
        verdict = "reachable but AUTH FAILED — check the API key"
        ok = False
    elif status == 404:
        verdict = "reachable but /v1/messages not found — wrong base URL path?"
        ok = False
    else:
        verdict = f"reachable, unexpected status {status}"
        ok = status < 500
    return {"ok": ok, "status": status, "latency_ms": latency, "verdict": verdict}
