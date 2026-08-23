import asyncio
import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

BOT_NAME = os.getenv(
    "BOT_NAME",
    "Global X Bypass"
).strip()

BASE_URL = os.getenv(
    "BASE_URL",
    "http://92.118.206.166:30022/api"
).strip().rstrip("/")

API_KEY = os.getenv(
    "API_KEY",
    ""
).strip()

LOG_CHANNEL_ID = int(
    os.getenv(
        "LOG_CHANNEL_ID",
        "0"
    ).strip() or 0
)


# ============================================================
# DISCORD BOT TOKENS
# ============================================================

def load_bot_tokens():
    """
    Reads BOT_TOKENS from Render.

    Recommended:
        BOT_TOKENS=TOKEN1,TOKEN2,TOKEN3

    Also supports:
        BOT_TOKEN=TOKEN

    Whitespace and accidental surrounding quotes are removed.
    """

    raw = os.getenv(
        "BOT_TOKENS",
        ""
    ).strip()

    if not raw:
        raw = os.getenv(
            "BOT_TOKEN",
            ""
        ).strip()

    if not raw:
        return []

    tokens = []

    for token in raw.split(","):
        token = token.strip()

        # Remove accidental quotes from Render
        if (
            len(token) >= 2
            and token[0] == token[-1]
            and token[0] in ("'", '"')
        ):
            token = token[1:-1].strip()

        if token:
            tokens.append(token)

    return tokens


BOT_TOKENS = load_bot_tokens()


# ============================================================
# OWNERS
# ============================================================

OWNER_IDS = {
    768020734231969793,
    1190844956395446397,
}

# Discord role whose members are allowed to use the bot.
# This can be overridden in Render with OWNER_ROLE_ID.
OWNER_ROLE_ID = int(
    os.getenv(
        "OWNER_ROLE_ID",
        "1541084686934347806"
    ).strip() or 1541084686934347806
)

# Every member with OWNER_ROLE_ID starts with this many credits.
DEFAULT_OWNER_CREDITS = 100


# ============================================================
# UID SETTINGS
# ============================================================

MAX_DAYS = 30

# Default value shown in the UID modal.
DEFAULT_DAYS = 30

# Cost for adding a UID.
ADD_UID_COST = 1

# Removing UID is free.
REMOVE_UID_COST = 0


# ============================================================
# DATABASE
# ============================================================

DATA_FILE = Path(
    os.getenv(
        "DATA_FILE",
        Path(__file__).resolve().with_name(
            "resellers.json"
        )
    )
)

db_lock = asyncio.Lock()

db = {
    "resellers": {},
    "owner_accounts": {}
}


def load_database():
    global db

    if not DATA_FILE.exists():
        db = {
            "resellers": {},
            "owner_accounts": {}
        }
        return

    try:
        with DATA_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            data = {}

        if not isinstance(
            data.get("resellers"),
            dict
        ):
            data["resellers"] = {}

        if not isinstance(
            data.get("owner_accounts"),
            dict
        ):
            data["owner_accounts"] = {}

        db = data

        print(
            f"Database loaded: "
            f"{len(db['resellers'])} resellers"
        )

    except Exception as error:
        print(
            f"Database load error: {error}"
        )

        db = {
            "resellers": {},
            "owner_accounts": {}
        }


load_database()


def write_database_locked():
    """
    Atomic database write.

    Caller must hold db_lock.
    """

    temporary_file = DATA_FILE.with_suffix(
        ".tmp"
    )

    with temporary_file.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            db,
            file,
            indent=4,
            ensure_ascii=False
        )

        file.flush()

        try:
            os.fsync(
                file.fileno()
            )
        except OSError:
            pass

    temporary_file.replace(
        DATA_FILE
    )


async def add_reseller_record(
    user_id,
    username
):

    async with db_lock:

        db["resellers"][
            str(user_id)
        ] = {
            "username": username,
            "credits": 0,
            "created_at":
                datetime.now(
                    timezone.utc
                ).isoformat()
        }

        write_database_locked()


async def delete_reseller_record(
    user_id
):

    async with db_lock:

        old = db[
            "resellers"
        ].pop(
            str(user_id),
            None
        )

        if old is not None:
            write_database_locked()

        return old


async def add_credits_record(
    user_id,
    amount
):

    async with db_lock:

        reseller = db[
            "resellers"
        ].get(
            str(user_id)
        )

        if not reseller:
            return None

        old_balance = int(
            reseller.get(
                "credits",
                0
            )
        )

        new_balance = (
            old_balance + amount
        )

        reseller[
            "credits"
        ] = new_balance

        write_database_locked()

        return (
            old_balance,
            new_balance
        )


async def reserve_credit(
    user_id
):

    async with db_lock:

        reseller = db[
            "resellers"
        ].get(
            str(user_id)
        )

        if not reseller:
            return None

        credits = int(
            reseller.get(
                "credits",
                0
            )
        )

        if credits < ADD_UID_COST:
            return None

        reseller[
            "credits"
        ] = (
            credits - ADD_UID_COST
        )

        write_database_locked()

        return reseller[
            "credits"
        ]


async def refund_credit(
    user_id
):

    async with db_lock:

        reseller = db[
            "resellers"
        ].get(
            str(user_id)
        )

        if not reseller:
            return None

        reseller[
            "credits"
        ] = int(
            reseller.get(
                "credits",
                0
            )
        ) + ADD_UID_COST

        write_database_locked()

        return reseller[
            "credits"
        ]


async def ensure_owner_account(
    user_id,
    username
):

    async with db_lock:

        key = str(user_id)
        account = db["owner_accounts"].get(key)

        if account is None:
            db["owner_accounts"][key] = {
                "username": username,
                "credits": DEFAULT_OWNER_CREDITS,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            write_database_locked()
            return DEFAULT_OWNER_CREDITS

        # Preserve an existing balance. Only fill missing fields.
        changed = False
        if "username" not in account:
            account["username"] = username
            changed = True
        if "credits" not in account:
            account["credits"] = DEFAULT_OWNER_CREDITS
            changed = True
        if changed:
            write_database_locked()

        return int(account.get("credits", DEFAULT_OWNER_CREDITS))


def get_owner_account(user_id):
    return db["owner_accounts"].get(str(user_id))


async def add_owner_credits_record(
    user_id,
    amount,
    username=None
):

    async with db_lock:

        key = str(user_id)
        account = db["owner_accounts"].get(key)

        if account is None:
            account = {
                "username": username or str(user_id),
                "credits": DEFAULT_OWNER_CREDITS,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            db["owner_accounts"][key] = account

        old_balance = int(account.get("credits", DEFAULT_OWNER_CREDITS))
        new_balance = old_balance + amount
        account["credits"] = new_balance

        if username:
            account["username"] = username

        write_database_locked()
        return old_balance, new_balance


async def reserve_owner_credit(user_id):

    async with db_lock:

        account = db["owner_accounts"].get(str(user_id))
        if not account:
            return None

        credits = int(account.get("credits", DEFAULT_OWNER_CREDITS))
        if credits < ADD_UID_COST:
            return None

        account["credits"] = credits - ADD_UID_COST
        write_database_locked()
        return account["credits"]


async def refund_owner_credit(user_id):

    async with db_lock:

        account = db["owner_accounts"].get(str(user_id))
        if not account:
            return None

        account["credits"] = int(account.get("credits", 0)) + ADD_UID_COST
        write_database_locked()
        return account["credits"]


def is_super_owner(user_id):
    return user_id in OWNER_IDS


def member_has_owner_role(member):
    if member is None:
        return False
    return any(role.id == OWNER_ROLE_ID for role in getattr(member, "roles", []))


def is_role_owner(user_id, guild=None, member=None):
    if member is not None:
        return member_has_owner_role(member)
    if guild is None:
        return False
    found = guild.get_member(user_id)
    return member_has_owner_role(found)


def is_owner(user_id, guild=None, member=None):
    return (
        is_super_owner(user_id)
        or
        is_role_owner(user_id, guild=guild, member=member)
    )


def is_reseller(user_id):
    return str(
        user_id
    ) in db["resellers"]


def get_reseller(user_id):
    return db[
        "resellers"
    ].get(
        str(user_id)
    )


# ============================================================
# EMBEDS
# ============================================================

BOT_LOGO = os.getenv(
    "BOT_LOGO",
    ""
).strip()


def create_embed(
    title,
    description=None,
    color=None
):

    embed = discord.Embed(
        title=title,
        description=description,
        color=(
            color
            or discord.Color.blurple()
        ),
        timestamp=discord.utils.utcnow()
    )

    if BOT_LOGO:
        embed.set_thumbnail(
            url=BOT_LOGO
        )

    embed.set_footer(
        text=(
            f"{BOT_NAME} "
            "• UID Management System"
        )
    )

    return embed


# ============================================================
# AUDIT LOGGING
# ============================================================

async def send_audit_log(
    bot,
    title,
    description=None,
    color=None,
    fields=None
):

    if not LOG_CHANNEL_ID:
        return

    try:

        channel = bot.get_channel(
            LOG_CHANNEL_ID
        )

        if channel is None:

            channel = await bot.fetch_channel(
                LOG_CHANNEL_ID
            )

        embed = create_embed(
            title,
            description,
            color
        )

        if bot.user:

            embed.add_field(
                name="🤖 Bot",
                value=(
                    f"{bot.user.mention}\n"
                    f"`{bot.user.id}`"
                ),
                inline=False
            )

        if fields:

            for (
                name,
                value,
                inline
            ) in fields:

                embed.add_field(
                    name=name,
                    value=value,
                    inline=inline
                )

        await channel.send(
            embed=embed
        )

    except discord.Forbidden:

        print(
            f"[{bot.user}] "
            f"Cannot send logs to "
            f"channel "
            f"{LOG_CHANNEL_ID}"
        )

    except discord.HTTPException as error:

        print(
            f"[{bot.user}] "
            f"Discord log error: "
            f"{error}"
        )

    except Exception as error:

        print(
            f"[{bot.user}] "
            f"Log error: "
            f"{error}"
        )


# ============================================================
# API
# ============================================================

def api_add_uid(
    uid,
    username,
    days
):

    response = requests.get(
        BASE_URL,
        params={
            "uid": uid,
            "username": username,
            "key": API_KEY,
            "days": days
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def api_remove_uid(
    uid
):

    response = requests.get(
        f"{BASE_URL}/remove",
        params={
            "uid": uid,
            "key": API_KEY
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# ADD UID MODAL
# ============================================================

class AddUIDModal(
    discord.ui.Modal,
    title="🔐 UID Whitelist"
):

    uid_input = discord.ui.TextInput(
        label="Target UID",
        placeholder="Enter UID...",
        required=True,
        max_length=50
    )

    username_input = discord.ui.TextInput(
        label="Client Name",
        placeholder="Enter client name...",
        required=True,
        max_length=50
    )

    days_input = discord.ui.TextInput(
        label="Duration (1-30 Days)",
        placeholder="Maximum 30 days",
        default=str(DEFAULT_DAYS),
        required=True,
        max_length=2
    )

    def __init__(
        self,
        bot
    ):

        super().__init__()

        self.bot_ref = bot


    async def on_submit(
        self,
        interaction
    ):

        user_id = interaction.user.id


        # ----------------------------------------------------
        # ACCESS CHECK
        # ----------------------------------------------------

        if (
            not is_owner(user_id, interaction.guild, interaction.user)
        ):

            await interaction.response.send_message(
                "🚫 You are not authorized.",
                ephemeral=True
            )

            await send_audit_log(
                self.bot_ref,
                "🚨 Unauthorized UID Addition",
                color=discord.Color.red(),
                fields=[
                    (
                        "User",
                        (
                            f"{interaction.user.mention}\n"
                            f"`{user_id}`"
                        ),
                        False
                    )
                ]
            )

            return


        is_role_owner_account = is_role_owner(
            user_id,
            guild=interaction.guild,
            member=interaction.user
        )


        # ----------------------------------------------------
        # INPUT
        # ----------------------------------------------------

        uid = (
            self.uid_input.value
            .strip()
        )

        username = (
            self.username_input.value
            .strip()
        )


        try:

            days = int(
                self.days_input.value
                .strip()
            )

        except ValueError:

            await interaction.response.send_message(
                "❌ Duration must be a number.",
                ephemeral=True
            )

            return


        # ----------------------------------------------------
        # MAX 30 DAYS
        # ----------------------------------------------------

        if (
            days < 1
            or
            days > MAX_DAYS
        ):

            await interaction.response.send_message(
                (
                    "❌ Duration must be between "
                    f"**1 and {MAX_DAYS} days**."
                ),
                ephemeral=True
            )

            return


        # ----------------------------------------------------
        # RESERVE CREDIT
        # ----------------------------------------------------

        reserved_remaining = None


        if is_role_owner_account:

            await ensure_owner_account(
                user_id,
                str(interaction.user)
            )

            reserved_remaining = (
                await reserve_owner_credit(
                    user_id
                )
            )

            if reserved_remaining is None:

                await interaction.response.send_message(
                    (
                        "💳 **Insufficient Credits**\n"
                        "Please contact an owner."
                    ),
                    ephemeral=True
                )

                return


        await interaction.response.defer(
            thinking=True
        )


        # ----------------------------------------------------
        # API REQUEST
        # ----------------------------------------------------

        try:

            data = await asyncio.to_thread(
                api_add_uid,
                uid,
                username,
                days
            )


            success = bool(
                data.get(
                    "success",
                    False
                )
            )


            # ------------------------------------------------
            # FAILED = REFUND
            # ------------------------------------------------

            if (
                not success
                and
                is_role_owner_account
            ):

                remaining = (
                    await refund_owner_credit(
                        user_id
                    )
                )

            else:

                remaining = (
                    reserved_remaining
                )


            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            if success:

                embed = create_embed(
                    "✅ UID Whitelisted",
                    (
                        "The UID has been "
                        "successfully added."
                    ),
                    discord.Color.green()
                )

                embed.add_field(
                    name="🎮 UID",
                    value=f"`{uid}`",
                    inline=True
                )

                embed.add_field(
                    name="👤 Client",
                    value=f"`{username}`",
                    inline=True
                )

                embed.add_field(
                    name="⏱️ Duration",
                    value=f"`{days} Days`",
                    inline=True
                )

                embed.add_field(
                    name="💳 Credits Remaining",
                    value=(
                        f"`{remaining}`"
                        if is_role_owner_account
                        else
                        "`UNLIMITED`"
                    ),
                    inline=True
                )

                await interaction.followup.send(
                    embed=embed
                )


            # ------------------------------------------------
            # FAILURE
            # ------------------------------------------------

            else:

                message = (
                    data.get(
                        "message"
                    )
                    or
                    data.get(
                        "error"
                    )
                    or
                    "The API rejected the UID request."
                )

                embed = create_embed(
                    "❌ UID Addition Failed",
                    (
                        f"{message}\n\n"
                        "No credit was charged."
                    ),
                    discord.Color.red()
                )

                embed.add_field(
                    name="🎮 UID",
                    value=f"`{uid}`",
                    inline=True
                )

                embed.add_field(
                    name="👤 Client",
                    value=f"`{username}`",
                    inline=True
                )

                embed.add_field(
                    name="⏱️ Duration",
                    value=f"`{days} Days`",
                    inline=True
                )

                await interaction.followup.send(
                    embed=embed
                )


            # ------------------------------------------------
            # AUDIT LOG
            # ------------------------------------------------

            await send_audit_log(
                self.bot_ref,
                "🔔 UID Addition",
                color=(
                    discord.Color.green()
                    if success
                    else
                    discord.Color.red()
                ),
                fields=[
                    (
                        "👤 Requested By",
                        (
                            f"{interaction.user.mention}\n"
                            f"`{user_id}`"
                        ),
                        False
                    ),
                    (
                        "🎮 UID",
                        f"`{uid}`",
                        True
                    ),
                    (
                        "Client",
                        f"`{username}`",
                        True
                    ),
                    (
                        "Duration",
                        f"`{days} Days`",
                        True
                    ),
                    (
                        "Result",
                        (
                            "✅ SUCCESS"
                            if success
                            else
                            "❌ FAILED"
                        ),
                        True
                    ),
                    (
                        "Account",
                        (
                            "Owner Role"
                            if is_role_owner_account
                            else
                            "Super Owner"
                        ),
                        True
                    ),
                    (
                        "Credits Remaining",
                        (
                            f"`{remaining}`"
                            if is_role_owner_account
                            else
                            "`UNLIMITED`"
                        ),
                        True
                    )
                ]
            )


        except requests.RequestException as error:

            if is_role_owner_account:

                remaining = (
                    await refund_owner_credit(
                        user_id
                    )
                )

            await interaction.followup.send(
                (
                    "❌ Unable to connect to "
                    "the UID API.\n"
                    "Your credit has been restored."
                    if is_role_owner_account
                    else
                    "❌ Unable to connect to "
                    "the UID API."
                )
            )

            await send_audit_log(
                self.bot_ref,
                "🚨 API Connection Error",
                color=discord.Color.red(),
                fields=[
                    (
                        "User",
                        (
                            f"{interaction.user.mention}\n"
                            f"`{user_id}`"
                        ),
                        False
                    ),
                    (
                        "UID",
                        f"`{uid}`",
                        True
                    ),
                    (
                        "Error",
                        f"`{str(error)[:500]}`",
                        False
                    )
                ]
            )


        except Exception as error:

            if is_role_owner_account:

                await refund_owner_credit(
                    user_id
                )

            print(
                f"UID error: {error}"
            )

            await interaction.followup.send(
                (
                    "❌ Unexpected error.\n"
                    "Your credit has been restored."
                    if is_role_owner_account
                    else
                    "❌ Unexpected error."
                )
            )


# ============================================================
# ADD UID VIEW
# ============================================================

class AddUIDView(
    discord.ui.View
):

    def __init__(
        self,
        bot,
        timeout=None
    ):

        super().__init__(
            timeout=timeout
        )

        self.bot_ref = bot


    @discord.ui.button(
        label="🚀 Add UID",
        style=discord.ButtonStyle.success,
        custom_id="globalx_add_uid"
    )
    async def add_uid_button(
        self,
        interaction,
        button
    ):

        user_id = interaction.user.id


        if (
            not is_owner(user_id, interaction.guild, interaction.user)
        ):

            await interaction.response.send_message(
                "🚫 You don't have permission.",
                ephemeral=True
            )

            return


        if is_role_owner(
            user_id,
            guild=interaction.guild,
            member=interaction.user
        ):

            await ensure_owner_account(
                user_id,
                str(interaction.user)
            )
            reseller = get_owner_account(
                user_id
            )

            if (
                not reseller
                or
                int(
                    reseller.get(
                        "credits",
                        DEFAULT_OWNER_CREDITS
                    )
                ) < ADD_UID_COST
            ):

                await interaction.response.send_message(
                    "💳 You have no credits.",
                    ephemeral=True
                )

                return


        await interaction.response.send_modal(
            AddUIDModal(
                self.bot_ref
            )
        )


# ============================================================
# BOT CLASS
# ============================================================

class GlobalXBot(
    commands.Bot
):

    def __init__(
        self,
        bot_index
    ):

        intents = discord.Intents.default()

        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

        self.bot_index = (
            bot_index
        )

        self.bot_label = (
            f"{BOT_NAME} "
            f"#{bot_index + 1}"
        )

        # Connection diagnostics.
        self.connection_started_at = None
        self.ready_count = 0
        self.last_gateway_error = None


    async def setup_hook(
        self
    ):

        self.add_view(
            AddUIDView(
                self,
                timeout=None
            )
        )


# ============================================================
# COMMAND REGISTRATION
# ============================================================

def register_commands(
    bot
):


    # ========================================================
    # DISCORD CONNECTION DIAGNOSTICS
    # ========================================================

    @bot.event
    async def on_connect():

        bot.connection_started_at = datetime.now(timezone.utc)

        print(
            f"[{bot.bot_label}] 🔌 Connected to Discord Gateway."
        )

    @bot.event
    async def on_disconnect():

        print(
            f"[{bot.bot_label}] ⚠️ Disconnected from Discord Gateway."
        )

    @bot.event
    async def on_resumed():

        print(
            f"[{bot.bot_label}] 🔄 Discord Gateway session resumed."
        )

    @bot.event
    async def on_error(event, *args, **kwargs):

        print(
            f"[{bot.bot_label}] ❌ Discord event error: {event}"
        )

    @bot.event
    async def on_member_update(before, after):

        before_has = member_has_owner_role(before)
        after_has = member_has_owner_role(after)

        if after_has:
            await ensure_owner_account(after.id, str(after))
        elif before_has and not after_has:
            print(
                f"[{bot.bot_label}] Owner role removed from {after} ({after.id})."
            )

    # ========================================================
    # READY
    # ========================================================

    @bot.event
    async def on_ready():

        bot.ready_count += 1

        print(
            "=" * 65
        )
        print(
            f"[{bot.bot_label}] ✅ DISCORD BOT ONLINE"
        )
        print(
            f"[{bot.bot_label}] User: {bot.user}"
        )
        print(
            f"[{bot.bot_label}] User ID: {bot.user.id}"
        )
        print(
            f"[{bot.bot_label}] Guilds connected: {len(bot.guilds)}"
        )
        print(
            f"[{bot.bot_label}] Owner role ID: {OWNER_ROLE_ID}"
        )

        # Initialize a 100-credit account for every current member
        # holding the configured owner role. Existing balances are kept.
        initialized = 0
        for guild in bot.guilds:
            role = guild.get_role(OWNER_ROLE_ID)
            if role:
                for member in role.members:
                    await ensure_owner_account(member.id, str(member))
                    initialized += 1
        print(
            f"[{bot.bot_label}] Owner-role accounts initialized/checked: {initialized}"
        )

        if bot.guilds:
            print(
                f"[{bot.bot_label}] Guild list: "
                + ", ".join(
                    f"{guild.name} ({guild.id})"
                    for guild in bot.guilds[:20]
                )
            )
        else:
            print(
                f"[{bot.bot_label}] ⚠️ Bot is online but "
                "is not connected to any guild."
            )

        print(
            f"[{bot.bot_label}] Ready event count: {bot.ready_count}"
        )
        print(
            "=" * 65
        )

        try:

            await bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=(
                        discord.ActivityType
                        .watching
                    ),
                    name="UID Management"
                )
            )

        except Exception as error:

            print(
                f"[{bot.bot_label}] "
                f"Presence error: {error}"
            )


    # ========================================================
    # ADD RESELLER
    # ========================================================

    @bot.command(
        name="addreseller"
    )
    async def addreseller(
        ctx,
        member: discord.Member = None
    ):

        if not is_super_owner(
            ctx.author.id
        ):

            await ctx.send(
                (
                    "🚫 **Access Denied**\n"
                    "Only owners can manage resellers."
                )
            )

            return


        if member is None:

            await ctx.send(
                "❌ Usage: `!addreseller @user`"
            )

            return


        if member.bot:

            await ctx.send(
                "❌ Bots cannot be resellers."
            )

            return


        if is_owner(
            member.id,
            ctx.guild,
            member
        ):

            await ctx.send(
                "❌ That user is already an owner."
            )

            return


        if is_reseller(
            member.id
        ):

            reseller = get_reseller(
                member.id
            )

            await ctx.send(
                (
                    f"⚠️ {member.mention} "
                    "is already a reseller.\n"
                    f"Credits: `{reseller['credits']}`"
                )
            )

            return


        await add_reseller_record(
            member.id,
            str(member)
        )


        embed = create_embed(
            "🎉 Reseller Added",
            (
                "A new reseller has been "
                "successfully registered."
            ),
            discord.Color.green()
        )

        embed.add_field(
            name="👤 Reseller",
            value=member.mention,
            inline=True
        )

        embed.add_field(
            name="🆔 Discord ID",
            value=f"`{member.id}`",
            inline=True
        )

        embed.add_field(
            name="💳 Starting Credits",
            value="`0`",
            inline=True
        )

        await ctx.send(
            embed=embed
        )


        await send_audit_log(
            bot,
            "👤 New Reseller Added",
            color=discord.Color.green(),
            fields=[
                (
                    "Reseller",
                    (
                        f"{member.mention}\n"
                        f"`{member.id}`"
                    ),
                    False
                ),
                (
                    "Added By",
                    (
                        f"{ctx.author.mention}\n"
                        f"`{ctx.author.id}`"
                    ),
                    False
                ),
                (
                    "Starting Credits",
                    "`0`",
                    True
                )
            ]
        )


    # ========================================================
    # ADD CREDITS
    # ========================================================

    @bot.command(
        name="addcredits"
    )
    async def addcredits(
        ctx,
        member: discord.Member = None,
        amount: int = None
    ):

        if not is_super_owner(
            ctx.author.id
        ):

            await ctx.send(
                "🚫 **Access Denied**"
            )

            return


        if (
            member is None
            or
            amount is None
        ):

            await ctx.send(
                (
                    "❌ Usage: "
                    "`!addcredits @user 50`"
                )
            )

            return


        if amount <= 0:

            await ctx.send(
                "❌ Credit amount must be greater than zero."
            )

            return


        if is_role_owner(
            member.id,
            guild=ctx.guild,
            member=member
        ):
            result = await add_owner_credits_record(
                member.id,
                amount,
                str(member)
            )
        else:
            await ctx.send(
                "❌ That user does not have the configured owner role."
            )
            return

        if result is None:

            await ctx.send(
                "❌ Could not update that account."
            )

            return


        old_balance, new_balance = result


        embed = create_embed(
            "💳 Credits Added",
            "Credits successfully added.",
            discord.Color.green()
        )

        embed.add_field(
            name="👤 Account",
            value=member.mention,
            inline=False
        )

        embed.add_field(
            name="➕ Added",
            value=f"`+{amount}`",
            inline=True
        )

        embed.add_field(
            name="📊 Previous",
            value=f"`{old_balance}`",
            inline=True
        )

        embed.add_field(
            name="💰 New Balance",
            value=f"`{new_balance}`",
            inline=True
        )

        await ctx.send(
            embed=embed
        )


    # ========================================================
    # REMOVE RESELLER
    # ========================================================

    @bot.command(
        name="removereseller"
    )
    async def removereseller(
        ctx,
        member: discord.Member = None
    ):

        if not is_super_owner(
            ctx.author.id
        ):

            await ctx.send(
                "🚫 **Access Denied**"
            )

            return


        if member is None:

            await ctx.send(
                (
                    "❌ Usage: "
                    "`!removereseller @user`"
                )
            )

            return


        reseller = get_reseller(
            member.id
        )

        if not reseller:

            await ctx.send(
                "❌ That user is not a reseller."
            )

            return


        old_credits = int(
            reseller.get(
                "credits",
                0
            )
        )


        await delete_reseller_record(
            member.id
        )


        embed = create_embed(
            "🗑️ Reseller Removed",
            (
                "The reseller account "
                "has been removed."
            ),
            discord.Color.orange()
        )

        embed.add_field(
            name="👤 Reseller",
            value=member.mention,
            inline=True
        )

        embed.add_field(
            name="💳 Lost Credits",
            value=f"`{old_credits}`",
            inline=True
        )

        await ctx.send(
            embed=embed
        )


    # ========================================================
    # ADD UID
    # ========================================================

    @bot.command(
        name="adduid"
    )
    async def adduid(
        ctx
    ):

        user_id = ctx.author.id


        if (
            not is_owner(user_id, ctx.guild, ctx.author)
        ):

            await ctx.send(
                "🚫 **Access Denied**"
            )

            return


        if is_role_owner(
            user_id,
            guild=ctx.guild,
            member=ctx.author
        ):

            await ensure_owner_account(
                user_id,
                str(ctx.author)
            )

            reseller = get_owner_account(
                user_id
            )

            if (
                not reseller
                or
                int(
                    reseller.get(
                        "credits",
                        0
                    )
                ) < ADD_UID_COST
            ):

                await ctx.send(
                    "💳 **Insufficient Credits**"
                )

                return


        embed = create_embed(
            "🚀 UID Management Panel",
            (
                "**Global X Bypass**\n\n"
                "Use the button below to "
                "whitelist a UID.\n\n"
                f"🔹 Maximum duration: "
                f"**{MAX_DAYS} days**\n"
                f"🔹 Default duration: "
                f"**{DEFAULT_DAYS} days**\n"
                f"🔹 Add UID: "
                f"**{ADD_UID_COST} credit**\n"
                "🔹 Remove UID: **FREE**"
            ),
            discord.Color.blurple()
        )


        if is_role_owner(user_id, guild=ctx.guild, member=ctx.author):

            embed.add_field(
                name="💳 Your Credits",
                value=(
                    f"`{get_owner_account(user_id)['credits']}`"
                ),
                inline=True
            )

            embed.add_field(
                name="👑 Account",
                value="`OWNER ROLE`",
                inline=True
            )

        else:

            embed.add_field(
                name="👑 Account",
                value="`OWNER`",
                inline=True
            )


        await ctx.send(
            embed=embed,
            view=AddUIDView(
                bot
            )
        )


    # ========================================================
    # REMOVE UID
    # ========================================================

    @bot.command(
        name="removeuid"
    )
    async def removeuid(
        ctx,
        uid: str = None
    ):

        user_id = ctx.author.id


        # IMPORTANT:
        # SUPER OWNERS AND OWNER-ROLE MEMBERS CAN REMOVE UID.

        if (
            not is_owner(user_id, ctx.guild, ctx.author)
        ):

            await ctx.send(
                (
                    "🚫 **Access Denied**\n"
                    "Only owner-role members can remove UIDs."
                )
            )

            return


        if not uid:

            await ctx.send(
                "❌ Usage: `!removeuid <uid>`"
            )

            return


        await ctx.send(
            "⏳ Processing UID removal..."
        )


        try:

            data = await asyncio.to_thread(
                api_remove_uid,
                uid
            )


            success = bool(
                data.get(
                    "success",
                    False
                )
            )


            message = (
                data.get(
                    "message"
                )
                or
                data.get(
                    "error"
                )
            )


            if success:

                description = (
                    "The UID has been "
                    "successfully removed."
                )

            else:

                description = (
                    message
                    or
                    "The API rejected "
                    "the removal request."
                )


            embed = create_embed(
                (
                    "🗑️ UID Removed"
                    if success
                    else
                    "❌ UID Removal Failed"
                ),
                description,
                (
                    discord.Color.green()
                    if success
                    else
                    discord.Color.red()
                )
            )


            embed.add_field(
                name="🎮 UID",
                value=f"`{uid}`",
                inline=True
            )

            embed.add_field(
                name="👤 Requested By",
                value=ctx.author.mention,
                inline=True
            )

            embed.add_field(
                name="💳 Credit Cost",
                value="`0`",
                inline=True
            )


            await ctx.send(
                embed=embed
            )


            await send_audit_log(
                bot,
                "🗑️ UID Removal Request",
                color=(
                    discord.Color.green()
                    if success
                    else
                    discord.Color.red()
                ),
                fields=[
                    (
                        "🎮 UID",
                        f"`{uid}`",
                        True
                    ),
                    (
                        "👤 Requested By",
                        (
                            f"{ctx.author.mention}\n"
                            f"`{user_id}`"
                        ),
                        False
                    ),
                    (
                        "Account",
                        (
                            "Super Owner"
                            if is_super_owner(user_id)
                            else
                            "Owner Role"
                        ),
                        True
                    ),
                    (
                        "Result",
                        (
                            "✅ SUCCESS"
                            if success
                            else
                            "❌ FAILED"
                        ),
                        True
                    ),
                    (
                        "Credit Cost",
                        "`0`",
                        True
                    )
                ]
            )


        except requests.RequestException as error:

            await ctx.send(
                "❌ API connection failed."
            )

            await send_audit_log(
                bot,
                "🚨 UID Removal API Error",
                color=discord.Color.red(),
                fields=[
                    (
                        "UID",
                        f"`{uid}`",
                        True
                    ),
                    (
                        "User",
                        ctx.author.mention,
                        True
                    ),
                    (
                        "Error",
                        f"`{str(error)[:500]}`",
                        False
                    )
                ]
            )


        except Exception as error:

            print(
                f"[{bot.bot_label}] "
                f"Remove UID error: "
                f"{error}"
            )

            await ctx.send(
                "❌ An unexpected error occurred."
            )


    # ========================================================
    # CREDITS
    # ========================================================

    @bot.command(
        name="credits"
    )
    async def credits(
        ctx
    ):

        user_id = ctx.author.id


        if is_super_owner(user_id):

            embed = create_embed(
                "👑 Owner Account",
                (
                    "Super owners have unlimited "
                    "UID management access."
                ),
                discord.Color.gold()
            )

            embed.add_field(
                name="💳 Credits",
                value="`UNLIMITED`",
                inline=True
            )

            await ctx.send(
                embed=embed
            )

            return


        if not is_role_owner(
            user_id,
            guild=ctx.guild,
            member=ctx.author
        ):

            await ctx.send(
                "🚫 You do not have the required owner role."
            )

            return


        await ensure_owner_account(
            user_id,
            str(ctx.author)
        )

        reseller = get_owner_account(
            user_id
        )


        embed = create_embed(
            "👑 Your Owner Account",
            "Current account information.",
            discord.Color.blurple()
        )

        embed.add_field(
            name="👑 Owner",
            value=ctx.author.mention,
            inline=True
        )

        embed.add_field(
            name="💰 Credits",
            value=f"`{reseller['credits']}`",
            inline=True
        )

        embed.add_field(
            name="🎮 UID Cost",
            value=f"`{ADD_UID_COST} Credit`",
            inline=True
        )

        embed.add_field(
            name="⏱️ Max Duration",
            value=f"`{MAX_DAYS} Days`",
            inline=True
        )

        embed.add_field(
            name="🗑️ Remove UID",
            value="`FREE`",
            inline=True
        )

        await ctx.send(
            embed=embed
        )


    # ========================================================
    # LIST RESELLERS
    # ========================================================

    @bot.command(
        name="resellers"
    )
    async def resellers(
        ctx
    ):

        if not is_super_owner(
            ctx.author.id
        ):

            await ctx.send(
                "🚫 Owner only."
            )

            return


        if not db["resellers"]:

            await ctx.send(
                "📭 No resellers registered."
            )

            return


        embed = create_embed(
            "👥 Reseller Management",
            (
                f"Total resellers: "
                f"`{len(db['resellers'])}`"
            ),
            discord.Color.blurple()
        )


        for (
            user_id,
            reseller
        ) in db[
            "resellers"
        ].items():

            embed.add_field(
                name=(
                    f"👤 "
                    f"{reseller.get('username', 'Unknown')}"
                ),
                value=(
                    f"ID: `{user_id}`\n"
                    f"Credits: "
                    f"`{reseller.get('credits', 0)}`"
                ),
                inline=False
            )


        await ctx.send(
            embed=embed
        )


    # ========================================================
    # HELP
    # ========================================================

    @bot.command(
        name="help"
    )
    async def help_command(
        ctx
    ):

        embed = create_embed(
            "📚 Global X Bypass",
            "Available commands for your account.",
            discord.Color.blurple()
        )


        if is_super_owner(
            ctx.author.id
        ):

            embed.add_field(
                name="👑 OWNER COMMANDS",
                value=(
                    "`!addreseller @user`\n"
                    "`!addcredits @user 50`\n"
                    "`!removereseller @user`\n"
                    "`!resellers`\n"
                    "`!adduid`\n"
                    "`!removeuid <uid>`\n"
                    "`!credits`"
                ),
                inline=False
            )


        elif is_role_owner(
            ctx.author.id,
            guild=ctx.guild,
            member=ctx.author
        ):

            embed.add_field(
                name="👑 OWNER ROLE COMMANDS",
                value=(
                    "`!adduid`\n"
                    "`!removeuid <uid>`\n"
                    "`!credits`"
                ),
                inline=False
            )

            embed.add_field(
                name="📌 LIMITS",
                value=(
                    f"Maximum UID duration: "
                    f"**{MAX_DAYS} days**\n"
                    f"Default duration: "
                    f"**{DEFAULT_DAYS} days**\n"
                    f"Add UID: "
                    f"**{ADD_UID_COST} credit**\n"
                    "Remove UID: **FREE**"
                ),
                inline=False
            )


        else:

            embed.add_field(
                name="🚫 NO ACCESS",
                value=(
                    "You need the configured owner role "
                    "to use this bot."
                ),
                inline=False
            )


        await ctx.send(
            embed=embed
        )


    # ========================================================
    # ERROR HANDLER
    # ========================================================

    @bot.event
    async def on_command_error(
        ctx,
        error
    ):

        if isinstance(
            error,
            commands.CommandNotFound
        ):
            return


        if isinstance(
            error,
            commands.MissingRequiredArgument
        ):

            await ctx.send(
                (
                    "❌ Missing required argument.\n"
                    "Use `!help`."
                )
            )

            return


        if isinstance(
            error,
            commands.BadArgument
        ):

            await ctx.send(
                (
                    "❌ Invalid argument.\n"
                    "Make sure you mention "
                    "the correct Discord user."
                )
            )

            return


        print(
            f"[{bot.bot_label}] "
            f"Command error: {error}"
        )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

flask_app = Flask(
    __name__
)


@flask_app.get("/")
def home():

    return jsonify({
        "status": "online",
        "service": BOT_NAME,
        "bots_configured": len(
            BOT_TOKENS
        ),
        "max_uid_days": MAX_DAYS,
        "default_uid_days": DEFAULT_DAYS,
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


@flask_app.get("/health")
def health():

    return jsonify({
        "status": "healthy",
        "bots_configured": len(
            BOT_TOKENS
        ),
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


def start_http_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    print(
        f"HTTP health server listening "
        f"on 0.0.0.0:{port}"
    )

    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# ============================================================
# BOT RUNNER
# ============================================================

async def run_one_bot(
    token,
    index
):

    bot = GlobalXBot(
        index
    )

    register_commands(
        bot
    )

    bot_label = f"[Bot #{index + 1}]"

    # Never print the token itself.
    token_length = len(token or "")
    token_present = bool(token and token.strip())

    print(
        f"{bot_label} Starting: {bot.bot_label}"
    )
    print(
        f"{bot_label} Token configured: {token_present}"
    )
    print(
        f"{bot_label} Token length: {token_length}"
    )
    print(
        f"{bot_label} Server target: "
        f"{LOG_CHANNEL_ID and 'configured' or 'not configured'}"
    )
    print(
        f"{bot_label} Intents: "
        f"message_content={bot.intents.message_content}, "
        f"members={bot.intents.members}"
    )
    print(
        f"{bot_label} Connecting to Discord Gateway..."
    )

    if not token_present:
        print(
            f"{bot_label} ❌ EMPTY DISCORD TOKEN. "
            "Check BOT_TOKENS or BOT_TOKEN in Render."
        )
        return

    try:

        await bot.start(
            token,
            reconnect=True
        )

        print(
            f"{bot_label} Discord connection closed normally."
        )

    except discord.LoginFailure as error:

        print(
            f"{bot_label} ❌ INVALID DISCORD TOKEN."
        )
        print(
            f"{bot_label} LoginFailure: {error}"
        )
        print(
            f"{bot_label} Generate a new bot token in "
            "Discord Developer Portal and update Render."
        )

    except discord.PrivilegedIntentsRequired as error:

        print(
            f"{bot_label} ❌ PRIVILEGED INTENTS REQUIRED."
        )
        print(
            f"{bot_label} Error: {error}"
        )
        print(
            f"{bot_label} Enable Message Content Intent "
            "and Server Members Intent in Discord Developer Portal."
        )

    except discord.HTTPException as error:

        print(
            f"{bot_label} ❌ DISCORD HTTP ERROR."
        )
        print(
            f"{bot_label} Status: {getattr(error, 'status', 'unknown')}"
        )
        print(
            f"{bot_label} Error: {error}"
        )

    except discord.GatewayNotFound as error:

        print(
            f"{bot_label} ❌ DISCORD GATEWAY NOT FOUND."
        )
        print(
            f"{bot_label} Error: {error}"
        )

    except asyncio.CancelledError:

        print(
            f"{bot_label} ⚠️ Bot task was cancelled."
        )
        raise

    except Exception as error:

        print(
            f"{bot_label} ❌ BOT STOPPED WITH UNEXPECTED ERROR."
        )
        print(
            f"{bot_label} Error type: {type(error).__name__}"
        )
        print(
            f"{bot_label} Error: {error}"
        )

    finally:

        print(
            f"{bot_label} Cleaning up Discord client..."
        )

        if not bot.is_closed():

            await bot.close()

        print(
            f"{bot_label} Discord client closed."
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    print("=" * 65)

    print(
        f"{BOT_NAME} - MULTI BOT HOST"
    )

    print(
        f"Configured bots: "
        f"{len(BOT_TOKENS)}"
    )

    print(
        "Super Owners: "
        f"{', '.join(map(str, OWNER_IDS))}"
    )

    print(
        f"Owner role ID: {OWNER_ROLE_ID}"
    )

    print(
        f"Default owner credits: {DEFAULT_OWNER_CREDITS}"
    )

    print(
        f"Maximum UID days: "
        f"{MAX_DAYS}"
    )

    print(
        f"Default UID days: "
        f"{DEFAULT_DAYS}"
    )

    print(
        f"Log channel: "
        f"{LOG_CHANNEL_ID or 'DISABLED'}"
    )

    print(
        f"Database: "
        f"{DATA_FILE}"
    )

    print(
        f"BOT_TOKENS entries: {len(BOT_TOKENS)}"
    )

    print(
        "Discord token source: "
        + (
            "BOT_TOKENS"
            if os.getenv("BOT_TOKENS", "").strip()
            else "BOT_TOKEN"
            if os.getenv("BOT_TOKEN", "").strip()
            else "NONE"
        )
    )

    print(
        f"API key configured: {bool(API_KEY)}"
    )

    print(
        f"Log channel ID: {LOG_CHANNEL_ID or 'DISABLED'}"
    )

    print("=" * 65)


    if not API_KEY:

        print(
            "❌ API_KEY is not configured."
        )

        raise SystemExit(1)


    if not BOT_TOKENS:

        print(
            "❌ BOT_TOKENS is not configured."
        )

        raise SystemExit(1)


    # Start Render health server
    health_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    health_thread.start()


    # Start all Discord bots
    tasks = []

    print(
        "Discord Gateway requirements:"
    )
    print(
        "  - Message Content Intent: ENABLED in code"
    )
    print(
        "  - Server Members Intent: ENABLED in code"
    )
    print(
        "  - These must also be enabled in Discord Developer Portal."
    )


    for (
        index,
        token
    ) in enumerate(
        BOT_TOKENS
    ):

        tasks.append(
            asyncio.create_task(
                run_one_bot(
                    token,
                    index
                )
            )
        )


    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )


    for (
        index,
        result
    ) in enumerate(
        results,
        start=1
    ):

        if isinstance(
            result,
            Exception
        ):

            print(
                f"[Bot #{index}] "
                f"stopped with error: "
                f"{result}"
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Stopped."
        )
