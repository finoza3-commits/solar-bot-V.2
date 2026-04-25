"""
Solar Monitor Bot — บอทเฝ้าระวังระบบโซลาร์เซลล์
ดึงข้อมูลจาก Deye Cloud API และส่งรายงานผ่าน Telegram
"""

import requests
import time
import logging
import json
import os
import threading
from datetime import datetime

import pytz
from flask import Flask

from config import (
    COST_PER_UNIT, SUN_THRESHOLD, OVERLOAD_LIMIT, TIMEZONE,
    STATUS_FILE, TELEGRAM_RETRIES, API_TIMEOUT, TELEGRAM_TIMEOUT,
    DEYE_API_URL, SOLAR_POWER_KEY, SOLAR_DAILY_KEY, HOME_POWER_KEY,
    HOME_DAILY_KEYS, GRID_DAILY_KEYS, GRID_POWER_KEYS,
    VOLTAGE_KEYS, LOW_VOLTAGE_LIMIT,
)

# ==========================================
# Logging
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ==========================================
# Environment Variables (ข้อมูลลับ)
# ==========================================

def _require_env(name: str) -> str:
    """อ่าน environment variable — หยุดทำงานถ้าไม่มี"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"ไม่พบ {name} — กรุณาตั้งค่าใน GitHub Secrets "
            f"(Settings → Secrets → Actions)"
        )
    return value


def get_credentials() -> dict:
    """โหลดข้อมูลลับจาก Environment Variables (GitHub Secrets)"""
    return {
        "deye_token": _require_env("DEYE_TOKEN"),
        "inverter_sn": _require_env("INVERTER_SN"),
        "telegram_bot_token": _require_env("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": _require_env("TELEGRAM_CHAT_ID"),
    }


# ==========================================
# ระบบความจำ (State Management)
# ==========================================

DEFAULT_STATE = {
    "last_solar_kwh": 0.0,
    "last_grid_kwh": 0.0,
    "is_overloaded": False,
    "last_hourly_run": -1,
    "sun_status": None,
    "is_offline": False,
    "last_update_id": 0,
    "monthly_solar_kwh": 0.0,
    "monthly_grid_kwh": 0.0,
    "monthly_solar_money": 0.0,
    "monthly_grid_money": 0.0,
    "current_month": "",
    "daily_hourly_history": [],
    "total_consumption_baseline": 0.0,
    "baseline_date": "",
}


def load_state() -> dict:
    """โหลดสถานะจากไฟล์ หรือใช้ค่าเริ่มต้นถ้าไม่มี/เสียหาย"""
    state = DEFAULT_STATE.copy()
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                state.update(saved)
            logger.info("โหลดสถานะจาก %s สำเร็จ", STATUS_FILE)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("status.json เสียหาย ใช้ค่าเริ่มต้น: %s", e)
        except FileNotFoundError:
            pass
    return state


def save_state(state: dict) -> None:
    """บันทึกสถานะลงไฟล์"""
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info("บันทึกสถานะลง %s สำเร็จ", STATUS_FILE)
    except OSError as e:
        logger.error("บันทึกสถานะล้มเหลว: %s", e)


# ==========================================
# Telegram
# ==========================================

def send_telegram(message: str, creds: dict) -> bool:
    """ส่งข้อความผ่าน Telegram พร้อม retry"""
    url = f"https://api.telegram.org/bot{creds['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": creds["telegram_chat_id"],
        "text": message,
        "parse_mode": "Markdown",
    }

    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
            if resp.status_code == 200:
                logger.info("ส่ง Telegram สำเร็จ")
                return True
            logger.warning(
                "Telegram error (attempt %d/%d) status=%d: %s",
                attempt, TELEGRAM_RETRIES, resp.status_code, resp.text,
            )
        except requests.RequestException as e:
            logger.warning(
                "Telegram request failed (attempt %d/%d): %s",
                attempt, TELEGRAM_RETRIES, e,
            )
        if attempt < TELEGRAM_RETRIES:
            time.sleep(2)

    logger.error("ส่ง Telegram ล้มเหลวหลัง %d ครั้ง", TELEGRAM_RETRIES)
    return False


# ==========================================
# Telegram Chat Commands (คุยเช็คสถานะ + คุยถาม)
# ==========================================

def get_telegram_updates(creds: dict, last_update_id: int) -> list:
    """ดึงข้อความใหม่จาก Telegram (getUpdates)"""
    url = f"https://api.telegram.org/bot{creds['telegram_bot_token']}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": 0,
        "allowed_updates": '["message"]',
    }
    try:
        resp = requests.get(url, params=params, timeout=TELEGRAM_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data.get("result", [])
        logger.warning("getUpdates failed: status=%d", resp.status_code)
    except requests.RequestException as e:
        logger.warning("getUpdates error: %s", e)
    return []


def get_weather() -> str:
    """ดึงข้อมูลสภาพอากาศ (อุณหภูมิภายนอก) จาก Open-Meteo (พิกัดเริ่มต้น: กรุงเทพฯ)"""
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=13.754&longitude=100.5014&current=temperature_2m,weather_code&timezone=Asia%2FBangkok"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            temp = data["current"]["temperature_2m"]
            code = data["current"]["weather_code"]
            
            weather_map = {
                0: "☀️", 1: "🌤", 2: "⛅️", 3: "☁️",
                45: "🌫", 48: "🌫",
                51: "🌧", 53: "🌧", 55: "🌧",
                61: "🌧", 63: "🌧", 65: "🌧",
                80: "🌦", 95: "⛈"
            }
            desc = weather_map.get(code, "🌡")
            return f"{desc} {temp}°C"
    except Exception:
        pass
    return ""


def _build_status_reply(readings: dict | None, current_time: str) -> str:
    """สร้างข้อความตอบกลับสถานะปัจจุบัน"""
    if readings is None:
        return "❌ ระบบ Offline หรือเชื่อมต่อ Deye API ไม่ได้"

    solar_pwr = readings["solar_pwr"]
    solar_daily_kwh = readings["solar_daily_kwh"]
    home_pwr = readings["home_pwr"]
    home_daily_kwh = readings.get("home_daily_kwh", 0.0)
    grid_pwr = readings["grid_pwr"]
    grid_daily_kwh = readings["grid_daily_kwh"]

    solar_status = "🟢 ตอนนี้โซลาร์กำลังผลิตไฟฟ้า" if solar_pwr > SUN_THRESHOLD else "🔴 ตอนนี้โซลาร์ไม่ได้ผลิตไฟฟ้า"

    # คำนวณเงิน
    saved_money = solar_daily_kwh * COST_PER_UNIT
    home_cost = home_daily_kwh * COST_PER_UNIT
    grid_cost = grid_daily_kwh * COST_PER_UNIT
    est_grid_cost = max(0, home_cost - saved_money)

    weather_str = readings.get("current_weather", "")
    weather_line = f"🌡 สภาพอากาศ: {weather_str}\n" if weather_str else ""

    return (
        f"📊 *[ สถานะระบบ Real-time ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"{weather_line}"
        f"----------\n"
        f"☀️ โซลาร์: {solar_pwr:,.0f} W   |   ⚡️ ไฟฟ้า: {grid_pwr:,.0f} W\n"
        f"🏠 บ้านใช้ไฟฟ้า : {home_pwr:,.0f} W\n"
        f"🔋 แรงดันไฟฟ้า: {readings.get('voltage', 0):,.1f} V | ความถี่: {readings.get('ac_frequency', 0):.2f} Hz\n"
        f"{solar_status}\n"
        f"----------\n"
        f"📈 *ยอดสะสมวันนี้*\n"
        f"☀️ ผลิตได้: {solar_daily_kwh:,.2f} kWh (ประหยัด {saved_money:,.2f} ฿)\n"
        f"⚡️ ซื้อไฟฟ้า: {grid_daily_kwh:,.2f} kWh (เสียค่าไฟ {grid_cost:,.2f} ฿)\n"
        f"🏠 บ้านใช้รวม: {home_daily_kwh:,.2f} kWh (ตีเป็นเงิน {home_cost:,.2f} ฿)\n"
        f"💸 ประเมินค่าไฟที่ต้องจ่าย: {est_grid_cost:,.2f} ฿\n"
        f"----------\n"
        f"📊 *สถิติสะสมตลอดอายุ*\n"
        f"☀️ ผลิตรวม: {readings.get('total_production', 0):,.2f} kWh\n"
        f"🏠 ใช้ไฟรวม: {readings.get('total_consumption', 0):,.2f} kWh"
    )


def _build_cost_reply(readings: dict | None, current_time: str) -> str:
    """สร้างข้อความตอบกลับเรื่องค่าไฟ"""
    if readings is None:
        return "❌ ไม่สามารถคำนวณค่าไฟได้ — ระบบ Offline"

    solar_daily_kwh = readings["solar_daily_kwh"]
    grid_daily_kwh = readings["grid_daily_kwh"]
    home_daily_kwh = readings.get("home_daily_kwh", 0.0)
    
    solar_money = solar_daily_kwh * COST_PER_UNIT
    grid_money = grid_daily_kwh * COST_PER_UNIT
    home_money = home_daily_kwh * COST_PER_UNIT
    
    net = solar_money - grid_money

    net_text = (
        f"✅ ประหยัดสุทธิ {net:,.2f} ฿"
        if net >= 0
        else f"❌ จ่ายค่าไฟเพิ่ม {abs(net):,.2f} ฿"
    )

    return (
        f"💰 *[ สรุปค่าไฟวันนี้ ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"☀️ จากโซลาร์: {solar_daily_kwh:,.2f} kWh\n"
        f"💡 ประหยัดเงิน: *{solar_money:,.2f} บาท*\n"
        f"----------\n"
        f"⚡️ ซื้อไฟฟ้า: {grid_daily_kwh:,.2f} kWh\n"
        f"💸 เสียค่าไฟ: *{grid_money:,.2f} บาท*\n"
        f"----------\n"
        f"🏠 บ้านใช้ไฟฟ้า: {home_daily_kwh:,.2f} kWh\n"
        f"📊 ถ้าไม่มีโซลาร์ต้องจ่าย: *{home_money:,.2f} บาท*\n"
        f"----------\n"
        f"💵 *สรุปส่วนต่าง: {net_text}*\n"
        f"📌 (คิดที่หน่วยละ {COST_PER_UNIT} บาท)"
    )


def _build_solar_reply(readings: dict | None, current_time: str) -> str:
    """สร้างข้อความตอบกลับเรื่องการผลิตไฟโซลาร์"""
    if readings is None:
        return "❌ ไม่สามารถดึงข้อมูลโซลาร์ได้ — ระบบ Offline"

    solar_pwr = readings["solar_pwr"]
    solar_daily_kwh = readings["solar_daily_kwh"]
    home_pwr = readings["home_pwr"]
    grid_pwr = readings["grid_pwr"]

    if solar_pwr > SUN_THRESHOLD:
        solar_pct = (solar_pwr / home_pwr * 100) if home_pwr > 0 else 0
        status = f"🟢 กำลังผลิตไฟ ({solar_pct:.0f}% ของโหลด)"
    else:
        status = "🔴 ไม่ผลิตไฟ (แดดหมด/กลางคืน)"

    pv1 = readings.get('dc_voltage_pv1', 0)
    pv2 = readings.get('dc_voltage_pv2', 0)

    return (
        f"☀️ *[ ข้อมูลโซลาร์เซลล์ ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"📡 สถานะ: {status}\n"
        f"⚡️ กำลังผลิต AC: *{solar_pwr:,.0f} W*\n"
        f"📊 ผลิตสะสมวันนี้: *{solar_daily_kwh:,.2f} kWh* (ประหยัด {solar_daily_kwh * COST_PER_UNIT:,.2f} ฿)\n"
        f"----------\n"
        f"🔗 *แผงโซลาร์ String 1 (PV1)*\n"
        f"• แรงดัน: {pv1:.1f} V | กระแส: {readings.get('dc_current_pv1', 0):.2f} A\n"
        f"• กำลังผลิต: {readings.get('dc_power_pv1', 0):,.0f} W\n"
        f"🔗 *แผงโซลาร์ String 2 (PV2)*\n"
        f"• แรงดัน: {pv2:.1f} V | กระแส: {readings.get('dc_current_pv2', 0):.2f} A\n"
        f"• กำลังผลิต: {readings.get('dc_power_pv2', 0):,.0f} W\n"
        f"----------\n"
        f"🏆 *ผลิตไฟสะสมตลอดอายุ: {readings.get('total_production', 0):,.2f} kWh*\n"
        f"----------\n"
        f"☀️ จากโซลาร์: {solar_pwr:,.0f} W\n"
        f"🔌 ดึงไฟฟ้า: {grid_pwr:,.0f} W\n"
        f"🏠 บ้านใช้: {home_pwr:,.0f} W"
    )


def _build_grid_reply(readings: dict | None, current_time: str) -> str:
    """สร้างข้อความตอบกลับเรื่องการดึงไฟจากการไฟฟ้า (กริด)"""
    if readings is None:
        return "❌ ไม่สามารถดึงข้อมูลกริดได้ — ระบบ Offline"

    grid_pwr = readings["grid_pwr"]
    grid_daily_kwh = readings["grid_daily_kwh"]
    home_pwr = readings["home_pwr"]
    solar_pwr = readings["solar_pwr"]

    if grid_pwr > 0:
        grid_pct = (grid_pwr / home_pwr * 100) if home_pwr > 0 else 0
        status = f"🔴 กำลังดึงไฟฟ้า ({grid_pct:.0f}% ของโหลด)"
    elif grid_pwr < 0:
        status = "🔄 จ่ายไฟย้อนเข้าสายส่ง (Export)"
    else:
        status = "🟢 ไม่ได้ดึงไฟฟ้า (ใช้โซลาร์ล้วน/แบตเตอรี่)"

    display_grid_pwr = abs(grid_pwr)

    return (
        f"⚡️ *[ ข้อมูลไฟฟ้า ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"📡 สถานะ: {status}\n"
        f"🔌 กำลังดึงไฟ: *{display_grid_pwr:,.0f} W*\n"
        f"📊 ซื้อไฟสะสมวันนี้: *{grid_daily_kwh:,.2f} kWh* (เสียค่าไฟ {grid_daily_kwh * COST_PER_UNIT:,.2f} ฿)\n"
        f"----------\n"
        f"🔋 แรงดันไฟฟ้า: *{readings.get('voltage', 0):,.1f} V*\n"
        f"⚡️ ความถี่: {readings.get('ac_frequency', 0):.2f} Hz\n"
        f"🔌 กระแส AC: {readings.get('ac_current', 0):.2f} A\n"
        f"----------\n"
        f"🏆 *สถิติสะสมตลอดอายุ*\n"
        f"⚡️ ซื้อไฟรวม: {readings.get('total_energy_purchased', 0):,.2f} kWh\n"
        f"🔄 ส่งขายกริดรวม: {readings.get('total_grid_feed_in', 0):,.2f} kWh\n"
        f"----------\n"
        f"☀️ จากโซลาร์: {solar_pwr:,.0f} W\n"
        f"🏠 บ้านใช้: {home_pwr:,.0f} W"
    )


def _build_help_reply() -> str:
    """สร้างข้อความแสดงคำสั่งที่ใช้ได้"""
    return (
        f"📖 *[ คำสั่งที่ใช้ได้ ]*\n"
        f"----------\n"
        f"📊 `/status` หรือ *สถานะ*\n"
        f"→ เช็คสถานะระบบ Real-time\n\n"
        f"💰 `/cost` หรือ *ค่าไฟ*\n"
        f"→ สรุปค่าไฟและเงินประหยัดวันนี้\n\n"
        f"📅 `/month` หรือ *เดือน*\n"
        f"→ สรุปยอดสะสมรวมของเดือนนี้\n\n"
        f"☀️ `/solar` หรือ *โซลาร์*\n"
        f"→ ข้อมูลการผลิตไฟจากโซลาร์\n\n"
        f"⚡️ `/grid` หรือ *ไฟฟ้า*\n"
        f"→ ข้อมูลการดึงไฟจากการไฟฟ้า\n\n"
        f"❓ `/help` หรือ *ช่วย*\n"
        f"→ แสดงรายการคำสั่งนี้\n"
        f"----------\n"
        f"💡 _บอทตอบกลับทุก 5 นาที (ตาม GitHub Actions cron)_"
    )


def _build_unknown_reply(text: str) -> str:
    """สร้างข้อความตอบกลับเมื่อไม่เข้าใจคำสั่ง"""
    return (
        f"🤔 ไม่เข้าใจคำสั่ง: `{text[:50]}`\n\n"
        f"พิมพ์ `/help` หรือ *ช่วย* เพื่อดูคำสั่งที่ใช้ได้"
    )


def _build_month_reply(state: dict, current_time: str) -> str:
    """สร้างข้อความสรุปยอดรายเดือน"""
    month = state.get("current_month", "-")
    if not month:
        return "⏳ ยังไม่มีข้อมูลสะสมรายเดือน (ระบบจะเริ่มเก็บข้อมูลคืนนี้ตอนเที่ยงคืน)"
        
    solar_kwh = state.get("monthly_solar_kwh", 0.0)
    grid_kwh = state.get("monthly_grid_kwh", 0.0)
    solar_money = state.get("monthly_solar_money", 0.0)
    grid_money = state.get("monthly_grid_money", 0.0)
    net = solar_money - grid_money
    
    net_text = (
        f"✅ ประหยัดสุทธิ {net:,.2f} ฿"
        if net >= 0
        else f"❌ จ่ายค่าไฟเพิ่ม {abs(net):,.2f} ฿"
    )

    return (
        f"📅 *[ สรุปยอดประจำเดือน: {month} ]*\n"
        f"⏰ อัปเดตล่าสุด: {current_time}\n"
        f"----------\n"
        f"☀️ *พลังงานจากโซลาร์*\n"
        f"• ผลิตสะสม: {solar_kwh:,.2f} kWh\n"
        f"• 💡 ประหยัดเงิน: {solar_money:,.2f} บาท\n"
        f"----------\n"
        f"⚡️ *พลังงานจากไฟฟ้า*\n"
        f"• ซื้อสะสม: {grid_kwh:,.2f} kWh\n"
        f"• 💸 เสียค่าไฟ: {grid_money:,.2f} บาท\n"
        f"----------\n"
        f"💵 *สรุป: {net_text}*\n"
        f"📌 คิดที่หน่วยละ {COST_PER_UNIT} บาท"
    )


def process_chat_commands(state: dict, creds: dict,
                          readings: dict | None, current_time: str) -> None:
    """ดึงข้อความจาก Telegram แล้วตอบกลับตามคำสั่ง"""
    last_update_id = state.get("last_update_id", 0)
    updates = get_telegram_updates(creds, last_update_id)

    if not updates:
        return

    logger.info("พบข้อความใหม่ %d รายการ", len(updates))

    for update in updates:
        update_id = update.get("update_id", 0)
        message = update.get("message", {})
        text = message.get("text", "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        # อัพเดท last_update_id
        if update_id > last_update_id:
            last_update_id = update_id

        # ตอบเฉพาะ chat ที่ตั้งค่าไว้
        if chat_id != creds["telegram_chat_id"]:
            logger.info("ข้ามข้อความจาก chat_id: %s", chat_id)
            continue

        # ไม่มีข้อความ
        if not text:
            continue

        # จัดการกรณีคำสั่งมี @botname ต่อท้าย เช่น /status@solar_bot
        if text.startswith('/'):
            cmd = text.split()[0].split('@')[0]
        else:
            cmd = text

        # จับคู่คำสั่ง
        if cmd in ("/status", "สถานะ", "เช็ค", "เช็คสถานะ", "status"):
            reply = _build_status_reply(readings, current_time)
        elif cmd in ("/cost", "ค่าไฟ", "เงิน", "ประหยัด", "cost"):
            reply = _build_cost_reply(readings, current_time)
        elif cmd in ("/month", "เดือน", "รายเดือน", "month"):
            reply = _build_month_reply(state, current_time)
        elif cmd in ("/solar", "โซลาร์", "แผง", "ผลิต", "solar"):
            reply = _build_solar_reply(readings, current_time)
        elif cmd in ("/grid", "กริด", "ไฟหลวง", "การไฟฟ้า", "grid"):
            reply = _build_grid_reply(readings, current_time)
        elif cmd in ("/help", "ช่วย", "help", "คำสั่ง", "/start", "เมนู"):
            reply = _build_help_reply()
        else:
            reply = _build_unknown_reply(text)

        send_telegram(reply, creds)

    # บันทึก last_update_id
    state["last_update_id"] = last_update_id


# ==========================================
# Deye API
# ==========================================

def _safe_float(value) -> float:
    """แปลงค่าเป็น float อย่างปลอดภัย"""
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def fetch_inverter_data(creds: dict) -> dict | None:
    """
    ดึงข้อมูลจาก Deye Cloud API
    คืนค่า dict ของ power readings หรือ None ถ้าล้มเหลว
    """
    headers = {
        "authorization": creds["deye_token"],
        "Content-Type": "application/json",
    }
    payload = {"deviceList": [creds["inverter_sn"]]}

    response = requests.post(
        DEYE_API_URL, headers=headers, json=payload, timeout=API_TIMEOUT
    )

    if response.status_code != 200:
        logger.error("Deye API status %d", response.status_code)
        return None

    data = response.json()
    device_list = data.get("deviceDataList")
    if not device_list:
        logger.error("ไม่พบข้อมูลอุปกรณ์จาก Deye API")
        return None

    device_data = device_list[0].get("dataList", [])
    return _parse_device_data(device_data)


def _parse_device_data(device_data: list) -> dict:
    """แปลงข้อมูลดิบจาก Deye เป็น dict ที่ใช้งานง่าย"""
    # === DEBUG: แสดง Key ทั้งหมดจาก API เพื่อหา Voltage Key ===
    all_keys = []
    for item in device_data:
        k = item.get("key", "")
        v = item.get("value", "")
        u = item.get("unit", "")
        all_keys.append(f"{k}={v} {u}")
    logger.info("=== Deye API Keys ทั้งหมด ===\n%s", "\n".join(all_keys))
    # === จบ DEBUG ===

    solar_pwr = 0.0
    solar_daily_kwh = 0.0
    home_pwr = 0.0
    home_daily_kwh = 0.0
    grid_daily_kwh = 0.0
    grid_pwr = 0.0
    voltage = 0.0
    dc_voltage_pv1 = 0.0
    dc_voltage_pv2 = 0.0
    ac_frequency = 0.0
    ac_current = 0.0
    dc_current_pv1 = 0.0
    dc_current_pv2 = 0.0
    dc_power_pv1 = 0.0
    dc_power_pv2 = 0.0
    total_production = 0.0
    total_consumption = 0.0
    total_grid_feed_in = 0.0
    total_energy_purchased = 0.0

    # Keys ที่ต้องตรวจ auto-scale (อาจเป็น kW หรือ W)
    auto_scale_keys = {
        SOLAR_POWER_KEY, HOME_POWER_KEY,
        *GRID_POWER_KEYS,
    }

    for item in device_data:
        key = item.get("key")
        val = _safe_float(item.get("value"))
        unit = str(item.get("unit") or "").strip().lower()

        # แปลง kW → W
        if unit == "kw":
            val *= 1000

        # Auto-scale: ค่าระหว่าง 0-20 น่าจะเป็น kW ไม่ใช่ W
        if key in auto_scale_keys and 0 < val < 20:
            val *= 1000

        if key == SOLAR_POWER_KEY:
            solar_pwr = val
        elif key == SOLAR_DAILY_KEY:
            solar_daily_kwh = val
        elif key == HOME_POWER_KEY:
            home_pwr = val
        elif key in HOME_DAILY_KEYS:
            home_daily_kwh = val
        elif key in GRID_DAILY_KEYS:
            pass # We will calculate this manually instead of reading from API
        elif key in GRID_POWER_KEYS:
            grid_pwr = val
        elif key in VOLTAGE_KEYS and voltage == 0.0:
            voltage = _safe_float(item.get("value"))
        elif key == "DCVoltagePV1":
            dc_voltage_pv1 = _safe_float(item.get("value"))
        elif key == "DCVoltagePV2":
            dc_voltage_pv2 = _safe_float(item.get("value"))
        elif key == "ACOutputFrequencyR":
            ac_frequency = _safe_float(item.get("value"))
        elif key == "ACCurrentRUA":
            ac_current = _safe_float(item.get("value"))
        elif key == "DCCurrentPV1":
            dc_current_pv1 = _safe_float(item.get("value"))
        elif key == "DCCurrentPV2":
            dc_current_pv2 = _safe_float(item.get("value"))
        elif key == "DCPowerPV1":
            dc_power_pv1 = _safe_float(item.get("value"))
        elif key == "DCPowerPV2":
            dc_power_pv2 = _safe_float(item.get("value"))
        elif key == "TotalActiveProduction":
            total_production = _safe_float(item.get("value"))
        elif key == "TotalConsumption":
            total_consumption = _safe_float(item.get("value"))
        elif key == "TotalGridFeedIn":
            total_grid_feed_in = _safe_float(item.get("value"))
        elif key == "TotalEnergyPurchased":
            total_energy_purchased = _safe_float(item.get("value"))

    # Fallback: คำนวณ grid_pwr จาก home - solar (ถ้าอ่านไม่ได้)
    if grid_pwr == 0.0:
        grid_pwr = home_pwr - solar_pwr

    # คำนวณ grid_daily_kwh จาก ส่วนต่าง (Home - Solar) ตามที่ผู้ใช้ต้องการ
    if home_daily_kwh > 0:
        grid_daily_kwh = max(0.0, home_daily_kwh - solar_daily_kwh)
    else:
        # ถ้าหาคีย์ Home Daily ไม่เจอ ให้ลองใช้ค่าจาก Grid Purchased เผื่อไว้
        for item in device_data:
            if item.get("key") in GRID_DAILY_KEYS:
                grid_daily_kwh = _safe_float(item.get("value"))
                break

    return {
        "solar_pwr": solar_pwr,
        "solar_daily_kwh": solar_daily_kwh,
        "home_pwr": home_pwr,
        "home_daily_kwh": home_daily_kwh,
        "grid_daily_kwh": grid_daily_kwh,
        "grid_pwr": grid_pwr,
        "voltage": voltage,
        "dc_voltage_pv1": dc_voltage_pv1,
        "dc_voltage_pv2": dc_voltage_pv2,
        "ac_frequency": ac_frequency,
        "ac_current": ac_current,
        "dc_current_pv1": dc_current_pv1,
        "dc_current_pv2": dc_current_pv2,
        "dc_power_pv1": dc_power_pv1,
        "dc_power_pv2": dc_power_pv2,
        "total_production": total_production,
        "total_consumption": total_consumption,
        "total_grid_feed_in": total_grid_feed_in,
        "total_energy_purchased": total_energy_purchased,
    }


# ==========================================
# การแจ้งเตือน (Alerts)
# ==========================================

def check_online_status(is_online: bool, state: dict, creds: dict, current_time: str) -> None:
    """ตรวจสอบสถานะ online/offline แล้วแจ้งเตือน"""
    if not is_online:
        if not state["is_offline"]:
            send_telegram(
                f"❌ *[ แจ้งเตือน: ระบบ Offline! ]*\n"
                f"ติดต่ออินเวอร์เตอร์ไม่ได้ ⚡️ อาจเกิดจากไฟตก หรือเน็ตหลุด\n"
                f"⏰ เวลา: {current_time}",
                creds,
            )
            state["is_offline"] = True
    else:
        if state["is_offline"]:
            send_telegram(
                f"✅ *[ แจ้งเตือน: ระบบกลับมา Online แล้ว ]*\n"
                f"อินเวอร์เตอร์เชื่อมต่อสำเร็จและเริ่มทำงานต่อแล้ว",
                creds,
            )
            state["is_offline"] = False


def check_startup(is_startup: bool, creds: dict) -> None:
    """ส่งข้อความตอนระบบเริ่มทำงานครั้งแรก"""
    if is_startup:
        send_telegram(
            f"🚀 *[ ระบบออนไลน์พร้อมทำงาน! ]*\n"
            f"บอทผู้ช่วยโซลาร์เซลล์เชื่อมต่อสำเร็จแล้ว ✅\n"
            f"----------\n"
            f"📡 เฝ้าระวังไฟตก เน็ตหลุด และโหลดเกินให้คุณตลอด 24 ชม.\n"
            f"⚠️ แจ้งเตือนทันทีเมื่อดึงไฟฟ้าเกิน: {OVERLOAD_LIMIT:,.0f} W",
            creds,
        )


def check_sun_status(solar_pwr: float, state: dict, creds: dict) -> None:
    """ตรวจสอบสถานะแดดออก/หมด แล้วแจ้งเตือนเมื่อเปลี่ยน"""
    current_sun = "แดดออก" if solar_pwr > SUN_THRESHOLD else "แดดหมด"

    if state["sun_status"] and current_sun != state["sun_status"]:
        if current_sun == "แดดออก":
            send_telegram(
                f"🌅 *[ แจ้งเตือน: แดดออกแล้ว! ]*\n"
                f"ระบบโซลาร์เริ่มผลิตไฟฟ้า: {solar_pwr:,.0f} W",
                creds,
            )
        else:
            send_telegram(
                f"🌙 *[ แจ้งเตือน: แดดหมดแล้ว! ]*\n"
                f"ระบบเข้าสู่โหมดพักการผลิตไฟฟ้า\n"
                f"*(บอทจะสรุปยอดรวมของวันนี้ให้ตอน 23:55 น. ครับ)*",
                creds,
            )

    state["sun_status"] = current_sun


def check_overload(solar_pwr: float, grid_pwr: float, home_pwr: float,
                   state: dict, creds: dict) -> None:
    """ตรวจสอบการใช้ไฟเกินกำลังผลิต"""
    is_ovl = (solar_pwr > SUN_THRESHOLD) and (grid_pwr > OVERLOAD_LIMIT)

    if is_ovl and not state["is_overloaded"]:
        send_telegram(
            f"⚠️ *[ แจ้งเตือนด่วน! ]*\n"
            f"ใช้งานเกินกำลังการผลิต!\n"
            f"----------\n"
            f"☀️ โซลาร์ผลิต: {solar_pwr:,.0f} W\n"
            f"⚡️ ดึงไฟฟ้า: {grid_pwr:,.0f} W\n"
            f"🔋 บ้านใช้ไฟรวม: {home_pwr:,.0f} W\n"
            f"*(กรุณาลดการใช้ไฟฟ้าเพื่อประหยัดค่าไฟ)*",
            creds,
        )
        state["is_overloaded"] = True
    elif not is_ovl and state["is_overloaded"]:
        send_telegram(
            f"✅ *[ สถานะปกติ ]*\n"
            f"การดึงไฟจากการไฟฟ้าลดลงอยู่ในเกณฑ์ปลอดภัยแล้ว (ดึงไฟฟ้า {grid_pwr:,.0f} W)",
            creds,
        )
        state["is_overloaded"] = False


def check_low_voltage(voltage: float, state: dict, creds: dict, current_time: str) -> None:
    """ตรวจสอบแรงดันไฟฟ้าต่ำ แจ้งเตือนเมื่อต่ำกว่า LOW_VOLTAGE_LIMIT"""
    if voltage <= 0:
        return  # ไม่มีข้อมูลแรงดัน

    is_low = voltage < LOW_VOLTAGE_LIMIT

    if is_low and not state.get("is_low_voltage", False):
        send_telegram(
            f"⚠️ *[ แจ้งเตือน: แรงดันไฟฟ้าตก! ]*\n"
            f"🔋 แรงดันปัจจุบัน: *{voltage:.1f} V* (ต่ำกว่า {LOW_VOLTAGE_LIMIT} V)\n"
            f"⏰ เวลา: {current_time}\n"
            f"----------\n"
            f"ไฟอาจตกหรือไม่เสถียร ควรลดการใช้ไฟฟ้าหนัก\n"
            f"หรือตรวจสอบระบบไฟฟ้าในบ้าน",
            creds,
        )
        state["is_low_voltage"] = True
    elif not is_low and state.get("is_low_voltage", False):
        send_telegram(
            f"✅ *[ แรงดันไฟฟ้ากลับสู่ปกติแล้ว ]*\n"
            f"🔋 แรงดันปัจจุบัน: *{voltage:.1f} V*",
            creds,
        )
        state["is_low_voltage"] = False


# ==========================================
# รายงานรายชั่วโมง
# ==========================================

def _calculate_financials(solar_daily_kwh: float, grid_daily_kwh: float,
                          solar_hourly: float, grid_hourly: float) -> dict:
    """คำนวณค่าใช้จ่ายและเงินที่ประหยัดได้"""
    solar_money_hr = solar_hourly * COST_PER_UNIT
    grid_money_hr = grid_hourly * COST_PER_UNIT
    solar_money_day = solar_daily_kwh * COST_PER_UNIT
    grid_money_day = grid_daily_kwh * COST_PER_UNIT
    net_money = solar_money_day - grid_money_day

    net_text = (
        f"✅ ประหยัดไฟสุทธิ {net_money:,.2f} ฿"
        if net_money >= 0
        else f"❌ จ่ายค่าไฟเพิ่ม {abs(net_money):,.2f} ฿"
    )

    return {
        "solar_money_hr": solar_money_hr,
        "grid_money_hr": grid_money_hr,
        "solar_money_day": solar_money_day,
        "grid_money_day": grid_money_day,
        "net_money": net_money,
        "net_text": net_text,
    }


def send_daily_summary(current_time: str, financials: dict,
                       solar_daily_kwh: float, grid_daily_kwh: float,
                       creds: dict, state: dict = None) -> None:
    """ส่งสรุปยอดรวมประจำวัน (เที่ยงคืน)"""
    
    # สร้างกราฟ/ตารางรายชั่วโมง
    hourly_text = ""
    if state and "daily_hourly_history" in state and state["daily_hourly_history"]:
        hourly_text = "\n----------\n📈 *กราฟการทำงานรายชั่วโมง*\n"
        for entry in state["daily_hourly_history"]:
            time_str = entry["time"]
            s_val = entry["solar"]
            g_val = entry["grid"]
            # สร้างบาร์กราฟเล็กๆ สัดส่วน: 1 บล็อก = 0.5 kWh
            s_bar = "☀️" * min(8, max(1, int(s_val * 2))) if s_val > 0 else ""
            g_bar = "⚡️" * min(8, max(1, int(g_val * 2))) if g_val > 0 else ""
            
            hourly_text += f"`{time_str}` | โซลาร์ {s_val:.1f} {s_bar}\n"
            if g_val > 0:
                hourly_text += f"`     ` | ไฟฟ้า  {g_val:.1f} {g_bar}\n"
            
        # ล้างข้อมูลเมื่อส่งสรุปแล้ว
        state["daily_hourly_history"] = []

    msg = (
        f"🏆 *[ สรุปยอดรวมประจำวัน ]*\n"
        f"📅 วันที่: {current_time}\n"
        f"----------\n"
        f"💰 *วันนี้ประหยัดเงินได้: {financials['solar_money_day']:,.2f} บาท*\n"
        f"💸 *วันนี้เสียค่าไฟไป: {financials['grid_money_day']:,.2f} บาท*\n"
        f"💵 *สรุปวันนี้: {financials['net_text']}*\n"
        f"----------\n"
        f"📊 *หน่วยไฟฟ้ารวม*\n"
        f"• ผลิต: {solar_daily_kwh:,.2f} | ซื้อ: {grid_daily_kwh:,.2f} kWh"
        f"{hourly_text}"
    )
    send_telegram(msg, creds)


def process_hourly(current_hour: int, current_minute: int, current_time: str,
                   state: dict, readings: dict, creds: dict) -> None:
    """ประมวลผลรายงานรายชั่วโมง (ถ้าถึงเวลา)"""
    if state.get("last_hourly_run") == current_hour:
        return

    solar_daily_kwh = readings["solar_daily_kwh"]
    grid_daily_kwh = readings["grid_daily_kwh"]
    solar_pwr = readings["solar_pwr"]

    # คำนวณหน่วยไฟรายชั่วโมง
    if current_hour != 0:
        solar_hourly = max(0, solar_daily_kwh - state["last_solar_kwh"])
        grid_hourly = max(0, grid_daily_kwh - state["last_grid_kwh"])
    else:
        solar_hourly = solar_daily_kwh
        grid_hourly = grid_daily_kwh

    financials = _calculate_financials(
        solar_daily_kwh, grid_daily_kwh, solar_hourly, grid_hourly
    )
    # เก็บ hourly ไว้ใน financials สำหรับรายงาน
    financials["solar_hourly"] = solar_hourly
    financials["grid_hourly"] = grid_hourly

    # เก็บประวัติรายชั่วโมงสำหรับสรุปรายวัน
    if "daily_hourly_history" not in state:
        state["daily_hourly_history"] = []
    
    # บันทึกเฉพาะตอนที่ผลิตไฟได้หรือใช้กริดเกิน 0.05 หน่วย เพื่อไม่ให้รก
    if solar_hourly > 0.05 or grid_hourly > 0.05:
        state["daily_hourly_history"].append({
            "time": f"{current_hour:02d}:00",
            "solar": solar_hourly,
            "grid": grid_hourly
        })

    # อัพเดทสถานะ
    state["last_hourly_run"] = current_hour
    state["last_solar_kwh"] = solar_daily_kwh
    state["last_grid_kwh"] = grid_daily_kwh


# ==========================================
# Main & Web Server (For Render)
# ==========================================

app = Flask(__name__)

@app.route('/')
def home():
    state = {}
    readings = {}
    if global_state is not None:
        with state_lock:
            state = global_state.copy()
    if global_readings is not None:
        with readings_lock:
            readings = global_readings.copy()
            
    # extract data
    solar_pwr = readings.get("solar_pwr", 0)
    home_pwr = readings.get("home_pwr", 0)
    grid_pwr = readings.get("grid_pwr", 0)
    
    solar_daily_kwh = readings.get("solar_daily_kwh", 0)
    grid_daily_kwh = readings.get("grid_daily_kwh", 0)
    solar_daily_money = solar_daily_kwh * COST_PER_UNIT
    grid_daily_money = grid_daily_kwh * COST_PER_UNIT
    
    monthly_solar_kwh = state.get("monthly_solar_kwh", 0)
    monthly_grid_kwh = state.get("monthly_grid_kwh", 0)
    monthly_solar_money = state.get("monthly_solar_money", 0)
    monthly_grid_money = state.get("monthly_grid_money", 0)
    
    current_month = state.get("current_month", "กำลังรวบรวมข้อมูล")
    
    weather_str = readings.get("current_weather", "")
    weather_html = f'<span style="font-size: 1.25rem; color: var(--text-muted); font-weight: 400; margin-left: 10px; vertical-align: middle;">{weather_str}</span>' if weather_str else ""
    
    html = f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Solar Bot Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=Kanit:wght@300;500;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #0f172a;
                --card-bg: rgba(30, 41, 59, 0.7);
                --text: #f8fafc;
                --text-muted: #94a3b8;
                --primary: #3b82f6;
                --solar: #eab308;
                --grid: #ef4444;
                --home: #10b981;
                --accent: #8b5cf6;
            }}
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: 'Kanit', 'Inter', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 2rem;
                background-image: 
                    radial-gradient(circle at 15% 50%, rgba(59, 130, 246, 0.15), transparent 25%),
                    radial-gradient(circle at 85% 30%, rgba(234, 179, 8, 0.15), transparent 25%);
                background-attachment: fixed;
            }}
            h1 {{
                font-size: 2.5rem;
                font-weight: 700;
                margin-bottom: 2rem;
                background: linear-gradient(to right, #60a5fa, #a78bfa);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-align: center;
            }}
            .dashboard {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 1.5rem;
                width: 100%;
                max-width: 1200px;
            }}
            .card {{
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 1.5rem;
                padding: 1.5rem;
                transition: transform 0.3s ease, box-shadow 0.3s ease;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            }}
            .card:hover {{
                transform: translateY(-5px);
                box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.2), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
            }}
            .card-header {{
                font-size: 1.25rem;
                font-weight: 600;
                margin-bottom: 1rem;
                color: var(--text-muted);
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }}
            .data-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 1rem;
                padding-bottom: 1rem;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            }}
            .data-row:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
            .label {{ font-size: 1rem; color: var(--text-muted); }}
            .value {{ font-size: 1.5rem; font-weight: 700; }}
            .value.solar {{ color: var(--solar); }}
            .value.grid {{ color: var(--grid); }}
            .value.home {{ color: var(--home); }}
            .value.money {{ color: var(--accent); }}
            
            @media (max-width: 768px) {{
                body {{ padding: 1rem; }}
                h1 {{ font-size: 2rem; }}
            }}
            
            /* Animations */
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.7; }}
            }}
            .live-indicator {{
                display: inline-block;
                width: 10px;
                height: 10px;
                background-color: var(--home);
                border-radius: 50%;
                animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
                margin-right: 8px;
            }}
        </style>
    </head>
    <body>
        <h1><span class="live-indicator"></span>Solar Dashboard {weather_html}</h1>
        
        <div class="dashboard">
            <!-- Real-time Card -->
            <div class="card">
                <div class="card-header">⚡️ สถานะปัจจุบัน (Real-time)</div>
                <div class="data-row">
                    <span class="label">โซลาร์เซลล์</span>
                    <span class="value solar">{{solar_pwr:,.0f}} W</span>
                </div>
                <div class="data-row">
                    <span class="label">ไฟฟ้า</span>
                    <span class="value grid">{{grid_pwr:,.0f}} W</span>
                </div>
                <div class="data-row">
                    <span class="label">บ้านใช้ไฟ</span>
                    <span class="value home">{{home_pwr:,.0f}} W</span>
                </div>
            </div>

            <!-- Daily Card -->
            <div class="card">
                <div class="card-header">📈 สรุปวันนี้ (Daily)</div>
                <div class="data-row">
                    <span class="label">ผลิตไฟได้</span>
                    <span class="value solar">{{solar_daily_kwh:,.2f}} kWh</span>
                </div>
                <div class="data-row">
                    <span class="label">ประหยัดเงิน</span>
                    <span class="value money">{{solar_daily_money:,.2f}} ฿</span>
                </div>
                <div class="data-row">
                    <span class="label">ซื้อไฟฟ้า</span>
                    <span class="value grid">{{grid_daily_kwh:,.2f}} kWh</span>
                </div>
                <div class="data-row">
                    <span class="label">เสียค่าไฟ</span>
                    <span class="value grid">{{grid_daily_money:,.2f}} ฿</span>
                </div>
            </div>

            <!-- Monthly Card -->
            <div class="card">
                <div class="card-header">📅 สรุปเดือนนี้ ({{current_month}})</div>
                <div class="data-row">
                    <span class="label">ผลิตไฟได้รวม</span>
                    <span class="value solar">{{monthly_solar_kwh:,.2f}} kWh</span>
                </div>
                <div class="data-row">
                    <span class="label">ประหยัดเงินรวม</span>
                    <span class="value money">{{monthly_solar_money:,.2f}} ฿</span>
                </div>
                <div class="data-row">
                    <span class="label">ซื้อไฟฟ้ารวม</span>
                    <span class="value grid">{{monthly_grid_kwh:,.2f}} kWh</span>
                </div>
                <div class="data-row">
                    <span class="label">เสียค่าไฟรวม</span>
                    <span class="value grid">{{monthly_grid_money:,.2f}} ฿</span>
                </div>
            </div>
        </div>
        
        <div style="margin-top: 3rem; color: var(--text-muted); font-size: 0.875rem;">
            อัปเดตอัตโนมัติทุกๆ 5 นาที • <a href="javascript:window.location.reload(true)" style="color: var(--primary); text-decoration: none;">รีเฟรชข้อมูล</a>
        </div>
    </body>
    </html>
    """
    return html

global_state = None
global_readings = None
state_lock = threading.Lock()
readings_lock = threading.Lock()

def deye_monitoring_loop(creds):
    """ลูปตรวจสอบ Deye API ทุก 5 นาที"""
    global global_state, global_readings
    logger.info("=== เริ่มทำงาน Deye Monitoring Loop ===")
    
    while True:
        try:
            tz = pytz.timezone(TIMEZONE)
            now = datetime.now(tz)
            current_time = now.strftime("%Y-%m-%d %H:%M")
            current_hour = now.hour
            current_minute = now.minute

            with state_lock:
                state = global_state
                is_startup = not state.get("_has_started")

            readings = fetch_inverter_data(creds)
            if readings is not None:
                weather_str = get_weather()
                if weather_str:
                    readings["current_weather"] = weather_str

            with readings_lock:
                global_readings = readings

            # --- เพิ่มลอจิกสะสมค่าไฟบ้านเอง (Manual Accumulation) ---
            with state_lock:
                now_dt = datetime.now(pytz.timezone(TIMEZONE))
                last_calc_str = global_state.get("last_calc_time")
                
                # ถ้ารีเซ็ตวันใหม่
                current_date = now_dt.strftime("%Y-%m-%d")
                if global_state.get("calc_date") != current_date:
                    global_state["manual_home_daily_kwh"] = 0.0
                    global_state["calc_date"] = current_date

                if last_calc_str:
                    try:
                        last_dt = datetime.fromisoformat(last_calc_str)
                        hours_passed = (now_dt - last_dt).total_seconds() / 3600.0
                        # กันกรณีเวลาเพี้ยนหรือพึ่งเปิดบอทใหม่
                        if 0 < hours_passed < 1.0:
                            # คำนวณ kWh = (Watt / 1000) * ชั่วโมงที่ผ่านไป
                            added_kwh = (readings["home_pwr"] / 1000.0) * hours_passed
                            global_state["manual_home_daily_kwh"] = global_state.get("manual_home_daily_kwh", 0.0) + added_kwh
                    except ValueError:
                        pass
                
                global_state["last_calc_time"] = now_dt.isoformat()
                
                # เอาค่าที่สะสมเอง ยัดกลับเข้าไปใน readings (ถ้า API ส่งมาให้ด้วย ก็เอาค่าที่มากที่สุด)
                # --- ใช้ TotalConsumption baseline เพื่อคำนวณยอดใช้ไฟรายวันที่แม่นยำ ---
                total_cons = readings.get("total_consumption", 0.0)
                if total_cons > 0:
                    if global_state.get("baseline_date") != current_date:
                        # วันใหม่: ตั้ง baseline
                        global_state["total_consumption_baseline"] = total_cons
                        global_state["baseline_date"] = current_date
                    
                    baseline = global_state.get("total_consumption_baseline", total_cons)
                    home_daily_from_total = max(0.0, total_cons - baseline)
                    
                    # ใช้ค่าที่มากที่สุดระหว่าง API / manual / total_consumption
                    readings["home_daily_kwh"] = max(
                        readings.get("home_daily_kwh", 0.0),
                        global_state.get("manual_home_daily_kwh", 0.0),
                        home_daily_from_total
                    )
                else:
                    readings["home_daily_kwh"] = max(
                        readings.get("home_daily_kwh", 0.0), 
                        global_state.get("manual_home_daily_kwh", 0.0)
                    )
                
                # คำนวณกริดใหม่ด้วยค่าที่อัปเดตแล้ว
                if readings["home_daily_kwh"] > 0:
                    readings["grid_daily_kwh"] = max(0.0, readings["home_daily_kwh"] - readings["solar_daily_kwh"])
                
                # --- ส่งสรุปรายวันตอน 23:55 ---
                if now_dt.hour == 23 and now_dt.minute >= 50:
                    if global_state.get("last_daily_run") != current_date:
                        financials = _calculate_financials(
                            readings["solar_daily_kwh"], readings["grid_daily_kwh"], 0.0, 0.0
                        )
                        send_daily_summary(current_time, financials, readings["solar_daily_kwh"], readings["grid_daily_kwh"], creds, global_state)
                        
                        # --- เก็บสถิติรายเดือน ---
                        current_month_str = now_dt.strftime("%Y-%m")
                        if global_state.get("current_month") and global_state.get("current_month") != current_month_str:
                            # ส่งสรุปเดือนเก่าก่อนรีเซ็ต
                            month_reply = _build_month_reply(global_state, current_time)
                            send_telegram(f"📢 *จบเดือนแล้ว! สรุปยอดของเดือน {global_state.get('current_month')} ครับ*\n\n{month_reply}", creds)
                            
                            # รีเซ็ต
                            global_state["monthly_solar_kwh"] = 0.0
                            global_state["monthly_grid_kwh"] = 0.0
                            global_state["monthly_solar_money"] = 0.0
                            global_state["monthly_grid_money"] = 0.0

                        global_state["current_month"] = current_month_str
                        global_state["monthly_solar_kwh"] = global_state.get("monthly_solar_kwh", 0.0) + readings["solar_daily_kwh"]
                        global_state["monthly_grid_kwh"] = global_state.get("monthly_grid_kwh", 0.0) + readings["grid_daily_kwh"]
                        global_state["monthly_solar_money"] = global_state.get("monthly_solar_money", 0.0) + financials["solar_money_day"]
                        global_state["monthly_grid_money"] = global_state.get("monthly_grid_money", 0.0) + financials["grid_money_day"]
                        # -----------------------

                        global_state["last_daily_run"] = current_date
                # ------------------------------

                save_state(global_state)
            # ----------------------------------------------------

            logger.info("อัปเดตข้อมูลจาก Deye สำเร็จ (Home: %.2f W, Home_Daily: %.2f kWh, Grid_Daily: %.2f kWh)", 
                        readings["home_pwr"], readings.get("home_daily_kwh", 0), readings.get("grid_daily_kwh", 0))

            if readings is None:
                check_online_status(False, state, creds, current_time)
            else:
                check_online_status(True, state, creds, current_time)

                if is_startup:
                    state["_has_started"] = True
                    # ป้องกันกราฟพุ่งกระโดดจากการเริ่มรันครั้งแรกบน Render
                    state["last_solar_kwh"] = readings["solar_daily_kwh"]
                    state["last_grid_kwh"] = readings["grid_daily_kwh"]
                    state["last_hourly_run"] = current_hour
                    check_startup(True, creds)

                check_sun_status(readings["solar_pwr"], state, creds)
                check_overload(
                    readings["solar_pwr"], readings["grid_pwr"],
                    readings["home_pwr"], state, creds,
                )
                check_low_voltage(
                    readings.get("voltage", 0), state, creds, current_time,
                )
                process_hourly(current_hour, current_minute, current_time,
                               state, readings, creds)

            with state_lock:
                save_state(state)

        except Exception as e:
            logger.exception("Deye Monitoring Error")
            if not global_state.get("is_offline"):
                send_telegram(f"⚠️ *[ ระบบขัดข้อง ]*\nDeye API Error\n`{e}`", creds)
                global_state["is_offline"] = True

        time.sleep(300)  # รอ 5 นาทีก่อนเช็คใหม่


def telegram_polling_loop(creds):
    """ลูปตรวจสอบคำสั่งจาก Telegram ทุก 5 วินาที"""
    global global_state, global_readings
    logger.info("=== เริ่มทำงาน Telegram Polling Loop ===")
    
    while True:
        try:
            tz = pytz.timezone(TIMEZONE)
            now = datetime.now(tz)
            current_time = now.strftime("%Y-%m-%d %H:%M")

            with state_lock:
                state = global_state

            with readings_lock:
                readings = global_readings

            process_chat_commands(state, creds, readings, current_time)

            with state_lock:
                save_state(state)

        except Exception as e:
            logger.exception("Telegram Polling Error")

        time.sleep(5)  # รอ 5 วินาทีก่อนดึงแชทใหม่


def start_bot():
    """ตั้งค่าและเริ่ม Thread ของบอท"""
    global global_state
    creds = get_credentials()
    global_state = load_state()

    t1 = threading.Thread(target=deye_monitoring_loop, args=(creds,), daemon=True)
    t2 = threading.Thread(target=telegram_polling_loop, args=(creds,), daemon=True)
    
    t1.start()
    t2.start()


if __name__ == "__main__":
    # 1. เริ่มทำงานบอทใน Background
    start_bot()
    
    # 2. เริ่มทำงาน Web Server สำหรับ Render
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
