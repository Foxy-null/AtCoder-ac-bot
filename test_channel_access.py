import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import main


def channel_with_permissions(**overrides):
    permissions = {
        "view_channel": True,
        "send_messages": True,
        "embed_links": True,
        **overrides,
    }
    channel = Mock()
    channel.guild.me = object()
    channel.permissions_for.return_value = SimpleNamespace(**permissions)
    return channel


def discord_error(error_type, status):
    response = SimpleNamespace(status=status, reason="test")
    return error_type(response, {"message": "test", "code": 0})


class ChannelAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_current_dm_when_channel_is_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            main.init_db(db_path)
            channel = Mock(spec=discord.DMChannel)
            channel.id = 101
            interaction = SimpleNamespace(
                channel=channel,
                guild=None,
                response=SimpleNamespace(send_message=AsyncMock()),
            )

            with patch.object(main, "DB_PATH", db_path):
                await main.register.callback(interaction, "alice")

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT discord_id, atcoder_handle, channel_id
                    FROM subscriptions
                    """
                ).fetchone()

        self.assertEqual(row, (None, "alice", 101))
        interaction.response.send_message.assert_awaited_once_with(
            "Discordユーザーなしで AtCoderのハンドル 「alice」 を登録しました！\n"
            "ACをした際の通知は このDM に送信されます。"
        )

    async def test_checks_each_channel_once(self):
        channel = channel_with_permissions()
        with (
            patch.object(main.bot, "get_channel", return_value=channel) as get_channel,
            patch.object(main.bot, "fetch_channel", new=AsyncMock()) as fetch_channel,
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels(
                [101, 101]
            )

        self.assertEqual(channels, {101: channel})
        self.assertEqual(retry_channel_ids, set())
        get_channel.assert_called_once_with(101)
        fetch_channel.assert_not_awaited()
        unregister.assert_not_called()

    async def test_deletes_channel_without_send_permission(self):
        channel = channel_with_permissions(send_messages=False)
        with (
            patch.object(main.bot, "get_channel", return_value=channel),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, set())
        unregister.assert_called_once_with(101, "missing_permissions")

    async def test_deletes_channel_not_found_by_discord(self):
        error = discord_error(discord.NotFound, 404)
        with (
            patch.object(main.bot, "get_channel", return_value=None),
            patch.object(
                main.bot,
                "fetch_channel",
                new=AsyncMock(side_effect=error),
            ),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, set())
        unregister.assert_called_once_with(101, error)

    async def test_keeps_registration_after_temporary_error(self):
        error = discord_error(discord.HTTPException, 500)
        with (
            patch.object(main.bot, "get_channel", return_value=None),
            patch.object(
                main.bot,
                "fetch_channel",
                new=AsyncMock(side_effect=error),
            ),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels, retry_channel_ids = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        self.assertEqual(retry_channel_ids, {101})
        unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
