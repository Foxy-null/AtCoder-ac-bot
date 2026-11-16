import asyncio
import configparser
import datetime
import html
import math
import re
import sqlite3
import time
from collections import defaultdict
from urllib.parse import quote, urlencode

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz

from novisteps import get_novisteps_grades

from database import (
    DB_PATH,
    SUBMISSION_LOOKBACK_SECONDS,
    delete_subscriptions,
    delete_subscriptions_for_channel,
    get_subscriptions_for_channels,
    init_db,
)


ATCODER_PROFILE_URL = "https://atcoder.jp/users/{handle}"
ATCODER_AVATAR_URL = "https://img.atcoder.jp/assets/icon/avatar.png"
ATCODER_PROBLEMS_URL = "https://kenkoooo.com/atcoder/resources/problems.json"
ATCODER_DIFFICULTY_URL = "https://kenkoooo.com/atcoder/resources/problem-models.json"
ATCODER_CONTESTS_URL = "https://kenkoooo.com/atcoder/resources/contests.json"
ATCODER_CONTEST_PROBLEMS_URL = "https://kenkoooo.com/atcoder/resources/contest-problem.json"
ATCODER_CONTEST_ARCHIVE_URL = "https://atcoder.jp/contests/archive"
ATCODER_USER_SUBMISSIONS_URL = (
    "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"
    "?user={handle}&from_second={from_second}"
)
ATCODER_SUBMISSION_PAGE_LIMIT = 500
ATCODER_API_DELAY_SECONDS = 1.1
DIFFICULTY_WAIT_SECONDS = 3 * 24 * 60 * 60
CATALOG_RETRY_SECONDS = 5 * 60
UNREGISTER_PAGE_SIZE = 10
UNREGISTER_STATUS = "アプデ: /unregisterで登録解除"
RESULT_EMOJIS = {
    "AC": "<:AC:1537153851843411989>",
    "CE": "<:CE:1537153874048065667>",
    "IE": "<:IE:1537153860894982224>",
    "MLE": "<:MLE:1537153883325857863>",
    "OLE": "<:OLE:1537153863151517777>",
    "RE": "<:RE:1537153876145471580>",
    "TLE": "<:TLE:1537153881300148345>",
    "WA": "<:WA:1537153868134096976>",
}


intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    activity=discord.CustomActivity(name=UNREGISTER_STATUS),
)

config = configparser.ConfigParser()
config.read("config.ini")


def get_db_connection():
    return sqlite3.connect(DB_PATH)


def can_send_notifications(channel):
    if isinstance(channel, discord.DMChannel):
        return True
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


async def fetch_user_submissions_since(session, handle, from_second):
    submissions = {}
    cursor = from_second

    while True:
        url = ATCODER_USER_SUBMISSIONS_URL.format(
            handle=handle,
            from_second=cursor,
        )
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
            # ponytail: the upstream API cannot paginate a full page from one
            # second; keep the cursor unchanged rather than lose data.
            print(
                "提出一覧を安全にページングできません: "
                f"from_second={cursor} count={len(page)}"
            )
            return None

        cursor = next_cursor
        await asyncio.sleep(ATCODER_API_DELAY_SECONDS)


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


def build_difficulty_display(difficulty, grade, problem_id, problem_title):
    parts = []
    if difficulty is not None:
        parts.append(f"diff: {difficulty}")
    if grade is not None:
        value = grade["grade"]
        label = value[1:] + value[0]
        url = f"https://atcoder-novisteps.vercel.app/votes/{quote(problem_id, safe='')}"
        parts.append(
            f"NoviSteps: [{label}]({url})"
            + ("（暫定）" if grade["provisional"] else "")
        )

    if difficulty is not None:
        color = difficulty_to_color(difficulty)
    elif grade is not None:
        value = grade["grade"]
        level = max(0, 3 - int(value[1:])) if value[0] == "Q" else min(7, int(value[1:]) + 2)
        # These boundaries select colors only; grades never become numerical difficulty.
        color = difficulty_to_color(level * 400)
    else:
        color = difficulty_to_color(None)
        star = re.search(r"★\s*([1-7])(?!\d)", problem_title)
        if problem_id.startswith("typical90_") and star:
            color = difficulty_to_color(max(0, int(star[1]) - 2) * 400)
    return " | ".join(parts) if parts else "難易度: 不明", color


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
    for name, url in (
        ("problems", ATCODER_PROBLEMS_URL),
        ("difficulty", ATCODER_DIFFICULTY_URL),
        ("contests", ATCODER_CONTESTS_URL),
        ("contest_problems", ATCODER_CONTEST_PROBLEMS_URL),
    ):
        if name in cache:
            continue
        await asyncio.sleep(ATCODER_API_DELAY_SECONDS)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ValueError(f"status={resp.status}")
                data = await resp.json()
            expected_type = dict if name == "difficulty" else list
            if not isinstance(data, expected_type):
                raise ValueError(f"expected {expected_type.__name__}")
            if name == "problems":
                data = {
                    (problem["contest_id"], problem["id"]): problem["title"]
                    for problem in data
                }
            elif name == "contests":
                for contest in data:
                    if not (
                        isinstance(contest["title"], str)
                        and isinstance(contest["rate_change"], str)
                        and isinstance(contest["start_epoch_second"], int)
                        and isinstance(contest["duration_second"], int)
                    ):
                        raise ValueError("invalid contest information")
                data = {contest["id"]: contest for contest in data}
            elif name == "contest_problems":
                contests_by_problem = defaultdict(set)
                for pair in data:
                    contests_by_problem[pair["problem_id"]].add(pair["contest_id"])
                data = dict(contests_by_problem)
            cache[name] = data
        except Exception as error:
            print(f"問題情報の取得に失敗しました: resource={name} error={error}")
            cache[name] = None


async def get_contest_kind(session, contest, cache):
    contest_id = contest["id"]
    if re.fullmatch(r"(?:abc|arc|agc)\d+", contest_id):
        return "Algorithm"
    if contest_id.startswith("ahc"):
        return "Heuristic"

    kinds = cache.setdefault("contest_kinds", {})
    if contest_id not in kinds:
        query = urlencode({"lang": "en", "keyword": contest["title"]})
        try:
            async with session.get(
                f"{ATCODER_CONTEST_ARCHIVE_URL}?{query}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"status={resp.status}")
                page = await resp.text()
            kind = "Unknown"
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.DOTALL):
                if re.search(
                    rf"href=['\"]/contests/{re.escape(contest_id)}['\"]",
                    row,
                ):
                    match = re.search(r"title=['\"](Algorithm|Heuristic)['\"]", row)
                    if match:
                        kind = match.group(1)
                    break
            kinds[contest_id] = kind
        except Exception as error:
            print(
                f"コンテスト種別の取得に失敗しました: "
                f"contest_id={contest_id} error={error}"
            )
            kinds[contest_id] = None
    return kinds[contest_id]


def should_wait_for_difficulty(model, contest_end_times, current_time):
    # Existing models without difficulty have already been processed upstream.
    return (
        model is None
        and bool(contest_end_times)
        and current_time < min(contest_end_times) + DIFFICULTY_WAIT_SECONDS
    )


async def resolve_problem_difficulty(session, submission, catalog, current_time):
    """Return (ready_to_notify, difficulty); unavailable data is retried next poll."""
    await get_problem_catalog(session, catalog)
    if catalog["difficulty"] is None:
        return False, None

    contest_id = submission["contest_id"]
    problem_id = submission["problem_id"]
    model = catalog["difficulty"].get(problem_id)
    if model is not None and not isinstance(model, dict):
        print(f"問題モデルの形式が不正です: problem_id={problem_id}")
        return False, None
    diff = model.get("difficulty") if model is not None else None
    if diff is not None and (
        isinstance(diff, bool)
        or not isinstance(diff, (int, float))
        or not math.isfinite(diff)
    ):
        print(f"難易度の形式が不正です: problem_id={problem_id}")
        return False, None
    if model is not None and diff is None:
        return True, None
    if catalog["contests"] is None or catalog["contest_problems"] is None:
        return False, None

    contest_ids = catalog["contest_problems"].get(problem_id, set()) | {contest_id}
    catalog_incomplete = (
        contest_id not in catalog["contests"]
        or problem_id not in catalog["contest_problems"]
    )
    source_contests = sorted(
        (
            catalog["contests"][source_id]
            for source_id in contest_ids
            if source_id in catalog["contests"]
        ),
        key=lambda contest: contest["start_epoch_second"] or math.inf,
    )
    contest_end_times = []
    is_algorithm = False
    is_heuristic = contest_id.startswith("ahc")
    for contest in source_contests:
        rated = contest["rate_change"].strip() not in {"", "-"}
        if diff is None and not rated:
            continue
        kind = await get_contest_kind(session, contest, catalog)
        if kind is None:
            return False, None
        if kind == "Unknown":
            catalog_incomplete = True
        elif kind == "Heuristic":
            is_heuristic = True
        elif kind == "Algorithm":
            is_algorithm = True
            if rated:
                contest_end_times.append(
                    contest["start_epoch_second"] + contest["duration_second"]
                )
            if diff is not None:
                break
    if is_heuristic and not is_algorithm:
        diff = None
    # shortcut: five minutes for catalog lag; fetch contest pages if delays grow.
    if (
        model is None
        and catalog_incomplete
        and current_time < submission["epoch_second"] + CATALOG_RETRY_SECONDS
    ):
        return False, None
    if diff is None and should_wait_for_difficulty(model, contest_end_times, current_time):
        return False, None
    return True, diff


async def is_heuristic_problem(session, submission, catalog):
    contest_ids = (catalog["contest_problems"] or {}).get(
        submission["problem_id"], set()
    ) | {submission["contest_id"]}
    heuristic = any(contest_id.startswith("ahc") for contest_id in contest_ids)
    for contest_id in sorted(contest_ids):
        contest = (catalog["contests"] or {}).get(contest_id)
        if contest is None:
            continue
        kind = await get_contest_kind(session, contest, catalog)
        if kind == "Algorithm":
            return False
        if kind == "Heuristic":
            heuristic = True
    return heuristic


async def fetch_problem_title(session, problem_url):
    try:
        async with session.get(problem_url) as resp:
            if resp.status != 200:
                return None
            page = await resp.text()
    except Exception:
        return None

    match = re.search(r"<title>\s*(.*?)\s*</title>", page, re.DOTALL)
    if match is None:
        return None
    return html.unescape(match.group(1)).replace(" - ", ". ", 1)


def build_submission_urls(handle, submission):
    contest_id = submission["contest_id"]
    problem_id = submission["problem_id"]
    submission_id = submission["id"]
    return {
        "submission_url": f"https://atcoder.jp/contests/{contest_id}/submissions/{submission_id}",
        "problem_url": f"https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}",
        "user_url": ATCODER_PROFILE_URL.format(handle=handle),
    }


@bot.tree.command(name="register", description="提出結果の通知を登録します")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.describe(
    user="紐づけるDiscordユーザー（省略可）",
    channel="通知を送信するチャンネル（省略時は現在のチャンネル）",
    atcoder_handle="登録したいAtCoderハンドル",
    notify_wrong_answers="WA・RE・TLEなどの不正解も通知する（省略時はオフ）",
)
async def register(
    interaction: discord.Interaction,
    atcoder_handle: str,
    channel: discord.TextChannel | None = None,
    user: discord.User | None = None,
    notify_wrong_answers: bool = False,
):
    if interaction.guild_id is not None and bot.get_guild(interaction.guild_id) is None:
        await interaction.response.send_message(
            "このサーバーにはBot本体が参加していません。"
            "Botをサーバーに追加してから再試行してください。",
            ephemeral=True,
        )
        return

    channel = channel or interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.DMChannel)):
        await interaction.response.send_message(
            "このチャンネルは通知先に指定できません。",
            ephemeral=True,
        )
        return

    if not can_send_notifications(channel):
        await interaction.response.send_message(
            "Botに通知の送信・埋め込み権限がないため登録できません。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                ATCODER_PROFILE_URL.format(handle=atcoder_handle)
            ) as response:
                profile_status = response.status
    except Exception as error:
        print(f"AtCoderユーザーの確認に失敗しました: error={error}")
        profile_status = None

    if profile_status != 200:
        message = (
            f"AtCoderのハンドル 「{atcoder_handle}」 は存在しません。"
            if profile_status == 404
            else "AtCoderユーザーを確認できませんでした。時間をおいて再試行してください。"
        )
        await interaction.edit_original_response(content=message)
        return

    discord_id = user.id if user else None
    channel_id = channel.id
    current_time = int(time.time())

    conn = get_db_connection()
    c = conn.cursor()
    exists = c.execute(
        "SELECT 1 FROM subscriptions WHERE atcoder_handle = ? AND channel_id = ?",
        (atcoder_handle, channel_id),
    ).fetchone() is not None
    c.execute(
        """
        INSERT INTO subscriptions (
            discord_id,
            atcoder_handle,
            channel_id,
            last_checked_time,
            notify_wrong_answers
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(atcoder_handle, channel_id) DO UPDATE SET
            discord_id = excluded.discord_id,
            notify_wrong_answers = excluded.notify_wrong_answers
        """,
        (
            discord_id,
            atcoder_handle,
            channel_id,
            current_time,
            notify_wrong_answers,
        ),
    )
    conn.commit()
    conn.close()

    destination = (
        channel.mention if isinstance(channel, discord.TextChannel) else "このDM"
    )
    if exists:
        await interaction.edit_original_response(
            content=(
                f"AtCoderのハンドル 「{atcoder_handle}」 の既存登録を更新しました！\n"
                f"Discordユーザー: {user.mention if user else 'なし'}\n"
                f"通知対象: {'ACと不正解' if notify_wrong_answers else 'ACのみ'}\n"
                f"通知先: {destination}"
            )
        )
    else:
        user_text = f"{user.mention} に" if user else "Discordユーザーなしで"
        await interaction.edit_original_response(
            content=(
                f"{user_text} AtCoderのハンドル 「{atcoder_handle}」 を登録しました！\n"
                f"{'ACと不正解' if notify_wrong_answers else 'ACをした際'}の通知は "
                f"{destination} に送信されます。"
            )
        )


class UnregisterConfirmationModal(discord.ui.Modal):
    confirmation = discord.ui.TextInput(
        label="確認のため「全件解除」と入力してください",
        max_length=4,
    )

    def __init__(self, view, subscriptions):
        super().__init__(title="サーバーの全登録を解除")
        self.unregister_view = view
        self.subscriptions = subscriptions

    async def on_submit(self, interaction):
        if self.confirmation.value != "全件解除":
            await interaction.response.send_message(
                "入力が一致しないため解除しませんでした。",
                ephemeral=True,
            )
            return
        await self.unregister_view.delete_confirmed(
            interaction,
            self.subscriptions,
            administrator=True,
        )


class UnregisterView(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=180)
        self.user_id = interaction.user.id
        self.member = interaction.user
        self.guild = interaction.guild
        self.channel_id = interaction.channel.id if self.guild is None else None
        self.mode = "self"
        self.page = 0
        self.selected_id = None
        self.confirmation = None
        self.records = []
        self.reload()

    @property
    def is_manager(self):
        return bool(
            self.guild and self.member.guild_permissions.manage_guild
        )

    def reload(self):
        channel_ids = (
            [self.channel_id]
            if self.guild is None
            else (channel.id for channel in self.guild.channels)
        )
        records = get_subscriptions_for_channels(
            channel_ids
        )
        if self.mode == "self" and self.guild is not None:
            records = [
                record
                for record in records
                if record["discord_id"] == self.user_id
                or (
                    record["discord_id"] is None
                    and self.can_view_channel(record["channel_id"])
                )
            ]
        self.records = records
        self.page = min(self.page, max(0, self.page_count - 1))
        self.selected_id = None
        self.confirmation = None
        self.rebuild_items()

    @property
    def page_count(self):
        return max(1, math.ceil(len(self.records) / UNREGISTER_PAGE_SIZE))

    @property
    def page_records(self):
        start = self.page * UNREGISTER_PAGE_SIZE
        return self.records[start : start + UNREGISTER_PAGE_SIZE]

    def can_view_channel(self, channel_id, member=None):
        if self.guild is None:
            return channel_id == self.channel_id
        channel = self.guild.get_channel(channel_id)
        return bool(
            channel
            and channel.permissions_for(member or self.member).view_channel
        )

    def channel_text(self, channel_id, plain=False):
        if self.guild is None:
            return "このDM"
        channel = self.guild.get_channel(channel_id)
        if self.can_view_channel(channel_id):
            return f"#{channel.name}" if plain else channel.mention
        return "閲覧権限のないチャンネル"

    def owner_text(self, discord_id, plain=False):
        if discord_id is None:
            return "ユーザー未紐づけ"
        member = self.guild.get_member(discord_id)
        if member is None:
            return f"Discord ID: {discord_id}" if plain else f"<@{discord_id}>"
        return member.display_name if plain else member.mention

    def build_embed(self):
        if self.confirmation is not None:
            label, subscriptions, _ = self.confirmation
            return discord.Embed(
                title="登録解除の確認",
                description=(
                    f"{label}（{len(subscriptions)}件）を解除します。\n"
                    "この操作は取り消せません。"
                ),
                color=discord.Color.red(),
            )

        title = (
            "サーバー内の登録管理"
            if self.mode == "admin"
            else "登録解除"
        )
        if not self.records:
            description = "対象の登録はありません。"
        else:
            description = "解除する登録を選択してください。"

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue(),
        )
        for record in self.page_records:
            value = f"通知先: {self.channel_text(record['channel_id'])}"
            if self.mode == "admin":
                value += f"\nユーザー: {self.owner_text(record['discord_id'])}"
            elif record["discord_id"] is None:
                value += "\n共有登録（Discordユーザー未紐づけ）"
            embed.add_field(
                name=record["atcoder_handle"],
                value=value,
                inline=False,
            )
        if self.records:
            embed.set_footer(
                text=(
                    f"{len(self.records)}件 "
                    f"（{self.page + 1}/{self.page_count}ページ）"
                )
            )
        return embed

    def rebuild_items(self):
        self.clear_items()

        if self.confirmation is not None:
            cancel = discord.ui.Button(
                label="戻る",
                style=discord.ButtonStyle.secondary,
            )
            cancel.callback = self.cancel_confirmation
            self.add_item(cancel)

            confirm = discord.ui.Button(
                label="解除を確定",
                style=discord.ButtonStyle.danger,
            )
            confirm.callback = self.confirm_delete
            self.add_item(confirm)
            return

        if self.records:
            options = []
            for record in self.page_records:
                channel = self.channel_text(record["channel_id"], plain=True)
                description = f"通知先: {channel}"
                if self.mode == "admin":
                    owner = self.owner_text(record["discord_id"], plain=True)
                    description += f" / {owner}"
                elif record["discord_id"] is None:
                    description += " / 共有登録"
                options.append(
                    discord.SelectOption(
                        label=record["atcoder_handle"][:100],
                        value=str(record["id"]),
                        description=description[:100],
                    )
                )
            select = discord.ui.Select(
                placeholder="解除する登録を選択",
                options=options,
                row=0,
            )
            select.callback = self.select_subscription
            self.add_item(select)

            previous = discord.ui.Button(
                label="前へ",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
                row=1,
            )
            previous.callback = self.previous_page
            self.add_item(previous)

            following = discord.ui.Button(
                label="次へ",
                style=discord.ButtonStyle.secondary,
                disabled=self.page + 1 >= self.page_count,
                row=1,
            )
            following.callback = self.next_page
            self.add_item(following)

            remove = discord.ui.Button(
                label="選択した登録を解除",
                style=discord.ButtonStyle.danger,
                disabled=self.selected_id is None,
                row=1,
            )
            remove.callback = self.confirm_selected
            self.add_item(remove)

            if self.guild is None or self.mode == "admin" or any(
                record["discord_id"] == self.user_id
                for record in self.records
            ):
                remove_all = discord.ui.Button(
                    label=(
                        "このサーバーの全登録を解除"
                        if self.mode == "admin"
                        else (
                            "このDMの全登録を解除"
                            if self.guild is None
                            else "自分の登録をすべて解除"
                        )
                    ),
                    style=discord.ButtonStyle.danger,
                    row=2,
                )
                remove_all.callback = self.confirm_all
                self.add_item(remove_all)

        if self.mode == "admin":
            back = discord.ui.Button(
                label="自分の登録に戻る",
                style=discord.ButtonStyle.secondary,
                row=3,
            )
            back.callback = self.show_self
            self.add_item(back)
        elif self.is_manager:
            admin = discord.ui.Button(
                label="サーバー内の登録を管理",
                style=discord.ButtonStyle.primary,
                row=3,
            )
            admin.callback = self.show_admin
            self.add_item(admin)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "このメニューは操作できません。",
            ephemeral=True,
        )
        return False

    async def ensure_manager(self, interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "サーバーの管理権限が必要です。",
            ephemeral=True,
        )
        return False

    async def select_subscription(self, interaction):
        self.selected_id = int(interaction.data["values"][0])
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def previous_page(self, interaction):
        self.page -= 1
        self.selected_id = None
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def next_page(self, interaction):
        self.page += 1
        self.selected_id = None
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def show_admin(self, interaction):
        if not await self.ensure_manager(interaction):
            return
        self.mode = "admin"
        self.page = 0
        self.reload()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def show_self(self, interaction):
        self.mode = "self"
        self.page = 0
        self.reload()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def confirm_selected(self, interaction):
        selected = next(
            (
                record
                for record in self.records
                if record["id"] == self.selected_id
            ),
            None,
        )
        if selected is None:
            await interaction.response.send_message(
                "登録が見つかりません。メニューを開き直してください。",
                ephemeral=True,
            )
            return
        if self.mode == "admin" and not await self.ensure_manager(interaction):
            return
        self.confirmation = (
            (
                f"「{selected['atcoder_handle']}」の共有登録"
                if self.mode == "self" and selected["discord_id"] is None
                else f"「{selected['atcoder_handle']}」の登録"
            ),
            [(selected["id"], selected["channel_id"])],
            self.mode == "self" and selected["discord_id"] is None,
        )
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def confirm_all(self, interaction):
        records = self.records
        if self.mode == "self" and self.guild is not None:
            records = [
                record
                for record in records
                if record["discord_id"] == self.user_id
            ]
        subscriptions = [
            (record["id"], record["channel_id"])
            for record in records
        ]
        if self.mode == "admin":
            if not await self.ensure_manager(interaction):
                return
            await interaction.response.send_modal(
                UnregisterConfirmationModal(self, subscriptions)
            )
            return

        self.confirmation = (
            (
                "このDMの登録すべて"
                if self.guild is None
                else "自分の登録すべて"
            ),
            subscriptions,
            False,
        )
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def cancel_confirmation(self, interaction):
        self.confirmation = None
        self.rebuild_items()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def confirm_delete(self, interaction):
        _, subscriptions, unlinked = self.confirmation
        await self.delete_confirmed(
            interaction,
            subscriptions,
            administrator=self.mode == "admin",
            unlinked=unlinked,
        )

    async def delete_confirmed(
        self,
        interaction,
        subscriptions,
        administrator,
        unlinked=False,
    ):
        if administrator and not await self.ensure_manager(interaction):
            return
        if unlinked and not all(
            self.can_view_channel(channel_id, interaction.user)
            for _, channel_id in subscriptions
        ):
            self.reload()
            embed = self.build_embed()
            embed.description = "通知先チャンネルの閲覧権限が必要です。"
            await interaction.response.edit_message(embed=embed, view=self)
            return
        records = {
            (record["id"], record["channel_id"]): record
            for record in self.records
        }
        deleted = delete_subscriptions(
            subscriptions,
            discord_id=(
                None
                if administrator or self.guild is None
                else self.user_id
            ),
            unlinked_only=unlinked,
        )
        if unlinked and deleted:
            record = records[subscriptions[0]]
            print(
                "共有登録を解除しました: "
                f"actor_id={interaction.user.id} "
                f"atcoder_handle={record['atcoder_handle']} "
                f"channel_id={record['channel_id']}"
            )
        self.reload()
        embed = self.build_embed()
        embed.description = f"{deleted}件の登録を解除しました。"
        await interaction.response.edit_message(embed=embed, view=self)
        if deleted:
            if len(subscriptions) == 1:
                handle = records[subscriptions[0]]["atcoder_handle"]
                message = (
                    f"{interaction.user.mention} が AtCoderのハンドル "
                    f"「{handle}」の登録を解除しました。"
                )
            else:
                message = (
                    f"{interaction.user.mention} がAC通知の登録を"
                    f"{deleted}件解除しました。"
                )
            await interaction.followup.send(message, ephemeral=False)


@bot.tree.command(
    name="unregister",
    description="AC通知の登録を解除します",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
async def unregister(interaction: discord.Interaction):
    view = UnregisterView(interaction)
    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )


@tasks.loop(minutes=1)
async def check_ac_submissions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, discord_id, atcoder_handle, channel_id, last_submission_id,
               last_checked_time, notify_wrong_answers
        FROM subscriptions
        """
    )
    rows = c.fetchall()
    notified_submissions = set(
        c.execute(
            "SELECT subscription_id, submission_id FROM notified_submissions"
        ).fetchall()
    )
    conn.close()

    if not rows:
        return

    channels, _ = await get_accessible_channels(
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
            "notify_wrong_answers": bool(row[6]),
        }
        subscriptions_by_handle[subscription["atcoder_handle"].lower()].append(
            subscription
        )

    async with aiohttp.ClientSession() as session:
        catalog_cache = {}
        submission_cache = {}
        current_time = int(time.time())
        novisteps_grades = await get_novisteps_grades(session, DB_PATH, current_time)

        async def get_submission_metadata(submission):
            submission_id = submission["id"]
            if submission_id in submission_cache:
                return submission_cache[submission_id]
            submission_cache[submission_id] = None

            ready, diff = await resolve_problem_difficulty(
                session, submission, catalog_cache, current_time
            )
            if not ready:
                return None
            contest_id = submission["contest_id"]
            problem_id = submission["problem_id"]
            problem_key = (contest_id, problem_id)
            urls = build_submission_urls(submission["atcoder_handle"], submission)
            problem_title = (catalog_cache["problems"] or {}).get(problem_key)
            if problem_title is None:
                problem_title = (
                    await fetch_problem_title(session, urls["problem_url"])
                    or f"{contest_id} {problem_id}"
                )

            if diff is not None and diff <= 400:
                diff = math.ceil(400.0 / math.exp((400.0 - diff) / 400.0))

            grade = novisteps_grades.get(problem_id)
            if grade is not None and diff is None and await is_heuristic_problem(
                session, submission, catalog_cache
            ):
                grade = None
            difficulty_text, color = build_difficulty_display(
                diff, grade, problem_id, problem_title
            )
            cached = {
                "title": problem_title,
                "difficulty_text": difficulty_text,
                "color": color,
                **urls,
            }
            submission_cache[submission_id] = cached
            return cached

        # ponytail: 10-day reconciliation window; extend it if upstream lag
        # ever exceeds the window.
        lookback_start = current_time - SUBMISSION_LOOKBACK_SECONDS
        for handle, subscriptions in subscriptions_by_handle.items():
            from_second = max(
                lookback_start,
                min(
                    subscription["last_checked_time"] or lookback_start
                    for subscription in subscriptions
                ),
            )
            handle_submissions = await fetch_user_submissions_since(
                session,
                handle,
                from_second,
            )
            if handle_submissions is None:
                continue
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
                notification_start = max(
                    lookback_start,
                    subscription["last_checked_time"] or lookback_start,
                )

                for submission in handle_submissions:
                    submission_time = submission["epoch_second"]
                    if submission_time < notification_start:
                        continue

                    result = submission["result"]
                    if result != "AC" and (
                        not subscription["notify_wrong_answers"]
                        or result in {"WJ", "WR"}
                    ):
                        continue

                    if (
                        last_submission_id is not None
                        and submission["id"] <= last_submission_id
                    ):
                        continue
                    notification_key = (
                        subscription["id"],
                        submission["id"],
                    )
                    if notification_key in notified_submissions:
                        continue

                    metadata = await get_submission_metadata(
                        {
                            **submission,
                            "atcoder_handle": subscription["atcoder_handle"],
                        }
                    )
                    if metadata is None:
                        continue
                    author_name, avatar_url, author_url = await resolve_author(
                        session,
                        subscription["atcoder_handle"],
                        discord_id,
                        atcoder_profile,
                    )
                    embed = discord.Embed(
                        title=f"{metadata['title']} {RESULT_EMOJIS.get(result, result)}",
                        url=metadata["problem_url"],
                        description=f"[🔎提出]({metadata['submission_url']}) | "
                        + metadata["difficulty_text"],
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
                            f" | {submission['language']}"
                        )
                    )
                    try:
                        await channel.send(embed=embed)
                    except (discord.Forbidden, discord.NotFound) as error:
                        unregister_channel(channel_id, error)
                        channels.pop(channel_id, None)
                        break
                    except Exception as error:
                        print(
                            f"メッセージ送信時にエラーが発生しました: "
                            f"channel_id={channel_id} error={error}"
                        )
                        break

                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        """
                        INSERT OR IGNORE INTO notified_submissions (
                            subscription_id,
                            submission_id
                        )
                        VALUES (?, ?)
                        """,
                        notification_key,
                    )
                    conn.commit()
                    conn.close()
                    notified_submissions.add(notification_key)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    if not check_ac_submissions.is_running():
        check_ac_submissions.start()


if __name__ == "__main__":
    init_db()
    bot.run(config["DISCORD"]["TOKEN"])
