# Encode Bot (GDrive / Telegram)

Downloads videos from a GDrive folder/file or a direct link, encodes them with ffmpeg, and uploads
the result to GDrive (`/genc`) or Telegram (`/tenc`).

## Railway variables
| Variable | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | my.telegram.org -> API development tools |
| `BOT_TOKEN` | @BotFather |
| `OWNER_ID` | your Telegram user id (@userinfobot) |
| `TAG_NAME` (optional) | tag used in output folder names, default `Blaze Ash` |
| `MAX_CONCURRENT` (optional) | number of jobs at the same time, default 1 |
| `AUDIO_BITRATE` (optional) | default `128k` |

## Volume
Railway -> service -> Settings -> Volumes -> New Volume -> mount path `/data`.
Settings, the users list and rclone.conf files are stored in `/data` and survive restarts.

## Commands
- `/genc link` - encode and upload to GDrive (new folder: `{res} {source folder} [TAG]`)
- `/tenc link` - encode and upload to Telegram
- `link` = GDrive folder/file link or a direct download link; add `-n Name.mkv` to set the output name
- `/settings` - encode settings, quality name swap, send as media/document, auto delete, GDrive connect
- `/rclone` - send your rclone.conf in a private chat with `/rclone` as the caption
- `/status`, `/cancel id` (or tap the `/cancel_<id>` link in the status message)
- Owner only: `/panel`, `/restart`

The bot registers its own command menu on startup, nothing to set in BotFather.
