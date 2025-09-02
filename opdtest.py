# file: opdy.py
import asyncio
import os
import logging
import sqlite3
import json
import threading
import re
import requests
import urllib.parse
from queue import Queue, Empty
from typing import Dict, List, Optional

from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# =============== CONFIG ===============
BOT_TOKEN = "7741178469:AAEXmDVBCDCp6wY0AzPzxpuEzNRcKId86_o"
WEBAPP_URL = "https://4c80fa80f94c.ngrok-free.app"  # ha iPad/ngrok: "https://<valami>.ngrok-free.app"
DB_NAME = "restaurant_orders.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Értesítések sorban (pl. éttermi csoportnak visszaírás)
notification_queue: "Queue[Dict]" = Queue()

def improved_address_cleaner(addr: str) -> str:
    """
    JAVÍTOTT magyar cím tisztító és kódoló
    Kezeli: irányítószámokat, kötőjeleket, ékezeteket, komplex formátumokat
    """
    if not addr or not addr.strip():
        return ""
    
    # 1. ALAPVETŐ TISZTÍTÁS
    addr = addr.strip()
    
    # 2. SORSZÁM ELTÁVOLÍTÁS JAVÍTVA
    # "1. Budapest" -> "Budapest", de "1051 Budapest" -> "1051 Budapest"
    if re.match(r'^\d{1,2}\.\s+', addr):  # Csak 1-2 számjegy + pont + szóköz
        addr = re.sub(r'^\d{1,2}\.\s+', '', addr)
    
    # 3. MAGYAR CÍM NORMALIZÁLÁS
    # Kerület rövidítések normalizálása
    addr = re.sub(r'\bV\.\s*ker\.?\s*,?\s*', 'V. kerület, ', addr)
    addr = re.sub(r'\b(I{1,3}|IV|VI{1,3}|IX|XI{1,3}|XIV|XV{1,3}|XIX|XX{1,3})\.\s*ker\.?\s*,?\s*', 
                lambda m: m.group(1) + '. kerület, ', addr)
    
    # Dupla szóközök eltávolítása  
    addr = re.sub(r'\s+', ' ', addr).strip()
    
    # 4. OKOS ENKÓDOLÁS - NEM túl agresszív
    # Google/Apple Maps jól kezeli ezeket enkódolás nélkül:
    safe_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    safe_chars += "áéíóöőúüűÁÉÍÓÖŐÚÜŰ"  # Magyar ékezetes karakterek
    safe_chars += ".,-"  # Alapvető írásjel
    
    # URL enkódolás a safe karakterek megőrzésével
    return urllib.parse.quote(addr, safe=safe_chars)

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

            # opcionális, de hasznos indexek:
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
        """Aktív listához: pending + accepted + picked_up (hogy felvétel után is lehessen 'Kiszállítva'-ra zárni)."""
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
        """Futár adott státuszú rendeléseinek címeit adja vissza."""
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
        """Futár rendeléseinek számát adja vissza (opcionálisan státusz szerint szűrve)."""
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
        # csak csoportban figyelünk szövegre
        app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, self.handle_group_message))

        # értesítési queue ürítése időzítővel
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
                        await asyncio.sleep(1)  # 1 sec várakozás újrapróbálás előtt
                    else:
                        logger.error(f"Final failure sending notification to {item['chat_id']}: {item.get('text', '')[:50]}...")

    def send_notification(self, chat_id: int, text: str):
        """Értesítés hozzáadása a sorhoz megfelelő formázással"""
        try:
            # Ellenőrzi hogy a text nem üres és a chat_id érvényes
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
        """
        STRICT formátum:
        Cím: <cím>
        Telefonszám: <telefon>
        Megjegyzés: <megjegyzés>
        """
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
        # csak csoportban
        if update.effective_chat.type not in ("group","supergroup"):
            return
        parsed = self.parse_order_message(update.message.text or "")
        if not parsed:
            return
        gid = update.effective_chat.id
        gname = update.effective_chat.title or "Ismeretlen"
        item = {
            "restaurant_name": gname,                 # étterem = csoport neve
            "restaurant_address": parsed["address"],  # Cím
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

HTML_TEMPLATE = r"""
<!doctype html>
<html lang="hu">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Futár</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#fff;color:#111;margin:16px}
    .container{max-width:720px;margin:0 auto}
    .tabs{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
    .tab{padding:8px 12px;border:1px solid #bbb;border-radius:999px;background:#fafafa;cursor:pointer}
    .tab.active{background:#1a73e8;color:#fff;border-color:#1a73e8}
    .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0;background:#fafafa}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .pill{padding:2px 8px;border-radius:999px;background:#eee;font-size:12px}
    .time-buttons{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
    .time-btn{border:1px solid #1a73e8;border-radius:10px;padding:10px;background:#fff;cursor:pointer;font-size:12px}
    .time-btn.selected{background:#1a73e8;color:#fff}
    .accept-btn{border:0;border-radius:10px;padding:12px;width:100%;background:#1a73e8;color:#fff;cursor:pointer}
    .muted{color:#666;font-size:12px}
    
    /* JAVÍTOTT navigációs gombok */
    .nav-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:8px 0}
    .nav{display:block;text-decoration:none;border:1px solid #1a73e8;border-radius:8px;padding:8px;background:#fff;text-align:center;font-size:11px;color:#1a73e8}
    .nav.apple{border-color:#000;color:#000;background:#f5f5f7}
    .nav:hover{opacity:0.8}
    
    .ok{display:none;background:#d4edda;color:#155724;border-radius:8px;padding:10px;margin:8px 0}
    .err{display:none;background:#f8d7da;color:#721c24;border-radius:8px;padding:10px;margin:8px 0}
    
    /* JAVÍTOTT útvonal gombok */
    .routebar{display:none;gap:6px;margin:8px 0;flex-wrap:wrap}
    .routebtn{border:0;border-radius:10px;padding:10px 12px;background:#1a73e8;color:#fff;cursor:pointer;font-size:12px}
    .routebtn.apple{background:#000}
    .routebtn:hover{opacity:0.9}
  </style>
</head>
<body>
  <div class="container">
    <h2>🍕 Futár felület</h2>

    <div class="tabs">
      <button class="tab" id="tab-av" onclick="setTab('available')">Elérhető</button>
      <button class="tab" id="tab-ac" onclick="setTab('accepted')">Elfogadott</button>
      <button class="tab" id="tab-pk" onclick="setTab('picked_up')">Felvett</button>
      <button class="tab" id="tab-dv" onclick="setTab('delivered')">Kiszállított</button>
    </div>

    <!-- JAVÍTOTT útvonal gombok Apple Maps támogatással -->
    <div class="routebar" id="routebar">
      <button class="routebtn" onclick="openOptimizedRoute('google')">🗺️ Google Maps - Összes cím</button>
      <button class="routebtn apple" onclick="openOptimizedRoute('apple')">🍎 Apple Maps - Összes cím</button>
    </div>

    <div class="ok" id="ok"></div>
    <div class="err" id="err"></div>
    <div id="list">Betöltés…</div>
  </div>

<script>
  const tg = window.Telegram?.WebApp; 
  if(tg) tg.expand();
  
  const API = window.location.origin;
  let selectedETA = {}; // order_id -> 10/20/30
  let TAB = (new URLSearchParams(location.search).get('tab')) || 'available';

  function ok(m){ 
    const d=document.getElementById('ok'); 
    d.textContent=m; 
    d.style.display='block'; 
    setTimeout(()=>d.style.display='none', 3000); 
  }
  
  function err(m){ 
    const d=document.getElementById('err'); 
    d.textContent=m; 
    d.style.display='block'; 
    setTimeout(()=>d.style.display='none', 5000); 
  }

  // JAVÍTOTT navigációs linkek - jelenlegi helyről indul
  function googleMapsLink(addr){
    const cleanAddr = addr.replace(/^\d{1,2}\.\s+/, ''); // Sorszám eltávolítás
    // Mobilon az alkalmazást nyitja, asztali böngészőben webet
    const isMobile = /Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
    if (isMobile) {
        return `https://maps.google.com/?q=${encodeURIComponent(cleanAddr)}`;
    }
    return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(cleanAddr)}&travelmode=driving`;
  }
  
  function appleMapsLink(addr){
    return `maps://?daddr=${encodeURIComponent(addr)}`;
  }
  
  function wazeLink(addr){
    return `https://waze.com/ul?q=${encodeURIComponent(addr)}&navigate=yes`;
  }

  function render(order){
    // JAVÍTOTT navigációs gombok ráccsal
    const nav = `
      <div class="nav-grid">
        <a class="nav" href="${googleMapsLink(order.restaurant_address)}" target="_blank">🗺️ Google</a>
        <a class="nav apple" href="${appleMapsLink(order.restaurant_address)}" target="_blank">🍎 Apple</a>
        <a class="nav" href="${wazeLink(order.restaurant_address)}" target="_blank">🚗 Waze</a>
      </div>
    `;
    
    const timeBtns = `
      <div class="time-buttons" style="${order.status==='pending'?'':'display:none'}">
        <button class="time-btn" data-oid="${order.id}" data-eta="10">⏱️ 10 perc</button>
        <button class="time-btn" data-oid="${order.id}" data-eta="20">⏱️ 20 perc</button>
        <button class="time-btn" data-oid="${order.id}" data-eta="30">⏱️ 30 perc</button>
      </div>
    `;
    
    let btnLabel = '🚚 Rendelés elfogadása';
    if(order.status==='accepted') btnLabel = '✅ Felvettem';
    if(order.status==='picked_up') btnLabel = '✅ Kiszállítva / Leadva';

    const showBtn = order.status !== 'delivered';
    
    return `
      <div class="card" id="card-${order.id}">
        <div class="row">
          <b>${order.group_name || order.restaurant_name}</b>
          <span class="pill">${order.status}</span>
        </div>
        <div>📍 <b>Cím:</b> ${order.restaurant_address}</div>
        ${order.phone_number ? `<div>📞 <b>Telefon:</b> ${order.phone_number}</div>` : ''}
        ${order.order_details ? `<div>📝 <b>Megjegyzés:</b> ${order.order_details}</div>` : ''}
        <div class="muted">ID: #${order.id} • ${order.created_at}</div>
        ${nav}
        ${timeBtns}
        ${showBtn ? `<button class="accept-btn" id="btn-${order.id}" onclick="doAction(${order.id}, '${order.status}')">${btnLabel}</button>` : ''}
      </div>
    `;
  }

  function wireTimeButtons(){
    document.querySelectorAll('.time-btn').forEach(b=>{
      b.addEventListener('click', ()=>{
        const oid = b.dataset.oid, eta = b.dataset.eta;
        document.querySelectorAll(`[data-oid="${oid}"]`).forEach(x=>x.classList.remove('selected'));
        b.classList.add('selected');
        selectedETA[oid] = eta;
        if(tg?.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
      });
    });
  }

  async function load(){
    // tab aktív állapot
    document.getElementById('tab-av').classList.toggle('active', TAB==='available');
    document.getElementById('tab-ac').classList.toggle('active', TAB==='accepted');
    document.getElementById('tab-pk').classList.toggle('active', TAB==='picked_up');
    document.getElementById('tab-dv').classList.toggle('active', TAB==='delivered');
    document.getElementById('routebar').style.display = (TAB==='picked_up') ? 'flex' : 'none';

    const list = document.getElementById('list');
    list.innerHTML = 'Betöltés…';

    let data = [];
    try{
      if(TAB === 'available'){
        const r = await fetch(`${API}/api/orders_by_status?status=pending`);
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
        data = await r.json();
      }else{
        const r = await fetch(`${API}/api/my_orders`, {
          method:'POST', 
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ initData: tg?.initData || '', status: TAB })
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
        const j = await r.json();
        if(!j.ok) throw new Error(j.error||'Hálózati hiba');
        data = j.orders || [];
      }
    }catch(e){
      console.error('Load error:', e);
      err(e.message||'Hiba a betöltésnél');
      data = [];
    }

    if(!data.length){ 
      list.innerHTML = '<div class="muted">Nincs rendelés.</div>'; 
      return; 
    }
    
    list.innerHTML = data.map(render).join('');
    wireTimeButtons();
  }

  async function doAction(orderId, status){
    const btn = document.getElementById(`btn-${orderId}`);
    if(!btn || btn.disabled) return;
    
    btn.disabled = true; 
    const old = btn.textContent; 
    btn.textContent = '⏳...';
    
    try{
      let apiUrl, payload;
      
      if(status==='pending'){
        const eta = selectedETA[orderId]; 
        if(!eta) throw new Error('Válassz időt (10/20/30 perc).');
        apiUrl = `${API}/api/accept_order`;
        payload = { order_id: orderId, estimated_time: eta, initData: tg?.initData || '' };
      } else if(status==='accepted'){
        apiUrl = `${API}/api/pickup_order`;
        payload = { order_id: orderId, initData: tg?.initData || '' };
      } else if(status==='picked_up'){
        apiUrl = `${API}/api/mark_delivered`;
        payload = { order_id: orderId, initData: tg?.initData || '' };
      } else {
        throw new Error('Ismeretlen státusz');
      }
      
      const r = await fetch(apiUrl, {
        method:'POST', 
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
      const j = await r.json(); 
      if(!j.ok) throw new Error(j.error||'Szerver hiba');
      
      // Sikeres műveletek kezelése
      if(status==='pending'){
        ok('Elfogadva.');
        btn.textContent = '✅ Felvettem';
        btn.setAttribute('onclick', `doAction(${orderId}, 'accepted')`);
        const tb = document.querySelector(`#card-${orderId} .time-buttons`); 
        if(tb) tb.style.display='none';
        const pill = document.querySelector(`#card-${orderId} .pill`); 
        if(pill) pill.textContent='accepted';
      } else if(status==='accepted'){
        ok('Felvéve.');
        btn.textContent = '✅ Kiszállítva / Leadva';
        btn.setAttribute('onclick', `doAction(${orderId}, 'picked_up')`);
        const pill = document.querySelector(`#card-${orderId} .pill`); 
        if(pill) pill.textContent='picked_up';
      } else if(status==='picked_up'){
        ok('Kiszállítva.');
        const card = document.getElementById(`card-${orderId}`);
        if(card){ 
          card.style.opacity='0.4'; 
          setTimeout(()=>card.remove(), 400); 
        }
      }
      
      btn.disabled = false;
      if(tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      
    }catch(e){
      console.error('Action error:', e);
      err(e.message || 'Hiba a művelet végrehajtásakor');
      btn.disabled = false; 
      btn.textContent = old;
      if(tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
    }
  }

  // JAVÍTOTT útvonal megnyitás
  async function openOptimizedRoute(mapType = 'google'){
    try{
      const r = await fetch(`${API}/api/opt_route`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ 
          initData: tg?.initData || '', 
          mapType: mapType 
        })
      });
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || 'Nem sikerült útvonalat készíteni.');
  
      // iOS/Telegram WebApp fix mindkét térképhez
      if (tg?.openLink) {
        tg.openLink(j.url);
      } else {
        window.open(j.url, '_blank');
      }
    }catch(e){
      console.error('Route error:', e);
      err(e.message || 'Hiba');
    }
  }
  
  function setTab(t){
    TAB = t;
    load();
  }

  // Kezdeti betöltés és automatikus frissítés
  load();
  setInterval(load, 30000); // 30 másodpercenként frissít
</script>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html lang="hu">
<head><meta charset="utf-8"><title>Admin</title></head>
<body>
  <h1>Admin statisztika</h1>
  <h2>Heti futár bontás</h2>
  <table border="1">
    <tr><th>Hét</th><th>Futár</th><th>Darab</th><th>Átlag idő (perc)</th></tr>
    {% for r in weekly_courier %}
    <tr>
      <td>{{ r.week }}</td>
      <td>{{ r.courier_name or r.delivery_partner_id }}</td>
      <td>{{ r.cnt }}</td>
      <td>{{ r.avg_min }}</td>
    </tr>
    {% endfor %}
  </table>

  <h2>Étterem bontás</h2>
  <table border="1">
    <tr><th>Hét</th><th>Csoport</th><th>Darab</th><th>Átlag idő</th></tr>
    {% for r in weekly_restaurant %}
    <tr>
      <td>{{ r.week }}</td>
      <td>{{ r.group_name }}</td>
      <td>{{ r.cnt }}</td>
      <td>{{ r.avg_min }}</td>
    </tr>
    {% endfor %}
  </table>

  <h2>Részletes kézbesítések</h2>
  <table border="1">
    <tr><th>Dátum</th><th>Futár</th><th>Csoport</th><th>Cím</th><th>Idő (perc)</th></tr>
    {% for r in deliveries %}
    <tr>
      <td>{{ r.delivered_at }}</td>
      <td>{{ r.courier_name or r.delivery_partner_id }}</td>
      <td>{{ r.group_name }}</td>
      <td>{{ r.restaurant_address }}</td>
      <td>{{ r.min }}</td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
"""

@app.route("/")
def index():
    # egyszerű WebApp: aktív rendeléslista
    try:
        orders = db.get_open_orders()
        return render_template_string(HTML_TEMPLATE, orders=orders)
    except Exception as e:
        logger.error(f"index error: {e}")
        return "error", 500

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

        # Értesítés az éttermi csoportnak (csak minim infó, ahogy kérted)
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
            # (engedékeny: ha nincs beírva partner_id, a felvevő lesz az)
            pass

        db.update_order_status(order_id, "picked_up",
                               partner_id=user.get("id"))  # biztos, hogy az övé
        # értesítés a csoportnak
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

        # értesítés csoportnak (opcionális)
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
    """
    status=pending -> minden pending
    status=accepted/picked_up/delivered -> csak az adott futáré (courier_id alapján)
    """
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
    """Body: { initData: <tg.initData>, status: 'accepted'|'picked_up'|'delivered' }"""
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


@app.route("/api/opt_route", methods=["POST"])
def api_opt_route_improved():
    try:
        data = request.json or {}
        user = validate_telegram_data(data.get("initData",""))
        if not user:
            return jsonify({"ok": False, "error":"unauthorized"}), 401

        map_type = data.get("mapType", "google")
        
        rows = db.get_partner_addresses(partner_id=user["id"], status="picked_up")
        addrs = [r["restaurant_address"] for r in rows if r.get("restaurant_address") and r["restaurant_address"].strip()]
        
        if not addrs:
            return jsonify({"ok": False, "error": "no_addresses"})

        def clean_encode_address(addr):
            return improved_address_cleaner(addr)
        
        if map_type == "apple":
            # Apple Maps
            if len(addrs) == 1:
                url = f"maps://?daddr={clean_encode_address(addrs[0])}"
            else:
                first_addr = clean_encode_address(addrs[0])
                last_addr = clean_encode_address(addrs[-1])
                url = f"maps://?saddr={first_addr}&daddr={last_addr}"
                
        else:
            # Google Maps - JAVÍTOTT címkezeléssel
            if len(addrs) == 1:
                url = f"https://www.google.com/maps/dir/?api=1&destination={clean_encode_address(addrs[0])}&travelmode=driving"
                
            elif len(addrs) == 2:
                # Két cím: első->második
                dest = clean_encode_address(addrs[1])
                waypoint = clean_encode_address(addrs[0])
                url = f"https://www.google.com/maps/dir/?api=1&destination={dest}&waypoints={waypoint}&travelmode=driving"
                
            else:
                # Több cím (3+)
                destination = clean_encode_address(addrs[-1])
                waypoints = [clean_encode_address(addr) for addr in addrs[:-1]]
                
                # Google Maps waypoints limit (23)
                if len(waypoints) > 23:
                    waypoints = waypoints[:23]
                    logger.warning(f"Waypoints truncated: {len(addrs)} -> 24 (23 waypoints + destination)")
                
                waypoints_str = "|".join(waypoints)
                url = f"https://www.google.com/maps/dir/?api=1&destination={destination}&waypoints={waypoints_str}&travelmode=driving"
                # Ha a link nagyon hosszú vagy sok cím van, akkor rövidítünk
            if len(url) > 1800 or len(addrs) >= 4:
                short_url = shorten_url(url)
                final_url = short_url
            else:
                final_url = url

            return jsonify({
                "ok": True,
                "url": final_url,
                "count": len(addrs),
                "mapType": map_type,
                "addresses_original": addrs,
                "addresses_cleaned": [clean_encode_address(addr) for addr in addrs]
            })

        
    except Exception as e:
        logger.error(f"api_opt_route error: {e}")
        return jsonify({"ok": False, "error": f"internal_server_error: {str(e)}"}), 500

    
@app.route("/admin")
def admin_page():
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Heti futár statisztika
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

        # Heti étterem statisztika
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

        # Részletes lista
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



# =============== Indítás ===============
def run_flask():
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__":
    # Flask háttérszálban
    threading.Thread(target=run_flask, daemon=True).start()
    # Bot főszálon (stabil)
    RestaurantBot().run()
