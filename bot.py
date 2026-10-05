import asyncio
import json
import logging
import os
import random
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in .env")

CONFIG_PATH = Path(__file__).parent / "config.json"
MEDIA_EXTENSIONS = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
    ".jfif": "photo", ".bmp": "photo",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video",
    ".gif": "animation",
    ".pdf": "document",
}
# Archives are unpacked and posted as an album, never sent as a raw file.
COMIC_EXTENSIONS = {".zip", ".cbz"}
# Only photos and videos are allowed to share a Telegram media group.
COMIC_PAGE_TYPES = {"photo", "video"}
MEDIA_GROUP_LIMIT = 10
CHUNK_DELAY = 2
COMIC_ORDERS = ("name", "date", "zip_order")
# Tracks how far a multi-batch comic got, so a retry resumes instead of re-posting.
STATE_FILENAME = ".post_state.json"
MAX_RETRIES = 1
RETRY_DELAY = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("auto-poster")


def load_config() -> list[dict]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    groups = data.get("groups", [])
    if not groups:
        raise RuntimeError("No groups defined in config.json")
    return groups


# bot.py reads these with [] rather than .get(), so a missing one is fatal, not a default.
REQUIRED_GROUP_KEYS = ("name", "chat_id", "folder")


def group_label(group, index: int) -> str:
    """A name for logging that still works when 'name' is the broken key."""
    if isinstance(group, dict):
        name = group.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return f"#{index + 1}"


def missing_keys(group: dict) -> list[str]:
    """Required keys that are absent, empty, or the wrong type to be usable."""
    bad = []
    for key in REQUIRED_GROUP_KEYS:
        value = group.get(key)
        # chat_id may legitimately be written as a bare number in config.json.
        if key == "chat_id" and isinstance(value, int) and not isinstance(value, bool):
            continue
        if not isinstance(value, str) or not value.strip():
            bad.append(key)
    return bad


def validate_groups(groups: list[dict]) -> list[dict]:
    """
    Drops groups the bot cannot use, instead of dying on a KeyError partway through
    setup. Runs before setup_folders, which is the first place a bad group would bite.
    """
    usable = []
    seen_chat_ids = {}

    for index, group in enumerate(groups):
        label = group_label(group, index)

        if not isinstance(group, dict):
            log.error("Group %s is not an object in config.json, skipping it", label)
            continue

        bad = missing_keys(group)
        if bad:
            log.error(
                "Group %s is unusable: %s not set in config.json. Skipping it.",
                label, ", ".join(bad),
            )
            continue

        chat_id = str(group["chat_id"]).strip()
        if chat_id in seen_chat_ids:
            # Jobs are keyed by chat_id with replace_existing=True, so a duplicate
            # silently unschedules the group that came before it.
            log.warning(
                "Group '%s' shares its chat_id with '%s'. Only the last of them is scheduled.",
                label, seen_chat_ids[chat_id],
            )
        seen_chat_ids[chat_id] = label

        usable.append(group)

    skipped = len(groups) - len(usable)
    if skipped:
        log.error("%d of %d group(s) skipped because of config errors above", skipped, len(groups))

    return usable


def enabled_groups(groups: list[dict]) -> list[dict]:
    """Groups with "enabled": false are kept in config.json but never scheduled."""
    active = []
    for g in groups:
        if g.get("enabled", True):
            active.append(g)
        else:
            log.info("Skipping disabled group '%s'", g.get("name", "?"))
    return active


def setup_folders(groups: list[dict]):
    for g in groups:
        folder = Path(g["folder"])
        to_send = folder / "To_Send"
        already_sent = folder / "Already_Sent"
        to_send.mkdir(parents=True, exist_ok=True)
        already_sent.mkdir(parents=True, exist_ok=True)
        for sub in (to_send, already_sent):
            gitkeep = sub / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.touch()


def media_type_for(path: Path) -> str | None:
    return MEDIA_EXTENSIONS.get(path.suffix.lower())


def load_state(group_folder: Path) -> dict:
    path = group_folder / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s (%s), starting from scratch", path, e)
        return {}


def save_state(group_folder: Path, state: dict):
    path = group_folder / STATE_FILENAME
    try:
        if not state:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)  # atomic, so a crash can't leave a half-written state
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)


def prune_state(group_folder: Path, state: dict) -> dict:
    """Drop entries whose archive is no longer anywhere in the group."""
    live = {}
    for archive_name, entry in state.items():
        in_queue = (group_folder / "To_Send" / archive_name).exists()
        in_sent = (group_folder / "Already_Sent" / archive_name).exists()
        if in_queue or in_sent:
            live[archive_name] = entry
        else:
            log.info("Dropping stale resume state for '%s'", archive_name)
    return live


def comic_signature(archive: Path, order_mode: str, total_batches: int) -> dict:
    """Identifies an exact archive + batching. Any change invalidates a resume."""
    return {
        "size": archive.stat().st_size,
        "order": order_mode,
        "batches": total_batches,
    }


def resume_point(group_folder: Path, archive: Path, signature: dict) -> int:
    """How many batches of this comic already reached Telegram."""
    entry = load_state(group_folder).get(archive.name)
    if not isinstance(entry, dict):
        return 0
    if any(entry.get(key) != value for key, value in signature.items()):
        log.info("'%s' changed since the last attempt, restarting from page 1", archive.name)
        return 0
    done = entry.get("done", 0)
    if not isinstance(done, int) or done < 0:
        return 0
    return min(done, signature["batches"])


def record_progress(group_folder: Path, archive: Path, signature: dict, done: int):
    state = load_state(group_folder)
    state[archive.name] = {**signature, "done": done}
    save_state(group_folder, prune_state(group_folder, state))


def clear_progress(group_folder: Path, archive: Path):
    state = load_state(group_folder)
    if state.pop(archive.name, None) is None and not state:
        return
    save_state(group_folder, prune_state(group_folder, state))


def is_comic(path: Path) -> bool:
    return path.suffix.lower() in COMIC_EXTENSIONS


def is_postable(path: Path) -> bool:
    return media_type_for(path) is not None or is_comic(path)


def comic_page_count(archive: Path, order_mode: str = "name") -> int:
    """How many upload slots this file costs: its pages if a comic, else 1.

    Namelist only, nothing is unpacked. Uses the same filters as extract_comic so
    the count agrees with what would actually be posted. Anything unreadable
    counts as 1 rather than blocking the queue.
    """
    if not is_comic(archive):
        return 1
    try:
        with zipfile.ZipFile(archive) as zf:
            pages = 0
            for info in zf.infolist():
                if info.is_dir() or "__MACOSX" in info.filename:
                    continue
                base = os.path.basename(info.filename.replace("\\", "/"))
                if not base or base.startswith("."):
                    continue
                if media_type_for(Path(base)) not in COMIC_PAGE_TYPES:
                    continue
                pages += 1
            return max(1, pages)
    except Exception as e:
        log.warning("Could not read archive %s, counting it as 1 slot: %s", archive.name, e)
        return 1


def slot_cost(path: Path, order_mode: str = "name", pages_as_uploads: bool = False) -> int:
    """Upload slots one queue entry consumes. Files always cost 1; a comic costs
    its pages only when the group opts in with comic_pages_as_uploads."""
    if pages_as_uploads and is_comic(path):
        return comic_page_count(path, order_mode)
    return 1


def scan_folder(folder: Path, recursive: bool = False) -> list[Path]:
    if not folder.is_dir():
        return []
    entries = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in entries if p.is_file() and is_postable(p))


def natural_key(name: str) -> list:
    """Sort key where digit runs compare numerically, so page2 sorts before page10."""
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def order_pages(entries: list[zipfile.ZipInfo], mode: str) -> list[zipfile.ZipInfo]:
    if mode == "zip_order":
        return list(entries)
    if mode == "date":
        # Zip timestamps only have 2s resolution, so tie-break on the name.
        return sorted(entries, key=lambda i: (i.date_time, natural_key(i.filename)))
    return sorted(entries, key=lambda i: natural_key(i.filename))


def extract_comic(archive: Path, dest: Path, order_mode: str) -> list[Path]:
    """Unpack an archive's postable pages into dest, in reading order."""
    pages = []
    with zipfile.ZipFile(archive) as zf:
        entries = []
        for info in zf.infolist():
            if info.is_dir() or "__MACOSX" in info.filename:
                continue
            base = os.path.basename(info.filename.replace("\\", "/"))
            if not base or base.startswith("."):
                continue
            if media_type_for(Path(base)) not in COMIC_PAGE_TYPES:
                log.warning("  skipping non-page entry: %s", info.filename)
                continue
            entries.append(info)

        for index, info in enumerate(order_pages(entries, order_mode)):
            base = os.path.basename(info.filename.replace("\\", "/"))
            log.info("  %3d. %s", index + 1, base)
            # Index prefix keeps disk order aligned; using the basename defeats zip-slip.
            target = dest / f"{index:04d}_{base}"
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            pages.append(target)
    return pages


def get_next_media(group_folder: Path, count: int = 1, post_order: str = "oldest",
                   comic_order: str = "name", pages_as_uploads: bool = False) -> list[Path]:
    to_send = group_folder / "To_Send"
    already_sent = group_folder / "Already_Sent"

    files = scan_folder(to_send)
    if post_order == "newest":
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    elif post_order == "random":
        random.shuffle(files)
    else:
        files.sort(key=lambda p: p.stat().st_mtime)

    budget = max(1, count)
    if not pages_as_uploads:
        results = files[:budget]
    else:
        # Pack the run by upload slots, in queue order: a 6-page comic consumes 6
        # of the budget. First in line always starts, even over budget, so a comic
        # bigger than the budget posts rather than wedging the queue behind it.
        results = []
        used = 0
        for f in files:
            cost = slot_cost(f, comic_order, True)
            if used + cost <= budget:
                results.append(f)
                used += cost
            elif not results:
                results.append(f)
                used += cost
                break
            else:
                break

    if not results:
        all_sent_files = scan_folder(already_sent, recursive=True)
        if all_sent_files:
            results = random.sample(all_sent_files, min(budget, len(all_sent_files)))

    return results


async def upload_single(bot: Bot, chat_id: str, file_path: Path, media: str) -> bool:
    for attempt in range(1 + MAX_RETRIES):
        try:
            if media == "photo":
                await bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path))
            elif media == "video":
                await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path))
            elif media == "animation":
                await bot.send_animation(chat_id=chat_id, animation=FSInputFile(file_path))
            else:
                await bot.send_document(chat_id=chat_id, document=FSInputFile(file_path))
            return True
        except Exception as e:
            log.warning("Upload attempt %d failed for %s: %s", attempt + 1, file_path.name, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    return False


async def upload_media_group(bot: Bot, chat_id: str, files: list[Path]) -> bool:
    for attempt in range(1 + MAX_RETRIES):
        try:
            builder = MediaGroupBuilder(caption=None)
            for f in files:
                builder.add(type=media_type_for(f), media=FSInputFile(f))
            await bot.send_media_group(chat_id=chat_id, media=builder.build())
            return True
        except Exception as e:
            log.warning("Media group attempt %d failed: %s", attempt + 1, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    return False


async def upload_comic(bot: Bot, chat_id: str, archive: Path, order_mode: str, group_folder: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="comic_") as tmp:
        log.info("Unpacking '%s' (page order: %s)", archive.name, order_mode)
        try:
            pages = await asyncio.to_thread(extract_comic, archive, Path(tmp), order_mode)
        except Exception as e:
            log.error("Could not read archive %s: %s", archive.name, e)
            return False

        if not pages:
            log.error("No postable pages found in %s", archive.name)
            return False

        chunks = [pages[i:i + MEDIA_GROUP_LIMIT] for i in range(0, len(pages), MEDIA_GROUP_LIMIT)]
        signature = comic_signature(archive, order_mode, len(chunks))
        start = resume_point(group_folder, archive, signature)
        if start:
            log.info(
                "Resuming '%s' at batch %d/%d - %d page(s) already posted",
                archive.name, start + 1, len(chunks), sum(len(c) for c in chunks[:start]),
            )

        for index in range(start, len(chunks)):
            chunk = chunks[index]
            if len(chunk) == 1:
                # Telegram rejects a media group holding a single item.
                ok = await upload_single(bot, chat_id, chunk[0], media_type_for(chunk[0]))
            else:
                ok = await upload_media_group(bot, chat_id, chunk)
            if not ok:
                log.error(
                    "Comic '%s' failed on batch %d/%d - will resume there next run",
                    archive.name, index + 1, len(chunks),
                )
                return False
            if index + 1 < len(chunks):
                # Persist after every batch so a crash resumes too, not just a failed send.
                record_progress(group_folder, archive, signature, index + 1)
                await asyncio.sleep(CHUNK_DELAY)

        clear_progress(group_folder, archive)
        return True


def move_to_sent(source: Path, group_folder: Path):
    already_sent = group_folder / "Already_Sent"
    if source.parent == already_sent:
        log.info("Already in Already_Sent, skipping move: %s", source.name)
        return
    dest = already_sent / source.name
    if dest.exists():
        log.info("Destination already exists, skipping move: %s", dest)
        return
    shutil.move(str(source), str(dest))
    log.info("Moved %s -> %s", source.name, already_sent)


async def post_task(bot: Bot, group: dict):
    name = group["name"]
    chat_id = group["chat_id"]
    folder = Path(group["folder"])
    count = group.get("files_per_post", 1)
    post_order = group.get("post_order", "oldest")
    comic_order = group.get("comic_order", "name")
    pages_as_uploads = group.get("comic_pages_as_uploads", False) is True
    if comic_order not in COMIC_ORDERS:
        log.warning("[%s] Unknown comic_order '%s', falling back to 'name'", name, comic_order)
        comic_order = "name"

    log.info("[%s] Running post task (up to %d upload(s), order: %s%s)", name, count, post_order,
             ", comics count by pages" if pages_as_uploads else "")
    results = get_next_media(folder, count, post_order, comic_order, pages_as_uploads)
    if not results:
        log.warning("[%s] No media available in To_Send or Already_Sent", name)
        return

    for source in results:
        comic = is_comic(source)
        label = f"comic '{source.name}'" if comic else f"file '{source.name}'"
        log.info("[%s] Posting %s", name, label)

        if comic:
            success = await upload_comic(bot, chat_id, source, comic_order, folder)
        else:
            success = await upload_single(bot, chat_id, source, media_type_for(source))

        if success:
            if source.parent == folder / "To_Send":
                log.info("[%s] Upload successful, moving %s", name, label)
                move_to_sent(source, folder)
            else:
                log.info("[%s] Upload successful (from Already_Sent, no move needed)", name)
        else:
            log.error("[%s] Upload failed after retries, NOT moving %s", name, label)


def schedule_groups(bot: Bot, groups: list[dict]) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    for group in groups:
        name = group["name"]
        schedule = group.get("schedule", {})
        interval = schedule.get("interval_minutes")
        hour = schedule.get("hour", 12)
        minute = schedule.get("minute", 0)
        jitter = group.get("jitter_minutes", 15)

        async def task_with_jitter(b=bot, g=group, j=jitter):
            delay = random.uniform(-j * 60, j * 60)
            log.info("[%s] Jitter delay: %.0fs", g["name"], delay)
            await asyncio.sleep(delay)
            await post_task(b, g)

        if interval:
            trigger = IntervalTrigger(minutes=interval)
            scheduler.add_job(
                task_with_jitter,
                trigger,
                id=f"group_{group['chat_id']}",
                replace_existing=True,
            )
            log.info("Scheduled group '%s' every %d min (jitter: +/- %d min)", name, interval, jitter)
        else:
            trigger = CronTrigger(hour=hour, minute=minute)
            scheduler.add_job(
                task_with_jitter,
                trigger,
                id=f"group_{group['chat_id']}",
                replace_existing=True,
            )
            log.info("Scheduled group '%s' daily at %02d:%02d (jitter: +/- %d min)", name, hour, minute, jitter)

    return scheduler


async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set. Exiting.")
        return

    groups = load_config()
    groups = validate_groups(groups)
    if not groups:
        log.error("No usable groups left in config.json. Fix the errors above and restart.")
        return

    # Folders are prepared for every group, disabled ones included, so re-enabling
    # a group does not need a restart to get its queue back.
    setup_folders(groups)

    active = enabled_groups(groups)
    if not active:
        log.warning("Every group is disabled. Nothing will be posted.")

    bot = Bot(token=BOT_TOKEN)
    scheduler = schedule_groups(bot, active)
    scheduler.start()

    me = await bot.get_me()
    log.info("Bot started as @%s (%s)", me.username, me.id)
    log.info("Managing %d of %d groups", len(active), len(groups))

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
