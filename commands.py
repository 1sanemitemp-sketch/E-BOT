from pyrogram.types import BotCommand, BotCommandScopeChat

import config
from store import db

USER_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "All commands"),
    BotCommand("settings", "Encode, GDrive and upload settings"),
    BotCommand("genc", "Encode and upload to GDrive"),
    BotCommand("tenc", "Encode and upload to Telegram"),
    BotCommand("rclone", "Save your rclone.conf"),
    BotCommand("status", "Running jobs"),
    BotCommand("cancel", "Cancel a job"),
]
ADMIN_EXTRA = [
    BotCommand("a", "Authorize a user"),
    BotCommand("ua", "Unauthorize a user"),
    BotCommand("alist", "List authorized users"),
    BotCommand("ualist", "List unauthorized users"),
]
OWNER_EXTRA = [
    BotCommand("admin", "Make a user admin"),
    BotCommand("unadmin", "Remove an admin"),
    BotCommand("panel", "User control panel"),
    BotCommand("restart", "Restart the bot"),
]


def commands_for(uid):
    cmds = list(USER_COMMANDS)
    if db.is_admin(uid):
        cmds += ADMIN_EXTRA
    if uid == config.OWNER_ID:
        cmds += OWNER_EXTRA
    return cmds


async def refresh_commands(client, uid):
    """Admins and the owner get an extended command menu, everyone else the default one."""
    scope = BotCommandScopeChat(chat_id=uid)
    try:
        if db.is_admin(uid):
            await client.set_bot_commands(commands_for(uid), scope=scope)
        else:
            await client.delete_bot_commands(scope=scope)
    except Exception:
        pass
