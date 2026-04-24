"""
Solar Monitor Bot — บอทเฝ้าระวังระบบโซลาร์เซลล์
ดึงข้อมูลจาก Deye Cloud API และส่งรายงานผ่าน Telegram
"""

import requests
import time
import logging
import json
import os
from datetime import datetime

import pytz

from config import (
    COST_PER_UNIT, SUN_THRESHOLD, OVERLOAD_LIMIT, TIMEZONE,
    STATUS_FILE, TELEGRAM_RETRIES, API_TIMEOUT, TELEGRAM_TIMEOUT,
    DEYE_API_URL, SOLAR_POWER_KEY, SOLAR_DAILY_KEY, HOME_POWER_KEY,
    GRID_DAILY_KEYS, GRID_POWER_KEYS,
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

# ค่าเริ่มต้น (ใช้ env var แทนได้ถ้าตั้งค่าไว้)
_DEFAULTS = {
    "DEYE_TOKEN": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsib2F1dGgyLXJlc291cmNlIl0sInVzZXJfbmFtZSI6IjBfbml0aXRob245OTdAZ21haWwuY29tXzIiLCJzY29wZSI6WyJhbGwiXSwiZGV0YWlsIjp7Im9yZ2FuaXphdGlvbklkIjowLCJ0b3BHcm91cElkIjpudWxsLCJncm91cElkIjpudWxsLCJyb2xlSWQiOi0xLCJ1c2VySWQiOjEzNzQzNzEzLCJ2ZXJzaW9uIjoxMDAwLCJpZGVudGlmaWVyIjoibml0aXRob245OTdAZ21haWwuY29tIiwiaWRlbnRpdHlUeXBlIjoyLCJtZGMiOiJ1YyIsImFwcElkIjoiMjAyNjA0MjQ3NzM0MDAyIiwibWZhU3RhdHVzIjpudWxsLCJ0ZW5hbnQiOiJEZXllIn0sImV4cCI6MTc4MjE4MzUwMywibWRjIjoidWMiLCJhdXRob3JpdGllcyI6WyJhbGwiXSwianRpIjoiMTI2N2NmMjUtZDE2NS00NWIyLWFhZDgtMzdiMjE0MjcyM2M0IiwiY2xpZW50X2lkIjoidGVzdCJ9.Q6sfytmKAyjTfhhZpkDnmO33s8jNT2t5OJsJWFn_ZSJqAHjODrn5dUnwK2otRPMPEva-n_t390O0oUiVNgfEQWa_2BYI9WAANBuOkVySbmXRfuqknksqh8J696THxiarJWVqjJNF0DzP1AVnjkbu1mOlpUw4ORwCLk71AkcGT2Ej3QonqhGYAHjKWc76QYmgakH4OazJhfyvLzHSXiBnqknWJH0BADnJ2Za75HEydt8TqklPLrL-dXtyAqA080kIqiXDLVdnpDEAIn5fkxG6vqmD1_AiZSqp-WxGkezpOGxmVkPJ3CWjv5egrxzMgIvCG7K8209c_AkEzq0tz9iFBQ",
    "INVERTER_SN": "2512272221",
    "TELEGRAM_BOT_TOKEN": "8222753214:AAFJOzToPcvIN5iClhcF-WbCTz3NShNDtdI",
    "TELEGRAM_CHAT_ID": "7065585231",
}


def get_credentials() -> dict:
    """โหลดข้อมูลลับ — ใช้ env var ถ้ามี, ไม่งั้นใช้ค่าเริ่มต้น"""
    return {
        "deye_token": os.environ.get("DEYE_TOKEN", _DEFAULTS["DEYE_TOKEN"]),
        "inverter_sn": os.environ.get("INVERTER_SN", _DEFAULTS["INVERTER_SN"]),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", _DEFAULTS["TELEGRAM_BOT_TOKEN"]),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", _DEFAULTS["TELEGRAM_CHAT_ID"]),
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
        elif key in GRID_DAILY_KEYS:
            grid_daily_kwh = val
        elif key in GRID_POWER_KEYS:
            grid_pwr = val

    # Fallback: คำนวณ grid power จาก home - solar
    if grid_pwr == 0.0:
        grid_pwr = max(0, home_pwr - solar_pwr)

    return {
        "solar_pwr": solar_pwr,
        "solar_daily_kwh": solar_daily_kwh,
        "home_pwr": home_pwr,
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
    if current_minute >= 5 or state["last_hourly_run"] == current_hour:
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

    if current_hour == 0:
        # สรุปยอดรวมประจำวัน (เที่ยงคืน)
        send_daily_summary(current_time, financials, solar_daily_kwh, grid_daily_kwh, creds)
    elif solar_pwr > 20:
        # รายงานรายชั่วโมง (ตอนมีแดด)
        send_hourly_report(current_time, state, readings, financials, creds)

    # อัพเดทสถานะ
    state["last_hourly_run"] = current_hour
    state["last_solar_kwh"] = solar_daily_kwh
    state["last_grid_kwh"] = grid_daily_kwh


# ==========================================
# Main
# ==========================================

def main():
    """จุดเริ่มต้นหลักของบอท"""
    logger.info("=== Solar Bot เริ่มทำงาน ===")

    # โหลด credentials
    creds = get_credentials()

    # เวลาปัจจุบัน
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    current_time = now.strftime("%Y-%m-%d %H:%M")
    current_hour = now.hour
    current_minute = now.minute

    # โหลดสถานะ
    is_startup = not os.path.exists(STATUS_FILE)
    state = load_state()

    try:
        # ดึงข้อมูลจาก Deye API
        readings = fetch_inverter_data(creds)

        if readings is None:
            # Offline
            check_online_status(False, state, creds, current_time)
        else:
            # Online
            check_online_status(True, state, creds, current_time)

            # แจ้งเตือนเมื่อเริ่มทำงานครั้งแรก
            check_startup(is_startup, creds)

            # ตรวจสอบสถานะแดด
            check_sun_status(readings["solar_pwr"], state, creds)

            # ตรวจสอบ overload
            check_overload(
                readings["solar_pwr"], readings["grid_pwr"],
                readings["home_pwr"], state, creds,
            )

            # รายงานรายชั่วโมง
            process_hourly(current_hour, current_minute, current_time,
                           state, readings, creds)

    except requests.RequestException as e:
        logger.error("เชื่อมต่อ Deye API ล้มเหลว: %s", e)
        if not state["is_offline"]:
            send_telegram(
                f"⚠️ *[ ระบบขัดข้อง ]*\n"
                f"ติดต่อ Server Deye ไม่ได้ (อาจเกิดจากเน็ตล่ม)\n"
                f"`{e}`",
                creds,
            )
            state["is_offline"] = True

    except Exception as e:
        logger.exception("เกิดข้อผิดพลาดที่ไม่คาดคิด")
        if not state["is_offline"]:
            send_telegram(
                f"⚠️ *[ ระบบขัดข้อง ]*\n"
                f"เกิดข้อผิดพลาดที่ไม่คาดคิด\n"
                f"`{e}`",
                creds,
            )
            state["is_offline"] = True

    finally:
        # บันทึกสถานะเสมอ ไม่ว่าจะสำเร็จหรือไม่
        save_state(state)
        logger.info("=== Solar Bot จบการทำงาน ===")


if __name__ == "__main__":
    main()
