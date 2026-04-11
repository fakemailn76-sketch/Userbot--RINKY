# -*- coding: utf-8 -*-
import asyncio, logging
from datetime import datetime, timedelta, timezone
from threading import Event
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import UserStatusOnline, UserStatusRecently  # for status checks

# -----------------------------
# Logging Setup
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)
logging.getLogger("telethon").setLevel(logging.WARNING)

# -----------------------------
# Hardcoded credentials
# -----------------------------
API_ID = 26311594
API_HASH = "8b955fda757c8f8ef6c5ffdcbb02cce6"
SESSION_STRING = "1BVtsOKsBu4cRKPyDA2MAOIL2XLTDRMM5BdSbgwOWt7-IMWq617SFA2ROKohZN3UVHO5i4INH8uqROlLUH10rkfo1vgpCJX2tg_-Jb4b3n9AYrkFeL6345HVYdGJKTFU2yZ8P-7ciR-kk9j-Zerbvaguwart8pT0QiUemxIysj2Pb7poA8HA93E_cggiWU1pXsz4HxGS4hRoBNcB47brKx4xvBfuBT8eYOQAlTpo5ITTKavjbaT8K8oFjeemMz1hKGwXQRFVs-22EoqqJvHRW6pbaETzqO_ZSSAgMqnoZ2_6KZlSYlPEXQZZNHlsitZpKJUYKnLm6e6Jmw3KNhprSMGEBSCCykH8="
ADMIN_ID = 7938724671

# -----------------------------
# Groups
# -----------------------------
GROUP_IDS = [-1002005046318]  # other groups can be added to this list

# This single GROUP will have the admin-online/offline behavior applied
SPECIAL_GROUP_ID = -1002005046318

# -----------------------------
# Admin usernames to watch (without @)
# -----------------------------
ADMIN_USERNAMES = ["ChatFmRobot", "FmRobotBD"]

# If an admin hides status (status == None), decide behavior:
# True  -> treat hidden/unknown as ONLINE (safe)
# False -> treat hidden/unknown as OFFLINE (permissive)
TREAT_HIDDEN_AS_ONLINE = False  # you asked for permissive behavior earlier

# -----------------------------
# Telethon client
# -----------------------------
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# -----------------------------
# Global state
# -----------------------------
is_sending = Event()
current_message = ""   
send_task: asyncio.Task | None = None
send_interval = 300  

last_sent_messages = {}   # gid -> msg_id
last_sent_times = {}      # gid -> datetime (UTC)
next_run_times = {}   # gid -> datetime (UTC)
group_intervals = {}      
group_overrides = {}      
floodwait_logged = set()  
stopped_groups = set()     

# Admin resolution & tracking (for ONLINE checks on SPECIAL_GROUP_ID)
ADMIN_IDS = set()         # resolved numeric ids from ADMIN_USERNAMES
ADMIN_ENTITIES = {}       # id -> entity
admin_was_online = None   # None = unknown, True/False tracked from previous iteration (for special group)

# -----------------------------
# Helpers
# -----------------------------
async def get_group_name(gid):
    try:
        entity = await client.get_entity(gid)
        return getattr(entity, "title", str(gid))
    except Exception:
        return str(gid)

async def resolve_admin_usernames():
    """Resolve ADMIN_USERNAMES into numeric IDs (fill ADMIN_IDS and ADMIN_ENTITIES)."""
    ADMIN_IDS.clear()
    ADMIN_ENTITIES.clear()
    for name in ADMIN_USERNAMES:
        try:
            ent = await client.get_entity(name)
            ADMIN_IDS.add(ent.id)
            ADMIN_ENTITIES[ent.id] = ent
            logging.info(f"🔐 Resolved admin {name} -> {ent.id}")
        except Exception as e:
            logging.warning(f"⚠️ Could not resolve admin '{name}': {e}")

def is_sender_admin(uid: int) -> bool:
    """Allow commands if sender is the original ADMIN_ID or one of resolved ADMIN_IDS."""
    if uid == ADMIN_ID:
        return True
    return uid in ADMIN_IDS

async def is_any_admin_online():
    """
    Check resolved admins. Returns:
      True  -> at least one admin appears online
      False -> all resolved admins appear offline
      None  -> couldn't determine (no admins resolved)
    """
    if not ADMIN_ENTITIES:
        return None
    any_known = False
    for aid in list(ADMIN_ENTITIES.keys()):
        try:
            ent = await client.get_entity(aid)  # refresh
            status = getattr(ent, "status", None)
            if status is None:
                # hidden/unknown
                if TREAT_HIDDEN_AS_ONLINE:
                    logging.debug(f"ℹ️ Admin {aid} status hidden -> treating as ONLINE")
                    return True
                else:
                    continue
            any_known = True
            if isinstance(status, (UserStatusOnline, UserStatusRecently)):
                logging.debug(f"ℹ️ Admin {aid} is ONLINE")
                return True
        except Exception as e:
            logging.warning(f"⚠️ Failed to check status for admin {aid}: {e}")
            continue
    if any_known:
        return False
    return None

async def delete_tracked_message_for_group(gid):
    """Delete tracked message only for a specific group (if present)."""
    try:
        msg_id = last_sent_messages.get(gid)
        if msg_id:
            await client.delete_messages(gid, msg_id)
            logging.info(f"🗑️ Deleted tracked message in {gid}")
        last_sent_messages.pop(gid, None)
        last_sent_times.pop(gid, None)
    except Exception as e:
        logging.warning(f"⚠️ Could not delete tracked msg in {gid}: {e}")

async def start_sending_loop():
    global send_task
    if send_task and not send_task.done():
        return
    send_task = asyncio.create_task(continuous_send())

async def stop_sending_loop(group_id: int = None):
    global send_task
    if group_id is None:
        is_sending.clear()
        stopped_groups.clear()
        if send_task and not send_task.done():
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass
        send_task = None
    else:
        stopped_groups.add(group_id)
        if group_id in group_overrides:
            del group_overrides[group_id]
        if group_id in last_sent_times:
            del last_sent_times[group_id]
        if group_id in last_sent_messages:
            del last_sent_messages[group_id]
        if group_id in floodwait_logged:
            floodwait_logged.remove(group_id)
# 🔧 FIX–2: clear scheduler
        if group_id in next_run_times:
            del next_run_times[group_id]
            
# -----------------------------
# Commands (Admin only)
# -----------------------------
@client.on(events.NewMessage(pattern=r'^/start$'))
async def handle_start(event):
    if not is_sender_admin(event.sender_id):
        return
    await event.reply(
        "🎛 ইউজারবট কমান্ড:\n"
        "/sendall <মেসেজ> - সব গ্রুপে পাঠান (reply সমর্থন)\n"
        "/send <group_id> <seconds> <মেসেজ> - নির্দিষ্ট গ্রুপে override পাঠান (reply সমর্থন)\n"
        "/stop - সব গ্রুপে বার্তা বন্ধ করুন\n"
        "/stop <group_id> - নির্দিষ্ট গ্রুপে override বন্ধ করুন\n"
        "/setinterval <seconds> - সব গ্রুপের default interval\n"
        "/setinterval <group_id> <seconds> - নির্দিষ্ট গ্রুপের interval\n"
        "/status - বট স্ট্যাটাস চেক করুন\n"
        "/list - গ্রুপ লিস্ট এবং স্ট্যাটাস\n"
        "/add <group_id> - নতুন গ্রুপ add করুন\n"
        "/remove <group_id> - গ্রুপ remove করুন\n"
        "/help - এই মেসেজ দেখুন"
    )

@client.on(events.NewMessage(pattern=r'^/help$'))
async def handle_help(event):
    await handle_start(event)

@client.on(events.NewMessage(pattern=r'^/sendall'))
async def handle_send_all(event):
    global current_message

    if not is_sender_admin(event.sender_id):
        return

    # Reply থাকলে → message object (format preserve)
    if event.is_reply:
        message = await event.get_reply_message()
    else:
        # Normal text / HTML
        message = event.raw_text[len("/sendall"):].strip()

    if not message:
        await event.reply(
            "⚡ ব্যবহার: `/sendall <মেসেজ>` অথবা reply দিয়ে `/sendall`",
            parse_mode="md"
        )
        return

    current_message = message
    is_sending.set()
    await start_sending_loop()

    await event.reply(
        f"🔃 বার্তা পাঠানো শুরু হয়েছে!\n"
        f"ইন্টারভাল: {send_interval} সেকেন্ড\n"
        f"`/stop` দিয়ে বন্ধ করুন",
        parse_mode="md"
    )

@client.on(events.NewMessage(pattern=r'^/send'))
async def handle_send(event):
    if not is_sender_admin(event.sender_id):
        return

    parts = event.raw_text.split(maxsplit=3)
    if len(parts) < 3 and not event.is_reply:
        await event.reply("⚡ ব্যবহার: `/send <group_id> <seconds> <message>` অথবা reply দিয়ে `/send <group_id> <seconds>`", parse_mode="md")
        return

    try:
        gid = int(parts[1])
        interval = int(parts[2])
        if interval < 60:
            await event.reply("⚠️ Interval কমপক্ষে 60 সেকেন্ড হতে হবে")
            return
    except ValueError:
        await event.reply("⚠️ Group ID এবং interval integer হতে হবে")
        return

    # মেসেজ নেওয়া
    if event.is_reply:
        message = (await event.get_reply_message()).message
    else:
        if len(parts) < 4:
            await event.reply("⚡ মেসেজ খালি থাকতে পারবে না")
            return
        message = parts[3]

    # override সেট করা
    group_overrides[gid] = {"message": message, "interval": interval}

    # 🩵 Fix: যদি আগে stop করা থাকত, আবার চালু করা হবে
    if gid in stopped_groups:
        stopped_groups.remove(gid)

    # গ্রুপ আইডি list-এ না থাকলে add করা
    if gid not in GROUP_IDS:
        GROUP_IDS.append(gid)

    # লুপ start
    is_sending.set()
    await start_sending_loop()

    gname = await get_group_name(gid)
    await event.reply(
        f"🎯 Override চালু! → {gname} ({gid}) | ইন্টারভাল: {interval}s", parse_mode="md"
    )

@client.on(events.NewMessage(pattern=r'^/stop(?:\s+(\-?\d+))?$'))
async def handle_stop_cmd(event):
    if not is_sender_admin(event.sender_id):
        return
    match = event.pattern_match.group(1)
    if match:
        gid = int(match)
        await stop_sending_loop(group_id=gid)
        gname = await get_group_name(gid)
        await event.reply(f"⛔ Override বন্ধ করা হয়েছে → {gname} ({gid})")
    else:
        await stop_sending_loop()
        await event.reply("⛔ সব গ্রুপে বার্তা বন্ধ করা হয়েছে")

@client.on(events.NewMessage(pattern=r'^/setinterval'))
async def handle_set_interval(event):
    global send_interval
    if not is_sender_admin(event.sender_id):
        return
    parts = event.raw_text.split()
    if len(parts) < 2:
        await event.reply("⚡ ব্যবহার: `/setinterval <seconds>` অথবা `/setinterval <group_id> <seconds>`", parse_mode="md")
        return
    try:
        if len(parts) == 2:
            interval = int(parts[1])
            if interval < 60:
                await event.reply("⚠️ Interval কমপক্ষে 60 সেকেন্ড হতে হবে")
                return
            send_interval = interval
            await event.reply(f"🔄 Default interval সব গ্রুপের জন্য: {send_interval}s")
        elif len(parts) == 3:
            gid = int(parts[1])
            interval = int(parts[2])
            if interval < 60:
                await event.reply("⚠️ Interval কমপক্ষে 60 সেকেন্ড হতে হবে")
                return
            group_intervals[gid] = interval
            gname = await get_group_name(gid)
            await event.reply(f"🔄 Interval সেট করা হলো → {gname} ({gid}): {interval}s")
    except ValueError:
        await event.reply("⚠️ সঠিক integer ব্যবহার করতে হবে")

# -----------------------------
# Add / Remove group commands
# -----------------------------
@client.on(events.NewMessage(pattern=r'^/add\s+(-?\d+)$'))
async def handle_add_group(event):
    if not is_sender_admin(event.sender_id):
        return
    try:
        gid = int(event.pattern_match.group(1))
        if gid not in GROUP_IDS:
            GROUP_IDS.append(gid)
            gname = await get_group_name(gid)
            await event.reply(f"✅ Group add করা হলো → {gname} ({gid})")
        else:
            await event.reply("⚠️ Group আগেই list-এ আছে")
    except ValueError:
        await event.reply("⚠️ Group ID integer হতে হবে")

@client.on(events.NewMessage(pattern=r'^/remove\s+(-?\d+)$'))
async def handle_remove_group(event):
    if not is_sender_admin(event.sender_id):
        return
    try:
        gid = int(event.pattern_match.group(1))
        if gid in GROUP_IDS:
            GROUP_IDS.remove(gid)
            gname = await get_group_name(gid)
            await event.reply(f"❌ Group remove করা হলো → {gname} ({gid})")
        else:
            await event.reply("⚠️ Group list-এ নেই")
    except ValueError:
        await event.reply("⚠️ Group ID integer হতে হবে")

# -----------------------------
# Status & List commands
# -----------------------------
@client.on(events.NewMessage(pattern=r'^/status$'))
async def handle_status(event):
    if not is_sender_admin(event.sender_id):
        return

    status = "চালু ✅" if is_sending.is_set() else "বন্ধ ❌"

    override_names = []
    for gid in group_overrides:
        try:
            entity = await client.get_entity(gid)
            title = getattr(entity, "title", str(gid))
            username = getattr(entity, "username", None)
            if username:
                override_names.append(f"[{title}](https://t.me/{username})")
            else:
                override_names.append(f"**{title}**")
        except Exception:
            override_names.append(str(gid))

    stopped_names = []
    for gid in stopped_groups:
        try:
            entity = await client.get_entity(gid)
            title = getattr(entity, "title", str(gid))
            username = getattr(entity, "username", None)
            if username:
                stopped_names.append(f"[{title}](https://t.me/{username})")
            else:
                stopped_names.append(f"**{title}**")
        except Exception:
            stopped_names.append(str(gid))

    override_str = ", ".join(override_names) if override_names else "N/A"
    stopped_str = ", ".join(stopped_names) if stopped_names else "N/A"

    preview = (current_message[:50] + "...") if current_message else "N/A"

    await event.reply(
        f"📊 বট স্ট্যাটাস:\n"
        f"অবস্থা: {status}\n"
        f"Delay: {send_interval}s\n"
        f"মোট গ্রুপ: {len(GROUP_IDS)}\n"
        f"Override গ্রুপ: {len(group_overrides)} {override_str}\n"
        f"Stopped গ্রুপ: {len(stopped_groups)} {stopped_str}\n"
        f"Global Message: {preview}",
        parse_mode="md"
    )

@client.on(events.NewMessage(pattern=r'^/list$'))
async def handle_list(event):
    if not is_sender_admin(event.sender_id):
        return

    global_text = "🌐 Global Groups:\n\n"
    override_text = "🎯 Override Groups:\n\n"
    stopped_text = "⛔ Stopped Groups:\n\n"

    for gid in GROUP_IDS:
        gname = await get_group_name(gid)
        username = None
        try:
            entity = await client.get_entity(gid)
            username = getattr(entity, "username", None)
        except Exception:
            pass

        if username:
            display_name = f"[{gname}](https://t.me/{username})"
        else:
            display_name = f"**{gname}**"

        preview_msg = current_message[:30] + "..." if current_message else "N/A"
        last_sent = last_sent_times.get(gid)

        # -------- STOPPED --------
        if gid in stopped_groups:
            status_icon = "❌"
            sent_str = "N/A"
            next_run_str = "N/A"
            stopped_text += (
                f"{status_icon} Group: {display_name}\n"
                f"⏳ Sent: {sent_str}\n"
                f"⏰ Next Run: {next_run_str}\n"
                f"📝 Message: {preview_msg}\n\n"
            )
            continue

        # -------- OVERRIDE --------
        if gid in group_overrides:
            status_icon = "✅" if last_sent else "❌"
            interval = group_overrides[gid]["interval"]
            message_preview = group_overrides[gid]["message"][:30] + "..."

            if last_sent:
                # 🔧 FIX: special group uses real scheduler
                if gid == SPECIAL_GROUP_ID and gid in next_run_times:
                    next_run = next_run_times[gid]
                else:
                    next_run = last_sent + timedelta(seconds=interval)

                sent_str = (last_sent + timedelta(hours=6)).strftime("%I:%M %p")
                next_run_str = (next_run + timedelta(hours=6)).strftime("%I:%M %p")
            else:
                sent_str = "Not sent yet"
                next_run_str = "N/A"

            override_text += (
                f"{status_icon} {display_name}\n"
                f"⏳ Sent: {sent_str}\n"
                f"⏰ Next Run: {next_run_str}\n"
                f"📝 Message: {message_preview}\n\n"
            )
            continue

        # -------- GLOBAL --------
        status_icon = "✅" if last_sent else "❌"
        interval = group_intervals.get(gid, send_interval)

        if last_sent:
            # 🔧 FIX: special group uses real scheduler
            if gid == SPECIAL_GROUP_ID and gid in next_run_times:
                next_run = next_run_times[gid]
            else:
                next_run = last_sent + timedelta(seconds=interval)

            sent_str = (last_sent + timedelta(hours=6)).strftime("%I:%M %p")
            next_run_str = (next_run + timedelta(hours=6)).strftime("%I:%M %p")
        else:
            sent_str = "Not sent yet"
            next_run_str = "N/A"

        global_text += (
            f"{status_icon} Group: {display_name}\n"
            f"⏳ Sent: {sent_str}\n"
            f"⏰ Next Run: {next_run_str}\n"
            f"📝 Message: {preview_msg}\n\n"
        )

    await event.reply(
        global_text + override_text + stopped_text,
        parse_mode="md",
        link_preview=True
    )

# -----------------------------
# Sending loop (FIXED scheduler + admin delete logic)
# -----------------------------
async def continuous_send():
    global admin_was_online
    logging.info(f"[{datetime.now().strftime('%H:%M:%S')}] ▶️ Sending loop started")

    try:
        while is_sending.is_set():
            now = datetime.now(timezone.utc)

            # -------- Admin status check --------
            admin_online = await is_any_admin_online()
            if admin_online is None:
                admin_online = TREAT_HIDDEN_AS_ONLINE

            # -------- DELETE LOGIC (admin offline -> online) --------
            if admin_was_online is False and admin_online is True:
                if SPECIAL_GROUP_ID in last_sent_messages:
                    try:
                        await client.delete_messages(
                            SPECIAL_GROUP_ID,
                            last_sent_messages[SPECIAL_GROUP_ID]
                        )
                        logging.info("🗑️ Admin online — last message deleted")
                    except Exception as e:
                        logging.warning(f"⚠️ Delete failed: {e}")

                    last_sent_messages.pop(SPECIAL_GROUP_ID, None)
                    last_sent_times.pop(SPECIAL_GROUP_ID, None)

            admin_was_online = admin_online

            # -------- Group loop --------
            for gid in GROUP_IDS:
                if not is_sending.is_set():
                    break

                if gid in stopped_groups:
                    continue

                # -------- Floodwait handling --------
                if gid in floodwait_logged:
                    if last_sent_times.get(gid, now) > now:
                        continue
                    else:
                        floodwait_logged.discard(gid)

                # -------- Message & interval resolve --------
                if gid in group_overrides:
                    message = group_overrides[gid]["message"]
                    interval = group_overrides[gid]["interval"]
                else:
                    message = current_message
                    interval = group_intervals.get(gid, send_interval)

                if not message:
                    continue

                # 🔥 -------- FIXED SCHEDULER FOR ALL GROUPS --------
                if gid not in last_sent_times:
                    # first run → allow send
                    pass
                else:
                    if gid not in next_run_times:
                        next_run_times[gid] = (
                            last_sent_times[gid] + timedelta(seconds=interval)
                        )

                    # special group → admin online skip
                    if gid == SPECIAL_GROUP_ID and admin_online:
                        if now >= next_run_times[gid]:
                            logging.info("⏭️ Admin online — scheduled send skipped")
                            next_run_times[gid] += timedelta(seconds=interval)
                        continue

                    if now < next_run_times[gid]:
                        continue

                gname = await get_group_name(gid)

                try:
                    # -------- delete previous tracked msg --------
                    if gid in last_sent_messages:
                        try:
                            await client.delete_messages(gid, last_sent_messages[gid])
                            logging.info(f"🗑️ আগের মেসেজ ডিলিট → {gname} ({gid})")
                        except Exception as e:
                            logging.warning(
                                f"⚠️ মেসেজ ডিলিট সম্ভব হয়নি → {gname} ({gid}) | {e}"
                            )

                    # -------- SEND HTML --------
                    if isinstance(message, str):
                        msg = await client.send_message(
                            gid,
                            message,
                            parse_mode="html",
                            link_preview=True
                        )
                    else:
                        msg = await client.send_message(
                            gid,
                            message,
                            link_preview=True
                        )

                    last_sent_messages[gid] = msg.id
                    last_sent_times[gid] = now
                    floodwait_logged.discard(gid)

                    # 🔥 MOVE SCHEDULE
                    next_run_times[gid] = now + timedelta(seconds=interval)

                    logging.info(f"✅ মেসেজ পাঠানো হয়েছে → {gname} ({gid})")

                except FloodWaitError as e:
                    wait_s = max(1, int(getattr(e, "seconds", 5)))
                    logging.warning(f"⏳ FloodWait → {gname} ({gid}): {wait_s}s")

                    last_sent_times[gid] = now + timedelta(seconds=wait_s)
                    floodwait_logged.add(gid)

                    # 🔥 skip slot
                    next_run_times[gid] = now + timedelta(seconds=interval)
                    continue

                except Exception as e:
                    logging.error(f"❌ Error → {gname} ({gid}) | {e}")

                    # 🔥 CRITICAL FIX (anti-spam)
                    last_sent_times[gid] = now
                    next_run_times[gid] = now + timedelta(seconds=interval)

                    continue

            await asyncio.sleep(1)

    finally:
        logging.info("⛔ Sending loop stopped")

# -----------------------------
# Entrypoint
# -----------------------------
async def main():
    await client.start()
    logging.info(f"🔹 ইউজারবট সক্রিয় হয়েছে...")
    # Resolve the additional admin usernames to numeric IDs (so online checks work for special group)
    await resolve_admin_usernames()
    logging.info(f"ℹ️ মোট গ্রুপ: {len(GROUP_IDS)}")
    if current_message:
        logging.info(f"🌐 Global Message: {current_message[:50]}...")
    if group_overrides:
        for gid, val in group_overrides.items():
            gname = await get_group_name(gid)
            logging.info(f"🎯 Override Active → {gname} ({gid}) | Interval {val['interval']}s | Msg: {val['message'][:30]}...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("🛑 Userbot stopped manually")
        pass
