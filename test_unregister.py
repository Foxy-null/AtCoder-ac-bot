import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import main


class Channel:
    def __init__(self, channel_id, name, visible=True):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.visible = visible

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self.visible)


class Guild:
    def __init__(self, members, channels):
        self.members = {member.id: member for member in members}
        self.channels = channels

    def get_member(self, member_id):
        return self.members.get(member_id)

    def get_channel(self, channel_id):
        return next(
            (
                channel
                for channel in self.channels
                if channel.id == channel_id
            ),
            None,
        )


def member(member_id, manage_guild=False):
    return SimpleNamespace(
        id=member_id,
        mention=f"<@{member_id}>",
        display_name=f"user-{member_id}",
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
    )


class UnregisterViewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = member(1, manage_guild=True)
        self.other = member(2)
        self.guild = Guild(
            [self.user, self.other],
            [
                Channel(101, "visible"),
                Channel(102, "private", visible=False),
            ],
        )
        self.interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
        )
        self.records = [
            {
                "id": 1,
                "discord_id": 1,
                "atcoder_handle": "alice",
                "channel_id": 101,
            },
            {
                "id": 2,
                "discord_id": 1,
                "atcoder_handle": "carol",
                "channel_id": 102,
            },
            {
                "id": 3,
                "discord_id": 2,
                "atcoder_handle": "bob",
                "channel_id": 101,
            },
            {
                "id": 4,
                "discord_id": None,
                "atcoder_handle": "dave",
                "channel_id": 101,
            },
            {
                "id": 5,
                "discord_id": None,
                "atcoder_handle": "eve",
                "channel_id": 102,
            },
        ]

    async def test_shows_self_and_visible_shared_records(self):
        with patch.object(
            main,
            "get_subscriptions_for_channels",
            return_value=self.records,
        ):
            view = main.UnregisterView(self.interaction)

            self.assertEqual([row["id"] for row in view.records], [1, 2, 4])
            embed = view.build_embed()
            self.assertEqual(embed.fields[0].value, "通知先: <#101>")
            self.assertEqual(
                embed.fields[1].value,
                "通知先: 閲覧権限のないチャンネル",
            )
            self.assertEqual(
                embed.fields[2].value,
                "通知先: <#101>\n共有登録（Discordユーザー未紐づけ）",
            )
            self.assertEqual(view.owner_text(999), "<@999>")
            self.assertEqual(
                view.owner_text(999, plain=True),
                "Discord ID: 999",
            )
            self.assertIn(
                "サーバー内の登録を管理",
                [
                    item.label
                    for item in view.children
                    if hasattr(item, "label")
                ],
            )

            view.mode = "admin"
            view.reload()
            self.assertEqual(
                [row["id"] for row in view.records],
                [1, 2, 3, 4, 5],
            )
            select = next(
                item
                for item in view.children
                if hasattr(item, "options")
            )
            bob = next(option for option in select.options if option.value == "3")
            self.assertIn("user-2", bob.description)

    async def test_delete_keeps_owner_check(self):
        response = SimpleNamespace(edit_message=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
            response=response,
            followup=followup,
        )
        with (
            patch.object(
                main,
                "get_subscriptions_for_channels",
                return_value=self.records,
            ),
            patch.object(main, "delete_subscriptions", return_value=1) as delete,
        ):
            view = main.UnregisterView(self.interaction)
            await view.delete_confirmed(
                interaction,
                [(1, 101)],
                administrator=False,
            )

        delete.assert_called_once_with(
            [(1, 101)],
            discord_id=1,
            unlinked_only=False,
        )
        response.edit_message.assert_awaited_once()
        followup.send.assert_awaited_once_with(
            "<@1> が AtCoderのハンドル 「alice」の登録を解除しました。",
            ephemeral=False,
        )

    async def test_shared_delete_requires_record_to_remain_unlinked(self):
        response = SimpleNamespace(edit_message=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
            response=response,
            followup=followup,
        )
        with (
            patch.object(
                main,
                "get_subscriptions_for_channels",
                return_value=self.records,
            ),
            patch.object(main, "delete_subscriptions", return_value=1) as delete,
            patch("builtins.print") as log,
        ):
            view = main.UnregisterView(self.interaction)
            await view.delete_confirmed(
                interaction,
                [(4, 101)],
                administrator=False,
                unlinked=True,
            )

        delete.assert_called_once_with(
            [(4, 101)],
            discord_id=1,
            unlinked_only=True,
        )
        log.assert_called_once()
        followup.send.assert_awaited_once_with(
            "<@1> が AtCoderのハンドル 「dave」の登録を解除しました。",
            ephemeral=False,
        )

    async def test_self_bulk_delete_excludes_shared_records(self):
        response = SimpleNamespace(edit_message=AsyncMock())
        interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
            response=response,
        )
        with patch.object(
            main,
            "get_subscriptions_for_channels",
            return_value=self.records,
        ):
            view = main.UnregisterView(self.interaction)
            await view.confirm_all(interaction)

        self.assertEqual(
            view.confirmation,
            ("自分の登録すべて", [(1, 101), (2, 102)], False),
        )

    async def test_shared_delete_rechecks_channel_visibility(self):
        response = SimpleNamespace(edit_message=AsyncMock())
        interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
            response=response,
        )
        with (
            patch.object(
                main,
                "get_subscriptions_for_channels",
                return_value=self.records,
            ),
            patch.object(main, "delete_subscriptions") as delete,
        ):
            view = main.UnregisterView(self.interaction)
            self.guild.get_channel(101).visible = False
            await view.delete_confirmed(
                interaction,
                [(4, 101)],
                administrator=False,
                unlinked=True,
            )

        delete.assert_not_called()
        response.edit_message.assert_awaited_once()

    async def test_opens_unregister_menu_for_current_dm(self):
        channel = Mock(spec=discord.DMChannel)
        channel.id = 201
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, mention="<@1>"),
            guild=None,
            channel=channel,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        records = [
            {
                "id": 1,
                "discord_id": 2,
                "atcoder_handle": "alice",
                "channel_id": 201,
            }
        ]
        delete_interaction = SimpleNamespace(
            user=interaction.user,
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch.object(
                main,
                "get_subscriptions_for_channels",
                return_value=records,
            ) as get_subscriptions,
            patch.object(
                main,
                "delete_subscriptions",
                return_value=1,
            ) as delete,
        ):
            await main.unregister.callback(interaction)
            view = interaction.response.send_message.await_args.kwargs["view"]
            await view.delete_confirmed(
                delete_interaction,
                [(1, 201)],
                administrator=False,
            )

        get_subscriptions.assert_called_with([201])
        self.assertEqual(view.records, records)
        self.assertEqual(
            view.build_embed().fields[0].value,
            "通知先: このDM",
        )
        delete.assert_called_once_with(
            [(1, 201)],
            discord_id=None,
            unlinked_only=False,
        )

    async def test_admin_delete_rechecks_permission(self):
        response = SimpleNamespace(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.user.guild_permissions.manage_guild = False
        interaction = SimpleNamespace(
            user=self.user,
            guild=self.guild,
            response=response,
        )
        with (
            patch.object(
                main,
                "get_subscriptions_for_channels",
                return_value=self.records,
            ),
            patch.object(main, "delete_subscriptions") as delete,
        ):
            view = main.UnregisterView(self.interaction)
            await view.delete_confirmed(
                interaction,
                [(3, 101)],
                administrator=True,
            )

        delete.assert_not_called()
        response.send_message.assert_awaited_once_with(
            "サーバーの管理権限が必要です。",
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()
