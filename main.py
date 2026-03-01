import os
import telebot
from telebot import types
import time
from datetime import datetime, timedelta
import pymongo
import certifi
import threading
from dotenv import load_dotenv

load_dotenv()

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
admin_env = os.getenv("ADMIN_ID", "1326069145")
ADMIN_LIST = [int(i.strip()) for i in admin_env.split(",") if i.strip().isdigit()]

CATEGORIES = {
    "Payment": {"slug": "payment", "icon": "💳", "priority": "HIGH"},
    "Tech Issue": {"slug": "tech", "icon": "🛠", "priority": "NORMAL"},
    "Feature Req": {"slug": "feature", "icon": "📦", "priority": "LOW"},
    "Other": {"slug": "other", "icon": "❓", "priority": "NORMAL"}
}

# --- DATABASE ---
db = None
settings_col = None

def connect_db():
    global db, settings_col
    try:
        client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
        client.admin.command('ping')
        db = client["SupportBotDB"]
        settings_col = db["settings"]
        print("✅ MongoDB Connected!")
    except Exception as e:
        print(f"❌ DB Error: {e}")

threading.Thread(target=connect_db, daemon=True).start()

def get_ticket_col(slug):
    if db is None: return None
    return db[f"tickets_{slug}"]

# --- BOT SETUP ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
user_states = {}

# --- HELPERS ---
def is_admin(uid): return uid in ADMIN_LIST

def smart_edit(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode="Markdown")
    except:
        bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")

def smart_edit_report(chat_id, message_id, text, media_type=None, media_id=None, reply_markup=None):
    if media_type == 'photo' and media_id:
        try:
            bot.edit_message_media(types.InputMediaPhoto(media_id, caption=text, parse_mode="Markdown"), chat_id, message_id, reply_markup=reply_markup)
        except:
            bot.delete_message(chat_id, message_id)
            bot.send_photo(chat_id, media_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
    elif media_type == 'video' and media_id:
        try:
            bot.edit_message_media(types.InputMediaVideo(media_id, caption=text, parse_mode="Markdown"), chat_id, message_id, reply_markup=reply_markup)
        except:
            bot.delete_message(chat_id, message_id)
            bot.send_video(chat_id, media_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        smart_edit(chat_id, message_id, text, reply_markup=reply_markup)

# --- ADMIN DASHBOARD ---
def render_admin_dashboard(chat_id, msg_id, slug, page=1, status_filter="open"):
    col = get_ticket_col(slug)
    if col is None:
        smart_edit(chat_id, msg_id, "❌ Database connecting... Try again in a moment.")
        return

    tickets = list(col.find({"status": status_filter}).sort("created_at", -1))
    count = len(tickets)
    per_page = 10
    total_pages = (count + per_page - 1) // per_page
    page = max(1, min(page, total_pages)) if total_pages > 0 else 1
    
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Resolved" if status_filter == "open" else "🟢 Open", 
                                       callback_data=f"dash|1|{'resolved' if status_filter == 'open' else 'open'}|{slug}"))

    if count == 0:
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="ticket_menu"))
        smart_edit(chat_id, msg_id, f"✅ *No {status_filter} tickets in {slug.upper()}.*", reply_markup=kb)
        return

    start_idx = (page - 1) * per_page
    current = tickets[start_idx : start_idx + per_page]
    
    for i in range(0, len(current), 2):
        row = [types.InlineKeyboardButton(f"🎫 #{t['_id']}", callback_data=f"view|{t['_id']}|{slug}") for t in current[i:i+2]]
        kb.row(*row)

    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"dash|{page-1}|{status_filter}|{slug}"))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=f"dash|{page+1}|{status_filter}|{slug}"))
    kb.row(*nav)
    kb.row(types.InlineKeyboardButton("🔙 Back to Categories", callback_data="ticket_menu"))
    
    smart_edit(chat_id, msg_id, f"🛠 *{slug.upper()} Tickets* ({status_filter.upper()})", reply_markup=kb)

# --- HANDLERS ---
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if is_admin(uid):
        bot.send_message(uid, "👋 *Admin Panel Active*\nUse /ticket to manage reports.")
    else:
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(f"{v['icon']} {k}", callback_data=f"cat|{v['slug']}") for k, v in CATEGORIES.items()]
        kb.add(*btns)
        bot.send_message(uid, "👋 *Welcome to Support Bot*\nChoose a category to start:", reply_markup=kb)

@bot.message_handler(commands=['ticket'])
def ticket_cmd(message):
    if not is_admin(message.from_user.id): return
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(f"{v['icon']} {k}", callback_data=f"adm_cat|{v['slug']}") for k, v in CATEGORIES.items()]
    kb.add(*btns)
    bot.send_message(message.chat.id, "📂 *Select Category:*", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: True)
def router(call):
    uid = call.from_user.id
    action = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if action == "ticket_menu":
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(f"{v['icon']} {k}", callback_data=f"adm_cat|{v['slug']}") for k, v in CATEGORIES.items()]
        kb.add(*btns)
        smart_edit(chat_id, msg_id, "📂 *Select Category:*", reply_markup=kb)
    elif action.startswith("cat|"):
        slug = action.split("|")[1]
        user_states[uid] = {'state': 'waiting', 'slug': slug}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Cancel", callback_data="user_start"))
        smart_edit(chat_id, msg_id, f"📝 *Category: {slug.upper()}*\nPlease describe your issue (you can send Photo/Video):", reply_markup=kb)
    elif action == "user_start":
        start(call.message)
    elif action.startswith("adm_cat|"):
        render_admin_dashboard(chat_id, msg_id, action.split("|")[1])
    elif action.startswith("dash|"):
        _, p, f, s = action.split("|")
        render_admin_dashboard(chat_id, msg_id, s, int(p), f)
    elif action.startswith("view|"):
        _, tid, slug = action.split("|")
        col = get_ticket_col(slug)
        t = col.find_one({"_id": tid}) if col else None
        if not t: return
        text = f"🎫 *Ticket #{tid}*\nCategory: `{slug.upper()}`\nStatus: `{t['status'].upper()}`\n\nIssue:\n{t['text']}"
        kb = types.InlineKeyboardMarkup()
        if t['status'] == 'open':
            kb.add(types.InlineKeyboardButton("📩 Reply", callback_data=f"reply|{tid}|{slug}"), types.InlineKeyboardButton("✅ Resolve", callback_data=f"res|{tid}|{slug}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=f"adm_cat|{slug}"))
        m_type = 'photo' if 'photo' in t else ('video' if 'video' in t else None)
        smart_edit_report(chat_id, msg_id, text, m_type, t.get(m_type), reply_markup=kb)
    elif action.startswith("res|"):
        _, tid, slug = action.split("|")
        get_ticket_col(slug).update_one({"_id": tid}, {"$set": {"status": "resolved"}})
        bot.answer_callback_query(call.id, "✅ Ticket Resolved!")
        render_admin_dashboard(chat_id, msg_id, slug)
    elif action.startswith("reply|"):
        _, tid, slug = action.split("|")
        user_states[uid] = {'state': 'admin_reply', 'tid': tid, 'slug': slug}
        bot.send_message(chat_id, f"📝 *Replying to #{tid}* ({slug.upper()}):\nSend your message now.")
    
    bot.answer_callback_query(call.id)

@bot.message_handler(content_types=['text', 'photo', 'video'])
def handle_all(message):
    uid = message.from_user.id
    state = user_states.get(uid)
    if not state: return

    if state['state'] == 'waiting':
        slug = state['slug']
        col = get_ticket_col(slug)
        if col is None or settings_col is None: return
        
        counter = settings_col.find_one_and_update({"_id": "ticket_counter"}, {"$inc": {"count": 1}}, upsert=True, return_document=pymongo.ReturnDocument.AFTER)
        tid = f"{slug[0].upper()}{counter['count']}"
        
        ticket_data = {'_id': tid, 'user_id': uid, 'text': message.text or message.caption or "[Media]", 'status': 'open', 'created_at': datetime.now()}
        if message.photo: ticket_data['photo'] = message.photo[-1].file_id
        elif message.video: ticket_data['video'] = message.video.file_id
        
        col.insert_one(ticket_data)
        bot.reply_to(message, f"✅ *Ticket Created: #{tid}*")
        for adm in ADMIN_LIST:
            try: bot.send_message(adm, f"⚠️ *New Ticket #{tid}* in {slug.upper()}")
            except: pass
        user_states.pop(uid, None)

    elif state['state'] == 'admin_reply':
        tid, slug = state['tid'], state['slug']
        col = get_ticket_col(slug)
        t = col.find_one({"_id": tid})
        if not t: return
        
        msg = message.text or message.caption or "[Media Reply]"
        try:
            if message.photo: bot.send_photo(t['user_id'], message.photo[-1].file_id, caption=f"📩 *Admin Reply (#{tid}):*\n{msg}")
            elif message.video: bot.send_video(t['user_id'], message.video.file_id, caption=f"📩 *Admin Reply (#{tid}):*\n{msg}")
            else: bot.send_message(t['user_id'], f"📩 *Admin Reply (#{tid}):*\n{msg}")
            bot.send_message(uid, "✅ Reply sent!")
        except: bot.send_message(uid, "❌ Failed to send reply.")
        user_states.pop(uid, None)

if __name__ == "__main__":
    print("🚀 Bot is starting...")
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
