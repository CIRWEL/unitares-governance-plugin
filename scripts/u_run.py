#!/usr/bin/env python3
"""Run a plain CLI under one sidecar-managed UNITARES process session."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from _http_auth import authorization_safe_urlopen


DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_SIDECAR_URL = "http://127.0.0.1:8768"
DEFAULT_MODEL_TYPE = "u-run"
STARTUP_TIMEOUT = 10.0
HTTP_TIMEOUT = 25.0


class URunError(RuntimeError):
    """The wrapper could not establish its local lifecycle contract."""


class SidecarUnavailable(URunError):
    """The configured sidecar did not accept a local HTTP request."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="u-run",
        description="Run one CLI process with sidecar-managed UNITARES lifecycle events.",
        usage="u-run [options] -- <agent command> [args ...]",
    )
    parser.add_argument(
        "--class",
        dest="agent_class",
        metavar="X",
        help=(
            "Explicit calibration/model class. It is never inferred from the "
            f"command; the neutral default is {DEFAULT_MODEL_TYPE!r}."
        ),
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL),
        help="Upstream UNITARES server URL.",
    )
    parser.add_argument(
        "--sidecar-url",
        default=os.environ.get("UNITARES_SIDECAR_URL", DEFAULT_SIDECAR_URL),
        help="Local identity sidecar base URL.",
    )
    parser.add_argument(
        "--slot",
        default="",
        help="Explicit sidecar slot; omitted creates a unique slot for this invocation.",
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Workspace in which the slot-scoped session cache is stored.",
    )
    return parser


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = build_parser()
    if "--" not in argv:
        if any(value in {"-h", "--help"} for value in argv):
            parser.parse_args(argv)
        parser.error("missing '--' before the child command")
    separator = argv.index("--")
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1 :]
    if not command:
        parser.error("a child command is required after '--'")
    if args.agent_class is not None and not args.agent_class.strip():
        parser.error("--class must not be empty")
    return args, command


def _http_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    slot: str = "",
    timeout: float = HTTP_TIMEOUT,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Accept": "application/json"},
    )
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if slot:
        request.add_header("X-UNITARES-Slot", slot)
    try:
        with authorization_safe_urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise URunError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        detail = getattr(exc, "reason", exc)
        raise SidecarUnavailable(f"{url} is unavailable: {detail}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise URunError(f"{url} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise URunError(f"{url} returned a non-object JSON response")
    return decoded


def _sidecar_bind(sidecar_url: str) -> tuple[str, int, str]:
    base = sidecar_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme.lower() != "http"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise URunError("--sidecar-url must be an HTTP base URL without a path")
    try:
        port = 80 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise URunError(f"invalid sidecar port: {exc}") from exc
    host = parsed.hostname or ""
    if host == "localhost":
        bind_host = "127.0.0.1"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise URunError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            ) from exc
        if address.version != 4 or not address.is_loopback:
            raise URunError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            )
        bind_host = host
    if not 1 <= port <= 65535:
        raise URunError("--sidecar-url port must be between 1 and 65535")
    return bind_host, port, base


def _probe_health(sidecar_url: str) -> dict[str, Any] | None:
    try:
        return _http_json(f"{sidecar_url}/health", timeout=0.5)
    except SidecarUnavailable:
        return None


def _validate_health(
    health: dict[str, Any], *, server_url: str, workspace: Path
) -> None:
    if health.get("success") is not True:
        raise URunError("sidecar health response did not report success")
    reported_server = str(health.get("server_url") or "").rstrip("/")
    if reported_server != server_url.rstrip("/"):
        raise URunError(
            "running sidecar targets a different UNITARES server "
            f"({reported_server or '<missing>'})"
        )
    reported_workspace = str(health.get("workspace") or "")
    if not reported_workspace or Path(reported_workspace).expanduser().resolve() != workspace:
        raise URunError(
            "running sidecar targets a different workspace "
            f"({reported_workspace or '<missing>'})"
        )


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def ensure_sidecar(
    *,
    sidecar_url: str,
    server_url: str,
    workspace: Path,
    slot: str,
    model_type: str,
) -> subprocess.Popen[Any] | None:
    host, port, sidecar_url = _sidecar_bind(sidecar_url)
    health = _probe_health(sidecar_url)
    if health is not None:
        _validate_health(health, server_url=server_url, workspace=workspace)
        return None

    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("identity_sidecar.py")),
        "--host",
        host,
        "--port",
        str(port),
        "--server-url",
        server_url,
        "--workspace",
        str(workspace),
        "--slot",
        slot,
        "--model-type",
        model_type,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(Path.home()),
            **_process_group_kwargs(),
        )
    except OSError as exc:
        raise URunError(f"could not start identity sidecar: {exc}") from exc

    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        while time.monotonic() < deadline:
            health = _probe_health(sidecar_url)
            if health is not None:
                _validate_health(health, server_url=server_url, workspace=workspace)
                # Another wrapper may have won the bind race. In that case our
                # failed process is not an owned, running sidecar.
                return process if process.poll() is None else None
            return_code = process.poll()
            if return_code is not None:
                raise URunError(
                    f"identity sidecar exited during startup with code {return_code}"
                )
            time.sleep(0.05)
        raise URunError("timed out waiting for identity sidecar health")
    except Exception:
        _stop_process(process)
        raise


def _start_session(
    *, sidecar_url: str, slot: str, model_type: str
) -> None:
    response = _http_json(
        f"{sidecar_url}/session/start",
        payload={"slot": slot, "model_type": model_type},
        slot=slot,
    )
    result = response.get("result")
    if (
        response.get("success") is not True
        or not isinstance(result, dict)
        or result.get("status") != "ok"
    ):
        detail = result.get("error") if isinstance(result, dict) else response.get("error")
        raise URunError(f"sidecar could not start a UNITARES session: {detail or response}")


def exit_complexity(wall_seconds: float, exit_code: int) -> float:
    """Bound an objective duration/failure proxy on the existing 0..0.85 scale."""
    duration_component = min(max(0.0, wall_seconds) / 3600.0, 1.0) * 0.35
    failure_component = 0.20 if exit_code != 0 else 0.0
    return round(min(0.85, 0.30 + duration_component + failure_component), 3)


def _exit_summary(return_code: int, exit_code: int, wall_seconds: float) -> str:
    if return_code < 0:
        return (
            f"u-run observed child termination by signal {-return_code} "
            f"(shell exit code {exit_code}) after {wall_seconds:.3f} seconds."
        )
    return f"u-run observed child exit code {exit_code} after {wall_seconds:.3f} seconds."


def _submit_exit_checkin(
    *,
    sidecar_url: str,
    slot: str,
    return_code: int,
    exit_code: int,
    wall_seconds: float,
) -> None:
    response = _http_json(
        f"{sidecar_url}/turn/stop",
        payload={
            "slot": slot,
            "event": "u_run_exit",
            "response_text": _exit_summary(return_code, exit_code, wall_seconds),
            "complexity": exit_complexity(wall_seconds, exit_code),
            "epistemic_class": "substrate_interpretation",
        },
        slot=slot,
    )
    if response.get("success") is not True:
        raise URunError(f"exit check-in was not accepted: {response}")


def _shell_exit_code(return_code: int) -> int:
    return 128 + (-return_code) if return_code < 0 else return_code


def _forward_to_child(child: subprocess.Popen[Any], signum: int) -> None:
    try:
        if os.name == "nt":
            child.send_signal(signum)
        else:
            os.killpg(child.pid, signum)
    except (OSError, ProcessLookupError):
        pass


@contextmanager
def _forward_signals(child: subprocess.Popen[Any]) -> Iterator[None]:
    previous: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        _forward_to_child(child, signum)

    forwarded = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        forwarded.append(signal.SIGHUP)
    try:
        for signum in forwarded:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_child(command: list[str], environment: dict[str, str]) -> tuple[int, int, float]:
    started = time.monotonic()
    try:
        child = subprocess.Popen(command, env=environment, **_process_group_kwargs())
    except OSError as exc:
        print(f"u-run: could not start child command: {exc}", file=sys.stderr)
        return 127, 127, time.monotonic() - started
    with _forward_signals(child):
        return_code = child.wait()
    return return_code, _shell_exit_code(return_code), time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    args, command = parse_args(list(sys.argv[1:] if argv is None else argv))
    workspace = Path(args.workspace).expanduser().resolve()
    server_url = args.server_url.rstrip("/")
    if not server_url:
        print("u-run: --server-url must not be empty", file=sys.stderr)
        return 125
    model_type = args.agent_class.strip() if args.agent_class else DEFAULT_MODEL_TYPE
    slot = args.slot.strip() or f"u-run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    owned_sidecar: subprocess.Popen[Any] | None = None

    try:
        _host, _port, sidecar_url = _sidecar_bind(args.sidecar_url)
        owned_sidecar = ensure_sidecar(
            sidecar_url=sidecar_url,
            server_url=server_url,
            workspace=workspace,
            slot=slot,
            model_type=model_type,
        )
        _start_session(sidecar_url=sidecar_url, slot=slot, model_type=model_type)
        environment = os.environ.copy()
        environment.update(
            {
                "UNITARES_SERVER_URL": server_url,
                "UNITARES_SIDECAR_URL": sidecar_url,
                "UNITARES_SIDECAR_SLOT": slot,
                "UNITARES_MODEL_TYPE": model_type,
            }
        )
        return_code, exit_code, wall_seconds = _run_child(command, environment)
        try:
            _submit_exit_checkin(
                sidecar_url=sidecar_url,
                slot=slot,
                return_code=return_code,
                exit_code=exit_code,
                wall_seconds=wall_seconds,
            )
        except URunError as exc:
            # Lifecycle telemetry must not replace the wrapped command's exit
            # status. The warning remains visible to the operator.
            print(f"u-run: warning: {exc}", file=sys.stderr)
        return exit_code
    except URunError as exc:
        print(f"u-run: {exc}", file=sys.stderr)
        return 125
    finally:
        _stop_process(owned_sidecar)


if __name__ == "__main__":
    raise SystemExit(main())
