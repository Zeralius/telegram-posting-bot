# Telegram Auto-Post Bot

Automatically posts media to multiple Telegram groups on a schedule. Supports images, videos, GIFs, and comic-style media groups.

## Features

- **Multi-group support** - manage unlimited groups from one bot instance
- **Per-group scheduling** - each group gets its own posting time and frequency
- **Flexible scheduling** - post once daily at a specific time, or every N minutes
- **Humanized timing** - configurable jitter (+/- minutes) so posts don't land exactly on the hour
- **Comic support** - drop a `.zip` in `To_Send/` and its pages are posted as a single album, split into batches of 10
- **Configurable post order** - choose oldest, newest, or random file selection per group
- **Batch posting** - post multiple files per scheduled run (`files_per_post`)
- **Comic page counting** - optionally count each page of a comic as its own upload
  (`comic_pages_as_uploads`), so a 6-page comic consumes 6 upload slots
- **Fallback mode** - when To_Send is empty, re-posts random files from Already_Sent
- **Safe file handling** - files only move to Already_Sent after successful upload
- **Resumable comics** - a comic interrupted part-way picks up at the batch it failed on instead of re-posting
- **Retry logic** - failed uploads retry once before skipping

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name and username for your bot
4. Copy the **bot token** you receive

### 2. Add Bot to Groups

1. Add your bot to each target Telegram group
2. Promote it to **Administrator** (it needs permission to post messages)
3. Get each group's **chat ID**:
   - Forward a message from the group to **@userinfobot**, or
   - Call `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` after posting a message in the group

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

Copy `.env.example` to `.env` and add your bot token:

```bash
cp .env.example .env
```

```
BOT_TOKEN=123456:ABC-DEF...
```

The repo ships with two placeholder groups (`Test Group A` and `Test Group B`) in
`config.json`. Replace their `chat_id` values with your own, rename them, and add or
remove entries as needed (see [Configuration Reference](#configuration-reference) below).
Media inside `groups/*/To_Send/` and `groups/*/Already_Sent/` is gitignored, so your own
queues never end up in version control.

### 5. Run

```bash
python bot.py
```

The bot will start, validate your groups, and begin posting on schedule.

## Folder Structure

```
telegram-posting-bot/
├── bot.py              # Main bot script
├── config.json         # Group definitions
├── .env.example        # Template for .env
├── .env                # Bot token (keep secret!)
├── requirements.txt    # Python dependencies
└── groups/
    ├── test_group_a/
    │   ├── To_Send/         # Drop media here
    │   └── Already_Sent/    # Auto-populated after posting
    ├── test_group_b/
    │   ├── To_Send/
    │   ├── Already_Sent/
    │   └── .post_state.json # Only while a long comic is part-posted; safe to delete
    └── ...
```

### Adding Media

- **Single files**: Drop images, videos, or GIFs directly into `groups/<name>/To_Send/`
- **Comics**: Drop a `.zip` (or `.cbz`) into `To_Send/`. The bot unpacks it to a temp folder, posts the pages as a Telegram album, and then moves the archive itself to `Already_Sent/`. The zip is never sent as a downloadable file.

Comics longer than 10 pages are split across several albums, because that is Telegram's hard cap on a media group. If one of those batches fails, the bot writes a `.post_state.json` in the group folder recording how many batches already landed, leaves the archive in `To_Send/`, and resumes from that batch on the next run rather than re-posting the pages that already went out. The state file is written after every batch, so a crash or restart mid-comic resumes as well. It is deleted once the comic finishes, and the resume is discarded if you swap the archive or change `comic_order` in the meantime. Non-image entries inside the archive (`.txt`, `.nfo`, `__MACOSX/`, dotfiles) are skipped, as are `.gif` pages — Telegram does not allow animations inside an album.

Subfolders inside `To_Send/` are ignored. Zips are the only way to post a comic.

#### Page order

The bot logs the page order it resolved every time it posts a comic, so you can check it against the group. Set `comic_order` per group to change how pages are sorted:

| Value | Behavior |
|---|---|
| `"name"` (default) | Natural filename sort, so `page2` comes before `page10`. Matches what comic readers do and is right for almost every archive. |
| `"date"` | The modification timestamp stored inside the zip for each page. |
| `"zip_order"` | The order the pages are physically stored in the archive. |

Prefer `"name"` unless a source gives you archives whose filenames carry no usable page numbers (hash-named scrapes, for example). Two warnings about `"date"`: zip timestamps have only 2-second resolution and carry no timezone, and they record when each page was *saved*, which for a scraped gallery is download order rather than reading order. When two pages share a timestamp, `"date"` falls back to the natural name sort.

### Adding a New Group

1. Create the folder structure: `groups/<name>/To_Send/` and `groups/<name>/Already_Sent/`
2. Add an entry to `config.json` (see below)
3. Restart the bot

The bot auto-creates folders and `.gitkeep` files on startup, so you can also just add the group to `config.json` and restart.

## Configuration Reference

### config.json

```json
{
  "groups": [
    {
      "name": "My Meme Group",
      "chat_id": "-1001234567890",
      "folder": "groups/my_meme_group",
      "schedule": {
        "hour": 10,
        "minute": 0
      },
      "jitter_minutes": 15,
      "files_per_post": 1,
      "post_order": "oldest"
    }
  ]
}
```

### Group Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | - | Display name for logging |
| `chat_id` | Yes | - | Telegram group chat ID (negative number) |
| `folder` | Yes | - | Path to group folder relative to project root |
| `schedule` | Yes | - | Scheduling configuration (see below) |
| `enabled` | No | `true` | Set to `false` to keep the group in config.json but leave it out of the schedule. Its folders are still created, so re-enabling it needs no other change. |
| `jitter_minutes` | No | `15` | Random delay +/- minutes before posting |
| `files_per_post` | No | `1` | How many files to post per scheduled run |
| `comic_pages_as_uploads` | No | `false` | Count each page of a comic as its own upload against `files_per_post`. A 6-page comic then consumes 6 slots, so it holds the queue the way 6 single posts would. The comic still posts whole in one run; the first in line always starts even when it is bigger than the budget, so a long comic cannot wedge the queue. |
| `post_order` | No | `"oldest"` | File selection order: `"oldest"`, `"newest"`, or `"random"` |
| `comic_order` | No | `"name"` | Page order inside a comic zip: `"name"`, `"date"`, or `"zip_order"` |

### Startup Validation

On startup the bot checks every group before scheduling anything, and reports problems
instead of crashing on them:

- A group missing `name`, `chat_id`, or `folder` (or with any of them left blank) is
  logged as an error and **skipped**. The rest of the groups still run. These three are
  read directly rather than with a default, so before this check a single missing key
  took the whole bot down at startup.
- Two groups sharing a `chat_id` log a warning. Jobs are keyed by chat ID, so only the
  last of them ends up scheduled and the earlier one silently never posts.
- If no usable groups are left, the bot logs an error and exits rather than idling.

### Schedule Options

**Once daily at a specific time:**

```json
"schedule": {
  "hour": 10,
  "minute": 30
}
```

Posts once per day at 10:30 (plus jitter).

**Every N minutes:**

```json
"schedule": {
  "interval_minutes": 60
}
```

Posts every 60 minutes (plus jitter).

### Post Order

Controls which files are picked from `To_Send`:

| Value | Behavior |
|---|---|
| `"oldest"` | Posts the oldest file first (by file modification time) |
| `"newest"` | Posts the newest file first |
| `"random"` | Picks a random file |

Comic zips sit in the same queue as everything else, so `post_order` decides when a comic's turn comes up. `comic_order` then decides the order of the pages within it.

### Example Configurations

**Post 3 random files every hour:**

```json
{
  "name": "Active Group",
  "chat_id": "-100111222333",
  "folder": "groups/active_group",
  "schedule": { "interval_minutes": 60 },
  "jitter_minutes": 10,
  "files_per_post": 3,
  "post_order": "random"
}
```

**Post 1 oldest file daily at 18:00:**

```json
{
  "name": "Evening Channel",
  "chat_id": "-100444555666",
  "folder": "groups/evening_channel",
  "schedule": { "hour": 18, "minute": 0 },
  "jitter_minutes": 15,
  "files_per_post": 1,
  "post_order": "oldest"
}
```

## How It Works

1. **Bot starts** - validates token, creates folders, registers scheduled jobs
2. **Scheduled job fires** - applies random jitter delay, then runs post task
3. **Media selection** - picks files based on `post_order` and `files_per_post`
4. **Upload** - sends to Telegram with retry logic
5. **Move** - only moves files from To_Send to Already_Sent after confirmed success
6. **Fallback** - if To_Send is empty, re-posts random files from Already_Sent

## Supported Media Types

| Extension | Telegram Type |
|---|---|
| `.jpg`, `.jpeg`, `.png`, `.webp`, `.jfif`, `.bmp` | Photo |
| `.mp4`, `.mov`, `.avi`, `.mkv` | Video |
| `.gif` | Animation |
| `.pdf` | Document |
| `.zip`, `.cbz` | Unpacked and posted as an album (see [Adding Media](#adding-media)) |

## Requirements

- Python 3.10+
- aiogram 3.x
- APScheduler 3.x
- python-dotenv
