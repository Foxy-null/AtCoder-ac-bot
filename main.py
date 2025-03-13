import sqlite3

conn = sqlite3.connect("bot.db")
c = conn.cursor()
c.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        atcoder_handle TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        last_submission_id INTEGER,
        last_checked_time INTEGER
    )
"""
)
conn.commit()
conn.close()

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import sqlite3
import configparser
import time
import math

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

config = configparser.ConfigParser()
config.read("config.ini")


# データベース接続用
def get_db_connection():
    conn = sqlite3.connect("bot.db")  # ./bot.dbにアクセスするよ
    return conn


# AtCoderハンドルでユーザーを登録するやつ
@bot.tree.command(name="register", description="問題をACした際の通知を登録します")
@app_commands.describe(
    user="紐づけるDiscordユーザー",
    channel="通知を送信するチャンネル",
    atcoder_handle="登録したいAtCoderハンドル",
)
async def register(
    interaction: discord.Interaction,
    user: discord.User,
    atcoder_handle: str,
    channel: discord.TextChannel,
):
    discord_id = user.id
    channel_id = channel.id
    current_time = int(time.time())

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT OR REPLACE INTO users 
        (discord_id, atcoder_handle, channel_id, last_checked_time) 
        VALUES (?, ?, ?, ?)
    """,
        (discord_id, atcoder_handle, channel_id, current_time),
    )
    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"{user.mention} に AtCoderのハンドル 「{atcoder_handle}」 を紐づけて登録しました！\nACをした際の通知は {channel.mention} に送信されます。"
    )


# 毎分ACをチェックするタスク
@tasks.loop(minutes=1)
async def check_ac_submissions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT discord_id, atcoder_handle, channel_id, last_submission_id, last_checked_time FROM users"
    )
    users = c.fetchall()
    conn.close()

    async with aiohttp.ClientSession() as session:
        current_time = int(time.time())
        for user in users:
            discord_id, handle, channel_id, last_submission_id, last_checked_time = user
            from_second = (
                last_checked_time if last_checked_time else current_time - 60
            )  # 初回は1分前から

            url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={handle}&from_second={from_second}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"Failed to fetch submissions for {handle}")
                    print(f"API Error: {resp.status}")
                    continue
                submissions = await resp.json()

                # デバッグ↓
                # print(f"Successfully fetched submissions for {handle}. Status: {resp.status}")

            new_latest_time = last_checked_time

            # 最新のACのみ探す
            for submission in submissions:
                submission_time = submission["epoch_second"]
                if submission["result"] == "AC":
                    if (
                        last_submission_id is None
                        or submission["id"] > last_submission_id
                    ):
                        print(f"New AC submission found for {handle}")
                        contest_id = submission["contest_id"]
                        problem_id = submission["problem_id"]
                        language = submission["language"]

                        # 問題タイトルの取得
                        problem_api_url = (
                            f"https://kenkoooo.com/atcoder/resources/problems.json"
                        )
                        async with session.get(problem_api_url) as pr_resp:
                            if pr_resp.status != 200:
                                print(f"Failed to fetch problem info for {problem_id}")
                                continue
                            problems = await pr_resp.json()
                            problem = next(
                                (
                                    p
                                    for p in problems
                                    if p["contest_id"] == contest_id
                                    and p["id"] == problem_id
                                ),
                                None,
                            )

                            # 「[記号] - [問題名]」的なやつを取得する
                            title = (
                                problem["title"]
                                if problem
                                else f"{contest_id} {problem_id}"
                            )

                        # 提出URL
                        submission_id = submission["id"]
                        last_submission_id = submission_id
                        submission_url = f"https://atcoder.jp/contests/{contest_id}/submissions/{submission_id}"
                        problem_url = f"https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}"
                        user_url = f"https://atcoder.jp/users/{handle}"

                        # メッセージの送信
                        channel = bot.get_channel(channel_id)
                        difficulty = None

                        # Diff情報を取得するAPIからデータを取得
                        difficulty_api_url = (
                            "https://kenkoooo.com/atcoder/resources/problem-models.json"
                        )
                        async with session.get(difficulty_api_url) as diff_resp:
                            if diff_resp.status == 200:
                                difficulties = await diff_resp.json()
                                problem_difficulty = difficulties.get(problem_id)
                                if (
                                    problem_difficulty
                                    and "difficulty" in problem_difficulty
                                ):
                                    diff = problem_difficulty["difficulty"]
                                    if diff <= 400:
                                        diff = int(
                                            400.0 / math.exp((400.0 - diff) / 400.0)
                                        )
                                    difficulty = diff
                                else:
                                    difficulty = 0

                        # Embedの色を難易度に応じて決定
                        if difficulty is not None:
                            if difficulty < 0:
                                color = discord.Color.from_rgb(0, 0, 0)
                            if difficulty < 400:
                                color = discord.Color.from_rgb(128, 128, 128)
                            elif difficulty < 800:
                                color = discord.Color.from_rgb(128, 64, 0)
                            elif difficulty < 1200:
                                color = discord.Color.from_rgb(36, 128, 36)
                            elif difficulty < 1600:
                                color = discord.Color.from_rgb(0, 192, 192)
                            elif difficulty < 2000:
                                color = discord.Color.from_rgb(54, 54, 252)
                            elif difficulty < 2400:
                                color = discord.Color.from_rgb(192, 192, 0)
                            elif difficulty < 2800:
                                color = discord.Color.from_rgb(255, 128, 0)
                            else:
                                color = discord.Color.from_rgb(252, 54, 54)
                        else:
                            color = discord.Color.from_rgb(0, 0, 0)

                        if channel:
                            user = await bot.fetch_user(discord_id)
                            avatar_url = user.display_avatar.url

                            diff_text = (
                                f"diff: {difficulty}"
                                if difficulty is not None
                                else "diff: 判定不可"
                            )

                            embed = discord.Embed(
                                title=title + " <:AC_bot:1342654382277398700>",
                                url=problem_url,
                                description=f"[🔎提出]({submission_url}) | "
                                + diff_text
                                + f" | {language}",
                                color=color,
                            )
                            embed.set_author(
                                name=user.name,
                                url=user_url,
                                icon_url=avatar_url,
                            )
                            try:
                                await channel.send(embed=embed)
                            except Exception as e:
                                print(f"メッセージ送信時にエラーが発生しました: {e}")

                        # 最後の提出IDを更新
                        conn = get_db_connection()
                        c = conn.cursor()
                        c.execute(
                            """
                            UPDATE users 
                            SET last_submission_id = ?, last_checked_time = ?
                            WHERE discord_id = ?
                        """,
                            (submission_id, submission_time, discord_id),
                        )
                        conn.commit()
                        conn.close()
                        # break  # 最新のACだけ通知

                # `from_second` を更新
                if new_latest_time is None or submission_time > new_latest_time:
                    new_latest_time = submission_time

            # AC以外の提出でも `last_checked_time` を更新する
            if new_latest_time:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute(
                    """
                    UPDATE users 
                    SET last_checked_time = ?
                    WHERE discord_id = ?
                """,
                    (new_latest_time, discord_id),
                )
                conn.commit()
                conn.close()


# botが準備完了したときにタスクを開始
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    check_ac_submissions.start()


bot.run(config["DISCORD"]["TOKEN"])
