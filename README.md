# Encode Bot (GDrive / Telegram)

Downloads videos from a GDrive folder/file or a direct link, encodes them with ffmpeg, and uploads
the result to GDrive (`/genc`) or Telegram (`/tenc`).

## Railway variables
| Variable | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | my.telegram.org -> API development tools |
| `BOT_TOKEN` | @BotFather |
| `OWNER_ID` | your Telegram user id (@userinfobot) |
| `ADMIN_CONTACTS` (optional) | shown to unapproved users, default `@sanemi111 @abbacalling420` |

Everything else (tag, audio bitrate, max concurrent tasks, ...) is changed from the bot itself.

## Volume
Railway -> service -> Settings -> Volumes -> New Volume -> mount path `/data`.
Settings, the users list and rclone.conf files are stored in `/data` and survive restarts.

## Commands
- `/genc link` - encode and upload to GDrive
- `/tenc link` - encode and upload to Telegram
- `/gup link` / `/tup link` - the same, but nothing is encoded (plain upload to GDrive / Telegram). With `-e` an archive is extracted and every file in it is uploaded; a file that is not an archive is simply uploaded.
- `link` = GDrive folder/file link or a direct download link
- A link to an archive without `-e` is not encoded, it is uploaded as it is. Files above 2GB are uploaded in parts (`name.zip.001`, `.002`, ...).
- Folders are created only for folder links and for archives with several videos: `{tag} {name}` (the tag is set by each user in /settings and is empty by default; the resolution in the name is swapped when QUALITY NAME SWAP is on). A single file is uploaded without a folder.
- `-n Name.mkv` output name (with `-e` it names the downloaded archive), `-e` extract an archive and encode its videos,
  `-z` zip the encoded files into one zip, `-t image_link` one-off thumbnail
- `/settings` - encode settings (codec, bitrate, CRF 16-28, preset up to placebo, ...), tag, quality name swap, send as media/document, auto delete (permanent, a source folder is deleted too), thumbnail, GDrive connect
- `/rclone` - send `/rclone`, then send your rclone.conf file (no caption needed)
- `/settings` also has Reset buttons (encode settings, and Reset all)
- `/status` - one shared status message with every running task (everyone sees all tasks, but you can cancel only your own; admins can cancel any)
- `/settings` also has Tag, Auto Rename (with the list of all formats) and Color Fix (explicit colour tags so the encode looks the same as the source in every player)
- `/bot_settings` (owner): max tasks at once and tasks per user (0 = unlimited), approval, GDrive splitting (off by default), accepting tasks
- `/status`, `/cancel id` (or tap the `/cancel_<id>` link in a status message)

## Access control
New users must be approved. They get their ID and a "Request Approval" button; requests reach the owner and the admins.
- Admins and owner: `/a user_id`, `/ua user_id`, `/alist`, `/ualist`, `/broadcast` (or `/bc`): text, or reply to any message to broadcast it
- Owner only: `/admin user_id`, `/unadmin user_id`, `/panel`, `/bot_settings`, `/restart`

The bot registers its own command menu on startup, nothing to set in BotFather.
