import logging

from pyrogram.types import BotCommand, BotCommandScopeChat

import config
from store import db

log = logging.getLogger("menu")

USER = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "All commands"),
    BotCommand("settings", "Encode, GDrive and upload settings"),
    BotCommand("genc", "Encode and upload to GDrive"),
    BotCommand("tenc", "Encode and upload to Telegram"),
    BotCommand("gup", "Upload to GDrive without encoding"),
    BotCommand("tup", "Upload to Telegram without encoding"),
    BotCommand("rclone", "Save your rclone.conf"),
    BotCommand("status", "Running jobs"),
    BotCommand("cancel", "Cancel a job"),
]
ADMIN_EXTRA = [
    BotCommand("a", "Authorize a user, a group or all: /a id"),
    BotCommand("ua", "Unauthorize a user, a group or all: /ua id"),
    BotCommand("alist", "List authorized users"),
    BotCommand("ualist", "List unauthorized users"),
    BotCommand("glist", "List authorized groups"),
    BotCommand("broadcast", "Message everyone who started the bot"),
    BotCommand("admin", "Make an admin: /admin user_id"),
    BotCommand("unadmin", "Remove an admin: /unadmin user_id"),
    BotCommand("panel", "User control panel"),
    BotCommand("bot_settings", "Global bot settings"),
    BotCommand("setlog", "Set the log channel: /setlog id"),
    BotCommand("unsetlog", "Remove the log channel"),
    BotCommand("restart", "Restart the bot"),
]


def commands_for(uid):
    cmds = list(USER)
    if db.is_admin(uid):
        cmds += ADMIN_EXTRA
    return cmds


async def apply_menu(client, uid):
    try:
        await client.set_bot_commands(commands_for(uid), scope=BotCommandScopeChat(chat_id=uid))
    except Exception as e:
        log.info("Could not set the command menu for %s: %s", uid, e)


async def apply_all(client):
    await client.set_bot_commands(USER)
    for uid in db.admin_ids():
        await apply_menu(client, uid)
