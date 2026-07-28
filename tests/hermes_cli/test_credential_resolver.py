import json
import os
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

import hermes_cli.runtime_provider as runtime_provider
from hermes_cli.credential_resolver import (
    CredentialResolutionError,
    DovieBrokerCredentialResolver,
    StandaloneCredentialResolver,
)


def test_standalone_resolver_only_accepts_explicit_env_references(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    resolver = StandaloneCredentialResolver()

    status = resolver.status("env://DEEPSEEK_API_KEY")
    resolved = resolver.resolve(
        "env://DEEPSEEK_API_KEY",
        purpose="inference",
        connection_id="builtin:deepseek",
    )

    assert status.configured is True
    assert resolved.value == "secret-value"
    with pytest.raises(CredentialResolutionError):
        resolver.status("dovie-secure://model-credentials/cred_1")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_broker_bootstrap_rejects_broad_permissions(tmp_path: Path):
    bootstrap = tmp_path / "broker.json"
    bootstrap.write_text("{}", encoding="utf-8")
    bootstrap.chmod(0o644)

    with pytest.raises(CredentialResolutionError) as error:
        DovieBrokerCredentialResolver(bootstrap).status(
            "dovie-secure://model-credentials/cred_1"
        )

    assert error.value.code == "CREDENTIAL_BROKER_BOOTSTRAP_PERMISSIONS"


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX domain socket")
def test_broker_resolver_uses_length_prefixed_local_frame():
    # macOS caps AF_UNIX paths at 104 bytes. pytest's nested tmp_path can
    # exceed that limit before the socket filename is appended.
    with tempfile.TemporaryDirectory(prefix="dovie-cb-") as socket_root:
        socket_path = Path(socket_root) / "broker.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(1)
        observed = {}

        def serve_once():
            connection, _ = server.accept()
            with connection:
                length = struct.unpack(">I", connection.recv(4))[0]
                body = bytearray()
                while len(body) < length:
                    body.extend(connection.recv(length - len(body)))
                observed.update(json.loads(bytes(body).decode("utf-8")))
                response = json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "configured": True,
                            "status": "active",
                            "generation": 6,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                connection.sendall(struct.pack(">I", len(response)) + response)

        worker = threading.Thread(target=serve_once, daemon=True)
        worker.start()
        bootstrap = Path(socket_root) / "runtime.json"
        bootstrap.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "socket_path": str(socket_path),
                    "capability": "x" * 43,
                    "owner_id": "owner_12345678",
                    "runtime_id": "runtime-1",
                }
            ),
            encoding="utf-8",
        )
        bootstrap.chmod(0o600)
        try:
            status = DovieBrokerCredentialResolver(bootstrap).status(
                "dovie-secure://model-credentials/cred_1"
            )
        finally:
            worker.join(timeout=2)
            server.close()

    assert status.configured is True
    assert status.generation == 6
    assert observed["method"] == "credential.status"
    assert observed["capability"] == "x" * 43


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX domain socket")
def test_broker_resolver_accepts_private_short_socket_outside_process_tmpdir(
    tmp_path: Path,
    monkeypatch,
):
    hermes_process_temp = tmp_path / "hermes-runtime-tmp"
    hermes_process_temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(hermes_process_temp))
    with tempfile.TemporaryDirectory(prefix="node-system-temp-") as socket_root:
        endpoint_directory = Path(socket_root) / "dovie-cb-random"
        endpoint_directory.mkdir(mode=0o700)
        socket_path = endpoint_directory / "b.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        bootstrap = tmp_path / "runtime.json"
        bootstrap.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "socket_path": str(socket_path),
                    "capability": "x" * 43,
                    "owner_id": "owner_12345678",
                    "runtime_id": "runtime-1",
                }
            ),
            encoding="utf-8",
        )
        bootstrap.chmod(0o600)
        try:
            resolver = DovieBrokerCredentialResolver(bootstrap)
            resolver.validate_boundary("owner_12345678")
            with pytest.raises(CredentialResolutionError) as error:
                resolver.validate_boundary("owner_different")
            assert error.value.code == "CREDENTIAL_BROKER_OWNER_MISMATCH"
        finally:
            server.close()


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX domain socket")
def test_broker_resolver_rejects_unscoped_external_socket_directory(
    tmp_path: Path,
):
    with tempfile.TemporaryDirectory(prefix="external-socket-") as socket_root:
        endpoint_directory = Path(socket_root) / "arbitrary-directory"
        endpoint_directory.mkdir(mode=0o700)
        socket_path = endpoint_directory / "b.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        bootstrap_directory = tmp_path / "bootstrap"
        bootstrap_directory.mkdir()
        bootstrap = bootstrap_directory / "runtime.json"
        bootstrap.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "socket_path": str(socket_path),
                    "capability": "x" * 43,
                    "owner_id": "owner_12345678",
                    "runtime_id": "runtime-1",
                }
            ),
            encoding="utf-8",
        )
        bootstrap.chmod(0o600)
        try:
            with pytest.raises(CredentialResolutionError) as error:
                DovieBrokerCredentialResolver(bootstrap).validate_boundary(
                    "owner_12345678"
                )
            assert error.value.code == "CREDENTIAL_BROKER_UNAVAILABLE"
            assert "location is not trusted" in str(error.value)
        finally:
            server.close()


def test_managed_connection_forwards_validation_credential_purpose(monkeypatch):
    observed = {}

    class Repository:
        def get(self, connection_id):
            assert connection_id == "custom:test"
            return {
                "connection_id": connection_id,
                "provider_id": "custom",
                "kind": "custom_endpoint",
                "base_url": "http://127.0.0.1:1234/v1",
                "credential_ref": "dovie-secure://model-credentials/cred_1",
                "enabled": True,
                "revision": 1,
            }

    class Resolver:
        def resolve(self, credential_ref, *, purpose, connection_id):
            observed.update(
                credential_ref=credential_ref,
                purpose=purpose,
                connection_id=connection_id,
            )
            return type(
                "Resolved",
                (),
                {
                    "value": "secret",
                    "generation": 2,
                    "lease_id": "lease-1",
                },
            )()

    monkeypatch.setattr(
        "hermes_cli.model_connections.ModelConnectionRepository.for_runtime",
        lambda: Repository(),
    )
    monkeypatch.setattr(
        "hermes_cli.credential_resolver.runtime_credential_resolver",
        lambda: Resolver(),
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **kwargs: {
            "provider": "custom",
            "base_url": kwargs["explicit_base_url"],
            "api_key": kwargs["explicit_api_key"],
            "api_mode": "chat_completions",
        },
    )

    from hermes_cli.managed_connection_runtime import (
        resolve_managed_connection_runtime,
    )

    result = resolve_managed_connection_runtime(
        connection_id="custom:test",
        requested="custom",
        target_model="model-1",
        runtime_executor=None,
        codex_home=None,
        credential_purpose="validation",
        provider_registry=runtime_provider.PROVIDER_REGISTRY,
        normalize_runtime_executor=runtime_provider._normalize_runtime_executor,
        resolve_runtime=runtime_provider.resolve_runtime_provider,
        logger=runtime_provider.logger,
    )

    assert observed["purpose"] == "validation"
    assert result["credential_generation"] == 2


def test_managed_kimi_coding_key_selects_coding_endpoint(monkeypatch):
    class Repository:
        def get(self, connection_id):
            assert connection_id == "builtin:kimi-coding"
            return {
                "connection_id": connection_id,
                "provider_id": "kimi-coding",
                "kind": "builtin",
                "base_url": None,
                "api_mode": None,
                "credential_ref": (
                    "dovie-secure://model-credentials/kimi-coding"
                ),
                "enabled": True,
                "revision": 3,
            }

    class Resolver:
        def resolve(self, credential_ref, *, purpose, connection_id):
            assert credential_ref.endswith("/kimi-coding")
            assert purpose == "inference"
            assert connection_id == "builtin:kimi-coding"
            return type(
                "Resolved",
                (),
                {
                    "value": "sk-kimi-managed-key",
                    "generation": 4,
                    "lease_id": "lease-kimi",
                    "secret_kind": "api_key",
                },
            )()

    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_BASE_URL", raising=False)
    monkeypatch.setattr(
        "hermes_cli.model_connections.ModelConnectionRepository.for_runtime",
        lambda: Repository(),
    )
    monkeypatch.setattr(
        "hermes_cli.credential_resolver.runtime_credential_resolver",
        lambda: Resolver(),
    )
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: {})

    result = runtime_provider.resolve_runtime_provider(
        connection_id="builtin:kimi-coding",
        requested="kimi-coding",
        target_model="kimi-k3",
    )

    assert result["provider"] == "kimi-coding"
    assert result["base_url"] == "https://api.kimi.com/coding"
    assert result["api_mode"] == "anthropic_messages"
    assert result["api_key"] == "sk-kimi-managed-key"
    assert result["connection_id"] == "builtin:kimi-coding"
    assert result["credential_generation"] == 4
