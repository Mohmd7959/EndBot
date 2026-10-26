AAGT os

API_ID = 36357841
API_HASH = "dc3d364a47802ca079e4b3ed0fd8e62f"
BOT_TOKEN = "8973651211:AAEezyZN5mfEXn8QtyfFTtf3MX2oCId_eE0" 
DEV_ID = 8777227942
DEV_LINK = "https://t.me/rkpkk"

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "bot.db")
SESSIONS_DIR = os.path.join(BASE, "sessions")
VIDEOS_DIR = os.path.join(BASE, "download")

for d in [DATA_DIR, SESSIONS_DIR, VIDEOS_DIR]:
    os.makedirs(d, exist_ok=True)

MAX_ACCOUNTS_FREE = 10
MAX_ACCOUNTS_SUB = 100  # دعم لغاية 100 حساب فعلياً
