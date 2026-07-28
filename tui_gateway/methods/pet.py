"""Profile-safe petdex RPC methods shared by TUI rendering surfaces."""

from __future__ import annotations

import logging

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.profile_home_scope import session_home_scope

bind_server_globals(globals())
logger = logging.getLogger(__name__)


def _pet_config() -> dict:
    from hermes_cli.config import load_config

    config = load_config()
    display = config.get("display") if isinstance(config, dict) else {}
    display = display if isinstance(display, dict) else {}
    pet = display.get("pet")
    return pet if isinstance(pet, dict) else {}


@method("pet.cells")
def _(rid, params: dict) -> dict:
    """Return animated cell or kitty frames for the active pet state."""
    try:
        from agent.pet import constants, render, store
        from agent.pet.render import PetRenderer

        with session_home_scope(_sessions, params):
            config = _pet_config()
            if not bool(config.get("enabled")):
                return _ok(rid, {"enabled": False})
            pet = store.resolve_active_pet(str(config.get("slug", "") or ""))
            if pet is None or not pet.exists:
                return _ok(rid, {"enabled": False})

            state = str(params.get("state") or constants.PetState.IDLE.value)
            scale = float(
                config.get("scale", constants.DEFAULT_SCALE)
                or constants.DEFAULT_SCALE
            )
            cols = int(params.get("cols") or 0) or constants.resolve_cols(
                scale, config.get("unicode_cols", 0)
            )
            configured = str(config.get("render_mode", "auto") or "auto").lower()
            graphics_mode = (
                render.detect_terminal_graphics()
                if configured in {"", "auto"}
                else configured
            )
            if params.get("graphics") and graphics_mode == "kitty":
                image_id = render.kitty_image_id(pet.slug)
                payload = PetRenderer(
                    str(pet.spritesheet), mode="kitty", scale=scale
                ).kitty_payload(state, image_id=image_id)
                if payload:
                    count = len(payload["frames"]) or 1
                    return _ok(
                        rid,
                        {
                            "enabled": True,
                            "slug": pet.slug,
                            "displayName": pet.display_name,
                            "state": state,
                            "graphics": "kitty",
                            "imageId": image_id,
                            "color": render.kitty_color_hex(image_id),
                            "cols": payload["cols"],
                            "rows": payload["rows"],
                            "placeholder": payload["placeholder"],
                            "frames": payload["frames"],
                            "frameMs": constants.LOOP_MS / count,
                            "scale": scale,
                        },
                    )

            renderer = PetRenderer(
                str(pet.spritesheet),
                mode="unicode",
                scale=scale,
                unicode_cols=cols,
            )
            count = renderer.frame_count(state) or 1
            frames = []
            for index in range(count):
                grid = renderer.cells(state, index, cols=cols)
                frames.append(
                    [[[*top, *bottom] for top, bottom in row] for row in grid]
                )
            return _ok(
                rid,
                {
                    "enabled": True,
                    "slug": pet.slug,
                    "displayName": pet.display_name,
                    "state": state,
                    "cols": cols,
                    "frameMs": constants.LOOP_MS / count,
                    "frames": frames,
                    "scale": scale,
                },
            )
    except Exception:
        logger.debug("pet.cells failed", exc_info=True)
        return _ok(rid, {"enabled": False})


@method("pet.gallery")
def _(rid, params: dict) -> dict:
    """Merge petdex manifest entries with locally installed pets."""
    try:
        from agent.pet import store

        with session_home_scope(_sessions, params):
            config = _pet_config()
            installed = {pet.slug: pet for pet in store.installed_pets()}
            gallery = []
            seen = set()
            try:
                from agent.pet.manifest import fetch_manifest, prefetch

                if params.get("localOnly"):
                    prefetch()
                    manifest = []
                else:
                    manifest = fetch_manifest()
                for entry in manifest:
                    seen.add(entry.slug)
                    gallery.append(
                        {
                            "slug": entry.slug,
                            "displayName": entry.display_name,
                            "installed": entry.slug in installed,
                            "spritesheetUrl": entry.spritesheet_url,
                            "curated": "/curated/" in entry.spritesheet_url,
                            "generated": bool(
                                entry.slug in installed
                                and installed[entry.slug].generated
                            ),
                        }
                    )
            except Exception:
                logger.debug("pet manifest unavailable", exc_info=True)
            for slug, pet in installed.items():
                if slug not in seen:
                    gallery.append(
                        {
                            "slug": slug,
                            "displayName": pet.display_name,
                            "installed": True,
                            "spritesheetUrl": "",
                            "generated": pet.generated,
                        }
                    )
            return _ok(
                rid,
                {
                    "enabled": bool(config.get("enabled")),
                    "active": str(config.get("slug", "") or ""),
                    "pets": gallery,
                },
            )
    except Exception:
        logger.debug("pet.gallery failed", exc_info=True)
        return _ok(rid, {"enabled": False, "active": "", "pets": []})


@method("pet.select")
def _(rid, params: dict) -> dict:
    slug = str(params.get("slug") or "").strip()
    if not slug:
        return _err(rid, 4004, "missing slug")
    try:
        from agent.pet import store
        from hermes_cli.pets import _set_active

        with session_home_scope(_sessions, params):
            pet = store.install_pet(slug)
            _set_active(slug)
        return _ok(
            rid,
            {"ok": True, "slug": slug, "displayName": pet.display_name},
        )
    except Exception as exc:
        return _err(rid, 5031, f"could not adopt '{slug}': {exc}")


@method("pet.remove")
def _(rid, params: dict) -> dict:
    slug = str(params.get("slug") or "").strip()
    if not slug:
        return _err(rid, 4004, "missing slug")
    try:
        from agent.pet import store
        from hermes_cli.pets import _clear_active_if

        with session_home_scope(_sessions, params):
            removed = store.remove_pet(slug)
            _clear_active_if(slug)
        return _ok(rid, {"ok": removed, "slug": slug})
    except Exception as exc:
        return _err(rid, 5031, f"pet.remove failed: {exc}")


@method("pet.disable")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.pets import _set_enabled

        with session_home_scope(_sessions, params):
            _set_enabled(False)
        return _ok(rid, {"ok": True})
    except Exception as exc:
        return _err(rid, 5031, f"pet.disable failed: {exc}")


@method("pet.scale")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.pets import set_pet_scale

        with session_home_scope(_sessions, params):
            scale, error = set_pet_scale(params.get("scale"))
        if error:
            return _err(rid, 4004, error)
        return _ok(rid, {"ok": True, "scale": scale})
    except Exception as exc:
        return _err(rid, 5031, f"pet.scale failed: {exc}")


__all__ = []
