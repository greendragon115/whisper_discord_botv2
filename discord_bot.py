from __future__ import annotations

import os
import json
import logging
import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whispers.json")

ANONYMOUS_NAME = "Anonymous ()"
ANONYMOUS_COLOR = discord.Color.dark_grey()
ANONYMOUS_ICON: Optional[str] = None

GUILD_IDS: List[int] = [1543920625633071186]

MAX_MESSAGE_LENGTH = 3800

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("whisper-bot")


@dataclass
class WhisperRecord:
    message_id: int
    channel_id: int
    guild_id: int
    author_id: int
    original_content: str
    history: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    edited_at: Optional[str] = None

    @property
    def current_content(self) -> str:
        return self.history[-1] if self.history else self.original_content

    @property
    def is_edited(self) -> bool:
        return len(self.history) > 1

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "WhisperRecord":
        return WhisperRecord(**data)


class WhisperManager:
    def __init__(self, path: str):
        self.path = path
        self._records: Dict[str, WhisperRecord] = {}
        self._lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._records = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._records = {k: WhisperRecord.from_dict(v) for k, v in raw.items()}
            log.info("Loaded %d whisper(s) from %s", len(self._records), self.path)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("Could not load %s (%s); starting with an empty store.", self.path, exc)
            self._records = {}

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {k: v.to_dict() for k, v in self._records.items()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as exc:
            log.error("Could not save %s: %s", self.path, exc)

    async def add(self, record: WhisperRecord) -> None:
        async with self._lock:
            self._records[str(record.message_id)] = record
            self.save()

    async def remove(self, message_id: int) -> None:
        async with self._lock:
            self._records.pop(str(message_id), None)
            self.save()

    async def update(self, record: WhisperRecord) -> None:
        async with self._lock:
            self._records[str(record.message_id)] = record
            self.save()

    def get(self, message_id: int) -> Optional[WhisperRecord]:
        return self._records.get(str(message_id))

    def find_by_original_content(
            self, *, author_id: int, channel_id: int, content: str
    ) -> Optional[WhisperRecord]:
        target = content.strip()
        candidates = [
            r
            for r in self._records.values()
            if r.author_id == author_id
               and r.channel_id == channel_id
               and r.original_content.strip() == target
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.created_at, reverse=True)
        return candidates[0]

    def find_any_by_original_content(
            self, *, channel_id: int, content: str
    ) -> Optional[WhisperRecord]:
        target = content.strip()
        candidates = [
            r
            for r in self._records.values()
            if r.channel_id == channel_id and r.original_content.strip() == target
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.created_at, reverse=True)
        return candidates[0]

    def list_by_author(self, *, author_id: int, guild_id: int) -> List[WhisperRecord]:
        candidates = [
            r
            for r in self._records.values()
            if r.author_id == author_id and r.guild_id == guild_id
        ]
        candidates.sort(key=lambda r: r.created_at, reverse=True)
        return candidates


whisper_manager = WhisperManager(DATA_FILE)

intents = discord.Intents.default()
intents.message_content = True


class WhisperBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!whisperbot-no-prefix!", intents=intents)

    async def setup_hook(self) -> None:
        if GUILD_IDS:
            for guild_id in GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "Synced %d slash command(s) INSTANTLY on guild %s.",
                    len(synced),
                    guild_id,
                )
        else:
            synced = await self.tree.sync()
            log.info(
                "Synced %d slash command(s) GLOBALLY (can take up to ~1h to appear).",
                len(synced),
            )


bot = WhisperBot()


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="/whisper")
        )
    except discord.HTTPException:
        pass


def _author_kwargs() -> dict:
    kwargs = {"name": ANONYMOUS_NAME}
    if ANONYMOUS_ICON:
        kwargs["icon_url"] = ANONYMOUS_ICON
    return kwargs


def build_whisper_embed(content: str) -> discord.Embed:
    embed = discord.Embed(
        description=content,
        color=ANONYMOUS_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(**_author_kwargs())
    return embed


def build_history_embed(history: List[str]) -> discord.Embed:
    if len(history) <= 1:
        return build_whisper_embed(history[0] if history else "")

    parts = [history[0]]
    for idx, version in enumerate(history[1:], start=2):
        parts.append("━━━━━━━━━━━━━━━━━━")
        parts.append(f"✏️ **New version ({idx}):**\n{version}")

    description = "\n\n".join(parts)

    embed = discord.Embed(
        description=description,
        color=ANONYMOUS_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(**_author_kwargs())
    embed.set_footer(text="✏️ Message edited")
    return embed


def build_reply_embed(quoted_original: str, reply_text: str) -> discord.Embed:
    quoted_lines = "\n".join(f"> {line}" for line in quoted_original.splitlines()) or "> "
    description = f"{quoted_lines}\n\n↳ **Reply:**\n{reply_text}"

    embed = discord.Embed(
        description=description,
        color=ANONYMOUS_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(**_author_kwargs())
    return embed


async def find_target_message(
        channel: discord.abc.Messageable, content: str, history_limit: int = 500
):
    target = content.strip()

    whisper_record = whisper_manager.find_any_by_original_content(
        channel_id=channel.id,
        content=target,
    )
    if whisper_record is not None:
        try:
            message = await channel.fetch_message(whisper_record.message_id)
            return message, whisper_record.current_content
        except (discord.NotFound, discord.HTTPException):
            await whisper_manager.remove(whisper_record.message_id)

    async for message in channel.history(limit=history_limit):
        if message.content.strip() == target:
            return message, message.content

        for embed in message.embeds:
            if (embed.description or "").strip() == target:
                return message, embed.description

    return None, None


@bot.tree.command(name="whisper", description="Send an anonymous message in this channel.")
@app_commands.describe(mesaj="The message you want to send anonymously.")
async def whisper_command(interaction: discord.Interaction, mesaj: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works in a server, not in direct messages.", ephemeral=True
        )
        return

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "I can't send anonymous messages in this type of channel.", ephemeral=True
        )
        return

    mesaj = mesaj.strip()
    if not mesaj:
        await interaction.response.send_message("The message can't be empty.", ephemeral=True)
        return
    if len(mesaj) > MAX_MESSAGE_LENGTH:
        await interaction.response.send_message(
            f"The message is too long (max {MAX_MESSAGE_LENGTH} characters).", ephemeral=True
        )
        return

    embed = build_whisper_embed(mesaj)

    try:
        sent_message = await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to send messages in this channel.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        log.error("Error sending whisper: %s", exc)
        await interaction.response.send_message(
            "Something went wrong sending the message.", ephemeral=True
        )
        return

    record = WhisperRecord(
        message_id=sent_message.id,
        channel_id=channel.id,
        guild_id=interaction.guild.id,
        author_id=interaction.user.id,
        original_content=mesaj,
        history=[mesaj],
    )
    await whisper_manager.add(record)

    await interaction.response.send_message("✅ Your anonymous message was sent.", ephemeral=True)
    log.info(
        "Whisper sent by %s (%s) in #%s",
        interaction.user,
        interaction.user.id,
        getattr(channel, "name", channel.id),
    )


@bot.tree.command(
    name="delete",
    description="Delete an anonymous message you sent. Type the original message EXACTLY.",
)
@app_commands.describe(mesaj="The EXACT original text of the anonymous message you want to delete.")
async def delete_command(interaction: discord.Interaction, mesaj: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel = interaction.channel
    record = whisper_manager.find_by_original_content(
        author_id=interaction.user.id,
        channel_id=channel.id,
        content=mesaj,
    )

    if record is None:
        await interaction.response.send_message(
            "❌ I couldn't find any whisper of yours in this channel with that exact original "
            "text.\nMake sure you typed the message identically (capitalization, spaces, "
            "punctuation, emojis).",
            ephemeral=True,
        )
        return

    try:
        discord_message = await channel.fetch_message(record.message_id)
        await discord_message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to delete that message.", ephemeral=True)
        return
    except discord.HTTPException as exc:
        log.error("Error deleting whisper: %s", exc)
        await interaction.response.send_message("Something went wrong deleting the message.", ephemeral=True)
        return

    await whisper_manager.remove(record.message_id)
    await interaction.response.send_message("🗑️ The anonymous message was deleted.", ephemeral=True)
    log.info("Whisper %s deleted by %s", record.message_id, interaction.user)


@bot.tree.command(name="edit", description="Edit an anonymous message you sent.")
@app_commands.describe(
    mesaj_original="The EXACT original text of the message (as it was first sent).",
    mesaj_nou="The new text. Will appear BELOW the original, marked as 'New version'.",
)
async def edit_command(interaction: discord.Interaction, mesaj_original: str, mesaj_nou: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel = interaction.channel
    record = whisper_manager.find_by_original_content(
        author_id=interaction.user.id,
        channel_id=channel.id,
        content=mesaj_original,
    )

    if record is None:
        await interaction.response.send_message(
            "❌ I couldn't find any whisper of yours in this channel with that exact original "
            "text.\nMake sure you typed the ORIGINAL message identically (not an edited "
            "version, the message as it was first sent).",
            ephemeral=True,
        )
        return

    mesaj_nou = mesaj_nou.strip()
    if not mesaj_nou:
        await interaction.response.send_message("The new message can't be empty.", ephemeral=True)
        return

    new_history = record.history + [mesaj_nou]
    new_embed = build_history_embed(new_history)

    if len(new_embed.description or "") > 4096:
        await interaction.response.send_message(
            "This message has accumulated too many edits and exceeded Discord's limit. "
            "Delete it with /delete and send a new one.",
            ephemeral=True,
        )
        return

    try:
        discord_message = await channel.fetch_message(record.message_id)
        await discord_message.edit(embed=new_embed)
    except discord.NotFound:
        await interaction.response.send_message(
            "The original message no longer exists (it was deleted from Discord).", ephemeral=True
        )
        await whisper_manager.remove(record.message_id)
        return
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to edit that message.", ephemeral=True)
        return
    except discord.HTTPException as exc:
        log.error("Error editing whisper: %s", exc)
        await interaction.response.send_message("Something went wrong editing the message.", ephemeral=True)
        return

    record.history = new_history
    record.edited_at = datetime.now(timezone.utc).isoformat()
    await whisper_manager.update(record)

    await interaction.response.send_message(
        "✏️ The message was edited. The new version now appears below the original.",
        ephemeral=True,
    )
    log.info("Whisper %s edited by %s", record.message_id, interaction.user)


@bot.tree.command(name="my-whispers", description="Privately list your own anonymous whispers in this server.")
async def my_whispers_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    records = whisper_manager.list_by_author(
        author_id=interaction.user.id,
        guild_id=interaction.guild.id,
    )

    if not records:
        await interaction.response.send_message(
            "You haven't sent any whispers in this server yet.", ephemeral=True
        )
        return

    shown = records[:25]
    embed = discord.Embed(
        title="Your whispers",
        color=ANONYMOUS_COLOR,
    )

    for record in shown:
        jump_url = f"https://discord.com/channels/{record.guild_id}/{record.channel_id}/{record.message_id}"
        preview = record.original_content.strip()
        if len(preview) > 100:
            preview = preview[:97] + "..."
        label = "✏️ edited" if record.is_edited else "original"
        embed.add_field(
            name=f"#{record.channel_id} ({label})",
            value=f"{preview}\n[Jump to message]({jump_url})",
            inline=False,
        )

    if len(records) > len(shown):
        embed.set_footer(text=f"Showing your {len(shown)} most recent whispers out of {len(records)} total.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="whisper-reply",
    description="Reply anonymously to an existing whisper in this channel.",
)
@app_commands.describe(
    mesaj_tinta="Exact text of the whisper you want to reply to.",
    mesaj="Your reply, posted anonymously under the original.",
)
async def whisper_reply_command(
        interaction: discord.Interaction, mesaj_tinta: str, mesaj: str
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "I can't send anonymous messages in this type of channel.", ephemeral=True
        )
        return

    mesaj = mesaj.strip()
    if not mesaj:
        await interaction.response.send_message("Your reply can't be empty.", ephemeral=True)
        return
    if len(mesaj) > MAX_MESSAGE_LENGTH:
        await interaction.response.send_message(
            f"Your reply is too long (max {MAX_MESSAGE_LENGTH} characters).", ephemeral=True
        )
        return

    target_message, quoted_content = await find_target_message(channel, mesaj_tinta)

    if target_message is None:
        await interaction.response.send_message(
            "❌ I couldn't find any message in this channel with that exact text.\n"
            "Make sure you typed it exactly as it appears (capitalization, spaces, "
            "punctuation, emojis), and that it's recent enough to still be in the "
            "channel's history.",
            ephemeral=True,
        )
        return

    embed = build_reply_embed(quoted_content, mesaj)

    try:
        sent_message = await channel.send(
            embed=embed,
            reference=target_message,
            mention_author=False,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to send messages in this channel.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        log.error("Error sending whisper reply: %s", exc)
        await interaction.response.send_message("Something went wrong sending your reply.", ephemeral=True)
        return

    record = WhisperRecord(
        message_id=sent_message.id,
        channel_id=channel.id,
        guild_id=interaction.guild.id,
        author_id=interaction.user.id,
        original_content=mesaj,
        history=[mesaj],
    )
    await whisper_manager.add(record)

    await interaction.response.send_message("✅ Your anonymous reply was sent.", ephemeral=True)
    log.info(
        "Whisper reply sent by %s (%s) in #%s, replying to message %s",
        interaction.user,
        interaction.user.id,
        getattr(channel, "name", channel.id),
        target_message.id,
    )


class WhisperReplyModal(discord.ui.Modal, title="Whisper Reply"):
    reply_text = discord.ui.TextInput(
        label="Your anonymous reply",
        style=discord.TextStyle.paragraph,
        max_length=MAX_MESSAGE_LENGTH,
        required=True,
    )

    def __init__(self, target_message: discord.Message, quoted_content: str) -> None:
        super().__init__()
        self.target_message = target_message
        self.quoted_content = quoted_content

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mesaj = str(self.reply_text.value).strip()
        if not mesaj:
            await interaction.response.send_message("Your reply can't be empty.", ephemeral=True)
            return

        channel = self.target_message.channel
        embed = build_reply_embed(self.quoted_content, mesaj)

        try:
            sent_message = await channel.send(
                embed=embed,
                reference=self.target_message,
                mention_author=False,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to send messages in this channel.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            log.error("Error sending whisper reply from context menu: %s", exc)
            await interaction.response.send_message("Something went wrong sending your reply.", ephemeral=True)
            return

        record = WhisperRecord(
            message_id=sent_message.id,
            channel_id=channel.id,
            guild_id=interaction.guild.id,
            author_id=interaction.user.id,
            original_content=mesaj,
            history=[mesaj],
        )
        await whisper_manager.add(record)

        await interaction.response.send_message("✅ Your anonymous reply was sent.", ephemeral=True)
        log.info(
            "Whisper reply (context menu) sent by %s (%s) in #%s, replying to message %s",
            interaction.user,
            interaction.user.id,
            getattr(channel, "name", channel.id),
            self.target_message.id,
        )


@bot.tree.context_menu(name="Whisper Reply")
async def whisper_reply_context_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "I can't send anonymous messages in this type of channel.", ephemeral=True
        )
        return

    quoted_content = message.content
    if not quoted_content and message.embeds:
        quoted_content = message.embeds[0].description or ""
    if not quoted_content:
        quoted_content = "[no text content]"

    modal = WhisperReplyModal(target_message=message, quoted_content=quoted_content)
    await interaction.response.send_modal(modal)


class EditWhisperModal(discord.ui.Modal, title="Edit Whisper"):
    new_text = discord.ui.TextInput(
        label="New version",
        style=discord.TextStyle.paragraph,
        max_length=MAX_MESSAGE_LENGTH,
        required=True,
    )

    def __init__(self, target_message: discord.Message, record: WhisperRecord) -> None:
        super().__init__()
        self.target_message = target_message
        self.record = record
        self.new_text.default = record.current_content

    async def on_submit(self, interaction: discord.Interaction) -> None:
        mesaj_nou = str(self.new_text.value).strip()
        if not mesaj_nou:
            await interaction.response.send_message("The new message can't be empty.", ephemeral=True)
            return

        new_history = self.record.history + [mesaj_nou]
        new_embed = build_history_embed(new_history)

        if len(new_embed.description or "") > 4096:
            await interaction.response.send_message(
                "This message has accumulated too many edits and exceeded Discord's limit. "
                "Delete it and send a new one.",
                ephemeral=True,
            )
            return

        try:
            await self.target_message.edit(embed=new_embed)
        except discord.NotFound:
            await interaction.response.send_message(
                "The original message no longer exists (it was deleted from Discord).", ephemeral=True
            )
            await whisper_manager.remove(self.record.message_id)
            return
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to edit that message.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            log.error("Error editing whisper from context menu: %s", exc)
            await interaction.response.send_message("Something went wrong editing the message.", ephemeral=True)
            return

        self.record.history = new_history
        self.record.edited_at = datetime.now(timezone.utc).isoformat()
        await whisper_manager.update(self.record)

        await interaction.response.send_message(
            "✏️ The message was edited. The new version now appears below the original.",
            ephemeral=True,
        )
        log.info("Whisper %s edited (context menu) by %s", self.record.message_id, interaction.user)


@bot.tree.context_menu(name="Edit Whisper")
async def edit_whisper_context_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    record = whisper_manager.get(message.id)

    if record is None:
        await interaction.response.send_message(
            "This isn't a whisper message I'm tracking, so I can't edit it.", ephemeral=True
        )
        return

    if record.author_id != interaction.user.id:
        await interaction.response.send_message(
            "You can only edit your own whispers.", ephemeral=True
        )
        return

    modal = EditWhisperModal(target_message=message, record=record)
    await interaction.response.send_modal(modal)


@bot.tree.context_menu(name="Delete Whisper")
async def delete_whisper_context_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    record = whisper_manager.get(message.id)

    if record is None:
        await interaction.response.send_message(
            "This isn't a whisper message I'm tracking, so I can't delete it.", ephemeral=True
        )
        return

    if record.author_id != interaction.user.id:
        await interaction.response.send_message(
            "You can only delete your own whispers.", ephemeral=True
        )
        return

    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to delete that message.", ephemeral=True)
        return
    except discord.HTTPException as exc:
        log.error("Error deleting whisper from context menu: %s", exc)
        await interaction.response.send_message("Something went wrong deleting the message.", ephemeral=True)
        return

    await whisper_manager.remove(record.message_id)
    await interaction.response.send_message("🗑️ The anonymous message was deleted.", ephemeral=True)
    log.info("Whisper %s deleted (context menu) by %s", record.message_id, interaction.user)


@bot.tree.error
async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.error("Slash command error: %s", error)
    error_message = "Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_message, ephemeral=True)
        else:
            await interaction.response.send_message(error_message, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Missing bot token!\n"
            "Set the DISCORD_BOT_TOKEN environment variable, for example:\n"
            "  Windows (PowerShell):  $env:DISCORD_BOT_TOKEN='your-token'\n"
            "  Linux / macOS:         export DISCORD_BOT_TOKEN='your-token'\n"
            "Then run again: python whisper_bot.py"
        )
    bot.run(TOKEN)
