import os
import telebot
from telebot import types
import time
from datetime import datetime, timedelta
import pymongo
import certifi
import threading
import traceback

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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

def auto_delete_resolved():
    while True:
        try:
            time.sleep(3600) # Check every hour
            if db:
                for s in CATEGORIES.values():
                    col = get_ticket_col(s['slug'])
                    if col:
                        cutoff = datetime.now() - timedelta(days=30)
                        col.delete_many({"status": "resolved", "resolved_at": {"$lt": cutoff}})
        except: pass

threading.Thread(target=auto_delete_resolved, daemon=True).start()

def get_ticket_col(slug):
    if db is None: return None
    return db[f"tickets_{slug}"]

# --- BOT SETUP ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
user_states = {}

# --- HELPERS ---
def is_admin(uid): 
    return int(uid) in ADMIN_LIST

def smart_edit(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        if "message is not modified" in str(e): return
        try:
            bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except: pass

def get_pagination_row(prefix, current_page, total_pages, suffix=""):
    row = []
    row.append(types.InlineKeyboardButton("🗒️", callback_data="page_list"))
    row.append(types.InlineKeyboardButton("⬅️", callback_data="nav_back"))
    p = current_page
    added = 0
    while p <= total_pages and added < 4:
        text = f"·{p}·" if p == current_page else str(p)
        row.append(types.InlineKeyboardButton(text, callback_data=f"{prefix}{p}{suffix}"))
        p += 1; added += 1
    if p <= total_pages:
        row.append(types.InlineKeyboardButton(str(total_pages), callback_data=f"{prefix}{total_pages}{suffix}"))
    next_p = min(total_pages, current_page + 1)
    row.append(types.InlineKeyboardButton("➡️", callback_data=f"ignore" if current_page >= total_pages else f"{prefix}{next_p}{suffix}"))
    return row

# --- RENDERERS ---
def render_user_list(chat_id, msg_id, uid, slug, page=1):
    page = int(page); col = get_ticket_col(slug)
    if col is None: return
    count_res = list(col.aggregate([{"$group": {"_id": "$user_id"}}, {"$count": "total"}]))
    count = count_res[0]["total"] if count_res else 0
    per_page = 10; total_pages = max(1, (count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    if uid not in user_states: user_states[uid] = {}
    user_states[uid]['list_config'] = {'type': 'u_list', 'slug': slug, 'total': total_pages}
    pipeline = [
        {"$group": {"_id": "$user_id", "last_ticket": {"$max": "$created_at"}, "name": {"$first": "$name"}, "username": {"$first": "$username"}}},
        {"$sort": {"last_ticket": -1}}, {"$skip": (page - 1) * per_page}, {"$limit": per_page}
    ]
    current = list(col.aggregate(pipeline))
    kb = types.InlineKeyboardMarkup(row_width=1)
    if count == 0:
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="t_menu"))
        smart_edit(chat_id, msg_id, f"✅ *No users in {slug.upper()}*", reply_markup=kb); return
    for u in current:
        name = u.get("username") or u.get("name") or f"User {u['_id']}"
        kb.add(types.InlineKeyboardButton(f"👤 {name}", callback_data=f"u_tix|{slug}|{u['_id']}|1|open"))
    kb.row(*get_pagination_row(f"u_list|{slug}|", page, total_pages))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="t_menu"))
    smart_edit(chat_id, msg_id, f"👥 *Users in {slug.upper()}*", reply_markup=kb)

def render_user_tickets(chat_id, msg_id, uid, slug, target_uid, page=1, status="open", notification=""):
    page = int(page); target_uid = int(target_uid); col = get_ticket_col(slug)
    query = {"user_id": target_uid, "status": status}
    count = col.count_documents(query)
    per_page = 10; total_pages = max(1, (count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    if uid not in user_states: user_states[uid] = {}
    user_states[uid]['list_config'] = {'type': 'u_tix', 'slug': slug, 'target_uid': target_uid, 'status': status, 'total': total_pages}
    kb = types.InlineKeyboardMarkup(row_width=1)
    other_status = "resolved" if status == "open" else "open"
    kb.add(types.InlineKeyboardButton("✅ Show Resolved" if status == "open" else "🟢 Show Open", callback_data=f"u_tix|{slug}|{target_uid}|1|{other_status}"))
    current = list(col.find(query).sort("created_at", -1).skip((page - 1) * per_page).limit(per_page))
    for t in current:
        kb.add(types.InlineKeyboardButton(f"🎫 #{t['_id']}", callback_data=f"t_view|{slug}|{t['_id']}|{target_uid}|{page}|{status}"))
    if count > 0: kb.row(*get_pagination_row(f"u_tix|{slug}|{target_uid}|", page, total_pages, suffix=f"|{status}"))
    kb.add(types.InlineKeyboardButton("🔙 BACK TO USER LIST", callback_data=f"nav_back"))
    title = f"🛠 *Tickets for User {target_uid}* ({status.upper()})"
    if notification: title = f"*{notification}*\n\n{title}"
    smart_edit(chat_id, msg_id, title, reply_markup=kb)

def render_self_tickets(chat_id, msg_id, uid, page=1, status="open"):
    page = int(page); uid = int(uid); all_tix = []
    for s in CATEGORIES.values():
        col = get_ticket_col(s['slug'])
        if col is not None:
            tix = list(col.find({"user_id": uid, "status": status}))
            for t in tix: t['slug'] = s['slug']; all_tix.append(t)
    all_tix.sort(key=lambda x: x['created_at'], reverse=True)
    count = len(all_tix); per_page = 10; total_pages = max(1, (count + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Show Resolved" if status == "open" else "🟢 Show Open", callback_data=f"self_tix|{page}|{'resolved' if status == 'open' else 'open'}"))
    current = all_tix[(page-1)*per_page : page*per_page]
    for t in current: kb.add(types.InlineKeyboardButton(f"🎫 #{t['_id']}", callback_data=f"self_view|{t['slug']}|{t['_id']}"))
    kb.row(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="ignore"),
           types.InlineKeyboardButton("➡️", callback_data=f"ignore" if page >= total_pages else f"self_tix|{page+1}|{status}"))
    kb.add(types.InlineKeyboardButton("❌ CLOSE", callback_data="close_menu"))
    smart_edit(chat_id, msg_id, f"📂 *Your {status.upper()} Tickets:*", reply_markup=kb)

def render_ticket_view(chat_id, msg_id, slug, tid, target_uid, page, status):
    col = get_ticket_col(slug); t = col.find_one({"_id": tid})
    if not t: return
    history = t.get('history', []); media_btns = []
    chat_text = f"🎫 *Ticket #{tid}*\nCategory: `{slug.upper()}`\nStatus: `{t['status'].upper()}`\n\n"
    
    # Initial Message (Index 0)
    tag = ""
    if 'photo' in t: tag = "🖼️ [Photo #0] "; media_btns.append(types.InlineKeyboardButton("[0] 🖼️", callback_data=f"v_med|{slug}|{tid}|0"))
    elif 'video' in t: tag = "🎥 [Video #0] "; media_btns.append(types.InlineKeyboardButton("[0] 🎥", callback_data=f"v_med|{slug}|{tid}|0"))
    chat_text += f"👤 *You:* {tag}{t['text']}\n"
    
    for h in history:
        sender = "👤 *You*" if h['role'] == 'user' else "👨‍💻 *Admin*"
        tag = ""
        if 'photo' in h: 
            tag = f"🖼️ [Photo #{h['index']}] "
            media_btns.append(types.InlineKeyboardButton(f"[{h['index']}] 🖼️", callback_data=f"v_med|{slug}|{tid}|{h['index']}"))
        elif 'video' in h:
            tag = f"🎥 [Video #{h['index']}] "
            media_btns.append(types.InlineKeyboardButton(f"[{h['index']}] 🎥", callback_data=f"v_med|{slug}|{tid}|{h['index']}"))
        chat_text += f"{sender}: {tag}{h['text']}\n"

    kb = types.InlineKeyboardMarkup(row_width=4)
    if media_btns:
        kb.row(*media_btns)
        if len(media_btns) > 1:
            kb.add(types.InlineKeyboardButton("🖼️ VIEW ALL MEDIA", callback_data=f"v_all_med|{slug}|{tid}"))
    if t['status'] == 'open':
        kb.row(types.InlineKeyboardButton("📩 Reply", callback_data=f"t_rep|{slug}|{tid}|{target_uid}|{page}|{status}"),
               types.InlineKeyboardButton("✅ Resolve", callback_data=f"t_res|{slug}|{tid}|{target_uid}|{page}|{status}"))
    kb.add(types.InlineKeyboardButton("🔙 Back to Tickets", callback_data=f"nav_back"))
    smart_edit(chat_id, msg_id, chat_text, reply_markup=kb)

def render_self_view(chat_id, msg_id, uid, slug, tid):
    col = get_ticket_col(slug); t = col.find_one({"_id": tid})
    if not t: return
    history = t.get('history', []); media_btns = []; last_role = 'user'
    chat_text = f"🎫 *Ticket #{tid}* ({t['status'].upper()})\n\n"
    
    tag = ""
    if 'photo' in t: tag = "🖼️ [Photo #0] "; media_btns.append(types.InlineKeyboardButton("[0] 🖼️", callback_data=f"v_med|{slug}|{tid}|0"))
    elif 'video' in t: tag = "🎥 [Video #0] "; media_btns.append(types.InlineKeyboardButton("[0] 🎥", callback_data=f"v_med|{slug}|{tid}|0"))
    chat_text += f"👤 *You:* {tag}{t['text']}\n"
    
    for h in history:
        sender = "👤 *You*" if h['role'] == 'user' else "👨‍💻 *Admin*"
        tag = ""; last_role = h['role']
        if 'photo' in h: 
            tag = f"🖼️ [Photo #{h['index']}] "
            media_btns.append(types.InlineKeyboardButton(f"[{h['index']}] 🖼️", callback_data=f"v_med|{slug}|{tid}|{h['index']}"))
        elif 'video' in h:
            tag = f"🎥 [Video #{h['index']}] "
            media_btns.append(types.InlineKeyboardButton(f"[{h['index']}] 🎥", callback_data=f"v_med|{slug}|{tid}|{h['index']}"))
        chat_text += f"{sender}: {tag}{h['text']}\n"

    kb = types.InlineKeyboardMarkup(row_width=4)
    if media_btns:
        kb.row(*media_btns)
        if len(media_btns) > 1:
            kb.add(types.InlineKeyboardButton("🖼️ VIEW ALL MEDIA", callback_data=f"v_all_med|{slug}|{tid}"))
    if t['status'] == 'open':
        if last_role == 'admin':
            kb.add(types.InlineKeyboardButton("📩 Reply to Admin", callback_data=f"u_rep|{slug}|{tid}"))
        kb.add(types.InlineKeyboardButton("✅ Resolve My Ticket", callback_data=f"self_res|{slug}|{tid}"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=f"self_tix|1|{t['status']}"))
    smart_edit(chat_id, msg_id, chat_text, reply_markup=kb)

# --- ROUTER ---
def process_action(action, uid, chat_id, msg_id, call_id, is_back=False):
    if uid not in user_states: user_states[uid] = {}
    navigable = any(action.startswith(x) for x in ["u_list|", "u_tix|", "t_view|", "self_tix", "self_view"]) or action == "t_menu"
    if navigable and not is_back:
        curr = user_states[uid].get('current_view')
        if curr and curr != action:
            if 'hist' not in user_states[uid]: user_states[uid]['hist'] = []
            if not user_states[uid]['hist'] or user_states[uid]['hist'][-1] != curr:
                user_states[uid]['hist'].append(curr)
                if len(user_states[uid]['hist']) > 20: user_states[uid]['hist'].pop(0)
        user_states[uid]['current_view'] = action
    
    if action == "t_menu":
        if not is_admin(uid): return
        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [types.InlineKeyboardButton(f"{v['icon']} {k}", callback_data=f"adm_cat|{v['slug']}") for k, v in CATEGORIES.items()]
        kb.add(*btns); kb.add(types.InlineKeyboardButton("❌ Close", callback_data="close_menu"))
        smart_edit(chat_id, msg_id, "📂 *Select Category:*", reply_markup=kb)
    elif action == "close_menu":
        try: bot.delete_message(chat_id, msg_id)
        except: pass
    elif action.startswith("adm_cat|"):
        process_action(f"u_list|{action.split('|')[1]}|1", uid, chat_id, msg_id, call_id)
    elif action.startswith("u_list|"):
        render_user_list(chat_id, msg_id, uid, action.split("|")[1], action.split("|")[2])
    elif action.startswith("u_tix|"):
        _, s, t_uid, p, st = action.split("|")
        render_user_tickets(chat_id, msg_id, uid, s, t_uid, p, st)
    elif action.startswith("self_tix|"):
        _, p, st = action.split("|"); render_self_tickets(chat_id, msg_id, uid, p, st)
    elif action.startswith("self_view|"):
        render_self_view(chat_id, msg_id, uid, action.split("|")[1], action.split("|")[2])
    elif action == "nav_back":
        hist = user_states.get(uid, {}).get('hist', [])
        if hist: process_action(hist.pop(), uid, chat_id, msg_id, call_id, is_back=True)
        else: process_action("t_menu", uid, chat_id, msg_id, call_id, is_back=True)
    elif action.startswith("t_view|"):
        _, s, tid, t_uid, p, st = action.split("|")
        render_ticket_view(chat_id, msg_id, s, tid, t_uid, p, st)
    elif action.startswith("v_med|"):
        _, s, tid, idx = action.split("|"); col = get_ticket_col(s); t = col.find_one({"_id": tid})
        if not t: return
        file_id, m_type, m_text = None, None, ""
        if idx == "0":
            file_id = t.get('photo') or t.get('video'); m_type = 'photo' if 'photo' in t else 'video'
            m_text = t.get('text', '')
        else:
            for h in t.get('history', []):
                if h.get('index') == int(idx):
                    file_id = h.get('photo') or h.get('video'); m_type = 'photo' if 'photo' in h else 'video'; m_text = h.get('text', ''); break
        if file_id:
            try:
                caption = f"📩 Message: \"{m_text}\"" if m_text and m_text != "[Media]" else f"Media #{idx}"
                if m_type == 'photo': bot.send_photo(chat_id, file_id, caption=caption)
                else: bot.send_video(chat_id, file_id, caption=caption)
            except: bot.answer_callback_query(call_id, "❌ Error sending file.")
    elif action.startswith("v_all_med|"):
        _, s, tid = action.split("|"); col = get_ticket_col(s); t = col.find_one({"_id": tid})
        if not t: return
        media_list = []
        if 'photo' in t: media_list.append(types.InputMediaPhoto(t['photo'], caption=f"🎫 Ticket #{tid} - #0"))
        elif 'video' in t: media_list.append(types.InputMediaVideo(t['video'], caption=f"🎫 Ticket #{tid} - #0"))
        for h in t.get('history', []):
            if 'photo' in h: media_list.append(types.InputMediaPhoto(h['photo'], caption=f"Message #{h['index']}"))
            elif 'video' in h: media_list.append(types.InputMediaVideo(h['video'], caption=f"Message #{h['index']}"))
        if media_list:
            for i in range(0, len(media_list), 10):
                try: bot.send_media_group(chat_id, media_list[i:i+10])
                except: bot.answer_callback_query(call_id, "❌ Error sending album.")
        else: bot.answer_callback_query(call_id, "❌ No media found.")
    elif action.startswith("t_res|"):
        _, s, tid, t_uid, p, st = action.split("|"); col = get_ticket_col(s)
        col.update_one({"_id": tid}, {"$set": {"status": "resolved", "resolved_at": datetime.now()}})
        render_user_tickets(chat_id, msg_id, uid, s, t_uid, p, "open", notification=f"✅ Ticket #{tid} Resolved!")
    elif action.startswith("self_res|"):
        _, s, tid = action.split("|"); col = get_ticket_col(s)
        col.update_one({"_id": tid}, {"$set": {"status": "resolved", "resolved_at": datetime.now()}})
        render_self_tickets(chat_id, msg_id, uid, 1, "open")
    elif action.startswith("t_rep|"):
        _, s, tid, t_uid, p, st = action.split("|")
        user_states[uid].update({'state': 'admin_reply', 'tid': tid, 'slug': s, 'target_uid': int(t_uid), 'page': p, 'status': st, 'chat_id': chat_id, 'msg_id': msg_id, 'time': time.time()})
        smart_edit(chat_id, msg_id, f"📝 *Replying to #{tid}*...", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_reply")))
    elif action == "cancel_reply":
        state = user_states.pop(uid, None)
        if state: render_ticket_view(chat_id, msg_id, state['slug'], state['tid'], state['target_uid'], state['page'], state['status'])
    elif action.startswith("u_rep|"):
        _, s, tid = action.split("|")
        user_states[uid].update({'state': 'user_reply', 'tid': tid, 'slug': s, 'chat_id': chat_id, 'msg_id': msg_id, 'time': time.time()})
        smart_edit(chat_id, msg_id, f"📝 *Replying to Admin (#{tid})*...", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_reply")))
    elif action == "cancel_user_reply":
        state = user_states.pop(uid, None)
        if state: render_self_view(chat_id, msg_id, uid, state['slug'], state['tid'])
    elif action.startswith("cat|"):
        user_states[uid] = {'state': 'waiting', 'slug': action.split("|")[1], 'time': time.time()}
        smart_edit(chat_id, msg_id, f"📝 *Category: {user_states[uid]['slug'].upper()}*\nPlease describe your issue:", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Cancel", callback_data="user_start")))
    elif action == "user_start":
        user_states.pop(uid, None); start(types.Message(None, None, None, types.User(uid, False, "User"), None, None, None))

# --- HANDLERS ---
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.chat.id; kb = types.InlineKeyboardMarkup()
    if is_admin(uid): kb.add(types.InlineKeyboardButton("📂 View Tickets", callback_data="t_menu"))
    else:
        for k, v in CATEGORIES.items(): kb.add(types.InlineKeyboardButton(f"{v['icon']} {k}", callback_data=f"cat|{v['slug']}"))
        kb.add(types.InlineKeyboardButton("🎫 Check My Tickets", callback_data="self_tix|1|open"))
    bot.send_message(uid, "👋 *Support Bot Active*", reply_markup=kb)

@bot.message_handler(commands=['check'])
def check_cmd(message): render_self_tickets(message.chat.id, None, message.from_user.id, 1, "open")

@bot.callback_query_handler(func=lambda c: True)
def router(call):
    bot.answer_callback_query(call.id); process_action(call.data, call.from_user.id, call.message.chat.id, call.message.message_id, call.id)

@bot.message_handler(content_types=['text', 'photo', 'video'])
def handle_all(message):
    uid = message.from_user.id; state = user_states.get(uid)
    if state and time.time() - state.get('time', 0) > 180: user_states.pop(uid, None); state = None
    if not state: return
    try:
        col = get_ticket_col(state['slug'])
        if state['state'] == 'waiting':
            counter = settings_col.find_one_and_update({"_id": "ticket_counter"}, {"$inc": {"count": 1}}, upsert=True, return_document=pymongo.ReturnDocument.AFTER)
            tid = f"{(message.from_user.username or 'user').split('_')[0]}/{counter['count']}"
            ticket = {'_id': tid, 'user_id': uid, 'name': message.from_user.first_name, 'username': message.from_user.username, 'text': message.text or message.caption or "[Media]", 'status': 'open', 'created_at': datetime.now(), 'history': []}
            if message.photo: ticket['photo'] = message.photo[-1].file_id
            elif message.video: ticket['video'] = message.video.file_id
            col.insert_one(ticket); bot.reply_to(message, f"✅ *Ticket Created: #{tid}*")
            for adm in ADMIN_LIST: bot.send_message(adm, f"⚠️ *New Ticket #{tid}*", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📂 View", callback_data=f"t_view|{state['slug']}|{tid}|{uid}|1|open")))
            user_states.pop(uid, None)
        elif state['state'] in ['admin_reply', 'user_reply']:
            t = col.find_one({"_id": state['tid']}); idx = len(t.get('history', [])) + 1
            reply = {'role': 'admin' if state['state'] == 'admin_reply' else 'user', 'text': message.text or message.caption or "[Media]", 'time': datetime.now(), 'index': idx}
            if message.photo: reply['photo'] = message.photo[-1].file_id
            elif message.video: reply['video'] = message.video.file_id
            col.update_one({"_id": state['tid']}, {"$push": {"history": reply}})
            if state['state'] == 'admin_reply':
                target_uid = state['target_uid']
                kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📩 Reply to Admin", callback_data=f"u_rep|{state['slug']}|{state['tid']}"))
                try:
                    if message.photo: bot.send_photo(target_uid, message.photo[-1].file_id, caption=f"👨‍💻 *Admin Reply (#{state['tid']}):*\n{reply['text']}", reply_markup=kb)
                    elif message.video: bot.send_video(target_uid, message.video.file_id, caption=f"👨‍💻 *Admin Reply (#{state['tid']}):*\n{reply['text']}", reply_markup=kb)
                    else: bot.send_message(target_uid, f"👨‍💻 *Admin Reply (#{state['tid']}):*\n{reply['text']}", reply_markup=kb)
                except: pass
                render_user_tickets(state['chat_id'], state['msg_id'], uid, state['slug'], target_uid, state['page'], state['status'], notification=f"✅ Reply Sent to User for #{state['tid']}!")
            else:
                for adm in ADMIN_LIST: 
                    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📂 View", callback_data=f"t_view|{state['slug']}|{state['tid']}|{uid}|1|open"))
                    try:
                        if message.photo: bot.send_photo(adm, message.photo[-1].file_id, caption=f"📩 *User Reply (#{state['tid']}):*\n{reply['text']}", reply_markup=kb)
                        elif message.video: bot.send_video(adm, message.video.file_id, caption=f"📩 *User Reply (#{tid}):*\n{reply['text']}", reply_markup=kb)
                        else: bot.send_message(adm, f"📩 *User Reply (#{state['tid']}):*\n{reply['text']}", reply_markup=kb)
                    except: pass
                smart_edit(state['chat_id'], state['msg_id'], f"✅ *Reply sent for #{state['tid']}!*")
            user_states.pop(uid, None)
            try: bot.delete_message(message.chat.id, message.message_id)
            except: pass
    except: traceback.print_exc()

if __name__ == "__main__":
    bot.remove_webhook(); bot.infinity_polling(timeout=60)
