from __future__ import annotations

import socket
import urllib.request

import pytest


def test_non_loopback_connect_still_raises():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is not allowed"):
            sock.connect(("93.184.216.34", 80))
        with pytest.raises(RuntimeError):
            sock.connect_ex(("203.0.113.5", 443))
    finally:
        sock.close()


def test_create_connection_and_dns_to_external_host_raise():
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("example.com", 80)


def test_loopback_request_to_local_site_succeeds(local_site):
    local_site.add("/ping", "pong", content_type="text/plain")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(local_site.url("/ping"), timeout=5) as resp:
        assert resp.read() == b"pong"
    assert local_site.hits("/ping") == 1
