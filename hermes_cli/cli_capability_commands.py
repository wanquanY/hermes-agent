"""Interactive CLI handlers for optional upstream capability domains.

The upstream project keeps these handlers in a single large mixin.  The local
architecture groups the cross-cutting command adapters here while the domain
logic remains in ``agent.*``, ``cron.*`` and focused ``hermes_cli.*`` modules.
"""

from __future__ import annotations


class CLICapabilityCommandsMixin:
    """Adapters that connect capability modules to the interactive CLI."""

    def _handle_journey_command(self, cmd_original: str) -> None:
        import argparse
        import io
        import shlex
        from contextlib import redirect_stdout

        from cli import _cprint
        from hermes_cli.journey import register_cli

        parser = argparse.ArgumentParser(prog="/journey", add_help=False)
        register_cli(parser)
        rest = cmd_original.split(None, 1)
        try:
            args = parser.parse_args(shlex.split(rest[1]) if len(rest) > 1 else [])
        except SystemExit:
            return

        interactive = getattr(args, "journey_action", None) in ("delete", "edit")
        try:
            if interactive:
                args.func(args)
                return
            args.force_color = True
            buf = io.StringIO()
            with redirect_stdout(buf):
                args.func(args)
            _cprint(buf.getvalue().rstrip("\n"))
        except Exception as exc:
            _cprint(f"  /journey failed: {exc}")

    def _handle_pet_command(self, cmd: str) -> None:
        from agent.pet import store
        from agent.pet.manifest import ManifestError
        from hermes_cli.pets import (
            _set_active,
            _set_enabled,
            print_pet_gallery,
            set_pet_scale,
            toggle_pet_display,
        )

        parts = cmd.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        low = arg.lower()

        if not arg or low == "toggle":
            enabled, name, err = toggle_pet_display()
            if err:
                print(f"(x_x) {err}")
            elif enabled:
                print(f"(^_^)b {name} is out — it'll pop in shortly.")
            else:
                print(f"(-_-)zzZ {name} put away." if name else "(-_-)zzZ Pet put away.")
            return
        if low in ("list", "gallery", "browse", "all"):
            print_pet_gallery()
            return
        if low == "scale" or low.startswith("scale "):
            value = arg[len("scale") :].strip()
            if not value:
                print("(o_o) Usage: /pet scale <factor>  (e.g. /pet scale 0.5)")
                return
            scale, err = set_pet_scale(value)
            print(f"(x_x) {err}" if err else f"(^_^) Pet scale → {scale:g}.")
            return
        if low == "off":
            _set_enabled(False)
            print("(-_-)zzZ Pet put away.")
            return

        print(f"(o_o) Fetching '{arg}' from petdex…")
        try:
            pet = store.install_pet(arg)
        except (store.PetStoreError, ManifestError) as exc:
            print(f"(x_x) Couldn't adopt '{arg}': {exc}")
            return
        _set_active(arg)
        print(f"(^_^)b {pet.display_name} is out — it'll pop in shortly.")

    def _handle_hatch_command(self, cmd: str) -> None:
        from agent.pet import store
        from agent.pet.generate import orchestrate
        from agent.pet.generate.imagegen import GenerationError
        from hermes_cli.pets import _set_active

        parts = cmd.split(maxsplit=1)
        concept = parts[1].strip() if len(parts) > 1 else ""
        if not concept:
            try:
                concept = input("(o_o) Describe your pet: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if not concept:
            print("(o_o) Usage: /hatch <description>  (e.g. /hatch a tiny cyber fox)")
            return

        display_name = " ".join(word.capitalize() for word in concept.split()[:3])[:28].strip() or "Pet"
        slug = store.slugify(display_name) or store.slugify(concept) or "pet"
        print(f"(o_o) Designing '{concept}'… (a minute of image-model calls)")
        try:
            drafts = orchestrate.generate_base_drafts(concept, n=1)
        except GenerationError as exc:
            print(f"(x_x) Couldn't generate a base look: {exc}")
            return
        if not drafts:
            print("(x_x) No base draft came back — try again.")
            return

        def _progress(event: str, detail: str) -> None:
            if event == "row":
                print(f"  ┊ drawing {detail.split(':', 1)[0]}…")
            elif event == "compose":
                print("  ┊ composing spritesheet…")
            elif event == "save":
                print("  ┊ saving…")

        try:
            result = orchestrate.hatch_pet(
                base_image=drafts[0],
                slug=slug,
                display_name=display_name,
                concept=concept,
                on_progress=_progress,
            )
        except GenerationError as exc:
            print(f"(x_x) Hatch failed: {exc}")
            return
        _set_active(result.slug)
        print(f"(^_^)b {result.display_name} hatched and adopted — it'll pop in shortly!")

    def _handle_blueprint_command(self, cmd: str) -> None:
        import shlex

        try:
            tokens = shlex.split(cmd)[1:] if cmd else []
        except ValueError:
            tokens = (cmd or "").split()[1:]
        args = " ".join(shlex.quote(token) for token in tokens)
        try:
            from hermes_cli.blueprint_cmd import handle_blueprint_command

            result = handle_blueprint_command(args)
        except Exception as exc:
            self._console_print(f"Automation blueprint command failed: {exc}")
            return
        self._console_print(result.text)
        if result.agent_seed:
            self._pending_input.put(result.agent_seed)

    def _handle_learn_command(self, cmd: str) -> None:
        from agent.learn_prompt import build_learn_prompt

        parts = cmd.strip().split(None, 1)
        user_request = parts[1].strip() if len(parts) > 1 else ""
        prompt = build_learn_prompt(user_request)
        print(
            "\n⚡ Learning a skill from what you described..."
            if user_request
            else "\n⚡ Learning a skill from this conversation..."
        )
        if hasattr(self, "_pending_input"):
            self._pending_input.put(prompt)
        else:
            print("  /learn needs an active chat session to run.")

    def _handle_moa_command(self, cmd: str) -> None:
        from cli import _cprint
        from hermes_cli.moa_config import moa_usage, normalize_moa_config

        parts = cmd.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            _cprint(f"  {moa_usage()}")
            return

        config = self.config if isinstance(self.config, dict) else {}
        preset = normalize_moa_config(config.get("moa") or {})["default_preset"]
        self._pending_one_turn_model_restore = self._snapshot_model_runtime()
        self.requested_provider = "moa"
        self.provider = "moa"
        self.model = preset
        self.api_key = "moa-virtual-provider"
        self.base_url = "moa://local"
        self.api_mode = "chat_completions"
        self._explicit_api_key = self.api_key
        self._explicit_base_url = self.base_url
        self.agent = None
        self._active_agent_route_signature = None
        self._pending_input.put(payload)
        _cprint(
            f"  MoA one-shot queued with preset {preset}; previous model "
            "will be restored after this turn."
        )

    def _compose_in_editor(self, initial_text: str = "") -> str:
        import os
        import shlex
        import subprocess
        import tempfile

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            editor = "notepad" if os.name == "nt" else "nano"
        header = (
            "#! Compose your prompt below. Lines starting with '#!' are ignored.\n"
            "#! Save and quit to send; leave empty to cancel.\n\n"
        )
        fd, path = tempfile.mkstemp(suffix=".md", prefix="hermes_prompt_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(header)
                if initial_text:
                    handle.write(initial_text)
            try:
                subprocess.call([*shlex.split(editor), path])
            except Exception:
                subprocess.call(f"{editor} {shlex.quote(path)}", shell=True)
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return "\n".join(line for line in raw.splitlines() if not line.startswith("#!")).strip()

    def _handle_prompt_compose_command(self, cmd_original: str) -> None:
        from cli import _DIM, _RST, _cprint

        parts = (cmd_original or "").strip().split(None, 1)
        initial = parts[1] if len(parts) > 1 else ""
        try:
            composed = self._compose_in_editor(initial)
        except Exception as exc:
            _cprint(f"  {_DIM}(>_<) Could not open editor: {exc}{_RST}")
            return
        if not composed:
            _cprint(f"  {_DIM}(._.) Empty prompt — nothing sent.{_RST}")
            return
        self._pending_input.put(composed)

    def _handle_timestamps_command(self, cmd_original: str) -> None:
        from cli import _cprint, save_config_value
        from hermes_cli.colors import Colors

        parts = (cmd_original or "").strip().split(None, 1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        current = bool(getattr(self, "show_timestamps", False))
        if arg in {"status", "?"}:
            _cprint(f"  {Colors.BOLD}Message timestamps:{Colors.RESET} {'ON' if current else 'OFF'}")
            return
        if arg in {"on", "enable", "true", "1"}:
            new_state = True
        elif arg in {"off", "disable", "false", "0"}:
            new_state = False
        elif not arg:
            new_state = not current
        else:
            _cprint("  Usage: /timestamps [on|off|status]")
            return
        self.show_timestamps = new_state
        if save_config_value("display.timestamps", new_state):
            state = f"{Colors.GREEN}ON{Colors.RESET}" if new_state else f"{Colors.DIM}OFF{Colors.RESET}"
            _cprint(f"  Message timestamps: {state}")
        else:
            _cprint("  Failed to save timestamps setting to config.yaml")
