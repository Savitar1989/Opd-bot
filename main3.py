import os
import logging
import sqlite3
import json
import threading
from typing import Dict, List
from queue import Queue, Empty

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Config ----------------
BOT_TOKEN = "IDE_ÍRD_A_BOT_TOKENED"       # <-- tedd be a saját tokened!
WEBAPP_URL = "http://localhost:5000"      # iPad/ngrok esetén állítsd a HTTPS ngrok URL-re
DB_NAME = "restaurant_orders.db"

# ---------------- Queue: éttermi értesítések ----------------
notification_queue: "Queue[Dict]" = Queue()

# ---------------- Database ----------------
class DatabaseManager:
    def __init__(self) -> None:
        self.init_db()

    def init_db(self) -> None:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_name TEXT NOT NULL,           -- csoport neve
                restaurant_address TEXT NOT NULL,        -- "Rendelő neve" mező
                phone_number TEXT,
                order_details TEXT NOT NULL,             -- "Megjegyzés"
                group_id INTEGER NOT NULL,
                group_name TEXT,
                message_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',           -- pending, accepted, picked_up, delivered
                delivery_partner_id INTEGER,
                delivery_partner_name TEXT,
                delivery_partner_username TEXT,
                estimated_time INTEGER,
                accepted_at TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS registered_groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
        logger.info(f"DB path: {os.path.abspath(DB_NAME)}")

    def add_order(self, restaurant_name: str, address: str, phone: str,
                  details: str, group_id: int, group_name: str, message_id: int) -> int:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orders (restaurant_name, restaurant_address, phone_number, order_details, group_id, group_name, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (restaurant_name, address, phone, details, group_id, group_name, message_id)
        )
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return order_id

    def get_active_orders(self) -> List[Dict]:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, restaurant_name, restaurant_address, phone_number, order_details, created_at, status, group_name, group_id "
            "FROM orders WHERE status IN ('pending','accepted') ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        conn.close()
        orders: List[Dict] = []
        for row in rows:
            orders.append({
                "id": row[0],
                "restaurant_name": row[1],
                "restaurant_address": row[2],
                "phone_number": row[3],
                "order_details": row[4],
                "created_at": row[5],
                "status": row[6],
                "group_name": row[7],
                "group_id": row[8],
            })
        return orders

    def get_courier_orders(self, courier_id: int, status: str) -> List[Dict]:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, restaurant_name, restaurant_address, phone_number, order_details, created_at, accepted_at, status, group_name "
            "FROM orders WHERE delivery_partner_id=? AND status=? "
            "ORDER BY accepted_at DESC, created_at DESC",
            (courier_id, status)
        )
        rows = cursor.fetchall()
        conn.close()
        orders: List[Dict] = []
        for row in rows:
            orders.append({
                "id": row[0],
                "restaurant_name": row[1],
                "restaurant_address": row[2],
                "phone_number": row[3],
                "order_details": row[4],
                "created_at": row[5],
                "accepted_at": row[6],
                "status": row[7],
                "group_name": row[8],
            })
        return orders

    def get_order_by_id(self, order_id: int) -> Dict | None:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, restaurant_name, restaurant_address, phone_number, order_details, group_id, group_name, created_at, status, "
            "delivery_partner_id, delivery_partner_name, delivery_partner_username, estimated_time, accepted_at "
            "FROM orders WHERE id = ?",
            (order_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "id": row[0],
                "restaurant_name": row[1],
                "restaurant_address": row[2],
                "phone_number": row[3],
                "order_details": row[4],
                "group_id": row[5],
                "group_name": row[6],
                "created_at": row[7],
                "status": row[8],
                "delivery_partner_id": row[9],
                "delivery_partner_name": row[10],
                "delivery_partner_username": row[11],
                "estimated_time": row[12],
                "accepted_at": row[13],
            }
        return None

    def update_order_status(self, order_id: int, status: str,
                            partner_id: int | None = None,
                            partner_name: str | None = None,
                            partner_username: str | None = None,
                            estimated_time: int | None = None) -> None:
        """Status update; accepted_at csak 'accepted' esetén frissül."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE orders SET "
            "status=?, "
            "delivery_partner_id=COALESCE(?,delivery_partner_id), "
            "delivery_partner_name=COALESCE(?,delivery_partner_name), "
            "delivery_partner_username=COALESCE(?,delivery_partner_username), "
            "estimated_time=COALESCE(?,estimated_time), "
            "accepted_at=CASE WHEN ?='accepted' THEN CURRENT_TIMESTAMP ELSE accepted_at END "
            "WHERE id=?",
            (status, partner_id, partner_name, partner_username, estimated_time, status, order_id)
        )
        conn.commit()
        conn.close()

    def register_group(self, group_id: int, group_name: str) -> None:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO registered_groups (group_id, group_name) VALUES (?,?)", (group_id, group_name))
        conn.commit()
        conn.close()


db = DatabaseManager()

# ---------------- Telegram Bot ----------------
class RestaurantBot:
    def __init__(self) -> None:
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()

    async def process_notifications(self, context: ContextTypes.DEFAULT_TYPE):
        while True:
            try:
                item = notification_queue.get_nowait()
            except Empty:
                break
            chat_id = item.get("chat_id")
            text = item.get("text", "")
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Send notification failed: {e}")

    def setup_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CommandHandler("register", self.register_group))
        app.add_handler(CommandHandler("orders", self.show_orders_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        if app.job_queue:
            app.job_queue.run_repeating(self.process_notifications, interval=5)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if update.effective_chat.type == "private":
            keyboard = [[InlineKeyboardButton("🚚 Elérhető rendelések", web_app=WebAppInfo(url=f"{WEBAPP_URL}?tab=available"))],
                        [InlineKeyboardButton("📦 Felvett rendelések", web_app=WebAppInfo(url=f"{WEBAPP_URL}?tab=picked"))],
                        [InlineKeyboardButton("✅ Kiszállítottak", web_app=WebAppInfo(url=f"{WEBAPP_URL}?tab=delivered"))]]
            await update.message.reply_text(
                f"🍕 Üdv, {user.first_name}!\nVálassz nézetet:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                "🍕 Üdv! Használd a /register parancsot a csoport regisztrálásához.\n"
                "Rendelés formátum:\nRendelő neve: ...\nTelefonszám: ...\nMegjegyzés: ..."
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 **Parancsok**\n\n"
            "**/start** – Kezdő menü\n"
            "**/register** – Csoport regisztrálása (csak csoportban)\n"
            "**/orders** – Elérhető rendelések listája (privátban)\n",
            parse_mode="Markdown"
        )

    async def show_orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            await update.message.reply_text("Ez a parancs csak privát chatben használható.")
            return
        orders = db.get_active_orders()
        if not orders:
            await update.message.reply_text("🤷‍♀️ Jelenleg nincsenek elérhető rendelések.")
            return
        for order in orders[:10]:
            if order["status"] == "accepted":
                keyboard = [[InlineKeyboardButton("✅ Felvettem", callback_data=f"pickup_{order['id']}")]]
            else:
                keyboard = [[
                    InlineKeyboardButton("⏱️ 10 perc", callback_data=f"accept_{order['id']}_10"),
                    InlineKeyboardButton("⏱️ 20 perc", callback_data=f"accept_{order['id']}_20"),
                    InlineKeyboardButton("⏱️ 30 perc", callback_data=f"accept_{order['id']}_30"),
                ]]
            text = (
                f"🏢 **Csoport:** {order['group_name'] or 'Ismeretlen'}\n"
                f"👤 **Rendelő:** {order['restaurant_address']}\n"
                f"📞 **Telefon:** {order['phone_number'] or '—'}\n"
                f"📝 **Megjegyzés:** {order['order_details']}\n"
                f"📅 **Idő:** {order['created_at']}\n\n"
                f"**Rendelés ID:** #{order['id']}"
            )
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    async def register_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group","supergroup"):
            await update.message.reply_text("❌ Ezt csak csoportban lehet.")
            return
        gid = update.effective_chat.id
        gname = update.effective_chat.title or "Ismeretlen csoport"
        db.register_group(gid, gname)
        await update.message.reply_text(f"✅ A '{gname}' csoport regisztrálva.")

    def parse_order_message(self, text: str) -> Dict | None:
        """
        STRICT formátum:
        Rendelő neve: <név/cím>
        Telefonszám: <telefon>
        Megjegyzés: <megjegyzés>
        """
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        info: Dict[str,str] = {}
        def after_colon(s: str) -> str:
            return s.split(':',1)[1].strip() if ':' in s else ''
        for ln in lines:
            low = ln.lower()
            if low.startswith('rendelő neve:') or low.startswith('rendelo neve:') or low.startswith('rendelő:') or low.startswith('rendelo:'):
                info['address'] = after_colon(ln)           # rendelő neve/címe
            elif low.startswith('telefonszám:') or low.startswith('telefonszam:') or low.startswith('telefon:'):
                info['phone'] = after_colon(ln)
            elif low.startswith('megjegyzés:') or low.startswith('megjegyzes:') or low.startswith('megjegy:'):
                info['details'] = after_colon(ln)
        if 'address' in info:
            info.setdefault('phone','')
            info.setdefault('details','')
            return info
        return None

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group","supergroup"):
            return
        parsed = self.parse_order_message(update.message.text or "")
        if not parsed:
            return
        group_name = update.effective_chat.title or "Ismeretlen csoport"
        try:
            order_id = db.add_order(
                restaurant_name=group_name,
                address=parsed['address'],
                phone=parsed.get('phone',''),
                details=parsed['details'],
                group_id=update.effective_chat.id,
                group_name=group_name,
                message_id=update.message.message_id
            )
            await update.message.reply_text(
                "✅ **Rendelés rögzítve!**\n\n"
                f"👤 **Rendelő:** {parsed['address']}\n"
                f"📞 **Telefon:** {parsed.get('phone','—')}\n"
                f"📝 **Megjegyzés:** {parsed['details']}\n\n"
                f"**Rendelés ID:** #{order_id}\n**Állapot:** Futárra vár",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Save order failed: {e}")
            await update.message.reply_text("❌ Hiba történt a rendelés rögzítésekor!")

    def _partner_name(self, user) -> str:
        return user.first_name + (f" {user.last_name}" if getattr(user, "last_name", None) else "")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        user = query.from_user

        if data.startswith("accept_"):
            _, sid, seta = data.split("_")
            order_id = int(sid); eta = int(seta)
            order = db.get_order_by_id(order_id)
            if not order or order['status'] != 'pending':
                await query.edit_message_text("❌ Ez a rendelés már nem elérhető vagy elfogadták.")
                return
            partner_name = self._partner_name(user)
            db.update_order_status(order_id=order_id, status='accepted',
                                   partner_id=user.id,
                                   partner_name=partner_name,
                                   partner_username=user.username,
                                   estimated_time=eta)
            await query.edit_message_text(
                f"✅ **Rendelés elfogadva!**\n\n"
                f"🏢 **Csoport:** {order['group_name']}\n"
                f"📍 **Rendelő:** {order['restaurant_address']}\n"
                f"⏱️ **Becsült érkezés:** {eta} perc\n"
                f"**Rendelés ID:** #{order_id}\n\n"
                f"Nyomd meg a 'Felvettem' gombot, amikor átvetted a rendelést.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Felvettem", callback_data=f"pickup_{order_id}")]])
            )
            # értesítés az éttermi csoportnak (minimál infó)
            try:
                partner_contact = f"@{user.username}" if user.username else partner_name
                text = (
                    "🚚 **FUTÁR JELENTKEZETT!**\n\n"
                    f"👤 **Futár:** {partner_name}\n"
                    f"📱 **Kontakt:** {partner_contact}\n"
                    f"⏱️ **Becsült érkezés:** {eta} perc\n"
                    f"📋 **Rendelés ID:** #{order_id}\n\n"
                    "A futár hamarosan érkezik! 🍕➡️🚚"
                )
                await context.bot.send_message(chat_id=order['group_id'], text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Group notify failed: {e}")

        elif data.startswith("pickup_"):
            order_id = int(data.split("_")[1])
            order = db.get_order_by_id(order_id)
            if not order:
                await query.edit_message_text("❌ Ez a rendelés már nem található.")
                return
            db.update_order_status(order_id=order_id, status='picked_up')
            await query.edit_message_text(
                f"✅ **Rendelés felvéve!**\n\n"
                f"🏢 **Csoport:** {order['group_name']}\n"
                f"📋 **Rendelés ID:** #{order_id}\n\n"
                f"Indulhatsz a címre! 🚚💨",
                parse_mode="Markdown"
            )
            try:
                partner_name = self._partner_name(user)
                partner_contact = f"@{user.username}" if user.username else partner_name
                text = (
                    "📦 **RENDELÉS FELVÉVE!**\n\n"
                    f"👤 **Futár:** {partner_name}\n"
                    f"📱 **Kontakt:** {partner_contact}\n"
                    f"📋 **Rendelés ID:** #{order_id}\n"
                )
                await context.bot.send_message(chat_id=order['group_id'], text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Pickup notify failed: {e}")

    def run(self) -> None:
        logger.info(f"WEBAPP_URL: {WEBAPP_URL}")
        self.application.run_polling()

# ---------------- Flask Web App ----------------
app = Flask(__name__)
CORS(app)

def validate_telegram_data(init_data: str) -> Dict | None:
    try:
        data = {}
        for item in (init_data or "").split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                data[k] = v
        if "user" in data:
            import urllib.parse
            user = json.loads(urllib.parse.unquote(data["user"]))
            return user
        return None
    except Exception as e:
        logger.error(f"validate_telegram_data error: {e}")
        return None

@app.route("/")
def index():
    try:
        initial_tab = request.args.get("tab", "available")
        orders = db.get_active_orders()
        return render_template("index.html", orders=orders, initial_tab=initial_tab)
    except Exception as e:
        logger.error(f"index error: {e}")
        return f"Error: {e}", 500

@app.route("/api/orders")
def api_orders():
    try:
        return jsonify(db.get_active_orders())
    except Exception as e:
        logger.error(f"api_orders error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/my_orders", methods=["POST"])
def my_orders():
    try:
        data = request.json or {}
        tgdata = data.get("telegram_data")
        if not tgdata:
            return jsonify({"error": "Hiányzó Telegram initData"}), 400
        user = validate_telegram_data(tgdata)
        if not user:
            return jsonify({"error": "Érvénytelen Telegram adat"}), 400
        status = data.get("status", "picked_up")
        orders = db.get_courier_orders(user["id"], status)
        return jsonify(orders)
    except Exception as e:
        logger.error(f"my_orders error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/accept_order", methods=["POST"])
def accept_order():
    try:
        data = request.json or {}
        order_id = data.get("order_id")
        eta = data.get("estimated_time")
        tgdata = data.get("telegram_data")
        if not all([order_id, eta, tgdata]):
            return jsonify({"error": "Hiányzó adatok"}), 400
        user = validate_telegram_data(tgdata)
        if not user:
            return jsonify({"error": "Érvénytelen Telegram adat"}), 400
        order = db.get_order_by_id(int(order_id))
        if not order or order["status"] != "pending":
            return jsonify({"error": "Ez a rendelés már nem elérhető"}), 400

        partner_name = user["first_name"] + (f" {user.get('last_name','')}" if user.get("last_name") else "")
        partner_contact = f"@{user.get('username')}" if user.get("username") else partner_name

        db.update_order_status(order_id=int(order_id), status='accepted',
                               partner_id=user["id"], partner_name=partner_name,
                               partner_username=user.get("username"), estimated_time=int(eta))

        text = (
            "🚚 **FUTÁR JELENTKEZETT!**\n\n"
            f"👤 **Futár:** {partner_name}\n"
            f"📱 **Kontakt:** {partner_contact}\n"
            f"⏱️ **Becsült érkezés:** {eta} perc\n"
            f"📋 **Rendelés ID:** #{order_id}\n\n"
            "A futár hamarosan érkezik! 🍕➡️🚚"
        )
        notification_queue.put({"chat_id": order["group_id"], "text": text})
        return jsonify({"success": True, "message": "Rendelés elfogadva!", "order_accepted": True})
    except Exception as e:
        logger.error(f"accept_order error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/pickup_order", methods=["POST"])
def pickup_order():
    try:
        data = request.json or {}
        order_id = data.get("order_id")
        tgdata = data.get("telegram_data")
        if not all([order_id, tgdata]):
            return jsonify({"error": "Hiányzó adatok"}), 400
        user = validate_telegram_data(tgdata)
        if not user:
            return jsonify({"error": "Érvénytelen Telegram adat"}), 400
        order = db.get_order_by_id(int(order_id))
        if not order or order["status"] != "accepted":
            return jsonify({"error": "Ez a rendelés nem felvehető"}), 400

        partner_name = user["first_name"] + (f" {user.get('last_name','')}" if user.get("last_name") else "")
        partner_contact = f"@{user.get('username')}" if user.get("username") else partner_name

        db.update_order_status(order_id=int(order_id), status='picked_up')

        text = (
            "📦 **RENDELÉS FELVÉVE!**\n\n"
            f"👤 **Futár:** {partner_name}\n"
            f"📱 **Kontakt:** {partner_contact}\n"
            f"📋 **Rendelés ID:** #{order_id}\n"
        )
        notification_queue.put({"chat_id": order["group_id"], "text": text})
        return jsonify({"success": True, "message": "Rendelés felvéve!", "order_completed": True})
    except Exception as e:
        logger.error(f"pickup_order error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/mark_delivered", methods=["POST"])
def mark_delivered():
    """Kiszállítva státusz beállítása a futár által (csak a saját, picked_up rendeléseire)."""
    try:
        data = request.json or {}
        order_id = data.get("order_id")
        tgdata = data.get("telegram_data")
        if not all([order_id, tgdata]):
            return jsonify({"error": "Hiányzó adatok"}), 400
        user = validate_telegram_data(tgdata)
        if not user:
            return jsonify({"error": "Érvénytelen Telegram adat"}), 400
        order = db.get_order_by_id(int(order_id))
        if not order or order["status"] != "picked_up" or order.get("delivery_partner_id") not in (None, user["id"]):
            return jsonify({"error": "Nem jelölhető kiszállítottnak"}), 400

        db.update_order_status(order_id=int(order_id), status='delivered')

        # értesítés éttermi csoportnak opcionálisan
        try:
            text = (
                "✅ **KISZÁLLÍTVA!**\n\n"
                f"📋 **Rendelés ID:** #{order_id}\n"
            )
            notification_queue.put({"chat_id": order["group_id"], "text": text})
        except Exception:
            pass

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"mark_delivered error: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------- HTML sablon (régi kinézet + tabs + delivered + route) ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Éttermi Rendelések - Futár</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{font-family:system-ui,Roboto,Arial,sans-serif;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#000);padding:16px}
.container{max-width:640px;margin:0 auto}
.header{padding:16px;border-radius:12px;background:var(--tg-theme-secondary-bg-color,#f5f5f5);text-align:center;margin-bottom:16px}
.order-card{border:1px solid var(--tg-theme-hint-color,#ddd);border-radius:12px;padding:16px;margin-bottom:12px;background:var(--tg-theme-secondary-bg-color,#fafafa)}
.restaurant-name{font-weight:700;margin-bottom:4px}
.time-buttons{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.time-btn,.accept-btn{border:0;border-radius:10px;padding:12px;cursor:pointer}
.time-btn{background:var(--tg-theme-button-color,#007bff);color:var(--tg-theme-button-text-color,#fff)}
/* kiválasztott idő gomb */
.time-btn.selected{
  background: var(--tg-theme-bg-color, #fff);
  color: var(--tg-theme-button-color, #007bff);
  border: 2px solid var(--tg-theme-button-color, #007bff);
  font-weight: 600;
}
.accept-btn{background:#28a745;color:#fff;width:100%}
.secondary-btn{background:#6c757d;color:#fff;width:100%;border:0;border-radius:10px;padding:12px;cursor:pointer}
.success-message,.error-message{display:none;padding:12px;border-radius:10px;margin:10px 0}
.success-message{background:#d4edda;color:#155724}
.error-message{background:#f8d7da;color:#721c24}
.detail-label{font-weight:600}
.timestamp{color:var(--tg-theme-hint-color,#777);font-size:.9rem;margin-top:6px}

/* Tabs */
.tabs{display:flex;gap:8px;justify-content:center;margin-top:8px}
.tab-btn{background:transparent;border:1px solid var(--tg-theme-hint-color,#bbb);padding:8px 12px;border-radius:999px;cursor:pointer}
.tab-btn.active{background:var(--tg-theme-button-color,#007bff);color:var(--tg-theme-button-text-color,#fff);border-color:var(--tg-theme-button-color,#007bff)}
.route-row{display:flex;gap:8px;align-items:center;justify-content:center;margin-bottom:10px}
.route-btn{background:#1a73e8;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h2>🍕 Futár felület</h2>
    <p>Válaszd ki az időt, fogadd el, jelöld felvettnek – majd kiszállítottnak.</p>
    <div class="tabs">
      <button class="tab-btn" id="tab-available" onclick="setActiveTab('available')">Elérhető</button>
      <button class="tab-btn" id="tab-picked" onclick="setActiveTab('picked')">Felvett</button>
      <button class="tab-btn" id="tab-delivered" onclick="setActiveTab('delivered')">Kiszállított</button>
    </div>
  </div>

  <div class="success-message" id="successMessage"></div>
  <div class="error-message" id="errorMessage"></div>
  <div id="loading">Betöltés...</div>

  <!-- Available -->
  <div id="ordersContainer" style="display:none"></div>
  <div id="noOrders" style="display:none">Nincs rendelés.</div>

  <!-- Picked -->
  <div id="pickedTop" class="route-row" style="display:none">
    <button class="route-btn" onclick="openRouteForPicked()">🗺️ Útvonal az összes felvett címhez</button>
  </div>
  <div id="pickedContainer" style="display:none"></div>
  <div id="noPicked" style="display:none">Még nincs felvett rendelésed.</div>

  <!-- Delivered -->
  <div id="deliveredContainer" style="display:none"></div>
  <div id="noDelivered" style="display:none">Még nincs kiszállított rendelésed.</div>
</div>
<script>
let tg = window.Telegram.WebApp; tg.ready(); tg.expand();
let currentUser = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user : null;
let selectedTimes = {};
const API_BASE = window.location.origin;
let pollTimer = null;
const initialTab = (new URLSearchParams(window.location.search).get('tab') || '{{ initial_tab|default("available") }}');

function encode(s){return encodeURIComponent(s||'');}
function buildMultiStop(addresses){
  if(!addresses || !addresses.length) return '';
  const origin = 'My+Location';
  if(addresses.length===1){
    const dest = encode(addresses[0]);
    return `https://www.google.com/maps/dir/?api=1&origin=${origin}&destination=${dest}&travelmode=driving`;
  }
  const dest = encode(addresses[addresses.length-1]);
  const waypoints = addresses.slice(0,-1).slice(0,23).map(encode).join('|');
  let url = `https://www.google.com/maps/dir/?api=1&origin=${origin}&destination=${dest}&travelmode=driving`;
  if(waypoints) url += `&waypoints=${waypoints}`;
  return url;
}

async function loadOrders(){
  try{
    document.getElementById('loading').style.display='block';
    const res = await fetch(`${API_BASE}/api/orders`);
    const orders = await res.json();
    document.getElementById('loading').style.display='none';
    const container = document.getElementById('ordersContainer');
    if(!orders.length){ container.style.display='none'; document.getElementById('noOrders').style.display='block'; return;}
    container.style.display='block'; document.getElementById('noOrders').style.display='none';
    container.innerHTML = orders.map(order => `
      <div class="order-card">
        <div class="restaurant-name">${order.group_name || order.restaurant_name}</div>
        <div><span class="detail-label">Rendelő:</span> ${order.restaurant_address}</div>
        ${order.phone_number ? `<div><span class="detail-label">Telefonszám:</span> ${order.phone_number}</div>` : ''}
        <div><span class="detail-label">Megjegyzés:</span> ${order.order_details}</div>
        <div class="timestamp">${new Date(order.created_at).toLocaleString('hu-HU')}</div>

        <div class="time-buttons" style="${order.status==='accepted'?'display:none;':'display:grid;'}">
          <button class="time-btn" data-order="${order.id}" data-time="10">⏱️ 10 perc</button>
          <button class="time-btn" data-order="${order.id}" data-time="20">⏱️ 20 perc</button>
          <button class="time-btn" data-order="${order.id}" data-time="30">⏱️ 30 perc</button>
        </div>

        <button class="accept-btn"
          onclick="${order.status==='accepted'?`pickupOrder(${order.id})`:`acceptOrder(${order.id})`}"
          id="${order.status==='accepted'?`pickup-${order.id}`:`accept-${order.id}`}"
          ${order.status==='accepted'?'': 'disabled'}>
          ${order.status==='accepted'?'✅ Felvettem':'🚚 Rendelés elfogadása'}
        </button>
      </div>
    `).join('');

    document.querySelectorAll('.time-btn').forEach(btn=>{
      btn.addEventListener('click', function(){
        const oid = this.dataset.order, t = this.dataset.time;
        document.querySelectorAll(`[data-order="${oid}"]`).forEach(b=>b.classList.remove('selected'));
        this.classList.add('selected'); selectedTimes[oid]=t;
        const abtn = document.getElementById(`accept-${oid}`); if(abtn) abtn.disabled=false;
        if(tg.HapticFeedback){ tg.HapticFeedback.impactOccurred('light'); }
      });
    });
  }catch(err){
    document.getElementById('loading').style.display='none';
    showError('Hiba a rendelések betöltésekor');
  }
}

async function acceptOrder(orderId){
  const eta = selectedTimes[orderId]; if(!eta) return showError('Válassz időt!');
  if(!currentUser) return showError('Telegram adatok nem érhetők el');
  const btn = document.getElementById(`accept-${orderId}`);
  btn.disabled=true; const old=btn.innerHTML; btn.innerHTML='⏳ Feldolgozás...';
  try{
    const r = await fetch(`${API_BASE}/api/accept_order`,{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({order_id:orderId, estimated_time:eta, telegram_data: tg.initData})});
    const j = await r.json(); if(!j.success) throw new Error(j.error||'Ismeretlen hiba');
    showSuccess('Elfogadva, ' + eta + ' perc.');
    const card = btn.closest('.order-card'); const tb = card.querySelector('.time-buttons'); if(tb) tb.remove();
    btn.outerHTML = `<button class="accept-btn" onclick="pickupOrder(${orderId})" id="pickup-${orderId}">✅ Felvettem</button>`;
    if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  }catch(e){ showError('Hiba: '+e.message); btn.disabled=false; btn.innerHTML=old; if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error'); }
}

async function pickupOrder(orderId){
  if(!currentUser) return showError('Telegram adatok nem érhetők el');
  const btn = document.getElementById(`pickup-${orderId}`); if(btn){btn.disabled=true; btn.innerHTML='⏳ Feldolgozás...';}
  try{
    const r = await fetch(`${API_BASE}/api/pickup_order`,{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({order_id:orderId, telegram_data: tg.initData})});
    const j = await r.json(); if(!j.success) throw new Error(j.error||'Ismeretlen hiba');
    const card = btn.closest('.order-card'); if(card){ card.style.opacity='0'; setTimeout(()=>card.remove(), 350); }
    showSuccess('✅ Rendelés felvéve!'); if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  }catch(e){ showError('Hiba: '+e.message); if(btn){btn.disabled=false;} if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error'); }
}

/* Felvett (picked_up) lista */
async function loadPicked(){
  try{
    const res = await fetch(`${API_BASE}/api/my_orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telegram_data: tg.initData, status: 'picked_up' })
    });
    const orders = await res.json();
    const cont = document.getElementById('pickedContainer');
    const top = document.getElementById('pickedTop');
    if (!orders.length){
      cont.style.display = 'none'; top.style.display='none';
      document.getElementById('noPicked').style.display = 'block';
      return;
    }
    cont.style.display = 'block'; top.style.display='flex';
    document.getElementById('noPicked').style.display = 'none';
    cont.innerHTML = orders.map(o => `
      <div class="order-card">
        <div class="restaurant-name">${o.group_name || o.restaurant_name}</div>
        <div><span class="detail-label">Rendelő:</span> ${o.restaurant_address}</div>
        ${o.phone_number ? `<div><span class="detail-label">Telefonszám:</span> ${o.phone_number}</div>` : ''}
        <div><span class="detail-label">Megjegyzés:</span> ${o.order_details}</div>
        <div><span class="detail-label">Rendelés ID:</span> #${o.id}</div>
        <div class="timestamp">Felvéve: ${new Date(o.accepted_at || o.created_at).toLocaleString('hu-HU')}</div>
        <button class="secondary-btn" onclick="markDelivered(${o.id})">✅ Kiszállítva</button>
      </div>
    `).join('');
  }catch(e){
    showError('Hiba a felvett rendelések betöltésekor');
  }
}

async function markDelivered(orderId){
  try{
    const r = await fetch(`${API_BASE}/api/mark_delivered`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ order_id: orderId, telegram_data: tg.initData })
    });
    const j = await r.json();
    if(!j.success) throw new Error(j.error||'Ismeretlen hiba');
    showSuccess('✅ Kiszállítva jelölve.');
    loadPicked();
  }catch(e){
    showError('Hiba: '+e.message);
  }
}

async function openRouteForPicked(){
  try{
    const res = await fetch(`${API_BASE}/api/my_orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telegram_data: tg.initData, status: 'picked_up' })
    });
    const orders = await res.json();
    const addresses = (orders||[]).map(o=>o.restaurant_address).filter(Boolean);
    if(!addresses.length){ showError('Nincs felvett címed.'); return; }
    const url = buildMultiStop(addresses);
    if(!url){ showError('Nem sikerült útvonalat készíteni.'); return; }
    window.open(url, '_blank');
  }catch(e){
    showError('Hiba az útvonal készítésekor');
  }
}

/* Kiszállított lista */
async function loadDelivered(){
  try{
    const res = await fetch(`${API_BASE}/api/my_orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telegram_data: tg.initData, status: 'delivered' })
    });
    const orders = await res.json();
    const cont = document.getElementById('deliveredContainer');
    if (!orders.length){
      cont.style.display = 'none';
      document.getElementById('noDelivered').style.display = 'block';
      return;
    }
    cont.style.display = 'block';
    document.getElementById('noDelivered').style.display = 'none';
    cont.innerHTML = orders.map(o => `
      <div class="order-card">
        <div class="restaurant-name">${o.group_name || o.restaurant_name}</div>
        <div><span class="detail-label">Rendelő:</span> ${o.restaurant_address}</div>
        ${o.phone_number ? `<div><span class="detail-label">Telefonszám:</span> ${o.phone_number}</div>` : ''}
        <div><span class="detail-label">Megjegyzés:</span> ${o.order_details}</div>
        <div><span class="detail-label">Rendelés ID:</span> #${o.id}</div>
        <div class="timestamp">Lezárva: ${new Date(o.accepted_at || o.created_at).toLocaleString('hu-HU')}</div>
      </div>
    `).join('');
  }catch(e){
    showError('Hiba a kiszállított rendelések betöltésekor');
  }
}

/* Tabs + polling */
function setActiveTab(name){
  document.getElementById('tab-available').classList.toggle('active', name==='available');
  document.getElementById('tab-picked').classList.toggle('active', name==='picked');
  document.getElementById('tab-delivered').classList.toggle('active', name==='delivered');

  document.getElementById('ordersContainer').style.display = (name==='available') ? 'block' : 'none';
  document.getElementById('noOrders').style.display = 'none';

  document.getElementById('pickedContainer').style.display = (name==='picked') ? 'block' : 'none';
  document.getElementById('noPicked').style.display = 'none';
  document.getElementById('pickedTop').style.display = (name==='picked') ? 'flex' : 'none';

  document.getElementById('deliveredContainer').style.display = (name==='delivered') ? 'block' : 'none';
  document.getElementById('noDelivered').style.display = 'none';

  if (name==='available'){
    loadOrders(); startPolling();
  } else {
    if (pollTimer) { clearInterval(pollTimer); pollTimer=null; }
    if (name==='picked'){ loadPicked(); } else { loadDelivered(); }
  }
}
function startPolling(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(()=>{
    if (document.getElementById('tab-available').classList.contains('active')){
      loadOrders();
    }
  }, 30000);
}

function showSuccess(m){ const d=document.getElementById('successMessage'); d.textContent=m; d.style.display='block'; setTimeout(()=>d.style.display='none', 4000); }
function showError(m){ const d=document.getElementById('errorMessage'); d.textContent=m; d.style.display='block'; setTimeout(()=>d.style.display='none', 5000); }

setActiveTab(['available','picked','delivered'].includes(initialTab)?initialTab:'available');
</script>
</body>
</html>
"""

def create_templates():
    base = os.path.dirname(__file__)
    tdir = os.path.join(base, "templates")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE)

def run_flask():
    try:
        create_templates()  # fontos!
        app.run(host="0.0.0.0", port=5000, debug=False)
    except Exception as e:
        logger.error(f"Flask failed: {e}")

if __name__ == "__main__":
    logger.info("MAIN: Flask indítása külön szálon…")
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("MAIN: Bot indítása (polling)…")
    bot = RestaurantBot()
    bot.run()
