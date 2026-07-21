"""Context-local persistence routing for multiplexed profiles."""

from types import SimpleNamespace

from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_gateway.profile_storage import GatewayProfileStorageService


class _Resource:
    def __init__(self, identity):
        self.identity = identity
        self.closed = False

    def close(self):
        self.closed = True


def test_router_switches_storage_by_context_without_global_mutation(tmp_path):
    runner = SimpleNamespace()
    service = GatewayProfileStorageService(runner)
    first_store = _Resource("first-store")
    first_db = _Resource("first-db")
    second_store = _Resource("second-store")
    second_db = _Resource("second-db")
    service.install_primary(
        tmp_path / "first",
        session_store=first_store,
        session_db=first_db,
    )
    service.install_primary(
        tmp_path / "second",
        session_store=second_store,
        session_db=second_db,
    )

    first_token = set_hermes_home_override(tmp_path / "first")
    try:
        assert service.session_store_router.identity == "first-store"
        assert service.session_db_router.identity == "first-db"
    finally:
        reset_hermes_home_override(first_token)

    second_token = set_hermes_home_override(tmp_path / "second")
    try:
        assert service.session_store_router.identity == "second-store"
        assert service.session_db_router.identity == "second-db"
    finally:
        reset_hermes_home_override(second_token)


def test_router_closes_every_owned_profile_resource(tmp_path):
    service = GatewayProfileStorageService(SimpleNamespace())
    resources = [_Resource(index) for index in range(4)]
    service.install_primary(
        tmp_path / "first",
        session_store=resources[0],
        session_db=resources[1],
    )
    service.install_primary(
        tmp_path / "second",
        session_store=resources[2],
        session_db=resources[3],
    )

    service.session_store_router.close()
    service.session_db_router.close()

    assert all(resource.closed for resource in resources)
