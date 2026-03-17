import os
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import json
import time
import threading
import shutil
import zipfile
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from flask import Flask, jsonify
import logging

# ================= ENVIRONMENT VARIABLES =================
TOKEN = os.environ.get('BOT_TOKEN', "8648798788:AAGNZfQp6zmVy3MtqHjHdEjzIVrbw1RJKPU")
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', "@prime_xyron")
ADMIN_IDS = [int(id) for id in os.environ.get('ADMIN_IDS', "8373846582").split(',')]
PORT = int(os.environ.get('PORT', 5000))

# ================= CONFIGURATION =================
MAX_QUEUE_SIZE = 10
PROCESSING_TIME_PER_TASK = 30
AUTO_CLEANUP_MINUTES = 15
REFERRAL_BONUS = 2
DAILY_LIMIT_BASE = 5
BOT_NAME = "𝑷𝑿 𝑽𝑰𝑬𝑾 𝑺𝑶𝑼𝑹𝑪𝑬 𝑩𝑶𝑻"
FOOTER = f"\n\n━━━━━━━━━━━━━━━━━━━━\n  『 {BOT_NAME} 』👨‍💻"

# ================= FLASK APP FOR RENDER =================
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({
        "status": "active",
        "bot_name": BOT_NAME,
        "message": "Bot is running!"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

# ================= DATABASE SETUP =================
conn = sqlite3.connect("bot_data.db", check_same_thread=False)

cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS users
             (user_id INTEGER PRIMARY KEY,
              username TEXT,
              first_name TEXT,
              joined_date TEXT,
              daily_limit INTEGER DEFAULT 5,
              used_today INTEGER DEFAULT 0,
              last_reset TEXT,
              auto_delete BOOLEAN DEFAULT 1,
              banned BOOLEAN DEFAULT 0)''')
cur.execute('''CREATE TABLE IF NOT EXISTS referrals
             (referrer_id INTEGER,
              referred_id INTEGER UNIQUE,
              date TEXT,
              PRIMARY KEY (referrer_id, referred_id))''')
cur.execute('''CREATE TABLE IF NOT EXISTS queue
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              mode TEXT,
              url TEXT,
              added_time TEXT)''')
cur.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY,
              value TEXT)''')
cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pause', '0')")
conn.commit()
cur.close()

# ================= TELEGRAM BOT SETUP =================
bot = telebot.TeleBot(TOKEN)

# ================= HELPER FUNCTIONS =================
def send_msg(chat_id, text, **kwargs):
    return bot.send_message(chat_id, text + FOOTER, **kwargs)

def get_setting(key):
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None

def set_setting(key, value):
    cur = conn.cursor()
    cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    cur.close()

def get_user(user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    cur.close()
    return row

def create_user(user_id, username, first_name):
    now = datetime.now().isoformat()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date, daily_limit, used_today, last_reset, auto_delete, banned) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, username, first_name, now, DAILY_LIMIT_BASE, 0, now, 1, 0))
    conn.commit()
    cur.close()

def reset_daily_limits_if_needed():
    cur = conn.cursor()
    cur.execute("SELECT user_id, last_reset FROM users")
    rows = cur.fetchall()
    cur.close()
    now = datetime.now()
    for user_id, last_reset_str in rows:
        try:
            if last_reset_str and isinstance(last_reset_str, str):
                last_reset = datetime.fromisoformat(last_reset_str)
                if now - last_reset > timedelta(hours=24):
                    cur2 = conn.cursor()
                    cur2.execute("UPDATE users SET used_today=0, last_reset=? WHERE user_id=?", (now.isoformat(), user_id))
                    conn.commit()
                    cur2.close()
        except:
            cur2 = conn.cursor()
            cur2.execute("UPDATE users SET used_today=0, last_reset=? WHERE user_id=?", (now.isoformat(), user_id))
            conn.commit()
            cur2.close()

def check_force_join(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return True

def is_admin(user_id):
    return user_id in ADMIN_IDS

def can_use_bot(user_id):
    if get_setting('maintenance') == '1' and not is_admin(user_id):
        return False, "🔧 Bot is under maintenance. Please try later."
    user = get_user(user_id)
    if user and user[8]:
        return False, "🚫 You are banned from using this bot."
    if not check_force_join(user_id):
        return False, f"🚫 Access Denied!\n\nPlease join our channel to use this bot:\n{CHANNEL_USERNAME}"
    return True, "OK"

def add_to_queue(user_id, mode, url):
    now = datetime.now().isoformat()
    cur = conn.cursor()
    cur.execute("INSERT INTO queue (user_id, mode, url, added_time) VALUES (?,?,?,?)", (user_id, mode, url, now))
    conn.commit()
    row_id = cur.lastrowid
    cur.close()
    return row_id

def get_queue_position(queue_id):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM queue WHERE id <= ?", (queue_id,))
    pos = cur.fetchone()[0]
    cur.close()
    return pos

def get_queue_length():
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM queue")
    length = cur.fetchone()[0]
    cur.close()
    return length

def remove_from_queue(queue_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM queue WHERE id=?", (queue_id,))
    conn.commit()
    cur.close()

def increment_used(user_id):
    cur = conn.cursor()
    cur.execute("UPDATE users SET used_today = used_today + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    cur.close()

def get_user_limit(user_id):
    cur = conn.cursor()
    cur.execute("SELECT daily_limit, used_today FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return 0, 0
    return row[0], row[1]

def add_referral_bonus(referrer_id):
    cur = conn.cursor()
    cur.execute("UPDATE users SET daily_limit = daily_limit + ? WHERE user_id=?", (REFERRAL_BONUS, referrer_id))
    conn.commit()
    cur.close()

def process_referral(new_user_id, referrer_id):
    if referrer_id == new_user_id:
        return False
    if not get_user(referrer_id):
        return False
    cur = conn.cursor()
    cur.execute("SELECT * FROM referrals WHERE referred_id=?", (new_user_id,))
    if cur.fetchone():
        cur.close()
        return False
    now = datetime.now().isoformat()
    cur.execute("INSERT INTO referrals (referrer_id, referred_id, date) VALUES (?,?,?)", (referrer_id, new_user_id, now))
    conn.commit()
    cur.close()
    add_referral_bonus(referrer_id)
    try:
        referred_user = get_user(new_user_id)
        name = referred_user[2] or f"User {new_user_id}"
        send_msg(referrer_id, f"🎉 **New Referral!**\n\n{name} joined using your link.\n✨ You earned +{REFERRAL_BONUS} extra daily limit!", parse_mode="Markdown")
    except:
        pass
    return True

def get_referral_count(user_id):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,))
    count = cur.fetchone()[0]
    cur.close()
    return count

def get_referral_link(user_id):
    return f"https://t.me/{bot.get_me().username}?start={user_id}"

def build_menu(menu_name):
    with open('menu.json', 'r', encoding='utf-8') as f:
        menus = json.load(f)
    menu = menus.get(menu_name, menus['start'])
    text = menu['text']
    keyboard_rows = menu['keyboard']
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for row in keyboard_rows:
        buttons = []
        for btn in row:
            if isinstance(btn, dict):
                btn_text = btn['text']
            else:
                btn_text = btn
            buttons.append(KeyboardButton(btn_text))
        markup.add(*buttons)
    return text, markup

def extract_website(url, mode, user_id):
    folder = f"extract_{user_id}_{int(time.time())}"
    os.makedirs(folder, exist_ok=True)
    try:
        res = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        res.raise_for_status()
        html = res.text
        with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        soup = BeautifulSoup(html, "html.parser")
        files = []
        for tag in soup.find_all(["img", "script", "link"]):
            src = tag.get("src") or tag.get("href")
            if src and not src.startswith('data:'):
                files.append(urljoin(url, src))
        for file_url in files:
            try:
                r = requests.get(file_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                name = file_url.split("/")[-1].split("?")[0]
                if not name or len(name) > 100:
                    name = str(hash(file_url)) + ".bin"
                path = os.path.join(folder, name)
                with open(path, "wb") as f:
                    f.write(r.content)
            except:
                pass
        zip_name = f"extract_{user_id}_{int(time.time())}.zip"
        with zipfile.ZipFile(zip_name, "w") as zipf:
            for root, dirs, files in os.walk(folder):
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, folder)
                    zipf.write(filepath, arcname)
        shutil.rmtree(folder)
        return zip_name
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise e

# ================= BACKGROUND WORKER =================
def processing_worker():
    while True:
        try:
            if get_setting('pause') == '1':
                time.sleep(5)
                continue
            cur = conn.cursor()
            cur.execute("SELECT id, user_id, mode, url FROM queue ORDER BY id ASC LIMIT 1")
            task = cur.fetchone()
            cur.close()
            if not task:
                time.sleep(2)
                continue

            queue_id, user_id, mode, url = task
            user = get_user(user_id)
            if not user or user[8]:
                remove_from_queue(queue_id)
                continue

            limit, used = get_user_limit(user_id)
            if used >= limit:
                send_msg(user_id, "❌ You have reached your daily limit. Come back tomorrow or invite friends to increase your limit!")
                remove_from_queue(queue_id)
                continue

            send_msg(user_id, f"🚀 **Processing your request...**\nMode: {mode}\nURL: {url}\n⏳ Please wait.", parse_mode="Markdown")
            try:
                zip_file = extract_website(url, mode, user_id)
                with open(zip_file, 'rb') as f:
                    bot.send_document(user_id, f, caption=f"✅ **Extraction completed!**\n📦 Mode: {mode}\n🔗 {url}" + FOOTER, parse_mode="Markdown")
                os.remove(zip_file)
                increment_used(user_id)
            except Exception as e:
                send_msg(user_id, f"❌ **Extraction failed:** {str(e)}", parse_mode="Markdown")
            remove_from_queue(queue_id)

            queue_len = get_queue_length()
            if queue_len > 0:
                send_msg(user_id, f"👥 There are {queue_len} tasks remaining in queue.")
        except Exception as e:
            print(f"Worker error: {e}")
            time.sleep(5)

worker_thread = threading.Thread(target=processing_worker, daemon=True)
worker_thread.start()

def cleanup_old_files():
    while True:
        time.sleep(60)
        now = time.time()
        for f in os.listdir('.'):
            if f.startswith('extract_') and f.endswith('.zip'):
                if os.path.getmtime(f) < now - AUTO_CLEANUP_MINUTES * 60:
                    try:
                        os.remove(f)
                    except:
                        pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ================= BOT HANDLERS =================
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    args = message.text.split()
    if len(args) > 1:
        try:
            referrer_id = int(args[1])
            if not get_user(user_id):
                create_user(user_id, username, first_name)
                process_referral(user_id, referrer_id)
        except:
            pass
    if not get_user(user_id):
        create_user(user_id, username, first_name)
    reset_daily_limits_if_needed()
    allowed, msg = can_use_bot(user_id)
    if not allowed:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Joined", callback_data="check_join"))
        send_msg(user_id, msg, reply_markup=markup)
        return

    welcome = (
        f"╔══════════════════════╗\n"
        f"     🌟 **{BOT_NAME}** 🌟\n"
        f"╚══════════════════════╝\n\n"
        f"✨ **Welcome {first_name}!** ✨\n\n"
        f"📌 **I can extract any website's source code and files.**\n\n"
        f"⚡ **Features:**\n"
        f"├ 🔹 Fast Extraction\n"
        f"├ 🔹 Multiple Modes\n"
        f"├ 🔹 Smart Queue System\n"
        f"├ 🔹 Referral Bonus\n"
        f"└ 🔹 Auto Cleanup\n\n"
        f"📤 **Just send me a URL and choose a mode!**"
    )
    send_msg(user_id, welcome, parse_mode="Markdown")
    text, markup = build_menu('start')
    send_msg(user_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def check_join_callback(call):
    user_id = call.from_user.id
    if check_force_join(user_id):
        bot.answer_callback_query(call.id, "✅ Thank you for joining!")
        first_name = call.from_user.first_name or ""
        welcome = (
            f"╔══════════════════════╗\n"
            f"     🌟 **{BOT_NAME}** 🌟\n"
            f"╚══════════════════════╝\n\n"
            f"✨ **Welcome {first_name}!** ✨\n\n"
            f"📌 **I can extract any website's source code and files.**\n\n"
            f"⚡ **Features:**\n"
            f"├ 🔹 Fast Extraction\n"
            f"├ 🔹 Multiple Modes\n"
            f"├ 🔹 Smart Queue System\n"
            f"├ 🔹 Referral Bonus\n"
            f"└ 🔹 Auto Cleanup\n\n"
            f"📤 **Just send me a URL and choose a mode!**"
        )
        send_msg(user_id, welcome, parse_mode="Markdown")
        text, markup = build_menu('start')
        send_msg(user_id, text, reply_markup=markup)
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined yet. Please join the channel first.", show_alert=True)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = (
        f"╔══════════════════════╗\n"
        f"     👑 **ADMIN PANEL** 👑\n"
        f"╚══════════════════════╝\n\n"
        f"Select an option:"
    )
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("📊 Stats"),
        KeyboardButton("📢 Broadcast"),
        KeyboardButton("🚫 Ban User"),
        KeyboardButton("✅ Unban User"),
        KeyboardButton("⬆️ Increase Limit"),
        KeyboardButton("⬇️ Decrease Limit"),
        KeyboardButton("🎁 Set Referral Bonus"),
        KeyboardButton("🔧 Maintenance"),
        KeyboardButton("⏸ Pause"),
        KeyboardButton("🧹 Clean Files"),
        KeyboardButton("🔙 Back to Main")
    )
    send_msg(user_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🌐 Extract Website")
def extract_menu(msg):
    user_id = msg.chat.id
    allowed, err = can_use_bot(user_id)
    if not allowed:
        send_msg(user_id, err)
        return
    text, markup = build_menu('extract_menu')
    send_msg(user_id, text, reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in ["⚡ Fast Mode", "🎨 Full Mode", "🖼 Media Only"])
def handle_mode_selection(msg):
    user_id = msg.chat.id
    mode = msg.text
    send_msg(user_id, f"📤 **Send me the URL to extract in {mode} mode:**\n\n(Example: https://example.com)", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_url, mode)

def process_url(msg, mode):
    user_id = msg.chat.id
    url = msg.text.strip()
    if not url.startswith(('http://', 'https://')):
        send_msg(user_id, "❌ **Invalid URL!**\n\nPlease include **http://** or **https://**", parse_mode="Markdown")
        return
    limit, used = get_user_limit(user_id)
    if used >= limit:
        send_msg(user_id, "❌ **Limit Reached!**\n\nYou have reached your daily limit. Come back tomorrow or invite friends to increase your limit!")
        return
    if get_queue_length() >= MAX_QUEUE_SIZE:
        send_msg(user_id, "🚧 **Queue Full!**\n\nThe queue is full. Please try again later.")
        return
    queue_id = add_to_queue(user_id, mode, url)
    position = get_queue_position(queue_id)
    wait_time = (position - 1) * PROCESSING_TIME_PER_TASK
    send_msg(
        user_id,
        f"⏳ **Request Added to Queue!**\n\n"
        f"📌 Position: {position}\n"
        f"⏱ Estimated wait: {wait_time} seconds\n\n"
        f"✅ You'll be notified when processing starts.",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda msg: msg.text == "📊 My Status")
def status_menu(msg):
    user_id = msg.chat.id
    allowed, err = can_use_bot(user_id)
    if not allowed:
        send_msg(user_id, err)
        return
    text, markup = build_menu('status')
    send_msg(user_id, text, reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "📦 My Limit")
def my_limit(msg):
    user_id = msg.chat.id
    limit, used = get_user_limit(user_id)
    ref_count = get_referral_count(user_id)
    bonus = ref_count * REFERRAL_BONUS
    percentage = (used / limit) * 100 if limit > 0 else 0
    bar = "█" * int(percentage/10) + "░" * (10 - int(percentage/10))
    send_msg(
        user_id,
        f"╔══════════════════════╗\n"
        f"     📊 **YOUR LIMIT**\n"
        f"╚══════════════════════╝\n\n"
        f"📦 Used: {used}/{limit}\n"
        f"📊 Progress: {bar} {percentage:.1f}%\n"
        f"🎁 Referral Bonus: +{bonus}\n"
        f"👥 Invited: {ref_count}\n\n"
        f"🔗 **Your Referral Link:**\n`{get_referral_link(user_id)}`",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda msg: msg.text == "👥 My Referrals")
def my_referrals(msg):
    user_id = msg.chat.id
    cur = conn.cursor()
    cur.execute("SELECT referred_id, date FROM referrals WHERE referrer_id=?", (user_id,))
    rows = cur.fetchall()
    cur.close()
    if not rows:
        send_msg(user_id, "📭 **No Referrals Yet!**\n\nShare your link to earn bonus limits!")
        return
    text = "╔══════════════════════╗\n     👥 **YOUR REFERRALS**\n╚══════════════════════╝\n\n"
    for i, (ref_id, date) in enumerate(rows, 1):
        text += f"{i}. User `{ref_id}` – {date[:10]}\n"
    send_msg(user_id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🎁 Referral")
def referral_info(msg):
    user_id = msg.chat.id
    link = get_referral_link(user_id)
    ref_count = get_referral_count(user_id)
    send_msg(
        user_id,
        f"╔══════════════════════╗\n"
        f"     🎁 **REFERRAL**\n"
        f"╚══════════════════════╝\n\n"
        f"✨ **Earn +{REFERRAL_BONUS} extra daily limit per referral!**\n\n"
        f"🔗 **Your Link:**\n`{link}`\n\n"
        f"👥 **Total Referrals:** {ref_count}",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Settings")
def settings_menu(msg):
    user_id = msg.chat.id
    allowed, err = can_use_bot(user_id)
    if not allowed:
        send_msg(user_id, err)
        return
    text, markup = build_menu('settings')
    send_msg(user_id, text, reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "🗑 Auto Delete")
def toggle_auto_delete(msg):
    user_id = msg.chat.id
    cur = conn.cursor()
    cur.execute("SELECT auto_delete FROM users WHERE user_id=?", (user_id,))
    current = cur.fetchone()[0]
    cur.close()
    new_val = 0 if current else 1
    cur = conn.cursor()
    cur.execute("UPDATE users SET auto_delete=? WHERE user_id=?", (new_val, user_id))
    conn.commit()
    cur.close()
    state = "✅ ENABLED" if new_val else "❌ DISABLED"
    send_msg(user_id, f"🗑 **Auto Delete:** {state}", parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Reset Info")
def reset_info(msg):
    user_id = msg.chat.id
    send_msg(user_id, "🔄 **Reset Information**\n\n• Daily limit resets every 24 hours\n• Referral bonuses are permanent\n• Contact admin for manual reset")

@bot.message_handler(func=lambda msg: msg.text in ["🔙 Back to Main", "🔙 Back", "Back", "back", "BACK", "🔙"])
def back_to_main(msg):
    user_id = msg.chat.id
    text, markup = build_menu('start')
    send_msg(user_id, text, reply_markup=markup)

# ================= ADMIN HANDLERS =================
@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.from_user.id))
def admin_stats(message):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE banned=1")
    banned_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM queue")
    queue_len = cur.fetchone()[0]
    cur.execute("SELECT SUM(used_today) FROM users")
    today_used = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM referrals")
    total_ref = cur.fetchone()[0]
    cur.close()
    send_msg(
        message.chat.id,
        f"╔══════════════════════╗\n"
        f"     📊 **BOT STATS**\n"
        f"╚══════════════════════╝\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🚫 Banned: {banned_users}\n"
        f"⏳ Queue: {queue_len}\n"
        f"📦 Today's Extractions: {today_used}\n"
        f"🎁 Total Referrals: {total_ref}",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.from_user.id))
def admin_broadcast_prompt(message):
    msg = send_msg(message.chat.id, "📢 **Send the message to broadcast:**")
    bot.register_next_step_handler(msg, admin_broadcast_send)

def admin_broadcast_send(message):
    if not is_admin(message.from_user.id):
        return
    broadcast_text = message.text
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    cur.close()
    success = 0
    fail = 0
    for (uid,) in users:
        try:
            send_msg(uid, f"📢 **BROADCAST MESSAGE**\n\n{broadcast_text}", parse_mode="Markdown")
            success += 1
        except:
            fail += 1
    send_msg(message.chat.id, f"✅ **Broadcast Complete**\n\nSuccess: {success}\nFailed: {fail}")

@bot.message_handler(func=lambda msg: msg.text == "🚫 Ban User" and is_admin(msg.from_user.id))
def admin_ban_prompt(message):
    msg = send_msg(message.chat.id, "🚫 **Send the user ID to ban:**")
    bot.register_next_step_handler(msg, admin_ban_execute)

def admin_ban_execute(message):
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text.strip())
        cur = conn.cursor()
        cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id,))
        conn.commit()
        cur.close()
        send_msg(message.chat.id, f"✅ **User {user_id} has been banned.**")
    except:
        send_msg(message.chat.id, "❌ **Invalid user ID!**")

@bot.message_handler(func=lambda msg: msg.text == "✅ Unban User" and is_admin(msg.from_user.id))
def admin_unban_prompt(message):
    msg = send_msg(message.chat.id, "✅ **Send the user ID to unban:**")
    bot.register_next_step_handler(msg, admin_unban_execute)

def admin_unban_execute(message):
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text.strip())
        cur = conn.cursor()
        cur.execute("UPDATE users SET banned=0 WHERE user_id=?", (user_id,))
        conn.commit()
        cur.close()
        send_msg(message.chat.id, f"✅ **User {user_id} has been unbanned.**")
    except:
        send_msg(message.chat.id, "❌ **Invalid user ID!**")

@bot.message_handler(func=lambda msg: msg.text == "⬆️ Increase Limit" and is_admin(msg.from_user.id))
def admin_increase_prompt(message):
    msg = send_msg(message.chat.id, "⬆️ **Send user ID and amount (e.g., 12345 5):**")
    bot.register_next_step_handler(msg, admin_increase_execute)

def admin_increase_execute(message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        user_id = int(parts[0])
        amount = int(parts[1]) if len(parts) > 1 else 1
        cur = conn.cursor()
        cur.execute("UPDATE users SET daily_limit = daily_limit + ? WHERE user_id=?", (amount, user_id))
        conn.commit()
        cur.close()
        send_msg(message.chat.id, f"✅ **Limit increased by {amount} for user {user_id}.**")
    except:
        send_msg(message.chat.id, "❌ **Invalid format! Use: user_id amount**")

@bot.message_handler(func=lambda msg: msg.text == "⬇️ Decrease Limit" and is_admin(msg.from_user.id))
def admin_decrease_prompt(message):
    msg = send_msg(message.chat.id, "⬇️ **Send user ID and amount (e.g., 12345 5):**")
    bot.register_next_step_handler(msg, admin_decrease_execute)

def admin_decrease_execute(message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        user_id = int(parts[0])
        amount = int(parts[1]) if len(parts) > 1 else 1
        cur = conn.cursor()
        cur.execute("UPDATE users SET daily_limit = daily_limit - ? WHERE user_id=?", (amount, user_id))
        conn.commit()
        cur.close()
        send_msg(message.chat.id, f"✅ **Limit decreased by {amount} for user {user_id}.**")
    except:
        send_msg(message.chat.id, "❌ **Invalid format! Use: user_id amount**")

@bot.message_handler(func=lambda msg: msg.text == "🎁 Set Referral Bonus" and is_admin(msg.from_user.id))
def admin_setref_prompt(message):
    msg = send_msg(message.chat.id, "🎁 **Send new referral bonus value (number):**")
    bot.register_next_step_handler(msg, admin_setref_execute)

def admin_setref_execute(message):
    if not is_admin(message.from_user.id):
        return
    try:
        global REFERRAL_BONUS
        new_bonus = int(message.text.strip())
        REFERRAL_BONUS = new_bonus
        send_msg(message.chat.id, f"✅ **Referral bonus set to {new_bonus}.**")
    except:
        send_msg(message.chat.id, "❌ **Invalid number!**")

@bot.message_handler(func=lambda msg: msg.text == "🔧 Maintenance" and is_admin(msg.from_user.id))
def admin_maintenance_toggle(message):
    current = get_setting('maintenance')
    new = '0' if current == '1' else '1'
    set_setting('maintenance', new)
    state = "🔧 ENABLED" if new == '1' else "✅ DISABLED"
    send_msg(message.chat.id, f"**Maintenance mode:** {state}", parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "⏸ Pause" and is_admin(msg.from_user.id))
def admin_pause_toggle(message):
    current = get_setting('pause')
    new = '0' if current == '1' else '1'
    set_setting('pause', new)
    state = "⏸ PAUSED" if new == '1' else "▶️ RESUMED"
    send_msg(message.chat.id, f"**Extractor:** {state}", parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🧹 Clean Files" and is_admin(msg.from_user.id))
def admin_clean(message):
    count = 0
    for f in os.listdir('.'):
        if f.startswith('extract_') and f.endswith('.zip'):
            try:
                os.remove(f)
                count += 1
            except:
                pass
    send_msg(message.chat.id, f"🧹 **Cleaned {count} temporary files.**")

@bot.message_handler(func=lambda msg: True)
def fallback(message):
    user_id = message.chat.id
    allowed, err = can_use_bot(user_id)
    if not allowed:
        send_msg(user_id, err)
        return
    send_msg(user_id, "❓ **Sorry, I didn't understand that.**\n\nPlease use the menu buttons below.")

# ================= CREATE MENU.JSON IF NOT EXISTS =================
if not os.path.exists('menu.json'):
    default_menu = {
        "start": {
            "text": "📌 **Main Menu**\nChoose an option:",
            "keyboard": [
                ["🌐 Extract Website"],
                ["📊 My Status", "🎁 Referral"],
                ["⚙️ Settings"]
            ]
        },
        "extract_menu": {
            "text": "⚡ **Select Extract Mode**",
            "keyboard": [
                ["⚡ Fast Mode"],
                ["🎨 Full Mode"],
                ["🖼 Media Only"],
                ["🔙 Back to Main"]
            ]
        },
        "status": {
            "text": "📊 **Your Status**",
            "keyboard": [
                ["📦 My Limit"],
                ["👥 My Referrals"],
                ["🔙 Back to Main"]
            ]
        },
        "settings": {
            "text": "⚙️ **Settings Panel**",
            "keyboard": [
                ["🗑 Auto Delete"],
                ["🔄 Reset Info"],
                ["🔙 Back to Main"]
            ]
        }
    }
    with open('menu.json', 'w', encoding='utf-8') as f:
        json.dump(default_menu, f, indent=2, ensure_ascii=False)

# ================= START BOT IN SEPARATE THREAD =================
def run_bot():
    print(f"✅ {BOT_NAME} started successfully...")
    bot.infinity_polling()

# ================= MAIN =================
if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Run Flask app
    app.run(host='0.0.0.0', port=PORT)
