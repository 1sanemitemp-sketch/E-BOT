# Encode Bot (GDrive / Telegram)

GDrive folder ba direct link theke video download -> ffmpeg encode -> GDrive (`/genc`) ba Telegram (`/tenc`) e upload.

## Railway Variables
| Variable | Kothay pabe |
|---|---|
| `API_ID`, `API_HASH` | my.telegram.org -> API development tools |
| `BOT_TOKEN` | @BotFather |
| `OWNER_ID` | tomar Telegram id (@userinfobot) |
| `TAG_NAME` (optional) | folder naam er tag, default `Blaze Ash` |
| `MAX_CONCURRENT` (optional) | ek shathe koyta job, default 1 |
| `AUDIO_BITRATE` (optional) | default `128k` |

## Volume
Railway -> service -> Settings -> Volumes -> New Volume -> Mount Path `/data`.
Settings, users list, rclone.conf shob `/data` e thake, restart e haray na.

## Commands
- `/genc link` - encode + GDrive upload (notun folder: `{res} {source folder} [TAG]`)
- `/tenc link` - encode + Telegram upload
- `link` = GDrive folder/file link ba direct download link; `-n Name.mkv` diye naam dite paro
- `/settings` - Encode settings, QUALITY NAME SWAP, Send as Media/Document, Auto Delete, GDrive
- `/rclone` - private chat e rclone.conf pathao (caption `/rclone`)
- `/status`, `/cancel id`
- Owner: `/panel`, `/restart`

Commands bot nijei startup e set kore nay (BotFather e kichu korte hobe na).
