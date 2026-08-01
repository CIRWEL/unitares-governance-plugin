"""Tests for governance REST authentication headers."""

from __future__ import annotations

import ast
import sys
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _http_auth import (  # noqa: E402
    _AuthorizationSafeRedirectHandler,
    governance_json_headers,
)


def test_json_headers_omit_authorization_without_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)

    assert governance_json_headers() == {"Content-Type": "application/json"}

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "   ")
    assert governance_json_headers() == {"Content-Type": "application/json"}


def test_json_headers_read_bearer_token_for_each_request(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "first-token")
    assert governance_json_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer first-token",
    }

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", " second-token ")
    assert governance_json_headers()["Authorization"] == "Bearer second-token"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://example.test/start", "http://example.test/next"),
        ("http://EXAMPLE.test:80/start", "http://example.TEST/next"),
        ("https://example.test/start", "https://example.test:443/next"),
    ],
)
def test_same_origin_redirect_retains_authorization(source: str, target: str):
    request = urllib.request.Request(
        source,
        headers={"Authorization": "Bearer secret"},
    )

    redirected = _AuthorizationSafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        target,
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") == "Bearer secret"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://example.test/start", "http://other.test/next"),
        ("http://example.test/start", "http://sub.example.test/next"),
        ("http://example.test/start", "http://example.test:8080/next"),
        ("http://example.test/start", "https://example.test/next"),
        ("http://example.test:bad/start", "http://example.test/next"),
        ("http://example.test/start", "http://example.test:bad/next"),
    ],
)
def test_cross_origin_or_malformed_redirect_strips_authorization(
    source: str,
    target: str,
):
    request = urllib.request.Request(
        source,
        headers={"Authorization": "Bearer secret"},
    )

    redirected = _AuthorizationSafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        target,
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_redirect_chain_never_restores_stripped_authorization():
    handler = _AuthorizationSafeRedirectHandler()
    original = urllib.request.Request(
        "https://example.test/start",
        headers={"Authorization": "Bearer secret"},
    )
    cross_origin = handler.redirect_request(
        original,
        None,
        302,
        "Found",
        {},
        "https://other.test/middle",
    )
    assert cross_origin is not None

    returned = handler.redirect_request(
        cross_origin,
        None,
        302,
        "Found",
        {},
        "https://example.test/end",
    )

    assert returned is not None
    assert returned.get_header("Authorization") is None


def test_production_scripts_do_not_bypass_authorization_safe_opener():
    violations: list[str] = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name == "_http_auth.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_urlopen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                imported_urlopen.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "urlopen"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            raw_attribute = (
                isinstance(function, ast.Attribute)
                and function.attr == "urlopen"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "request"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "urllib"
            )
            raw_import = isinstance(function, ast.Name) and function.id in imported_urlopen
            if raw_attribute or raw_import:
                violations.append(f"{path.relative_to(SCRIPTS)}:{node.lineno}")

    assert violations == []
