import os
import telebot
from telebot import apihelper
import time
import random
import string
import threading
from telebot import types
from datetime import datetime, timedelta
import traceback
import pymongo
import certifi
import re

# ---------------- CONFIG & SECRETS ----------------
BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN") or os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

try:
    admin_env = os.getenv("ADMIN_ID", "0")
    ADMIN_LIST = [int(i.strip()) for i in admin_env.split(",") if i.strip().isdigit()]
except:
    ADMIN_LIST = []

if not BOT_TOKEN or not MONGO_URI:
    print("❌ Error: BOT_TOKEN or MONGO_URI missing!")
    exit(1)

try:
    client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["SupportBotDB"]
    tickets_col = db["tickets"]
    settings_col = db["settings"]
    banned_col = db["banned_users"]
    ratings_col = db["ratings"]
    
    tickets_col.create_index("expire_at", expireAfterSeconds=0)
    print("✅ Ultra-Advanced Support DB Connected!")
except Exception as e:
    print(f"❌ DB Error: {e}")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
user_states = {}

# --- SETTINGS ---
CATEGORIES = {
    "💳 Payment": "HIGH",
    "🛠 Tech Issue": "NORMAL",
    "📦 Feature Req": "LOW",
    "❓ Other": "NORMAL"
}
WORKING_HOURS = (10, 22) # 10 AM to 10 PM
AVG_RESPONSE_TIME = "15-30 minutes"
FAQ_SYSTEM = {
    "payment": "If your payment failed, please send the screenshot and transaction ID here. We will verify it within 1 hour.",
    "premium": "Premium plans are activated automatically after payment. If not, send your user ID here.",
    "shortener": "You can change your shortener from the main bot settings menu.",
    "how to buy": "Go to the main bot, click 'Buy Plan', and choose your preferred method."
}

# ---------------- HELPERS ----------------
def is_admin(uid): return uid in ADMIN_LIST
def is_banned(uid): return banned_col.find_one({"_id": uid}) is not None

def is_working_hours():
    now = datetime.now().hour
    return WORKING_HOURS[0] <= now < WORKING_HOURS[1]

def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

def smart_edit(chat_id, message_id, text, reply_markup=None, parse_mode="Markdown"):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
    except:
        try: bot.edit_message_caption(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        except: pass

def smart_edit_report(chat_id, message_id, text, media_type=None, media_id=None, reply_markup=None):
    if media_type == 'photo':
        try:
            bot.edit_message_media(types.InputMediaPhoto(media_id, caption=text, parse_mode="Markdown"), chat_id, message_id, reply_markup=reply_markup)
            return
        except: pass
    elif media_type == 'video':
        try:
            bot.edit_message_media(types.InputMediaVideo(media_id, caption=text, parse_mode="Markdown"), chat_id, message_id, reply_markup=reply_markup)
            return
        except: pass
    smart_edit(chat_id, message_id, text, reply_markup)

def render_admin_dashboard(chat_id, msg_id, page=1, status_filter="open"):
    query = {"status": status_filter}
    all_tickets = list(tickets_col.find(query).sort([("priority", 1), ("created_at", -1)]))
    count = len(all_tickets)
    
    kb = types.InlineKeyboardMarkup()
    f_row = [
        types.InlineKeyboardButton("🟢 Open" if status_filter=="open" else "Open", callback_data="dash|1|open"),
        types.InlineKeyboardButton("✅ Resolved" if status_filter=="resolved" else "Resolved", callback_data="dash|1|resolved")
    ]
    kb.row(*f_row)

    if count == 0:
        smart_edit(chat_id, msg_id, f"✅ *No {status_filter} tickets found.*", reply_markup=kb)
        return

    per_page = 8
    total_pages = (count + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    current = all_tickets[start_idx : start_idx + per_page]

    for t in current:
        p_tag = "🔥" if t.get('priority') == 'HIGH' else "🔹"
        kb.add(types.InlineKeyboardButton(f"{p_tag} #{t['_id']} | {t.get('category','N/A')}", callback_data=f"view_rep|{t['_id']}|{page}|{status_filter}"))

    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"dash|{page-1}|{status_filter}"))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=f"dash|{page+1}|{status_filter}"))
    kb.row(*nav)
    smart_edit(chat_id, msg_id, f"🛠 *Support Dashboard*\nFilter: `{status_filter.upper()}` | Count: `{count}`", reply_markup=kb)

# ---------------- HANDLERS ----------------
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if is_banned(uid):
        bot.send_message(uid, "❌ You are banned from using support.")
        return

    if is_admin(uid):
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📊 Dashboard", callback_data="dash|1|open"))
        bot.send_message(uid, "👋 *Admin Panel*", reply_markup=kb)
    else:
        if not is_working_hours():
            bot.send_message(uid, "🌙 *We are currently Offline.*\nOur working hours are 10 AM to 10 PM. You can still leave a message, and we will reply tomorrow!")
        
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(cat, callback_data=f"cat|{cat}") for cat in CATEGORIES.keys()]
        kb.add(*btns)
        bot.send_message(uid, "👋 *Welcome to Support Center*\nChoose a category:", reply_markup=kb)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid): return
    
    msg_text = message.text.replace("/broadcast", "").strip()
    if not msg_text:
        bot.send_message(uid, "Usage: `/broadcast Hello Users`")
        return

    users = tickets_col.distinct("user_id")
    bot.send_message(uid, f"🚀 Sending broadcast to {len(users)} users...")
    
    count = 0
    for u_id in users:
        try:
            bot.send_message(u_id, f"📢 *Important Update:*\n\n{msg_text}")
            count += 1
            time.sleep(0.05)
        except: pass
    
    bot.send_message(uid, f"✅ Broadcast sent to {count} users.")

@bot.callback_query_handler(func=lambda c: True)
def router(call):
    uid = call.from_user.id
    action = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if is_banned(uid): return

    if action.startswith("cat|"):
        cat = action.split("|")[1]
        user_states[uid] = {'state': 'waiting_report', 'category': cat}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
        smart_edit(chat_id, msg_id, f"📝 *Category:* {cat}\n\nPlease explain your issue in detail. You can send Text, Photo, Voice, or Video.", reply_markup=kb)

    elif action == "back_to_start":
        user_states.pop(uid, None)
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(cat, callback_data=f"cat|{cat}") for cat in CATEGORIES.keys()]
        kb.add(*btns)
        smart_edit(chat_id, msg_id, "👋 *Support Center*\nChoose a category:", reply_markup=kb)

    elif action.startswith("dash|"):
        if not is_admin(uid): return
        p = action.split("|")
        render_admin_dashboard(chat_id, msg_id, int(p[1]), p[2])

    elif action.startswith("view_rep|"):
        if not is_admin(uid): return
        p = action.split("|")
        tid, page, f_status = p[1], p[2], p[3]
        t = tickets_col.find_one({"_id": tid})
        if not t: return
        
        kb = types.InlineKeyboardMarkup(row_width=2)
        if t['status'] == 'open':
            kb.add(types.InlineKeyboardButton("✅ Resolve", callback_data=f"resolve|{tid}|{page}|{f_status}"),
                   types.InlineKeyboardButton("↩️ Reply", callback_data=f"reply|{tid}|{page}|{f_status}"))
        else:
            kb.add(types.InlineKeyboardButton("🔓 Re-open", callback_data=f"reopen|{tid}|{page}|{f_status}"))
        
        kb.add(types.InlineKeyboardButton("🚫 Ban", callback_data=f"ban|{t['user_id']}|{tid}"),
               types.InlineKeyboardButton("🔙 Back", callback_data=f"dash|{page}|{f_status}"))

        thread = ""
        for m in t.get('thread', []):
            role = "👤" if m['role'] == 'user' else "🤖"
            thread += f"\n\n{role} *{m['role'].upper()}:* {escape_md(m['msg'])}"

        m_type = 'photo' if t.get('photo') else ('video' if t.get('video') else None)
        m_id = t.get('photo') or t.get('video')
        
        txt = (f"🆔 *#{tid}* | {t.get('priority','NORMAL')}\n"
               f"👤 `USER:{t['user_id']}`\n"
               f"📂 `{t.get('category')}`\n\n"
               f"📝 {escape_md(t['text'])}{thread}")
        
        smart_edit_report(chat_id, msg_id, txt, m_type, m_id, kb)

    elif action.startswith("resolve|") or action.startswith("reopen|"):
        if not is_admin(uid): return
        cmd, tid, page, f_status = action.split("|")
        new_status = "resolved" if cmd == "resolve" else "open"
        tickets_col.update_one({"_id": tid}, {"$set": {"status": new_status}})
        
        if new_status == "resolved":
            kb = types.InlineKeyboardMarkup()
            kb.add(*[types.InlineKeyboardButton(f"{i}⭐", callback_data=f"rate|{tid}|{i}") for i in range(1,6)])
            try: bot.send_message(tickets_col.find_one({"_id": tid})['user_id'], "✅ *Ticket Resolved!*\nPlease rate our support:", reply_markup=kb)
            except: pass
            
        call.data = f"view_rep|{tid}|{page}|{f_status}"
        router(call)

    elif action.startswith("rate|"):
        tid, stars = action.split("|")[1], action.split("|")[2]
        ratings_col.insert_one({"ticket_id": tid, "user_id": uid, "rating": int(stars), "time": datetime.now()})
        smart_edit(chat_id, msg_id, f"🙏 *Thank you!* You rated us {stars} stars.")

    elif action.startswith("reply|"):
        if not is_admin(uid): return
        _, tid, page, f_status = action.split("|")
        user_states[uid] = {'state': 'reply_ticket', 'tid': tid, 'page': page, 'f_status': f_status, 'msg_id': msg_id}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"view_rep|{tid}|{page}|{f_status}"))
        smart_edit(chat_id, msg_id, "✍️ *Type your reply:*", reply_markup=kb)

    elif action.startswith("usr_reply|"):
        tid = action.split("|")[1]
        user_states[uid] = {'state': 'waiting_user_reply', 'tid': tid, 'msg_id': msg_id}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_reply"))
        smart_edit(chat_id, msg_id, "✍️ *Type your reply:*", reply_markup=kb)

    elif action == "cancel_user_reply":
        user_states.pop(uid, None)
        bot.edit_message_text("❌ Reply cancelled.", chat_id, msg_id)

    elif action.startswith("ban|"):
        if not is_admin(uid): return
        target, tid = int(action.split("|")[1]), action.split("|")[2]
        banned_col.update_one({"_id": target}, {"$set": {"at": datetime.now()}}, upsert=True)
        bot.answer_callback_query(call.id, "User Banned!", show_alert=True)

@bot.message_handler(content_types=['text', 'photo', 'video', 'voice', 'video_note', 'document', 'audio'])
def handle_all(message):
    uid = message.from_user.id
    if is_banned(uid): return
    state = user_states.get(uid)
    
    # AI FAQ Check (Only if not in active ticket thread)
    if not state and message.text:
        low_text = message.text.lower()
        for key, ans in FAQ_SYSTEM.items():
            if key in low_text:
                bot.reply_to(message, f"🤖 *Auto-Response:*\n\n{ans}\n\n_If this didn't help, click /start to open a ticket._")
                return

    if not state: return

    # 1. Create Ticket
    if state['state'] == 'waiting_report':
        tid = f"T{random.randint(10000,99999)}"
        txt = message.caption or message.text or f"[{message.content_type.upper()}]"
        
        media_id = None
        m_type = None
        if message.photo: media_id, m_type = message.photo[-1].file_id, 'photo'
        elif message.video: media_id, m_type = message.video.file_id, 'video'
        
        cat = state.get('category', 'Other')
        priority = CATEGORIES.get(cat, 'NORMAL')
        
        tickets_col.insert_one({
            '_id': tid, 'user_id': uid, 'text': txt, m_type: media_id,
            'category': cat, 'priority': priority, 'status': 'open',
            'thread': [], 'created_at': datetime.now(), 'expire_at': datetime.now() + timedelta(days=30)
        })
        
        bot.send_message(uid, f"✅ *Ticket Created: #{tid}*\nPriority: `{priority}`\nEst. Wait Time: `{AVG_RESPONSE_TIME}`\n\nPlease wait, we will reply soon.")
        for adm in ADMIN_LIST:
            try: bot.send_message(adm, f"⚠️ *New {priority} Ticket #{tid}*\nCategory: {cat}")
            except: pass
        user_states.pop(uid, None)

    # 2. Admin/User Reply Logic (Shared)
    elif state['state'] in ['reply_ticket', 'waiting_user_reply']:
        is_adm_reply = state['state'] == 'reply_ticket'
        tid = state['tid']
        t = tickets_col.find_one({"_id": tid})
        if not t: return
        
        msg_text = message.text or message.caption or f"[{message.content_type.upper()}]"
        role = "admin" if is_adm_reply else "user"
        
        tickets_col.update_one({"_id": tid}, {"$push": {"thread": {"role": role, "msg": msg_text, "time": datetime.now()}}})
        
        if is_adm_reply:
            kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Reply to Support", callback_data=f"usr_reply|{tid}"))
            try: bot.send_message(t['user_id'], f"📩 *Support Reply (#{tid}):*\n\n{msg_text}", reply_markup=kb)
            except: pass
            bot.send_message(uid, "✅ Reply sent.")
        else:
            for adm in ADMIN_LIST:
                try: bot.send_message(adm, f"📩 *User Reply* on Ticket `#{tid}`")
                except: pass
            bot.send_message(uid, "✅ Message sent to Support Team.")
        
        user_states.pop(uid, None)

if __name__ == "__main__":
    print("Ultra-Advanced Support Bot Started...")
    bot.infinity_polling()
