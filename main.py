import asyncio
import configparser
import datetime
import math
import re
import sqlite3
import time
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz

from database import (
    DB_PATH,
    delete_subscriptions_for_channel,
    get_submission_poll_time,
    init_db,
    set_submission_poll_time,
)


ATCODER_PROFILE_URL = "https://atcoder.jp/users/{handle}"
ATCODER_AVATAR_URL = "https://img.atcoder.jp/assets/icon/avatar.png"
ATCODER_PROBLEMS_URL = "https://kenkoooo.com/atcoder/resources/problems.json"
ATCODER_DIFFICULTY_URL = "https://kenkoooo.com/atcoder/resources/problem-models.json"
ATCODER_SUBMISSIONS_URL = (
    "https://kenkoooo.com/atcoder/atcoder-api/v3/from/{from_second}"
)
ATCODER_SUBMISSION_PAGE_LIMIT = 1000
ATCODER_API_DELAY_SECONDS = 1.1


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

config = configparser.ConfigParser()
config.read("config.ini")


def get_db_connection():
    return sqlite3.connect(DB_PATH)


def can_send_notifications(channel):
    permissions = channel.permissions_for(channel.guild.me)
    return (
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
    )


def unregister_channel(channel_id, reason):
    deleted = delete_subscriptions_for_channel(channel_id)
    print(
        f"通知先を登録解除しました: channel_id={channel_id} "
        f"deleted={deleted} reason={reason}"
    )


async def get_accessible_channels(channel_ids):
    channels = {}
    retry_channel_ids = set()
    for channel_id in set(channel_ids):
        try:
            channel = bot.get_channel(channel_id)
            if channel is None:
                channel = await bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound) as error:
            unregister_channel(channel_id, error)
            continue
        except Exception as error:
            print(
                f"通知先の確認に失敗しました: "
                f"channel_id={channel_id} error={error}"
            )
            retry_channel_ids.add(channel_id)
            continue

        if not can_send_notifications(channel):
            unregister_channel(channel_id, "missing_permissions")
            continue

        channels[channel_id] = channel
    return channels, retry_channel_ids


async def fetch_submissions_since(session, from_second):
    submissions = {}
    cursor = from_second

    while True:
        url = ATCODER_SUBMISSIONS_URL.format(from_second=cursor)
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"提出一覧の取得に失敗しました: status={response.status}")
                    return None
                page = await response.json()
        except Exception as error:
            print(f"提出一覧の取得に失敗しました: error={error}")
            return None

        for submission in page:
            submissions[submission["id"]] = submission

        if len(page) < ATCODER_SUBMISSION_PAGE_LIMIT:
            ordered = sorted(
                submissions.values(),
                key=lambda submission: (
                    submission["epoch_second"],
                    submission["id"],
                ),
            )
            return ordered

        next_cursor = max(submission["epoch_second"] for submission in page)
        if next_cursor <= cursor:
            # ponytail: the upstream API cannot paginate over 1000 submissions
            # in one second; keep the cursor unchanged rather than lose data.
            print(
                "提出一覧を安全にページングできません: "
                f"from_second={cursor} count={len(page)}"
            )
            return None

        cursor = next_cursor
        await asyncio.sleep(ATCODER_API_DELAY_SECONDS)


def group_registered_submissions(submissions, handles):
    registered = {handle.lower() for handle in handles}
    grouped = defaultdict(list)
    for submission in submissions:
        handle = submission["user_id"].lower()
        if handle in registered:
            grouped[handle].append(submission)
    return grouped


def difficulty_to_color(difficulty):
    if difficulty is None:
        return discord.Color.from_rgb(0, 0, 0)
    if difficulty < 0:
        return discord.Color.from_rgb(0, 0, 0)
    if difficulty < 400:
        return discord.Color.from_rgb(128, 128, 128)
    if difficulty < 800:
        return discord.Color.from_rgb(128, 64, 0)
    if difficulty < 1200:
        return discord.Color.from_rgb(36, 128, 36)
    if difficulty < 1600:
        return discord.Color.from_rgb(0, 192, 192)
    if difficulty < 2000:
        return discord.Color.from_rgb(54, 54, 252)
    if difficulty < 2400:
        return discord.Color.from_rgb(192, 192, 0)
    if difficulty < 2800:
        return discord.Color.from_rgb(255, 128, 0)
    return discord.Color.from_rgb(252, 54, 54)


async def fetch_atcoder_profile(session, handle):
    avatar_url = ATCODER_AVATAR_URL
    try:
        async with session.get(ATCODER_PROFILE_URL.format(handle=handle)) as resp:
            if resp.status != 200:
                return {"display_name": handle, "avatar_url": avatar_url}
            html = await resp.text()
    except Exception:
        return {"display_name": handle, "avatar_url": avatar_url}

    match = re.search(
        r"""<img class=['"]avatar['"] src=['"]([^'"]+)['"]""",
        html,
    )
    if match:
        avatar_url = match.group(1)
        if avatar_url.startswith("//"):
            avatar_url = f"https:{avatar_url}"

    return {"display_name": handle, "avatar_url": avatar_url}


async def resolve_author(session, handle, discord_id, atcoder_profile):
    user_url = ATCODER_PROFILE_URL.format(handle=handle)

    if discord_id is not None:
        try:
            discord_user = await bot.fetch_user(discord_id)
            return discord_user.name, discord_user.display_avatar.url, user_url
        except Exception:
            pass

    if atcoder_profile is None:
        atcoder_profile = await fetch_atcoder_profile(session, handle)

    return (
        atcoder_profile["display_name"],
        atcoder_profile["avatar_url"],
        user_url,
    )


async def get_problem_catalog(session, cache):
    if cache["problems"] is not None and cache["difficulty"] is not None:
        return

    if cache["problems"] is None:
        try:
            async with session.get(ATCODER_PROBLEMS_URL) as resp:
                if resp.status == 200:
                    problems = await resp.json()
                    cache["problems"] = {
                        (problem["contest_id"], problem["id"]): problem["title"]
                        for problem in problems
                    }
                else:
                    cache["problems"] = {}
        except Exception:
            cache["problems"] = {}

    if cache["difficulty"] is None:
        try:
            async with session.get(ATCODER_DIFFICULTY_URL) as resp:
                if resp.status == 200:
                    cache["difficulty"] = await resp.json()
                else:
                    cache["difficulty"] = {}
        except Exception:
            cache["difficulty"] = {}


def build_submission_urls(handle, submission):
    contest_id = submission["contest_id"]
    problem_id = submission["problem_id"]
    submission_id = submission["id"]
    return {
        "submission_url": f"https://atcoder.jp/contests/{contest_id}/submissions/{submission_id}",
        "problem_url": f"https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}",
        "user_url": ATCODER_PROFILE_URL.format(handle=handle),
    }


@bot.tree.command(name="register", description="問題をACした際の通知を登録します")
@app_commands.describe(
    user="紐づけるDiscordユーザー（省略可）",
    channel="通知を送信するチャンネル",
    atcoder_handle="登録したいAtCoderハンドル",
)
async def register(
    interaction: discord.Interaction,
    atcoder_handle: str,
    channel: discord.TextChannel,
    user: discord.User | None = None,
):
    if not can_send_notifications(channel):
        await interaction.response.send_message(
            "Botに通知の送信・埋め込み権限がないため登録できません。",
            ephemeral=True,
        )
        return

    discord_id = user.id if user else None
    channel_id = channel.id
    current_time = int(time.time())

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO subscriptions (
            discord_id,
            atcoder_handle,
            channel_id,
            last_checked_time
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(atcoder_handle, channel_id) DO UPDATE SET
            discord_id = excluded.discord_id
        """,
        (discord_id, atcoder_handle, channel_id, current_time),
    )
    conn.commit()
    conn.close()

    if user:
        user_text = f"{user.mention} に"
    else:
        user_text = "Discordユーザーなしで"

    await interaction.response.send_message(
        f"{user_text} AtCoderのハンドル 「{atcoder_handle}」 を登録しました！\n"
        f"ACをした際の通知は {channel.mention} に送信されます。"
    )


@tasks.loop(minutes=1)
async def check_ac_submissions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, discord_id, atcoder_handle, channel_id, last_submission_id, last_checked_time
        FROM subscriptions
        """
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    channels, retry_channel_ids = await get_accessible_channels(
        row[3] for row in rows
    )
    rows = [row for row in rows if row[3] in channels]
    if not rows:
        return

    subscriptions_by_handle = defaultdict(list)
    for row in rows:
        subscription = {
            "id": row[0],
            "discord_id": row[1],
            "atcoder_handle": row[2],
            "channel_id": row[3],
            "last_submission_id": row[4],
            "last_checked_time": row[5],
        }
        subscriptions_by_handle[subscription["atcoder_handle"].lower()].append(
            subscription
        )

    async with aiohttp.ClientSession() as session:
        catalog_cache = {"problems": None, "difficulty": None}
        submission_cache = {}

        async def get_submission_metadata(submission):
            submission_id = submission["id"]
            cached = submission_cache.get(submission_id)
            if cached is not None:
                return cached

            await get_problem_catalog(session, catalog_cache)
            contest_id = submission["contest_id"]
            problem_id = submission["problem_id"]
            problem_title = catalog_cache["problems"].get(
                (contest_id, problem_id),
                f"{contest_id} {problem_id}",
            )

            problem_difficulty = catalog_cache["difficulty"].get(problem_id)
            difficulty = 0
            if (
                problem_difficulty
                and isinstance(problem_difficulty, dict)
                and "difficulty" in problem_difficulty
            ):
                diff = problem_difficulty["difficulty"]
                if diff is not None and diff <= 400:
                    diff = int(400.0 / math.exp((400.0 - diff) / 400.0))
                difficulty = diff if diff is not None else 0

            cached = {
                "title": problem_title,
                "difficulty": difficulty,
                "color": difficulty_to_color(difficulty),
                **build_submission_urls(submission["atcoder_handle"], submission),
            }
            submission_cache[submission_id] = cached
            return cached

        from_second = get_submission_poll_time()
        submissions = await fetch_submissions_since(session, from_second)
        if submissions is None:
            return
        submissions_by_handle = group_registered_submissions(
            submissions,
            subscriptions_by_handle,
        )
        batch_failed = bool(retry_channel_ids)

        for handle, subscriptions in subscriptions_by_handle.items():
            handle_submissions = submissions_by_handle.get(handle, ())
            if not handle_submissions:
                continue
            atcoder_profile = None
            if any(subscription["discord_id"] is None for subscription in subscriptions):
                atcoder_profile = await fetch_atcoder_profile(
                    session,
                    subscriptions[0]["atcoder_handle"],
                )

            for subscription in subscriptions:
                discord_id = subscription["discord_id"]
                channel_id = subscription["channel_id"]
                channel = channels.get(channel_id)
                if channel is None:
                    continue
                last_submission_id = subscription["last_submission_id"]
                last_checked_time = subscription["last_checked_time"]
                new_latest_time = last_checked_time
                delivery_failed = False

                for submission in handle_submissions:
                    submission_time = submission["epoch_second"]
                    if (
                        last_checked_time is not None
                        and submission_time < last_checked_time
                    ):
                        continue
                    if new_latest_time is None or submission_time > new_latest_time:
                        new_latest_time = submission_time

                    if submission["result"] != "AC":
                        continue

                    if (
                        last_submission_id is not None
                        and submission["id"] <= last_submission_id
                    ):
                        continue

                    metadata = await get_submission_metadata(
                        {
                            **submission,
                            "atcoder_handle": subscription["atcoder_handle"],
                        }
                    )
                    author_name, avatar_url, author_url = await resolve_author(
                        session,
                        subscription["atcoder_handle"],
                        discord_id,
                        atcoder_profile,
                    )
                    diff_text = (
                        f"diff: {metadata['difficulty']}"
                        if metadata["difficulty"] is not None
                        else "diff: 判定不可"
                    )
                    embed = discord.Embed(
                        title=metadata["title"] + " <:AC_bot:1342654382277398700>",
                        url=metadata["problem_url"],
                        description=f"[🔎提出]({metadata['submission_url']}) | "
                        + diff_text
                        + f" | {submission['language']}",
                        color=metadata["color"],
                    )
                    embed.set_author(
                        name=author_name,
                        url=author_url,
                        icon_url=avatar_url,
                    )
                    embed.set_footer(
                        text=(
                            "提出日時: "
                            f"{datetime.datetime.fromtimestamp(submission_time, pytz.timezone('Asia/Tokyo')).strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                    )
                    try:
                        await channel.send(embed=embed)
                    except (discord.Forbidden, discord.NotFound) as error:
                        unregister_channel(channel_id, error)
                        channels.pop(channel_id, None)
                        delivery_failed = True
                        break
                    except Exception as error:
                        print(
                            f"メッセージ送信時にエラーが発生しました: "
                            f"channel_id={channel_id} error={error}"
                        )
                        delivery_failed = True
                        batch_failed = True
                        break

                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        """
                        UPDATE subscriptions
                        SET last_submission_id = ?, last_checked_time = ?
                        WHERE id = ?
                        """,
                        (submission["id"], submission_time, subscription["id"]),
                    )
                    conn.commit()
                    conn.close()

                    last_submission_id = submission["id"]
                    last_checked_time = submission_time

                if (
                    not delivery_failed
                    and new_latest_time is not None
                    and new_latest_time != last_checked_time
                ):
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        """
                        UPDATE subscriptions
                        SET last_checked_time = ?
                        WHERE id = ?
                        """,
                        (new_latest_time, subscription["id"]),
                    )
                    conn.commit()
                    conn.close()

        if not batch_failed and submissions:
            set_submission_poll_time(submissions[-1]["epoch_second"])


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    if not check_ac_submissions.is_running():
        check_ac_submissions.start()


if __name__ == "__main__":
    init_db()
    bot.run(config["DISCORD"]["TOKEN"])
