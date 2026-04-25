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

    solar_status = "🟢 ผลิตไฟ" if solar_pwr > SUN_THRESHOLD else "🔴 ไม่ผลิต"

    # คำนวณเงิน
    saved_money = solar_daily_kwh * COST_PER_UNIT
    home_cost = home_daily_kwh * COST_PER_UNIT
    est_grid_cost = max(0, home_cost - saved_money)

    return (
        f"📊 *[ สถานะระบบ Real-time ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"☀️ โซลาร์: {solar_pwr:,.0f} W ({solar_status})\n"
        f"🏠 บ้านใช้: {home_pwr:,.0f} W\n"
        f"⚡️ กริด: {grid_pwr:,.0f} W\n"
        f"----------\n"
        f"📈 *ยอดสะสมวันนี้*\n"
        f"☀️ ผลิตได้: {solar_daily_kwh:,.2f} kWh (ประหยัด {saved_money:,.2f} ฿)\n"
        f"🏠 บ้านใช้ไฟทั้งหมด: {home_daily_kwh:,.2f} kWh\n"
        f"💸 ประเมินค่าไฟที่ต้องจ่าย: {est_grid_cost:,.2f} ฿"
    )


def _build_cost_reply(readings: dict | None, current_time: str) -> str:
    """สร้างข้อความตอบกลับเรื่องค่าไฟ"""
    if readings is None:
        return "❌ ไม่สามารถคำนวณค่าไฟได้ — ระบบ Offline"

    solar_daily_kwh = readings["solar_daily_kwh"]
    grid_daily_kwh = readings["grid_daily_kwh"]
    solar_money = solar_daily_kwh * COST_PER_UNIT
    grid_money = grid_daily_kwh * COST_PER_UNIT
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
        f"☀️ ผลิตได้: {solar_daily_kwh:,.2f} kWh\n"
        f"💡 ประหยัดแล้ว: *{solar_money:,.2f} บาท*\n"
        f"----------\n"
        f"⚡️ ซื้อไฟกริด: {grid_daily_kwh:,.2f} kWh\n"
        f"💸 เสียค่าไฟ: *{grid_money:,.2f} บาท*\n"
        f"----------\n"
        f"💵 *สรุป: {net_text}*\n"
        f"📌 คิดที่หน่วยละ {COST_PER_UNIT} บาท"
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

    return (
        f"☀️ *[ ข้อมูลโซลาร์เซลล์ ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"📡 สถานะ: {status}\n"
        f"⚡️ กำลังผลิต: *{solar_pwr:,.0f} W*\n"
        f"📊 ผลิตสะสมวันนี้: *{solar_daily_kwh:,.2f} kWh*\n"
        f"----------\n"
        f"🏠 บ้านใช้: {home_pwr:,.0f} W\n"
        f"🔌 ดึงกริด: {grid_pwr:,.0f} W\n"
        f"☀️ จากโซลาร์: {solar_pwr:,.0f} W"
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
        status = f"🔴 กำลังดึงไฟหลวง ({grid_pct:.0f}% ของโหลด)"
    elif grid_pwr < 0:
        status = "🔄 จ่ายไฟย้อนเข้าสายส่ง (Export)"
    else:
        status = "🟢 ไม่ได้ดึงไฟหลวง (ใช้โซลาร์ล้วน/แบตเตอรี่)"

    display_grid_pwr = abs(grid_pwr)

    return (
        f"⚡️ *[ ข้อมูลไฟหลวง (กริด) ]*\n"
        f"⏰ เวลา: {current_time}\n"
        f"----------\n"
        f"📡 สถานะ: {status}\n"
        f"🔌 กำลังดึงไฟ: *{display_grid_pwr:,.0f} W*\n"
        f"📊 ซื้อไฟสะสมวันนี้: *{grid_daily_kwh:,.2f} kWh*\n"
        f"----------\n"
        f"🏠 บ้านใช้: {home_pwr:,.0f} W\n"
        f"☀️ จากโซลาร์: {solar_pwr:,.0f} W"
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
        f"☀️ `/solar` หรือ *โซลาร์*\n"
        f"→ ข้อมูลการผลิตไฟจากโซลาร์\n\n"
        f"⚡️ `/grid` หรือ *ไฟหลวง*\n"
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
    solar_pwr = 0.0
    solar_daily_kwh = 0.0
    home_pwr = 0.0
    home_daily_kwh = 0.0
    grid_daily_kwh = 0.0
    grid_pwr = 0.0

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
            f"⚠️ แจ้งเตือนทันทีเมื่อดึงไฟกริดเกิน: {OVERLOAD_LIMIT:,.0f} W",
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
                f"*(จะไม่มีการส่งรายงานรายชั่วโมงจนกว่าจะถึงเช้าพรุ่งนี้)*",
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
            f"⚡️ ดึงไฟกริด: {grid_pwr:,.0f} W\n"
            f"☀️ โซลาร์ผลิต: {solar_pwr:,.0f} W\n"
            f"🔋 บ้านใช้ไฟรวม: {home_pwr:,.0f} W\n"
            f"*(กรุณาลดการใช้ไฟฟ้าเพื่อประหยัดค่าไฟ)*",
            creds,
        )
        state["is_overloaded"] = True
    elif not is_ovl and state["is_overloaded"]:
        send_telegram(
            f"✅ *[ สถานะปกติ ]*\n"
            f"การดึงไฟจากการไฟฟ้าลดลงอยู่ในเกณฑ์ปลอดภัยแล้ว (ดึงกริด {grid_pwr:,.0f} W)",
            creds,
        )
        state["is_overloaded"] = False


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
                       creds: dict) -> None:
    """ส่งสรุปยอดรวมประจำวัน (เที่ยงคืน)"""
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
    )
    send_telegram(msg, creds)


def send_hourly_report(current_time: str, state: dict, readings: dict,
                       financials: dict, creds: dict) -> None:
    """ส่งรายงานสถานะรายชั่วโมง"""
    solar_pwr = readings["solar_pwr"]
    grid_pwr = readings["grid_pwr"]
    home_pwr = readings["home_pwr"]
    solar_daily_kwh = readings["solar_daily_kwh"]
    grid_daily_kwh = readings["grid_daily_kwh"]

    status_icon = "🌅" if solar_pwr > SUN_THRESHOLD else "🌤"

    msg = (
        f"🕒 *[ รายงานสถานะรายชั่วโมง ]*\n"
        f"⏱ เวลา: {current_time}\n"
        f"----------\n"
        f"☀️ *โซลาร์เซลล์ (ผลิตไฟ)*\n"
        f"• เดิม {state['last_solar_kwh']:,.2f} + ใหม่ {financials['solar_hourly']:,.2f} = รวม {solar_daily_kwh:,.2f} หน่วย\n"
        f"• 💡 ประหยัดเพิ่มชั่วโมงนี้: +{financials['solar_money_hr']:,.2f} ฿\n"
        f"----------\n"
        f"🔌 *ดึงไฟหลวง (เสียค่าไฟ)*\n"
        f"• เดิม {state['last_grid_kwh']:,.2f} + ใหม่ {financials['grid_hourly']:,.2f} = รวม {grid_daily_kwh:,.2f} หน่วย\n"
        f"• 💸 จ่ายเพิ่มชั่วโมงนี้: +{financials['grid_money_hr']:,.2f} ฿\n"
        f"----------\n"
        f"📈 *ยอดรวมเงินวันนี้ (สะสม)*\n"
        f"• 💰 ประหยัดแล้ว: {financials['solar_money_day']:,.2f} ฿\n"
        f"• 💸 เสียค่าไฟแล้ว: {financials['grid_money_day']:,.2f} ฿\n"
        f"• 💵 สรุปวันนี้: {financials['net_text']}\n"
        f"----------\n"
        f"🏠 *สถานะ Real-time*\n"
        f"• {status_icon} โซลาร์: {solar_pwr:,.0f} W\n"
        f"• ⚡️ ไฟหลวง: {grid_pwr:,.0f} W\n"
        f"• 🔋 โหลดรวม: {home_pwr:,.0f} W"
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

    if solar_pwr > 20 and current_hour != 0:
        # รายงานรายชั่วโมง (ตอนมีแดด)
        send_hourly_report(current_time, state, readings, financials, creds)

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
    return "Solar Bot is running 24/7!"

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
                
                # เอาค่าที่สะสมเอง ยัดกลับเข้าไปใน readings
                readings["home_daily_kwh"] = global_state.get("manual_home_daily_kwh", 0.0)
                
                # คำนวณกริดใหม่ด้วยค่าที่อัปเดตแล้ว
                if readings["home_daily_kwh"] > 0:
                    readings["grid_daily_kwh"] = max(0.0, readings["home_daily_kwh"] - readings["solar_daily_kwh"])
                
                # --- ส่งสรุปรายวันตอน 23:55 ---
                if now_dt.hour == 23 and now_dt.minute >= 50:
                    if global_state.get("last_daily_run") != current_date:
                        financials = _calculate_financials(
                            readings["solar_daily_kwh"], readings["grid_daily_kwh"], 0.0, 0.0
                        )
                        send_daily_summary(current_time_str, financials, readings["solar_daily_kwh"], readings["grid_daily_kwh"], creds)
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
