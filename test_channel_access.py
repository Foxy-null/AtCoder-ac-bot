import unittest
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
    async def test_checks_each_channel_once(self):
        channel = channel_with_permissions()
        with (
            patch.object(main.bot, "get_channel", return_value=channel) as get_channel,
            patch.object(main.bot, "fetch_channel", new=AsyncMock()) as fetch_channel,
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels = await main.get_accessible_channels([101, 101])

        self.assertEqual(channels, {101: channel})
        get_channel.assert_called_once_with(101)
        fetch_channel.assert_not_awaited()
        unregister.assert_not_called()

    async def test_deletes_channel_without_send_permission(self):
        channel = channel_with_permissions(send_messages=False)
        with (
            patch.object(main.bot, "get_channel", return_value=channel),
            patch.object(main, "unregister_channel") as unregister,
        ):
            channels = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
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
            channels = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
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
            channels = await main.get_accessible_channels([101])

        self.assertEqual(channels, {})
        unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
