# ==========================================
# ค่าคงที่สำหรับระบบ Solar Bot
# ==========================================

# ค่าไฟต่อหน่วย (บาท/kWh)
COST_PER_UNIT = 4.5

# กำลังผลิตขั้นต่ำ (W) ที่ถือว่า "แดดออก"
SUN_THRESHOLD = 100

# ค่ากริดสูงสุด (W) ก่อนแจ้งเตือน Overload
OVERLOAD_LIMIT = 1500

# Timezone
TIMEZONE = "Asia/Bangkok"

# ไฟล์เก็บสถานะ
STATUS_FILE = "status.json"

# จำนวนครั้ง retry ส่ง Telegram
TELEGRAM_RETRIES = 3

# Timeout สำหรับ API calls (วินาที)
API_TIMEOUT = 20
TELEGRAM_TIMEOUT = 10

# Deye API Endpoint
DEYE_API_URL = "https://eu1-developer.deyecloud.com/v1.0/device/latest"

# Key mapping สำหรับข้อมูลจาก Deye API
SOLAR_POWER_KEY = "TotalActiveACOutputPower"
SOLAR_DAILY_KEY = "DailyActiveProduction"
HOME_POWER_KEY = "TotalConsumptionPower"
GRID_DAILY_KEYS = ["DailyEnergyPurchased", "DailyGridPurchased"]
GRID_POWER_KEYS = ["TotalGridPower", "Total Grid Power"]
