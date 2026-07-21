"""Discord finite-choice view factory, isolated from the main adapter module."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def create_choice_picker_view_class(discord, component_check_auth):
    """Create the view after discord.py is available, including lazy installs."""

    class ChoicePickerView(discord.ui.View):
        def __init__(
            self,
            choices: list,
            on_choice_selected,
            allowed_user_ids: set,
            allowed_role_ids: set | None = None,
        ) -> None:
            super().__init__(timeout=120)
            self.on_choice_selected = on_choice_selected
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False
            self._message = None

            options = [
                discord.SelectOption(
                    label=str(choice.get("label") or choice.get("value") or "")[:100],
                    value=str(choice.get("value") or "")[:100],
                    description="current" if choice.get("is_current") else None,
                )
                for choice in list(choices)[:25]
            ]
            select = discord.ui.Select(
                placeholder="Choose an option...",
                options=options,
            )
            select.callback = self._on_select
            self.add_item(select)

        def _check_auth(self, interaction) -> bool:
            return component_check_auth(
                interaction,
                self.allowed_user_ids,
                self.allowed_role_ids,
            )

        async def _on_select(self, interaction) -> None:
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "⛔ You are not authorized to change this setting.",
                    ephemeral=True,
                )
                return
            if self.resolved:
                await interaction.response.defer()
                return
            self.resolved = True

            value = interaction.data.get("values", [""])[0]
            try:
                result_text = await self.on_choice_selected(
                    str(interaction.channel_id),
                    value,
                )
            except Exception as exc:
                logger.error("Choice picker selection failed: %s", exc)
                result_text = f"Error applying selection: {exc}"

            self.clear_items()
            self.stop()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=result_text,
                    color=discord.Color.green(),
                ),
                view=self,
            )

        async def on_timeout(self) -> None:
            if self.resolved or self._message is None:
                return
            try:
                self.clear_items()
                await self._message.edit(
                    embed=discord.Embed(
                        description="⏱ Selection expired — no change made.",
                        color=discord.Color.greyple(),
                    ),
                    view=self,
                )
            except Exception:
                pass

    return ChoicePickerView
