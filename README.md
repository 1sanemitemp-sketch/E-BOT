# Encode Bot (GDrive / Telegram)

Downloads videos from a GDrive folder/file or a direct link, encodes them with ffmpeg, and uploads
the result to GDrive (`/genc`) or Telegram (`/tenc`).

## Railway variables
| Variable | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | my.telegram.org -> API development tools |
| `BOT_TOKEN` | @BotFather |
| `OWNER_ID` | your Telegram user id (@userinfobot) |
| `LOG_CHANNEL` (optional) | log channel id (`-100...`); can also be set from the bot with `/setlog` |
| `TG_TRANSMISSIONS` (optional) | parallel Telegram upload/download chunks, default `20` (faster uploads) |
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

**Admins have the same power as the owner** (no limits, no queue): `/a`, `/ua`, `/alist`, `/ualist`, `/glist`, `/broadcast` (or `/bc`),
`/admin`, `/unadmin`, `/panel`, `/bot_settings`, `/setlog`, `/unsetlog`, `/restart`. The owner can never be removed.

- `/a user_id` / `/ua user_id` - authorize / unauthorize a user
- `/a all` - authorize everyone who started the bot (muted users are unmuted; banned users and admins are unchanged)
- `/ua all` - unauthorize everyone; the owner and the admins always stay
- `/a group_id` (or just `/a` inside the group) - authorize a group. `/ua group_id` removes it. `/glist` lists the groups.

### Groups
Make the bot an **admin** of the group (so it can read replies and delete the messages that set a tag or a format), then send `/a` in it.
Every member can then use the bot **in that group only** (not in PM, unless they are authorized themselves):
- `/status` in the group shows only the tasks that were started in that group
- files and results go to the user's PM (the user must have pressed Start once), the group gets a short card:
  `File(s) have been sent to User PM`

## Log channel
Create a channel, add the bot as an admin, then send `/setlog -100xxxxxxxxxx` (or set `LOG_CHANNEL` on Railway).
It receives: new users, every command (also from blocked users), task start / finish / failure / cancel with link, size and time,
approval requests, authorize / unauthorize (also all and groups), admin changes, panel actions, setting changes, GDrive connects,
groups the bot is added to, restarts.

## Auto Rename and -n
With Auto Rename on, `-n Name.mkv` first names the download, then the Auto Rename format builds the final name from it
(the same as `-e -n Pack.zip`: the archive gets the -n name, every video inside is renamed by the format).

## Thumbnails
`-t link` or a thumbnail set in /settings is used for every upload. Without one, a video (Media) gets a frame of the video and a
Document gets no thumbnail.

The bot registers its own command menu on startup, nothing to set in BotFather.
