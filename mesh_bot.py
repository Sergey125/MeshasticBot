import os
import time
import requests
import math
import threading
import re
import socket
import datetime
import telebot
from telebot.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

# ==========================================
# ⚙️ НАСТРОЙКИ (Заполните перед запуском)
# ==========================================
HELTEC_IP = "192.168.1.XXX" # IP-адрес вашей ноды
N8N_METEO_URL = "https://your-n8n-domain.com/webhook/meteo"
N8N_AI_URL = "https://your-n8n-domain.com/webhook/ai"

TG_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN" # Токен от @BotFather
TG_ADMIN_ID = 123456789              # Ваш Telegram ID

bot = telebot.TeleBot(TG_TOKEN)
mesh_interface = None
processed_packets = []

# --- ПАМЯТЬ БОТА ---
pending_messages = {}
recent_text_senders = [] 
START_TIME = datetime.datetime.now()

# ==========================================
# 💾 СИСТЕМА УЧЕТА СБОЕВ
# ==========================================
STATS_FILE = "bot_fails.txt"

def get_fails():
    try:
        with open(STATS_FILE, "r") as f:
            return int(f.read().strip())
    except: return 0

def add_fail():
    fails = get_fails()
    try:
        with open(STATS_FILE, "w") as f:
            f.write(str(fails + 1))
    except: pass

def reset_fails():
    try:
        with open(STATS_FILE, "w") as f:
            f.write("0")
    except: pass

def daily_reporter():
    while True:
        now = datetime.datetime.now()
        if now.hour == 7 and now.minute == 0:
            fails = get_fails()
            if fails == 0:
                msg = "🌅 Доброе утро!\n\n✅ Сбоев за прошедшие сутки: 0\n🚀 Бот-мост работает стабильно и ждет команд."
            else:
                msg = f"🌅 Доброе утро!\n\n⚠️ Зафиксировано авто-перезапусков за сутки: {fails}\n✅ Сейчас система в сети и работает в штатном режиме."
            send_to_tg_bg(msg)
            reset_fails()
            time.sleep(60)
        time.sleep(20)

# ==========================================
# 🚀 ФОНОВАЯ ОТПРАВКА В TELEGRAM
# ==========================================
def send_to_tg_bg(text):
    try: bot.send_message(TG_ADMIN_ID, text)
    except Exception as e: print(f"❌ Ошибка отправки в TG: {e}")

# ==========================================
# 🕵️‍♂️ ПАРАНОИДАЛЬНЫЙ СТОРОЖЕВОЙ ПЕС
# ==========================================
def watchdog():
    fails = 0
    while True:
        time.sleep(3)
        response = os.system(f"ping -c 1 -W 1 {HELTEC_IP} > /dev/null 2>&1")
        if response == 0: fails = 0
        else:
            fails += 1
            print(f"⚠️ Потерян пинг ({fails}/3)")
            
        if fails >= 3:
            print("❌ Heltec пропал с радаров. Жесткий рестарт!")
            send_to_tg_bg("🚨 ОШИБКА: Heltec пропал из Wi-Fi сети. Пытаюсь переподключиться...")
            add_fail() 
            time.sleep(2)
            os._exit(1)

def on_connection_lost(interface=None):
    print("❌ Библиотека Meshtastic потеряла связь с рацией (Errno 104)!")
    add_fail() 
    time.sleep(2)
    os._exit(1)

# ==========================================
# 📱 TELEGRAM: ОБРАБОТКА КНОПОК
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("send_") or call.data == "cancel_msg")
def callback_handler(call):
    user_id = call.from_user.id
    if user_id != TG_ADMIN_ID: return
        
    if call.data == "cancel_msg":
        bot.edit_message_text("❌ *Отправка отменена.*", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        if user_id in pending_messages: del pending_messages[user_id]
        return
        
    if user_id not in pending_messages:
        bot.edit_message_text("⚠️ *Ошибка: сообщение устарело.*", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
        return
            
    msg_data = pending_messages.pop(user_id)
    
    if call.data.startswith("send_ch_"):
        channel = int(call.data.split("_")[2])
        is_broadcast = True
        target = "^all"
    elif call.data.startswith("send_node_"):
        channel = 0
        is_broadcast = False
        target = call.data.split("send_node_")[1]
    elif call.data == "send_dm":
        channel = 0
        is_broadcast = False
        target = msg_data['target']
        
    bot.edit_message_text("⏳ *Отправляю в эфир...*", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
    
    threading.Thread(target=send_smart_message, args=(
        mesh_interface, msg_data['text'], is_broadcast, target, channel, "TG Admin"
    ), daemon=True).start()

# ==========================================
# 📱 TELEGRAM: ПРИЕМ СООБЩЕНИЙ И КОМАНД
# ==========================================
@bot.message_handler(func=lambda message: True)
def handle_tg_messages(message):
    global mesh_interface
    if message.from_user.id != TG_ADMIN_ID: return
    
    text = message.text.strip()
    lower_text = text.lower()

    if lower_text in ['/test', '\\test', 'тест', '/статус', 'статус']:
        uptime = datetime.datetime.now() - START_TIME
        fails = get_fails()
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        status_msg = (
            "📊 **Системный отчет:**\n\n"
            f"⏱ Аптайм службы: {hours}ч {minutes}м\n"
            f"⚠️ Сбоев сегодня: {fails}\n"
            f"📡 Соединение с рацией: {'✅ Активно' if mesh_interface else '❌ Оборвано'}"
        )
        bot.reply_to(message, status_msg, parse_mode='Markdown')
        return

    if not mesh_interface:
        bot.reply_to(message, "❌ Ошибка: Радиомодуль сейчас оффлайн.")
        return

    target = '^all'
    is_dm = False

    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text
        match_id = re.search(r'\((![a-f0-9]+)\)', reply_text, re.IGNORECASE)
        if match_id: 
            target = match_id.group(1)
            is_dm = True

    pending_messages[message.from_user.id] = {'text': text, 'target': target}
    markup = InlineKeyboardMarkup(row_width=1)
    
    if is_dm:
        markup.add(
            InlineKeyboardButton(f"✅ Отправить в личку ({target})", callback_data="send_dm"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_msg")
        )
        confirm_text = f"🛡 **Подтвердите личное сообщение:**\n\n💬 Текст: `{text}`\n👤 Кому: *{target}*"
    else:
        buttons = []
        try:
            for ch in mesh_interface.localNode.channels:
                if getattr(ch, 'role', 0) != 0: 
                    ch_name = ch.settings.name if ch.settings.name else ("LongFast" if ch.index == 0 else f"Канал {ch.index}")
                    buttons.append(InlineKeyboardButton(f"📡 {ch_name}", callback_data=f"send_ch_{ch.index}"))
        except: pass
        
        dm_buttons = []
        try:
            for uid in recent_text_senders:
                node_data = mesh_interface.nodes.get(uid, {})
                uname = node_data.get('user', {}).get('longName', uid)
                dm_buttons.append(InlineKeyboardButton(f"👤 {uname} (ЛС)", callback_data=f"send_node_{uid}"))
        except: pass

        if not buttons:
            buttons.append(InlineKeyboardButton("📡 LongFast (Ch 0)", callback_data="send_ch_0"))
            
        for btn in buttons: markup.add(btn)
        for btn in dm_buttons: markup.add(btn) 
        markup.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_msg"))
        
        confirm_text = f"🛡 **Куда отправляем?**\n\n💬 Текст: `{text}`"

    bot.reply_to(message, confirm_text, parse_mode="Markdown", reply_markup=markup)

def run_tg_polling():
    try: 
        bot.set_my_commands([BotCommand("/test", "📊 Системный статус")])
        bot.remove_webhook()
    except: pass
    bot.infinity_polling(timeout=10, long_polling_timeout=5)

# ==========================================
# 📡 ОСНОВНЫЕ ФУНКЦИИ РАДИОБОТА
# ==========================================
def calc_distance(lat1, lon1, lat2, lon2):
    if not all([lat1, lon1, lat2, lon2]): return None
    R = 6371000 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def send_smart_message(interface, text, is_broadcast, dest_id, channel_id, user_full_name):
    if is_broadcast:
        try:
            raw_name = interface.localNode.channels[channel_id].settings.name
            target_str = f"Группа: {raw_name}" if raw_name else ("Группа: LongFast" if channel_id == 0 else f"Группа: Ch {channel_id}")
        except: target_str = f"Группа: Ch {channel_id}"
    else: target_str = "🔒 В личку"

    if is_broadcast and user_full_name != "TG Admin": text = f"Для {user_full_name} {text}"
        
    chunks = []
    current_chunk = ""
    for line in text.split('\n'):
        while len(line) > 180:
            if current_chunk:
                chunks.append(current_chunk.strip('\n'))
                current_chunk = ""
            chunks.append(line[:180])
            line = line[180:]
        if len(current_chunk) + len(line) < 180: current_chunk += line + "\n"
        else:
            chunks.append(current_chunk.strip('\n'))
            current_chunk = line + "\n"
    if current_chunk: chunks.append(current_chunk.strip('\n'))

    total = len(chunks)
    full_sent_text = ""
    
    for i, chunk in enumerate(chunks):
        msg = f"[{i+1}/{total}]\n{chunk}" if total > 1 else chunk
        full_sent_text += msg + "\n"
        try:
            if is_broadcast: interface.sendText(msg, channelIndex=channel_id)
            else: interface.sendText(msg, destinationId=dest_id)
        except Exception as e: print(f"❌ Ошибка физической отправки: {e}")
        if i < total - 1: time.sleep(5)
            
    if user_full_name == "TG Admin":
        threading.Thread(target=send_to_tg_bg, args=(f"✅ Отправлено в эфир ({target_str}):\n{full_sent_text.strip()}",), daemon=True).start()
    else:
        threading.Thread(target=send_to_tg_bg, args=(f"🤖 Бот -> {user_full_name}:\n{full_sent_text.strip()}",), daemon=True).start()

# --- ФОНОВЫЕ ЗАДАЧИ ---
def bg_task_ai(interface, sender_id, user_full_name, to_id, channel_id, ai_prompt):
    is_broadcast = (to_id == '^all')
    try:
        response = requests.post(N8N_AI_URL, json={"from": sender_id, "user_name": user_full_name, "prompt": ai_prompt}, timeout=30)
        if response.status_code == 200 and response.text.strip():
            send_smart_message(interface, response.text.strip(), is_broadcast, dest_id=sender_id, channel_id=channel_id, user_full_name=user_full_name)
    except: pass

def bg_task_meteo(interface, sender_id, user_full_name, s_lat, s_lon, to_id, channel_id):
    is_broadcast = (to_id == '^all')
    if not s_lat or not s_lon:
        try:
            my_node = interface.nodes.get(interface.getMyUser().get('id'), {})
            s_lat, s_lon = my_node.get('position', {}).get('latitude'), my_node.get('position', {}).get('longitude')
        except: pass
    if not s_lat or not s_lon: s_lat, s_lon = 53.41, 59.00

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(N8N_METEO_URL, json={"from": sender_id, "lat": s_lat, "lon": s_lon}, timeout=15)
            if response.status_code == 200 and response.text.strip():
                send_smart_message(interface, response.text.strip(), is_broadcast, dest_id=sender_id, channel_id=channel_id, user_full_name=user_full_name)
                return 
        except: pass
        if attempt < max_retries: time.sleep(3)
    send_smart_message(interface, "❌ Ошибка: Сервер погоды не отвечает", is_broadcast, dest_id=sender_id, channel_id=channel_id, user_full_name=user_full_name)

# --- ОСНОВНОЙ ПРИЕМНИК ---
def on_receive(packet, interface):
    packet_id = packet.get('id')
    if packet_id:
        if packet_id in processed_packets: return
        processed_packets.append(packet_id)
        if len(processed_packets) > 50: processed_packets.pop(0)
            
    if 'decoded' in packet and packet['decoded'].get('portnum') == 'TEXT_MESSAGE_APP':
        raw_text = packet['decoded'].get('text', '').strip()
        lower_text = raw_text.lower()
        sender_id = packet.get('fromId')
        to_id = packet.get('toId')
        channel_id = packet.get('channel', 0)

        global recent_text_senders
        if sender_id in recent_text_senders:
            recent_text_senders.remove(sender_id)
        recent_text_senders.insert(0, sender_id)
        if len(recent_text_senders) > 5:
            recent_text_senders.pop()

        node_data = interface.nodes.get(sender_id, {})
        user_full_name = node_data.get('user', {}).get('longName', sender_id)
        s_lat = node_data.get('position', {}).get('latitude')
        s_lon = node_data.get('position', {}).get('longitude')
        rssi = packet.get('rxRssi', 'N/A')
        snr = packet.get('rxSnr', 'N/A')

        is_broadcast = (to_id == '^all')
        if is_broadcast:
            try:
                ch_name = interface.localNode.channels[channel_id].settings.name
                channel_name = ch_name if ch_name else ("LongFast" if channel_id == 0 else f"Канал {channel_id}")
            except: channel_name = f"Канал {channel_id}"
            target_info = f"Группа: {channel_name}"
        else: target_info = "🔒 Личное сообщение"

        send_to_tg_bg(f"📩 От: {user_full_name} ({sender_id})\n{target_info} | RSSI: {rssi}\nТекст: {raw_text}")

        if lower_text.startswith('@ии'):
            ai_prompt = raw_text[3:].strip()
            if ai_prompt: threading.Thread(target=bg_task_ai, args=(interface, sender_id, user_full_name, to_id, channel_id, ai_prompt), daemon=True).start()

        elif lower_text in ['info', '/info', 'help', '/help', 'инфо', '/инфо']:
            send_smart_message(interface, "🤖 Шлюз активен.\nКоманды:\nпинг - тест связи и хопов(не в LongFast)\nметео - погода", is_broadcast, dest_id=sender_id, channel_id=channel_id, user_full_name=user_full_name)

        elif lower_text.startswith('метео'):
            threading.Thread(target=bg_task_meteo, args=(interface, sender_id, user_full_name, s_lat, s_lon, to_id, channel_id), daemon=True).start()

        elif lower_text in ['ping', 'пинг']:
            if is_broadcast and channel_id == 0: return
            hop_limit = packet.get('hopLimit', 3)
            hop_start = packet.get('hopStart', 3 if hop_limit <= 3 else 7)
            hops_taken = max(0, hop_start - hop_limit)
            try:
                my_node = interface.nodes.get(interface.getMyUser().get('id'), {})
                m_lat, m_lon = my_node.get('position', {}).get('latitude'), my_node.get('position', {}).get('longitude')
            except: m_lat, m_lon = None, None

            dist = calc_distance(s_lat, s_lon, m_lat, m_lon)
            dist_str = f"{dist/1000:.1f} км" if dist and dist >= 1000 else f"{int(dist)} м" if dist else "неизвестно"
            signal_text = f"Хопы: {hops_taken}" if hops_taken > 0 else f"Сигнал: {rssi} dBm\nSNR: {snr} dB"
            send_smart_message(interface, f"Понг:\nДистанция: {dist_str}\n{signal_text}", is_broadcast, dest_id=sender_id, channel_id=channel_id, user_full_name=user_full_name)

def main():
    global mesh_interface
    print("🟢 Служба бота-моста запущена.")
    
    threading.Thread(target=run_tg_polling, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start() 
    threading.Thread(target=daily_reporter, daemon=True).start() 
    
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")
    
    while True:
        try:
            print(f"⏳ Пытаюсь подключиться к {HELTEC_IP}...")
            mesh_interface = meshtastic.tcp_interface.TCPInterface(hostname=HELTEC_IP)
            
            try:
                sock = mesh_interface.socket
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)  
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)  
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 2)    
            except: pass

            print("✅ Подключено! Жду команды...")
            
            while True:
                time.sleep(10) 
                
                if mesh_interface and hasattr(mesh_interface, '_rxThread'):
                    if not getattr(mesh_interface._rxThread, 'is_alive', lambda: True)():
                        print("❌ Внутренний поток библиотеки завис! Жесткий рестарт...")
                        add_fail()
                        time.sleep(2)
                        os._exit(1)
                
                if mesh_interface and hasattr(mesh_interface, 'socket') and mesh_interface.socket:
                    if mesh_interface.socket.fileno() == -1:
                        print("❌ ОС закрыла сокет. Рестарт...")
                        add_fail() 
                        time.sleep(2)
                        os._exit(1)
                        
        except Exception as e:
            print(f"❌ Сбой: {e}")
            time.sleep(2)
            
        finally:
            if mesh_interface:
                try: mesh_interface.close()
                except: pass
            mesh_interface = None
        time.sleep(5)

if __name__ == "__main__":
    main()
