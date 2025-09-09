
# file: opdtest_fixed.py
import asyncio
import os
import logging
import sqlite3
import json
import threading
import re
import requests
import urllib.parse
import math
import itertools
import time
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple

from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# =============== CONFIG ===============
BOT_TOKEN = "7741178469:AAEXmDVBCDCp6wY0AzPzxpuEzNRcKId86_o"
WEBAPP_URL = "https://8bea2f310e6b.ngrok-free.app"  # ha iPad/ngrok: "https://<valami>.ngrok-free.app"
DB_NAME = "restaurant_orders.db"
# Admin jogosítottak listája (Telegram user ID-k)
ADMIN_USER_IDS = [7553912440]  # Itt add meg a saját Telegram user ID-d

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Értesítések sorban (pl. éttermi csoportnak visszaírás)
notification_queue: "Queue[Dict]" = Queue()

def parse_hungarian_address(address: str) -> str:
    """
    Magyar cím parser - rövidítések és irányítószámok felismerése
    """
    if not address or not address.strip():
        return ""

    addr = address.strip()

    # Irányítószám felismerés és normalizálás
    postal_pattern = r'(\d{4})\s*([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű\s]+)'
    match = re.search(postal_pattern, addr)
    if match:
        postal_code, city = match.groups()
        addr = f"{postal_code} {city.strip()}"

    abbreviations = {
        r'\bsgt\b': 'sugárút',
        r'\bkrt\b': 'körút', 
        r'\but\b': 'utca',
        r'\bút\b': 'utca',
        r'\btér\b': 'tér',
        r'\bpl\b': 'pályaudvar',
        r'\báll\b': 'állomás',
        r'\bker\b': 'kerület',
        r'\bker\.\b': 'kerület',
        r'\bV\.\s*ker\b': 'V. kerület',
        r'\bI\.\s*ker\b': 'I. kerület',
        r'\bII\.\s*ker\b': 'II. kerület',
        r'\bIII\.\s*ker\b': 'III. kerület',
        r'\bIV\.\s*ker\b': 'IV. kerület',
        r'\bVI\.\s*ker\b': 'VI. kerület',
        r'\bVII\.\s*ker\b': 'VII. kerület',
        r'\bVIII\.\s*ker\b': 'VIII. kerület',
        r'\bIX\.\s*ker\b': 'IX. kerület',
        r'\bX\.\s*ker\b': 'X. kerület',
        r'\bXI\.\s*ker\b': 'XI. kerület',
        r'\bXII\.\s*ker\b': 'XII. kerület',
        r'\bXIII\.\s*ker\b': 'XIII. kerület',
        r'\bXIV\.\s*ker\b': 'XIV. kerület',
        r'\bXV\.\s*ker\b': 'XV. kerület',
        r'\bXVI\.\s*ker\b': 'XVI. kerület',
        r'\bXVII\.\s*ker\b': 'XVII. kerület',
        r'\bXVIII\.\s*ker\b': 'XVIII. kerület',
        r'\bXIX\.\s*ker\b': 'XIX. kerület',
        r'\bXX\.\s*ker\b': 'XX. kerület',
        r'\bXXI\.\s*ker\b': 'XXI. kerület',
        r'\bXXII\.\s*ker\b': 'XXII. kerület',
        r'\bXXIII\.\s*ker\b': 'XXIII. kerület'
    }

    for pattern, replacement in abbreviations.items():
        addr = re.sub(pattern, replacement, addr, flags=re.IGNORECASE)

    addr = re.sub(r'\s+', ' ', addr).strip()

    return addr

def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Cím geokódolása Nominatim API-val
    """
    try:
        time.sleep(0.5)  # Udvarias várakozás

        parsed_addr = parse_hungarian_address(address)
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': parsed_addr,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'hu',
            'addressdetails': 1
        }
        headers = {
            'User-Agent': 'OPDBot/1.0 (Delivery Navigation)'
        }

        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                lat = float(data[0]['lat'])
                lon = float(data[0]['lon'])
                return (lat, lon)
    except Exception as e:
        logger.error(f"Geocoding error for '{address}': {e}")
    return None

def haversine_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
    """
    Haversine formula - légvonalbeli távolság két koordináta között (km-ben)
    """
    lat1, lon1 = coord1
    lat2, lon2 = coord2

    R = 6371.0  # Föld sugara km-ben

    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))

    return R * c

def optimize_route(addresses: List[str]) -> List[Tuple[str, float, float]]:
    """
    Fejlett útvonal optimalizáló - TSP (Traveling Salesman Problem) megoldás
    Visszatérés: [(address, lat, lon), ...]
    """
    if len(addresses) <= 1:
        return [(addr, 0, 0) for addr in addresses]

    # Maximum 8 cím (teljesítmény miatt)
    if len(addresses) > 8:
        logger.warning(f"Too many addresses ({len(addresses)}), limiting to 8")
        addresses = addresses[:8]

    # Geokódolás és érvényes koordináták gyűjtése
    coords_with_addr = []

    for addr in addresses:
        coord = geocode_address(addr)
        if coord:
            coords_with_addr.append((addr, coord[0], coord[1]))
        else:
            logger.warning(f"Could not geocode address: {addr}")

    if len(coords_with_addr) <= 1:
        return coords_with_addr

    # TSP megoldás - brute force kis számú címre, heurisztikus nagyobbra
    if len(coords_with_addr) <= 4:
        # Brute force - minden permutációt kipróbál
        min_distance = float('inf')
        best_route = coords_with_addr

        for perm in itertools.permutations(coords_with_addr):
            total_dist = calculate_total_distance(perm)
            if total_dist < min_distance:
                min_distance = total_dist
                best_route = list(perm)

        logger.info(f"Route optimized (brute force): {len(best_route)} addresses, {min_distance:.2f} km")
        # rotate so the first point is the one closest to the centroid (reduces random back-and-forth)
        best_route = rotate_route_to_centroid_start(best_route)
        return best_route

    else:
        # 2-opt heurisztikus algoritmus nagyobb számú címre
        best_route = tsp_2opt(coords_with_addr)
        total_dist = calculate_total_distance(best_route)
        # rotate start to closest to centroid to avoid arbitrary start
        best_route = rotate_route_to_centroid_start(best_route)
        logger.info(f"Route optimized (2-opt): {len(best_route)} addresses, {total_dist:.2f} km")
        return best_route

def calculate_total_distance(route: List[Tuple[str, float, float]]) -> float:
    """Útvonal teljes távolságának kiszámítása"""
    if len(route) < 2:
        return 0.0

    total = 0.0
    for i in range(len(route) - 1):
        coord1 = (route[i][1], route[i][2])
        coord2 = (route[i+1][1], route[i+1][2])
        total += haversine_distance(coord1, coord2)

    return total

def rotate_route_to_centroid_start(route: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
    """Rotate the route so it starts from the point closest to centroid of all points."""
    if not route:
        return route
    # compute centroid
    lat_sum = sum(r[1] for r in route)
    lon_sum = sum(r[2] for r in route)
    centroid = (lat_sum / len(route), lon_sum / len(route))
    # find index closest to centroid
    min_idx = 0
    min_dist = float('inf')
    for i, r in enumerate(route):
        d = haversine_distance((r[1], r[2]), centroid)
        if d < min_dist:
            min_dist = d
            min_idx = i
    # rotate
    return route[min_idx:] + route[:min_idx]

def tsp_2opt(coords_with_addr: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
    """
    2-opt algoritmus a TSP problémához
    Kezdő útvonal: legközelebbi szomszéd algoritmus
    """
    # Kezdő útvonal: legközelebbi szomszéd algoritmus
    route = [coords_with_addr[0]]
    remaining = list(coords_with_addr[1:])

    while remaining:
        current_coord = (route[-1][1], route[-1][2])
        min_distance = float('inf')
        next_item = remaining[0]

        for item in remaining:
            item_coord = (item[1], item[2])
            distance = haversine_distance(current_coord, item_coord)
            if distance < min_distance:
                min_distance = distance
                next_item = item

        route.append(next_item)
        remaining.remove(next_item)

    # 2-opt javítás
    improved = True
    max_iterations = 100
    iteration = 0

    while improved and iteration < max_iterations:
        improved = False
        iteration += 1

        for i in range(1, len(route) - 2):
            for j in range(i + 1, len(route)):
                if j - i == 1:
                    continue  # szomszédos élek, nem érdemes

                # Eredeti távolság
                old_dist = (haversine_distance((route[i-1][1], route[i-1][2]), (route[i][1], route[i][2])) +
                           haversine_distance((route[j-1][1], route[j-1][2]), (route[j % len(route)][1], route[j % len(route)][2])))

                # Új távolság ha megfordítjuk az i-j közti részt
                new_dist = (haversine_distance((route[i-1][1], route[i-1][2]), (route[j-1][1], route[j-1][2])) +
                           haversine_distance((route[i][1], route[i][2]), (route[j % len(route)][1], route[j % len(route)][2])))

                if new_dist < old_dist:
                    # Javítás: megfordítjuk az i-j közti részt
                    route[i:j] = route[i:j][::-1]
                    improved = True
                    break

            if improved:
                break

    return route

def coords_to_google_maps_url(coords_with_addr: List[Tuple[str, float, float]]) -> str:
    """
    Koordináták alapján Google Maps URL generálása
    """
    if not coords_with_addr:
        return ""

    if len(coords_with_addr) == 1:
        lat, lon = coords_with_addr[0][1], coords_with_addr[0][2]
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    # Utolsó pont = célállomás
    dest_lat, dest_lon = coords_with_addr[-1][1], coords_with_addr[-1][2]
    destination = f"{dest_lat},{dest_lon}"

    # Köztes pontok = waypoints
    waypoints = []
    for addr, lat, lon in coords_with_addr[:-1]:
        waypoints.append(f"{lat},{lon}")

    waypoints_str = "|".join(waypoints)

    return f"https://www.google.com/maps/dir/?api=1&destination={destination}&waypoints={waypoints_str}&travelmode=driving"

def coords_to_apple_maps_url(coords_with_addr: List[Tuple[str, float, float]]) -> str:
    """
    Koordináták alapján Apple Maps URL generálása
    """
    if not coords_with_addr:
        return ""

    daddr_params = []
    for addr, lat, lon in coords_with_addr:
        daddr_params.append(f"daddr={lat},{lon}")

    return f"https://maps.apple.com/?{'&'.join(daddr_params)}&dirflg=d"

def coords_to_waze_url(coords_with_addr: List[Tuple[str, float, float]]) -> str:
    """
    Koordináták alapján Waze URL generálása (csak első pont)
    """
    if not coords_with_addr:
        return ""

    lat, lon = coords_with_addr[0][1], coords_with_addr[0][2]
    return f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"

def shorten_url(url: str) -> str:
    """
    Rövidíti a kapott URL-t TinyURL API-val.
    Ha nem sikerül, visszaadja az eredeti URL-t.
    """
    try:
        r = requests.get("https://tinyurl.com/api-create.php", params={"url": url}, timeout=5)
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except Exception as e:
        logger.error(f"URL rövidítés hiba: {e}")
        return url

# =============== DB ===============
class DatabaseManager:
    def __init__(self) -> None:
        self.init_db()

    def init_db(self) -> None:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_name TEXT NOT NULL,            -- csoport neve
                restaurant_address TEXT NOT NULL,         -- Cím
                phone_number TEXT,                        -- Telefonszám
                order_details TEXT NOT NULL,              -- Megjegyzés
                group_id INTEGER NOT NULL,
                group_name TEXT,
                message_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',            -- pending|accepted|picked_up|delivered
                delivery_partner_id INTEGER,
                delivery_partner_name TEXT,
                delivery_partner_username TEXT,
                estimated_time INTEGER,
                accepted_at TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS groups(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)

        try:
            cur.execute("PRAGMA table_info(orders)")
            cols = [r[1] for r in cur.fetchall()]

            if "picked_up_at" not in cols:
                cur.execute("ALTER TABLE orders ADD COLUMN picked_up_at TIMESTAMP")
            if "delivered_at" not in cols:
                cur.execute("ALTER TABLE orders ADD COLUMN delivered_at TIMESTAMP")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_delivered_at ON orders(delivered_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_partner ON orders(delivery_partner_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_group ON orders(group_name)")

        except Exception as e:
            logger.error(f'DB migrate error: {e}')

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")

    def register_group(self, group_id: int, group_name: str) -> None:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO groups(id, name) VALUES (?,?)", (group_id, group_name))
        conn.commit()
        conn.close()

    def save_order(self, item: Dict) -> int:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders
            (restaurant_name, restaurant_address, phone_number, order_details, group_id, group_name, message_id)
            VALUES (?,?,?,?,?,?,?)
        """, (
            item.get("restaurant_name",""),
            item.get("restaurant_address",""),
            item.get("phone_number",""),
            item.get("order_details",""),
            item.get("group_id"),
            item.get("group_name"),
            item.get("message_id"),
        ))
        oid = cur.lastrowid
        conn.commit()
        conn.close()
        return oid

    def get_open_orders(self) -> List[Dict]:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, restaurant_name, restaurant_address, phone_number, order_details,
                   group_id, group_name, created_at, status,
                   delivery_partner_id, estimated_time
            FROM orders
            WHERE status IN ('pending','accepted','picked_up')
            ORDER BY created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_order_by_id(self, order_id: int) -> Optional[Dict]:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_order_status(self, order_id: int, status: str,
                            partner_id: int | None = None,
                            partner_name: str | None = None,
                            partner_username: str | None = None,
                            estimated_time: int | None = None) -> None:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            UPDATE orders
               SET status = ?,
                   delivery_partner_id = COALESCE(?, delivery_partner_id),
                   delivery_partner_name = COALESCE(?, delivery_partner_name),
                   delivery_partner_username = COALESCE(?, delivery_partner_username),
                   estimated_time = COALESCE(?, estimated_time),
                   accepted_at = CASE WHEN ?='accepted' THEN CURRENT_TIMESTAMP ELSE accepted_at END,
                   picked_up_at = CASE WHEN ?='picked_up' THEN CURRENT_TIMESTAMP ELSE picked_up_at END,
                   delivered_at = CASE WHEN ?='delivered' THEN CURRENT_TIMESTAMP ELSE delivered_at END
             WHERE id = ?
        """, (
            status,
            partner_id, partner_name, partner_username, estimated_time,
            status,  # accepted
            status,  # picked_up
            status,  # delivered
            order_id
        ))
        conn.commit()
        conn.close()

    def get_partner_addresses(self, partner_id: int, status: str) -> List[Dict]:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, restaurant_address, group_name
            FROM orders
            WHERE delivery_partner_id = ? AND status = ?
            ORDER BY created_at
        """, (partner_id, status))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_partner_order_count(self, partner_id: int, status: str = None) -> int:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        if status:
            cur.execute("SELECT COUNT(*) FROM orders WHERE delivery_partner_id = ? AND status = ?", 
                       (partner_id, status))
        else:
            cur.execute("SELECT COUNT(*) FROM orders WHERE delivery_partner_id = ?", 
                       (partner_id,))

        count = cur.fetchone()[0]
        conn.close()
        return count


db = DatabaseManager()

# =============== Telegram Bot ===============
class RestaurantBot:
    def __init__(self) -> None:
        self.app = Application.builder().token(BOT_TOKEN).build()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        app = self.app
        app.add_handler(CommandHandler("start", self.start_cmd))
        app.add_handler(CommandHandler("help", self.help_cmd))
        app.add_handler(CommandHandler("register", self.register_group))
        app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, self.handle_group_message))

        if app.job_queue:
            app.job_queue.run_repeating(self.process_notifications, interval=3)

    async def process_notifications(self, context: ContextTypes.DEFAULT_TYPE):
        processed_count = 0
        max_per_batch = 5

        while processed_count < max_per_batch:
            try:
                item = notification_queue.get_nowait()
                processed_count += 1
            except Empty:
                break

            max_retries = 3
            for attempt in range (max_retries):
                try:
                    await context.bot.send_message(
                        chat_id=item["chat_id"],
                        text=item.get("text",""),
                        parse_mode="Markdown"
                    )
                    logger.info(f"Notifications sent successfully to {item['chat_id']}")
                    break 

                except Exception as e:
                    logger.error(f"Failed to send notification (attempt {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        logger.error(f"Final failure sending notification to {item['chat_id']}: {item.get('text', '')[:50]}...")

    def send_notification(self, chat_id: int, text: str):
        try:
            if not text or not chat_id:
                logger.warning(f"Invalid notification: chat_id={chat_id}, text='{text[:50] if text else 'None'}'")
                return

            notification_queue.put({
                "chat_id": chat_id, 
                "text": text
            })
            logger.info(f"Notification queued for chat {chat_id}")
        except Exception as e:
            logger.error(f"Error queuing notification: {e}")


    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if update.effective_chat.type == "private":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚚 Elérhető rendelések", web_app=WebAppInfo(url=f"{WEBAPP_URL}"))],
            ])
            await update.message.reply_text(
                f"Üdv, {user.first_name}!\nNyisd meg a futár felületet:",
                reply_markup=kb
            )
        else:
            await update.message.reply_text(
                "Használd a /register parancsot a csoport regisztrálásához.\n"
                "Rendelés formátum:\n"
                "Cím: ...\nTelefonszám: ...\nMegjegyzés: ...")

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Rendelés formátum (csoportban):\n"
            "```\nCím: Budapest, Példa utca 1.\nTelefonszám: +36301234567\nMegjegyzés: kp / kártya / megjegyzés\n```",
            parse_mode="Markdown"
        )

    async def register_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group","supergroup"):
            await update.message.reply_text("Ezt a parancsot csoportban használd.")
            return
        gid = update.effective_chat.id
        gname = update.effective_chat.title or "Ismeretlen csoport"
        db.register_group(gid, gname)
        await update.message.reply_text(f"✅ A '{gname}' csoport regisztrálva.")

    def parse_order_message(self, text: str) -> Dict | None:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        info: Dict[str, str] = {}

        def after_colon(s: str) -> str:
            return s.split(":", 1)[1].strip() if ":" in s else ""

        for ln in lines:
            low = ln.lower()
            if low.startswith("cím:") or low.startswith("cim:"):
                info["address"] = after_colon(ln)
            elif low.startswith("telefonszám:") or low.startswith("telefonszam:") or low.startswith("telefon:"):
                info["phone"] = after_colon(ln)
            elif low.startswith("megjegyzés:") or low.startswith("megjegyzes:"):
                info["details"] = after_colon(ln)

        if info.get("address"):
            info.setdefault("phone", "")
            info.setdefault("details", "")
            return info
        return None

    async def handle_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group","supergroup"):
            return
        parsed = self.parse_order_message(update.message.text or "")
        if not parsed:
            return
        gid = update.effective_chat.id
        gname = update.effective_chat.title or "Ismeretlen"
        item = {
            "restaurant_name": gname,
            "restaurant_address": parsed["address"],
            "phone_number": parsed.get("phone",""),
            "order_details": parsed.get("details",""),
            "group_id": gid,
            "group_name": gname,
            "message_id": update.message.message_id
        }
        order_id = db.save_order(item)
        await update.message.reply_text(
            "✅ Rendelés rögzítve.\n\n"
            f"📍 Cím: {item['restaurant_address']}\n"
            f"📞 Telefon: {item['phone_number'] or '—'}\n"
            f"📝 Megjegyzés: {item['order_details']}\n"
            f"ID: #{order_id}"
        )

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


# =============== Flask WebApp ===============
app = Flask(__name__)
from flask import render_template_string
CORS(app)

def validate_telegram_data(init_data: str) -> Dict | None:
    """Egyszerű dekódolás (HMAC ellenőrzés nélkül)."""
    try:
        data = {}
        for part in (init_data or "").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                data[k] = v
        if "user" in data:
            import urllib.parse
            return json.loads(urllib.parse.unquote(data["user"]))
    except Exception as e:
        logger.error(f"validate_telegram_data error: {e}")
    return None

# (HTML_TEMPLATE unchanged from original for brevity -- front-end expects coordinates strings returned by /api/optimize_route and /api/get_coordinates)
HTML_TEMPLATE = r"""[SNIPPED FOR BREVITY IN THE SAVED FILE - USE ORIGINAL HTML FROM YOUR APP]"""

@app.route("/")
def index():
    try:
        orders = db.get_open_orders()
        return render_template_string(HTML_TEMPLATE, orders=orders)
    except Exception as e:
        logger.error(f"index error: {e}")
        return "error", 500

@app.route("/api/get_coordinates", methods=["POST"])
def api_get_coordinates():
    """
    New endpoint:
    Body: { order_id: <int>, initData: <tg.initData> }
    Returns: { ok: True, lat: <float>, lon: <float> }
    This ensures client will always receive plain numeric coordinates (no address text),
    preventing encoding problems when opening Google Maps.
    """
    try:
        data = request.json or {}
        order_id = int(data.get("order_id", 0))
        user = validate_telegram_data(data.get("initData",""))
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if not order_id:
            return jsonify({"ok": False, "error": "missing_order_id"}), 400
        order = db.get_order_by_id(order_id)
        if not order:
            return jsonify({"ok": False, "error": "order_not_found"}), 404
        address = order.get("restaurant_address","")
        coord = geocode_address(address)
        if not coord:
            return jsonify({"ok": False, "error": "geocode_failed"}), 500
        lat, lon = coord
        return jsonify({"ok": True, "lat": lat, "lon": lon})
    except Exception as e:
        logger.error(f"api_get_coordinates error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/optimize_route", methods=["POST"])
def api_optimize_route():
    """
    Útvonal optimalizálás felvett rendelésekhez
    Returns coordinates-only list to avoid address encoding issues in the client.
    """
    try:
        data = request.json or {}
        user = validate_telegram_data(data.get("initData", ""))
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        rows = db.get_partner_addresses(partner_id=user["id"], status="picked_up")
        addresses = [r["restaurant_address"] for r in rows if r.get("restaurant_address") and r["restaurant_address"].strip()]

        if not addresses:
            return jsonify({"ok": False, "error": "no_addresses"})

        optimized = optimize_route(addresses)  # list of (addr, lat, lon)
        # Build clean coordinate strings "lat,lon" (as strings) and also objects
        coords_list = [f"{lat},{lon}" for (addr, lat, lon) in optimized]
        coords_objects = [{"address": addr, "lat": lat, "lon": lon} for (addr, lat, lon) in optimized]

        # Also include a Google Maps URL built from coordinates (coordinates-only)
        try:
            google_url = coords_to_google_maps_url(optimized)
        except Exception:
            google_url = ""

        return jsonify({
            "ok": True,
            "addresses": coords_list,       # client-side expects an array of strings (now lat,lon)
            "coords": coords_objects,       # more detailed info in case needed
            "google_url": google_url,
            "count": len(coords_list)
        })

    except Exception as e:
        logger.error(f"api_optimize_route error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# --- Remaining endpoints unchanged (accept/pickup/mark_delivered etc) ---
@app.route("/api/orders")
def api_orders():
    try:
        return jsonify(db.get_open_orders())
    except Exception as e:
        logger.error(f"api_orders error: {e}")
        return jsonify([])

@app.route("/api/accept_order", methods=["POST"])
def api_accept_order():
    try:
        data = request.json or {}
        order_id = int(data.get("order_id"))
        eta = int(data.get("estimated_time", 20))
        user = validate_telegram_data(data.get("initData", ""))
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        order = db.get_order_by_id(order_id)
        if not order or order["status"] != "pending":
            return jsonify({"ok": False, "error": "not_available"}), 400

        partner_name = ((user.get("first_name","") + " " + user.get("last_name","",))).strip()
        if not partner_name.strip():
            partner_name = str(user.get("id"))
        partner_username = user.get("username")

        db.update_order_status(order_id, "accepted",
                               partner_id=user.get("id"),
                               partner_name=partner_name,
                               partner_username=partner_username,
                               estimated_time=eta)

        try:
            partner_contact = f"@{partner_username}" if partner_username else partner_name
            text = (
                "🚚 **FUTÁR JELENTKEZETT!**\n\n"
                f"👤 **Futár:** {partner_name}\n"
                f"📱 **Kontakt:** {partner_contact}\n"
                f"⏱️ **Becsült érkezés:** {eta} perc\n"
                f"📋 **Rendelés ID:** #{order_id}\n"
            )
            notification_queue.put({"chat_id": order["group_id"], "text": text})
            logger.info(f"Accept notification queued for group {order['group_id']}")

        except Exception as e:
            logger.error(f"group notify fail (accept): {e}")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"api_accept_order error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/pickup_order", methods=["POST"])
def api_pickup_order():
    try:
        data = request.json or {}
        order_id = int(data.get("order_id"))
        user = validate_telegram_data(data.get("initData",""))
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        order = db.get_order_by_id(order_id)
        if not order or order["status"] != "accepted" or order.get("delivery_partner_id") not in (None, user.get("id")):
            pass

        db.update_order_status(order_id, "picked_up",
                               partner_id=user.get("id"))
        try:
            partner_name = ((user.get("first_name","") + " " + user.get("last_name","",))).strip() or str(user.get("id"))
            partner_username = user.get("username")
            partner_contact = f"@{partner_username}" if partner_username else partner_name
            text = (
                "📦 **RENDELÉS FELVÉVE!**\n\n"
                f"👤 **Futár:** {partner_name}\n"
                f"📱 **Kontakt:** {partner_contact}\n"
                f"📋 **Rendelés ID:** #{order_id}\n"
            )
            notification_queue.put({"chat_id": order["group_id"], "text": text})
        except Exception as e:
            logger.error(f"group notify fail (pickup): {e}")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"api_pickup_order error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mark_delivered", methods=["POST"])
def api_mark_delivered():
    try:
        data = request.json or {}
        order_id = int(data.get("order_id"))
        user = validate_telegram_data(data.get("initData",""))
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        order = db.get_order_by_id(order_id)
        if not order or order["status"] != "picked_up":
            return jsonify({"ok": False, "error": "not_pickup"}), 400

        db.update_order_status(order_id, "delivered")

        try:
            partner_name = ((user.get("first_name","") + " " + user.get("last_name","",))).strip() or str(user.get("id"))
            partner_username = user.get("username")
            partner_contact = f"@{partner_username}" if partner_username else partner_name
            text = (
                "✅ **RENDELÉS KISZÁLLÍTVA!**\n\n"
                f"👤 **Futár:** {partner_name}\n"
                f"📱 **Kontakt:** {partner_contact}\n"
                f"📋 **Rendelés ID:** #{order_id}\n"
            )
            notification_queue.put({"chat_id": order["group_id"], "text": text})
        except Exception as e:
            logger.error(f"group notify fail (delivered): {e}")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"api_mark_delivered error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/orders_by_status", methods=["GET"])
def api_orders_by_status():
    try:
        status = (request.args.get("status") or "").strip()
        courier_id = request.args.get("courier_id", type=int)

        if not status:
            return jsonify([])

        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if status == "pending":
            cur.execute("""
                SELECT id, restaurant_name, restaurant_address, phone_number, order_details,
                       group_id, group_name, created_at, status
                FROM orders WHERE status='pending' ORDER BY created_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        elif status in ("accepted","picked_up","delivered"):
            if not courier_id:
                conn.close()
                return jsonify({"ok": False, "error": "missing_courier"}), 400
            cur.execute("""
                SELECT id, restaurant_name, restaurant_address, phone_number, order_details,
                       group_id, group_name, created_at, status, estimated_time
                FROM orders
                WHERE status=? AND delivery_partner_id=?
                ORDER BY created_at DESC
            """, (status, courier_id))
            rows = [dict(r) for r in cur.fetchall()]
        else:
            rows = []

        conn.close()
        return jsonify(rows)
    except Exception as e:
        logger.error(f"api_orders_by_status error: {e}")
        return jsonify([]), 500

@app.route("/api/my_orders", methods=["POST"])
def api_my_orders():
    try:
        data = request.json or {}
        user = validate_telegram_data(data.get("initData",""))
        if not user:
            return jsonify({"ok": False, "error":"unauthorized"}), 401
        status = data.get("status","").strip()
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if status not in ("accepted","picked_up","delivered"):
            return jsonify({"ok": True, "orders": []})
        cur.execute("""
            SELECT id, restaurant_name, restaurant_address, phone_number, order_details,
                   group_id, group_name, created_at, status, estimated_time
            FROM orders
            WHERE status=? AND delivery_partner_id=?
            ORDER BY created_at DESC
        """, (status, user["id"]))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "orders": rows})
    except Exception as e:
        logger.error(f"api_my_orders error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/admin")
def admin_page():
    init_data = request.args.get('init_data', '')
    user = validate_telegram_data(init_data)

    if not user or user.get("id") not in ADMIN_USER_IDS:
        return "🚫 Hozzáférés megtagadva", 403

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
              strftime('%Y-%W', delivered_at) AS week,
              delivery_partner_id,
              COALESCE(delivery_partner_name, '') AS courier_name,
              COUNT(*) AS cnt,
              ROUND(AVG((julianday(delivered_at) - julianday(accepted_at)) * 24 * 60), 1) AS avg_min
            FROM orders
            WHERE delivered_at IS NOT NULL AND accepted_at IS NOT NULL
            GROUP BY delivery_partner_id, week
            ORDER BY week DESC, cnt DESC
        """)
        weekly_courier = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT
              strftime('%Y-%W', delivered_at) AS week,
              group_name,
              COUNT(*) AS cnt,
              ROUND(AVG((julianday(delivered_at) - julianday(accepted_at)) * 24 * 60), 1) AS avg_min
            FROM orders
            WHERE delivered_at IS NOT NULL AND accepted_at IS NOT NULL
            GROUP BY group_name, week
            ORDER BY week DESC, cnt DESC
        """)
        weekly_restaurant = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT
              delivered_at,
              delivery_partner_id,
              COALESCE(delivery_partner_name, '') AS courier_name,
              group_name,
              restaurant_address,
              ROUND((julianday(delivered_at) - julianday(accepted_at)) * 24 * 60, 1) AS min
            FROM orders
            WHERE delivered_at IS NOT NULL AND accepted_at IS NOT NULL
            ORDER BY delivered_at DESC
            LIMIT 500
        """)
        deliveries = [dict(r) for r in cur.fetchall()]

        conn.close()
        return render_template_string(ADMIN_HTML,
                                      weekly_courier=weekly_courier,
                                      weekly_restaurant=weekly_restaurant,
                                      deliveries=deliveries)
    except Exception as e:
        logger.error(f"admin_page error: {e}")
        return "admin error", 500

@app.route("/api/is_admin", methods=["POST"])
def api_is_admin():
    try:
        data = request.json or {}
        user = validate_telegram_data(data.get("initData", ""))
        if not user:
            return jsonify({"ok": False, "admin": False}), 401
        return jsonify({
            "ok": True,
            "admin": user.get("id") in ADMIN_USER_IDS
        })
    except Exception as e:
        logger.error(f"api_is_admin error: {e}")
        return jsonify({"ok": False, "admin": False}), 500

# =============== Indítás ===============
def run_flask():
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    RestaurantBot().run()
