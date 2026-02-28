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
# Use same environment variables but you can set different ones if needed
BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN") or os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except:
    ADMIN_ID = 0

if not BOT_TOKEN or not MONGO_URI:
    print("❌ Error: BOT_TOKEN or MONGO_URI missing!")
    exit(1)

try:
    client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    # Separate DB to keep it isolated as requested
    db = client["SupportBotDB"]
    tickets_col = db["tickets"]
    settings_col = db["settings"]
    
    # Auto-delete tickets at the end of the week
    tickets_col.create_index("expire_at", expireAfterSeconds=0)
    print("✅ Support DB Connected!")
except Exception as e:
    print(f"❌ DB Error: {e}")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

user_states = {}

# ---------------- HELPERS ----------------
def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\_").replace("*", "\*").replace("`", "`").replace("[", "\[")

def smart_edit(chat_id, message_id, text, reply_markup=None, parse_mode="Markdown"):
    try:
        bot.edit_message_caption(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            try:
                bot.edit_message_caption(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=None)
            except Exception:
                try:
                    bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=None)
                except Exception: pass

def smart_edit_report(chat_id, message_id, text, photo=None, reply_markup=None):
    if photo:
        try:
            bot.edit_message_media(
                types.InputMediaPhoto(photo, caption=text, parse_mode="Markdown"),
                chat_id, message_id, reply_markup=reply_markup
            )
            return
        except Exception:
            try:
                bot.edit_message_media(
                    types.InputMediaPhoto(photo, caption=text, parse_mode=None),
                    chat_id, message_id, reply_markup=reply_markup
                )
                return
            except Exception: pass
    smart_edit(chat_id, message_id, text, reply_markup)

def gen_code(length=4):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_setting(key, default):
    try:
        doc = settings_col.find_one({"_id": key})
        return doc["data"] if doc else default
    except: return default

LOG_CHANNELS = get_setting("logs", {"user": None})

def log_to_user_channel(text):
    cid = LOG_CHANNELS.get("user")
    if not cid: return
    try: bot.send_message(cid, text)
    except: pass

def render_panel_reports(chat_id, msg_id, page=1):
    all_tickets = list(tickets_col.find().sort("created_at", -1))
    count = len(all_tickets)
    if count == 0:
        smart_edit(chat_id, msg_id, "✅ *No Active Reports Found.*")
        return

    per_page = 10
    total_pages = max(1, (count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    current_tickets = all_tickets[start_idx : start_idx + per_page]

    kb = types.InlineKeyboardMarkup()
    row = []
    for t in current_tickets:
        row.append(types.InlineKeyboardButton(f"Report #{t['_id']}", callback_data=f"view_rep|{t['_id']}|{page}"))
        if len(row) == 2:
            kb.add(*row)
            row = []
    if row: kb.add(*row)

    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"panel_reports|{page-1}"))
    nav.append(types.InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="ignore"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=f"panel_reports|{page+1}"))
    kb.row(*nav)
    smart_edit(chat_id, msg_id, f"📋 *Active Reports: {count}*", reply_markup=kb)

# ---------------- HANDLERS ----------------
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📋 View Reports", callback_data="panel_reports|1"))
        bot.send_message(uid, "Welcome Admin! Use the button below to manage reports.", reply_markup=kb)
    else:
        bot.send_message(uid, "👋 *Support Center*

If you have any issues, please send your message (text/photo) here and our team will get back to you.")
        user_states[uid] = {'state': 'waiting_report'}

@bot.callback_query_handler(func=lambda c: True)
def router(call):
    uid = call.from_user.id
    action = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if action.startswith("panel_reports"):
        page = int(action.split("|")[1])
        render_panel_reports(chat_id, msg_id, page)
    
    elif action.startswith("view_rep|"):
        parts = action.split("|")
        tid, page = parts[1], parts[2]
        t = tickets_col.find_one({"_id": tid})
        if not t:
            bot.answer_callback_query(call.id, "❌ Report not found.")
            render_panel_reports(chat_id, msg_id, int(page))
            return

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Fix", callback_data=f"fix|{tid}|{page}"),
               types.InlineKeyboardButton("↩️ Reply", callback_data=f"reply|{tid}|{page}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=f"panel_reports|{page}"))

        thread_content = ""
        for m in t.get('thread', []):
            role = "👤 User" if m['role'] == 'user' else "🤖 Admin"
            thread_content += f"

━━━━━━━━━━━━━━━
*{role}:*
{escape_md(m['msg'])}"

        txt = (f"🆔 *Report ID:* `#{escape_md(tid)}`
"
               f"👤 *UserID:* `{t['user_id']}`

"
               f"📝 *Original:* 
{escape_md(t['text'])}{thread_content}")
        smart_edit_report(chat_id, msg_id, txt, photo=t.get('photo'), reply_markup=kb)

    elif action.startswith("fix|"):
        tid, page = action.split("|")[1], action.split("|")[2]
        tickets_col.delete_one({"_id": tid})
        bot.answer_callback_query(call.id, "✅ Report Fixed!")
        render_panel_reports(chat_id, msg_id, int(page))

    elif action.startswith("reply|"):
        parts = action.split("|")
        tid, page = parts[1], parts[2]
        user_states[uid] = {'state': 'reply_ticket', 'tid': tid, 'page': page, 'msg_id': msg_id}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"view_rep|{tid}|{page}"))
        smart_edit(chat_id, msg_id, f"✍️ *Reply to Report #{tid}:*", reply_markup=kb)

    elif action.startswith("usr_reply|"):
        tid = action.split("|")[1]
        user_states[uid] = {'state': 'waiting_user_reply', 'tid': tid, 'msg_id': msg_id}
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_reply"))
        smart_edit(chat_id, msg_id, "✍️ *Type your reply:*", reply_markup=kb)

    elif action == "cancel_user_reply":
        user_states.pop(uid, None)
        bot.edit_message_text("❌ Reply cancelled.", chat_id, msg_id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'document'])
def handle_msg(message):
    uid = message.from_user.id
    state = user_states.get(uid)
    if not state: return

    # 1. New Support Request
    if state['state'] == 'waiting_report':
        tid = f"REP{random.randint(1000,9999)}"
        txt = message.caption or message.text or "No Text"
        photo = message.photo[-1].file_id if message.photo else None
        
        now = datetime.now()
        expire_at = now + timedelta(days=7) # Auto-delete in 7 days
        
        tickets_col.insert_one({
            '_id': tid, 'user_id': uid, 'text': txt, 'photo': photo,
            'thread': [], 'created_at': now, 'expire_at': expire_at
        })
        bot.send_message(uid, f"✅ *Report Submitted! ID: #{tid}*")
        bot.send_message(ADMIN_ID, f"⚠️ *New Report #{tid}*
Use /start to view.")
        user_states.pop(uid, None)

    # 2. Admin Reply
    elif state['state'] == 'reply_ticket':
        tid = state['tid']
        t = tickets_col.find_one({"_id": tid})
        if not t: return
        
        msg_text = message.text or message.caption or "Media Reply"
        tickets_col.update_one({"_id": tid}, {"$push": {"thread": {"role": "admin", "msg": msg_text, "time": datetime.now()}}})
        
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Reply", callback_data=f"usr_reply|{tid}"))
        bot.send_message(t['user_id'], f"📩 *Admin Response (Report #{tid}):*

{msg_text}", reply_markup=kb)
        
        bot.send_message(uid, "✅ Reply sent.")
        user_states.pop(uid, None)

    # 3. User Reply to Admin
    elif state['state'] == 'waiting_user_reply':
        tid = state['tid']
        msg_text = message.text or message.caption or "Media Reply"
        tickets_col.update_one({"_id": tid}, {"$push": {"thread": {"role": "user", "msg": msg_text, "time": datetime.now()}}})
        
        bot.send_message(ADMIN_ID, f"📩 *New User Reply for Report #{tid}*")
        bot.send_message(uid, "✅ Reply sent to Admin.")
        user_states.pop(uid, None)

if __name__ == "__main__":
    print("Support Bot Started...")
    bot.infinity_polling()
