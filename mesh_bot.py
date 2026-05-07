import os
import time
import requests
import math
import threading
import re
import socket
import datetime
import telebot
import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

# --- ВАШИ НАСТРОЙКИ ---
HELTEC_IP = "192.168.1...."
N8N_METEO_URL = "https://адрес n8n/webhook/meteo"
N8N_AI_URL = "https://адрес n8n/webhook/ai"

# --- TELEGRAM НАСТРОЙКИ ---
TG_TOKEN = "123456789:123456783456789dfghjk"
TG_ADMIN_ID = 1234567890

bot = telebot.TeleBot(TG_TOKEN)
mesh_interface = None
processed_packets = []

# ==========================================
# 💾 СИСТЕМА УЧЕТА СБОЕВ (сохраняет между рестартами)
# ==========================================
STATS_FILE = "bot_fails.txt"

def get_fails():
    try:
        with open(STATS_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return 0

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
    """Каждое утро в 07:00 отправляет отчет и сбрасывает счетчик"""
    while True:
        now = datetime.datetime.now()
        # Проверяем, наступило ли 7:00 утра
        if now.hour == 7 and now.minute == 0:
            fails = get_fails()
            if fails == 0:
                msg = "🌅 Доброе утро!\n\n✅ Сбоев за прошедшие сутки: 0\n🚀 Бот-мост работает стабильно и ждет команд."
            else:
                msg = f"🌅 Доброе утро!\n\n⚠️ Зафиксировано авто-перезапусков за сутки: {fails}\n✅ Сейчас система в сети и работает в штатном режиме."
            
            send_to_tg_bg(msg)
            reset_fails()
            time.sleep(60) # Спим 1 минуту, чтобы не отправить два отчета в 07:00
            
        time.sleep(20) # Проверяем время каждые 20 секунд

# ==========================================
# 🚀 ФОНОВАЯ ОТПРАВКА В TELEGRAM
# ==========================================
def send_to_tg_bg(text):
    """Скрытно отправляет системные сообщения администратору"""
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
        if response == 0:
            fails = 0
        else:
            fails += 1
            print(f"⚠️ Потерян пинг ({fails}/3)")
            
        if fails >= 3:
            print("❌ Heltec пропал с радаров (перезагрузка). Жесткий рестарт!")
            send_to_tg_bg("🚨 ОШИБКА: Heltec пропал из Wi-Fi сети. Пытаюсь переподключиться...")
            add_fail() # Записываем сбой перед смертью
            time.sleep(2)
            os._exit(1)

def on_connection_lost(interface=None):
    print("❌ Библиотека Meshtastic потеряла связь с рацией (Errno 104)!")
    add_fail() # Записываем сбой перед смертью
    time.sleep(2)
    os._exit(1)

# ==========================================
# 📱 TELEGRAM: ПРИЕМ СООБЩЕНИЙ
# ==========================================
@bot.message_handler(func=lambda message: True)
def handle_tg_messages(message):
    global mesh_interface
    
    if message.from_user.id != TG_ADMIN_ID: return
    if not mesh_interface:
        bot.reply_to(message, "❌ Ошибка: Радиомодуль сейчас оффлайн.")
        return

    text = message.text
    target = '^all'
    channel = 0

    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text
        match_id = re.search(r'\((![a-f0-9]+)\)', reply_text, re.IGNORECASE)
        if match_id: target = match_id.group(1)
        if target == '^all':
            try:
                for ch in mesh_interface.localNode.channels:
                    ch_name = ch.settings.name
                    if ch_name and ch_name.lower() in reply_text.lower():
                        channel = ch.index
                        break
            except: pass

    elif text.startswith('/c'):
        parts = text.split(' ', 1)
        if len(parts) > 1 and parts[0][2:].isdigit():
            channel = int(parts[0][2:])
            text = parts[1].strip()

    is_broadcast = (target == '^all')
    send_smart_message(mesh_interface, text, is_broadcast, dest_id=target, channel_id=channel, user_full_name="TG Admin")

def run_tg_polling():
    try: bot.remove_webhook()
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
    
    # Запускаем фоновые процессы
    threading.Thread(target=run_tg_polling, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start() 
    threading.Thread(target=daily_reporter, daemon=True).start() # Запускаем утренний таймер
    
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
                        add_fail() # Записываем сбой
                        time.sleep(2)
                        os._exit(1)
                
                if mesh_interface and hasattr(mesh_interface, 'socket') and mesh_interface.socket:
                    if mesh_interface.socket.fileno() == -1:
                        print("❌ ОС закрыла сокет. Рестарт...")
                        add_fail() # Записываем сбой
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
