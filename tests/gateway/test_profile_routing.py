"""Profile routing contracts for the locally refactored gateway."""

import pytest

from hermes_gateway.profile_routing import (
    ProfileRoute,
    match_profile_route,
    parse_profile_routes,
)


def test_legacy_guild_alias_normalizes_to_platform_neutral_scope():
    route = ProfileRoute(
        name="guild",
        platform="discord",
        profile="ops",
        guild_id="guild-1",
    )

    assert route.scope_id == "guild-1"
    assert route.guild_id == "guild-1"
    assert route.specificity == 2


def test_route_is_immutable():
    route = ProfileRoute(name="x", platform="discord", profile="ops")

    with pytest.raises(AttributeError):
        route.profile = "other"


def test_all_configured_discriminators_match_conjunctively():
    route = ProfileRoute(
        name="channel",
        platform="discord",
        profile="ops",
        scope_id="guild-1",
        chat_id="channel-1",
    )

    assert route.matches(
        "discord",
        scope_id="guild-1",
        chat_id="thread-1",
        parent_chat_id="channel-1",
    )
    assert not route.matches(
        "discord",
        scope_id="guild-2",
        chat_id="channel-1",
    )


def test_thread_discriminator_never_matches_parent_channel():
    route = ProfileRoute(
        name="thread",
        platform="discord",
        profile="ops",
        thread_id="thread-1",
    )

    assert route.matches("discord", thread_id="thread-1")
    assert not route.matches("discord", parent_chat_id="thread-1")


def test_parse_routes_validates_and_sorts_most_specific_first():
    routes = parse_profile_routes(
        [
            {
                "name": "scope",
                "platform": "discord",
                "profile": "ops",
                "guild_id": "guild-1",
            },
            {
                "name": "thread",
                "platform": "discord",
                "profile": "ops",
                "scope_id": "guild-1",
                "chat_id": "channel-1",
                "thread_id": "thread-1",
            },
            {"name": "invalid", "platform": "discord"},
        ]
    )

    assert [route.name for route in routes] == ["thread", "scope"]


def test_disabled_string_false_does_not_match():
    [route] = parse_profile_routes(
        [
            {
                "name": "disabled",
                "platform": "discord",
                "profile": "ops",
                "enabled": "false",
            }
        ]
    )

    assert not route.matches("discord")


def test_match_profile_route_returns_first_specific_match():
    routes = parse_profile_routes(
        [
            {
                "name": "channel",
                "platform": "discord",
                "profile": "general",
                "chat_id": "channel-1",
            },
            {
                "name": "thread",
                "platform": "discord",
                "profile": "ops",
                "chat_id": "channel-1",
                "thread_id": "thread-1",
            },
        ]
    )

    matched = match_profile_route(
        routes,
        "discord",
        chat_id="channel-1",
        thread_id="thread-1",
    )

    assert matched is not None
    assert matched.profile == "ops"
