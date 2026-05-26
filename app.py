#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
  XAUUSD SIGNAL BOT v2 — Standalone | JustRunMy.App Ready
  Connexion: WebSocket Deriv direct (fallback HTTP si WS bloqué)
  Aucune variable d'environnement requise
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import time
import sys
import json
from datetime import datetime, timezone
from typing import Optional, Dict, List

try:
    import nest_asyncio
    nest_asyncio.apply()
except:
    pass

try:
    import aiohttp
except ImportError:
    print("[FATAL] pip install aiohttp")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS HARDCODÉS (pas de variables d'environnement)
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN = "pol9lAsyb0m9eUT"
APP_ID = 1089
SYMBOL = "frxXAUUSD"

TELEGRAM_BOT_TOKEN = "8842089863:AAGkqa0yPvojFVYj9vJH3kBOIiNfpdjbCPg"
TELEGRAM_CHAT_IDS = ["7426051015", "7612901356"]

FIB_OTE_MIN = 0.618
FIB_OTE_MAX = 0.786
MAX_HISTORY_M1 = 100
MAX_HISTORY_H1 = 100
SWING_LOOKBACK = 2
SIGNAL_COOLDOWN = 60
SIGNAL_VALIDITY_MINUTES = 15

DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
DERIV_HTTP_URL = "https://ws.derivws.com/websockets/v3"

# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT DERIV — WebSocket direct + fallback HTTP
# ═══════════════════════════════════════════════════════════════════════════════

class DerivClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.connected = False
        self.authorized = False
        self.use_http_fallback = False
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._subscriptions: Dict[str, callable] = {}
        self._listen_task = None
        self._shutdown = False

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def connect(self):
        # Essai WebSocket d'abord
        try:
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(DERIV_WS_URL, timeout=aiohttp.ClientTimeout(total=10))
            self.connected = True
            self.use_http_fallback = False
            self._listen_task = asyncio.create_task(self._listen_loop())
            return True
        except Exception as e:
            print(f"[WARN] WebSocket failed: {e}, trying HTTP fallback...")
            if self.session and not self.session.closed:
                await self.session.close()
            # Fallback HTTP
            try:
                self.session = aiohttp.ClientSession()
                self.use_http_fallback = True
                self.connected = True
                print("[INFO] Using HTTP fallback mode")
                return True
            except Exception as e2:
                print(f"[FATAL] HTTP fallback also failed: {e2}")
                return False

    async def disconnect(self):
        self._shutdown = True
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session and not self.session.closed:
            await self.session.close()
        self.connected = False
        self.authorized = False

    async def _listen_loop(self):
        if self.use_http_fallback:
            return
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    req_id = data.get("req_id")
                    if req_id is not None and req_id in self._pending:
                        fut = self._pending.pop(req_id)
                        if not fut.done():
                            fut.set_result(data)
                    if "tick" in data:
                        cb = self._subscriptions.get("ticks")
                        if cb:
                            cb(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self.connected = False
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            self.connected = False

    async def send(self, payload: dict, wait_response=True, timeout=15):
        if self.use_http_fallback:
            return await self._send_http(payload, timeout)

        if not self.connected or self.ws.closed:
            raise ConnectionError("WebSocket non connecte")
        req_id = self._next_id()
        payload["req_id"] = req_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self.ws.send_str(json.dumps(payload))
        if wait_response:
            return await asyncio.wait_for(fut, timeout=timeout)
        return None

    async def _send_http(self, payload: dict, timeout=15):
        """Fallback HTTP pour les requêtes si WebSocket est bloqué."""
        req_id = self._next_id()
        payload["req_id"] = req_id
        try:
            async with self.session.post(DERIV_HTTP_URL, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return await resp.json()
        except Exception as e:
            print(f"[ERROR] HTTP request failed: {e}")
            return None

    async def authorize(self, token):
        resp = await self.send({"authorize": token})
        if resp and "authorize" in resp:
            self.authorized = True
            return resp
        return None

    async def ticks_history(self, req):
        return await self.send(req)

    async def subscribe(self, req, callback):
        if self.use_http_fallback:
            # En mode HTTP, on ne peut pas vraiment subscribe, on pollera
            print("[WARN] Subscription not available in HTTP mode, using polling")
            return self
        sub_id = list(req.keys())[0]
        self._subscriptions[sub_id] = callback
        await self.send(req, wait_response=False)
        return self

# ═══════════════════════════════════════════════════════════════════════════════
# BOT PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

class XAUUSDSignalBot:
    def __init__(self):
        self.api = DerivClient()
        self.ws_connected = False
        self.authorized = False
        self.m1_history = []
        self.h1_history = []
        self.current_m1 = None
        self.current_minute = None
        self.last_price = None
        self.last_price_timestamp = 0
        self.last_tick_received_time = 0
        self.tick_queue = asyncio.Queue(maxsize=10000)
        self.tick_prices = []
        self.last_signal_time = 0
        self.last_signal_direction = None
        self.last_signal_msg = None
        self.last_signal_expire = 0
        self.last_signal_entry_price = None
        self.last_signal_sl = None
        self.last_signal_tp1 = None
        self.last_signal_tp2 = None
        self.last_signal_tp3 = None
        self.last_signal_validity_minutes = None
        self.swing_highs = []
        self.swing_lows = []
        self.last_bos = None
        self.active_fvgs = []
        self.h1_trend = "NEUTRAL"
        self.tasks = []
        self.shutdown_event = asyncio.Event()

    def _now_ms(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"

    def _log(self, tag, message):
        print(f"[{self._now_ms()}] [{tag}] {message}", flush=True)

    def _calculate_fib_levels(self, origin, extreme):
        diff = extreme - origin
        return {"0.0": origin, "0.618": origin + diff * FIB_OTE_MIN, "0.786": origin + diff * FIB_OTE_MAX, "1.0": origin + diff * 1.0}

    async def _send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with aiohttp.ClientSession() as session:
                for chat_id in TELEGRAM_CHAT_IDS:
                    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            self._log("TELEGRAM", f"HTTP {resp.status} pour chat {chat_id}")
        except Exception as e:
            self._log("TELEGRAM", f"Erreur: {str(e)}")

    async def _telegram_command_listener(self):
        self._log("TELEGRAM", "Listener commandes demarre...")
        last_update_id = 0
        while not self.shutdown_event.is_set():
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                params = {"offset": last_update_id + 1, "limit": 10}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("ok") and data.get("result"):
                                for update in data["result"]:
                                    last_update_id = update["update_id"]
                                    if "message" in update and "text" in update["message"]:
                                        msg_text = update["message"]["text"].strip().lower()
                                        chat_id = str(update["message"]["chat"]["id"])
                                        if chat_id in TELEGRAM_CHAT_IDS:
                                            if msg_text == "/signal":
                                                await self._handle_signal_command()
                                            elif msg_text == "/status":
                                                await self._handle_status_command()
                                            elif msg_text == "/price":
                                                await self._handle_price_command()
                                            elif msg_text == "/help":
                                                await self._handle_help_command()
                                            elif msg_text == "/check":
                                                await self._handle_check_command()
                await asyncio.sleep(2)
            except Exception as e:
                self._log("TELEGRAM", f"Erreur listener: {str(e)}")
                await asyncio.sleep(5)

    async def _handle_signal_command(self):
        self._log("COMMAND", "/signal recu — analyse complete...")
        now = time.time()
        elapsed = now - self.last_signal_time
        if elapsed < SIGNAL_COOLDOWN:
            remain = int(SIGNAL_COOLDOWN - elapsed)
            msg = f"⏳ <b>Attendez {remain}s</b>\nUn signal a deja ete envoye recemment."
            await self._send_telegram(msg)
            return

        if self.last_signal_msg and now < self.last_signal_expire and self.last_signal_direction:
            remain_min = int((self.last_signal_expire - now) / 60)
            msg = f"⚠️ <b>Signal encore valide</b> ({remain_min} min restantes)\n\n" + self.last_signal_msg + f"\n\n⏳ <i>Nouvelle analyse disponible dans {remain_min} min</i>"
            await self._send_telegram(msg)
            self._log("SIGNAL", f"RENVOYE (valide {remain_min}min restantes) | {self.last_signal_direction}")
            return

        if not self.m1_history or len(self.m1_history) < 5:
            await self._send_telegram("⏳ <b>Attendez...</b>\nPas assez de donnees marche. Reessayez dans 30 secondes.")
            return

        price_age = now - self.last_price_timestamp
        if self.last_price is not None and price_age <= 180:
            current_price = self.last_price
            price_source = "TICK"
        else:
            current_price = self.m1_history[-1]["close"]
            price_source = "M1_FALLBACK"
            if price_age > 180:
                self._log("SIGNAL", f"PRIX FIGE depuis {price_age:.0f}s — Fallback M1 close: {current_price}")
            else:
                self._log("SIGNAL", f"Pas de prix tick — Utilisation M1 close: {current_price}")

        m1_age = now - self.m1_history[-1]["epoch"]
        if m1_age > 300:
            await self._send_telegram("⏳ <b>Donnees trop vieilles</b>\nLe prix n'a pas ete mis a jour depuis plus de 5 minutes. Le bot force une reconnexion...\nReessayez /signal dans 30 secondes.")
            self.ws_connected = False
            return

        analysis = self._analyze_market_conditions(current_price)
        scores = analysis["scores"]
        problems = analysis["problems"]
        is_choppy = analysis["is_choppy"]
        has_liquidity_sweep = analysis["has_liquidity_sweep"]

        direction = self._force_direction(scores, current_price)
        if direction is None:
            direction = self._fallback_direction(current_price)
            problems.append(f"Aucun signal clair — Force par defaut: {direction}")

        final_scores = self._calculate_direction_scores(direction, current_price)
        total_score = sum(final_scores.values())
        confidence = min(total_score, 100)

        sl_offset, tp1_offset, tp2_offset, tp3_offset, validity_minutes = self._get_dynamic_levels(confidence)

        self.last_signal_time = now
        self.last_signal_direction = direction
        self.last_signal_entry_price = current_price
        self.last_signal_sl = sl_offset
        self.last_signal_tp1 = tp1_offset
        self.last_signal_tp2 = tp2_offset
        self.last_signal_tp3 = tp3_offset
        self.last_signal_validity_minutes = validity_minutes
        self.last_signal_expire = now + (validity_minutes * 60)

        msg = self._build_signal_message(direction, confidence, final_scores, current_price, problems, is_choppy, has_liquidity_sweep, price_source, sl_offset, tp1_offset, tp2_offset, tp3_offset, validity_minutes)
        self.last_signal_msg = msg
        await self._send_telegram(msg)
        self._log("SIGNAL", f"ENVOYE | {direction} | {confidence}% | Problemes: {len(problems)} | Source:{price_source} | Valide:{validity_minutes}min")

    async def _handle_status_command(self):
        trend = self.h1_trend
        price = self.last_price if self.last_price else "N/A"
        bos = self.last_bos["type"] if self.last_bos else "AUCUN"
        fvg_count = len([f for f in self.active_fvgs if not f["filled"]])
        msg = f"📊 <b>STATUT DU BOT</b>\n═══════════════════════\n📈 Tendance H1: <b>{trend}</b>\n💵 Dernier prix: <code>{price}</code>\n🔨 Dernier BOS: <b>{bos}</b>\n📐 FVG actifs: <b>{fvg_count}</b>\n📊 Bougies M1: <b>{len(self.m1_history)}</b>\n⏰ {self._now_ms()}"
        await self._send_telegram(msg)

    async def _handle_price_command(self):
        price = self.last_price if self.last_price else "N/A"
        await self._send_telegram(f"💵 <b>Prix XAUUSD actuel:</b> <code>{price}</code> USD")

    async def _handle_help_command(self):
        msg = ("🤖 <b>COMMANDES DISPONIBLES</b>\n"
               "═══════════════════════\n"
               "📡 /signal — Demander un signal ACHAT ou VENTE avec les %\n"
               "📊 /status — Voir le statut actuel du marche\n"
               "💵 /price — Voir le prix actuel de XAUUSD\n"
               "🔍 /check — Verifier le dernier signal actif (P&L, SL, TP)\n"
               "❓ /help — Afficher cette aide\n\n"
               "Le bot analyse en temps reel et choisit toujours entre ACHAT ou VENTE.")
        await self._send_telegram(msg)

    def _get_dynamic_levels(self, confidence):
        if confidence >= 80:
            return 4.00, 8.00, 16.00, 24.00, 20
        elif confidence >= 50:
            return 2.50, 5.00, 10.00, 15.00, 12
        else:
            return 1.50, 3.00, 6.00, 9.00, 5

    async def _handle_check_command(self):
        now = time.time()
        if not self.last_signal_direction:
            await self._send_telegram("❌ <b>Aucun signal actif</b>\nTape /signal pour generer un signal.")
            return
        if now > self.last_signal_expire:
            expired_min = int((now - self.last_signal_expire) / 60)
            await self._send_telegram(f"⏳ <b>SIGNAL EXPIRE</b>\nLe dernier signal a expire il y a {expired_min} min.\nTape /signal pour un nouveau.")
            return

        entry = self.last_signal_entry_price
        direction = self.last_signal_direction
        sl_offset = self.last_signal_sl
        tp1_offset = self.last_signal_tp1
        tp2_offset = self.last_signal_tp2
        tp3_offset = self.last_signal_tp3

        current = self.last_price if self.last_price else (self.m1_history[-1]["close"] if self.m1_history else None)
        if current is None:
            await self._send_telegram("⏳ <b>Donnees indisponibles</b>\nPrix actuel inconnu. Reessayez.")
            return

        if direction == "BULLISH":
            sl_price = entry - sl_offset
            tp1_price = entry + tp1_offset
            tp2_price = entry + tp2_offset
            tp3_price = entry + tp3_offset
            pnl_usd = current - entry
        else:
            sl_price = entry + sl_offset
            tp1_price = entry - tp1_offset
            tp2_price = entry - tp2_offset
            tp3_price = entry - tp3_offset
            pnl_usd = entry - current

        pnl_pct = (pnl_usd / entry) * 100 if entry != 0 else 0
        pnl_sign = "+" if pnl_usd >= 0 else ""

        dist_sl_usd = abs(current - sl_price)
        dist_tp1_usd = abs(current - tp1_price)
        dist_tp2_usd = abs(current - tp2_price)
        dist_tp3_usd = abs(current - tp3_price)

        dist_sl_pct = (dist_sl_usd / entry) * 100 if entry != 0 else 0
        dist_tp1_pct = (dist_tp1_usd / entry) * 100 if entry != 0 else 0
        dist_tp2_pct = (dist_tp2_usd / entry) * 100 if entry != 0 else 0
        dist_tp3_pct = (dist_tp3_usd / entry) * 100 if entry != 0 else 0

        remain_min = int((self.last_signal_expire - now) / 60)

        if direction == "BULLISH":
            if current <= sl_price:
                verdict = "🔴 COUPER — Le SL est touche ou depasse, ferme la position"
            elif current <= entry - 0.5 * sl_offset:
                verdict = "🟡 ATTENTION — Prix proche du SL, surveille de pres"
            elif current >= entry:
                verdict = "🟢 HOLD — Trade en profit ou neutre, laisse courir"
            else:
                verdict = "🟢 HOLD — Perte moderee, pas encore en danger"
        else:
            if current >= sl_price:
                verdict = "🔴 COUPER — Le SL est touche ou depasse, ferme la position"
            elif current >= entry + 0.5 * sl_offset:
                verdict = "🟡 ATTENTION — Prix proche du SL, surveille de pres"
            elif current <= entry:
                verdict = "🟢 HOLD — Trade en profit ou neutre, laisse courir"
            else:
                verdict = "🟢 HOLD — Perte moderee, pas encore en danger"

        emoji_dir = "🟢 ACHAT" if direction == "BULLISH" else "🔴 VENTE"

        msg = (f"🔍 <b>CHECK SIGNAL</b>\n"
               f"═══════════════════════\n"
               f"📌 Direction: <b>{emoji_dir}</b>\n"
               f"💵 Prix d'entree: <code>{entry:.2f}</code>\n"
               f"💵 Prix actuel: <code>{current:.2f}</code>\n\n"
               f"📊 <b>P&L ACTUEL:</b>\n"
               f"   {pnl_sign}{pnl_usd:.2f}$ ({pnl_sign}{pnl_pct:.2f}%)\n\n"
               f"🎯 <b>DISTANCES:</b>\n"
               f"   🛑 SL ({sl_price:.2f}): {dist_sl_usd:.2f}$ ({dist_sl_pct:.2f}%)\n"
               f"   🥇 TP1 ({tp1_price:.2f}): {dist_tp1_usd:.2f}$ ({dist_tp1_pct:.2f}%)\n"
               f"   🥈 TP2 ({tp2_price:.2f}): {dist_tp2_usd:.2f}$ ({dist_tp2_pct:.2f}%)\n"
               f"   🥉 TP3 ({tp3_price:.2f}): {dist_tp3_usd:.2f}$ ({dist_tp3_pct:.2f}%)\n\n"
               f"⏳ Validite: <b>{remain_min} min</b> restantes\n\n"
               f"{verdict}\n"
               f"⏰ {self._now_ms()}")
        await self._send_telegram(msg)

    def _analyze_market_conditions(self, current_price: float) -> Dict:
        scores = {"BOS": 0, "FVG": 0, "OTE": 0, "H1_TREND": 0, "MOMENTUM": 0}
        problems = []
        now = time.time()
        n = len(self.m1_history)

        last_5 = self.m1_history[-5:] if n >= 5 else self.m1_history
        last_10 = self.m1_history[-10:] if n >= 10 else self.m1_history

        if len(last_10) >= 5:
            highs = [c["high"] for c in last_10]
            lows = [c["low"] for c in last_10]
            range_10 = max(highs) - min(lows)
            avg_range = sum(c["high"] - c["low"] for c in last_10) / len(last_10)
            is_choppy = range_10 < 0.50 and avg_range < 0.08
            if is_choppy:
                problems.append("Marche en range etroit")
        else:
            is_choppy = False
            range_10 = 0

        has_liquidity_sweep = False
        if n >= 3:
            prev2 = self.m1_history[-3]
            prev1 = self.m1_history[-2]
            last = self.m1_history[-1]
            if prev1["high"] > prev2["high"] and last["close"] < prev2["high"]:
                has_liquidity_sweep = True
                problems.append("Prise de liquidite haussiere (sweep)")
            elif prev1["low"] < prev2["low"] and last["close"] > prev2["low"]:
                has_liquidity_sweep = True
                problems.append("Prise de liquidite baissiere (sweep)")

        if self.last_bos:
            bos_age = now - self.last_bos["epoch"]
            if bos_age < 900:
                if bos_age < 300:
                    scores["BOS"] = 30
                elif bos_age < 600:
                    scores["BOS"] = 20
                else:
                    scores["BOS"] = 10
            else:
                scores["BOS"] = 0
                problems.append("Dernier BOS trop vieux (>15 min)")
        else:
            problems.append("Aucun BOS recent")

        fvg_found = False
        for fvg in reversed(self.active_fvgs):
            if not fvg["filled"]:
                fvg_age = now - fvg["epoch"]
                if fvg_age < 600:
                    fvg_found = True
                    if fvg_age < 180:
                        scores["FVG"] = 25
                    elif fvg_age < 360:
                        scores["FVG"] = 15
                    else:
                        scores["FVG"] = 8
                break
        if not fvg_found:
            problems.append("Aucun FVG valide")

        if self.last_bos and self.last_bos.get("origin"):
            origin = self.last_bos["origin"]
            origin_price = origin["price"]
            extreme_price = self.last_bos.get("swing_high", {}).get("price") if self.last_bos["type"] == "BULLISH" else self.last_bos.get("swing_low", {}).get("price")
            if extreme_price:
                fib = self._calculate_fib_levels(origin_price, extreme_price)
                ote_low = min(fib["0.618"], fib["0.786"])
                ote_high = max(fib["0.618"], fib["0.786"])
                if ote_low <= current_price <= ote_high:
                    scores["OTE"] = 20
                elif abs(current_price - (ote_low + ote_high) / 2) < 0.30:
                    scores["OTE"] = 12
                elif abs(current_price - (ote_low + ote_high) / 2) < 0.60:
                    scores["OTE"] = 6
                else:
                    problems.append("Prix hors zone OTE")
            else:
                problems.append("Impossible de calculer OTE")
        else:
            problems.append("Pas de BOS avec origine")

        if self.h1_trend == "BULLISH":
            scores["H1_TREND"] = 15
        elif self.h1_trend == "BEARISH":
            scores["H1_TREND"] = -15
        else:
            scores["H1_TREND"] = 0
            problems.append("Tendance H1 neutre")

        if len(last_5) >= 3:
            closes = [c["close"] for c in last_5]
            net_move = closes[-1] - closes[0]
            if abs(net_move) > 0.15:
                scores["MOMENTUM"] = 10 if net_move > 0 else -10
            elif abs(net_move) > 0.08:
                scores["MOMENTUM"] = 5 if net_move > 0 else -5
            else:
                problems.append("Faible momentum bougies")

        if len(self.tick_prices) >= 10:
            recent_ticks = self.tick_prices[-10:]
            tick_move = recent_ticks[-1] - recent_ticks[0]
            if (scores["MOMENTUM"] > 0 and tick_move > 0) or (scores["MOMENTUM"] < 0 and tick_move < 0):
                if abs(tick_move) > 0.05:
                    scores["MOMENTUM"] = max(abs(scores["MOMENTUM"]), 10) * (1 if tick_move > 0 else -1)
            elif scores["MOMENTUM"] == 0:
                if abs(tick_move) > 0.05:
                    scores["MOMENTUM"] = 10 if tick_move > 0 else -10
                elif abs(tick_move) > 0.02:
                    scores["MOMENTUM"] = 5 if tick_move > 0 else -5

        return {"scores": scores, "problems": problems, "is_choppy": is_choppy, "has_liquidity_sweep": has_liquidity_sweep, "range_10": range_10}

    def _force_direction(self, scores: Dict[str, int], current_price: float) -> Optional[str]:
        bullish_score = 0
        bearish_score = 0

        if self.last_bos:
            if self.last_bos["type"] == "BULLISH":
                bullish_score += scores["BOS"]
            else:
                bearish_score += scores["BOS"]

        for fvg in reversed(self.active_fvgs):
            if not fvg["filled"]:
                if fvg["type"] == "BULLISH":
                    bullish_score += scores["FVG"]
                else:
                    bearish_score += scores["FVG"]
                break

        if self.last_bos and self.last_bos.get("origin"):
            origin = self.last_bos["origin"]
            origin_price = origin["price"]
            extreme_price = self.last_bos.get("swing_high", {}).get("price") if self.last_bos["type"] == "BULLISH" else self.last_bos.get("swing_low", {}).get("price")
            if extreme_price:
                fib = self._calculate_fib_levels(origin_price, extreme_price)
                ote_center = (fib["0.618"] + fib["0.786"]) / 2
                if current_price > ote_center:
                    bullish_score += scores["OTE"] // 2
                    bearish_score += scores["OTE"] // 2
                else:
                    bearish_score += scores["OTE"] // 2
                    bullish_score += scores["OTE"] // 2

        bullish_score += max(0, scores["H1_TREND"])
        bearish_score += max(0, -scores["H1_TREND"])
        bullish_score += max(0, scores["MOMENTUM"])
        bearish_score += max(0, -scores["MOMENTUM"])

        if max(bullish_score, bearish_score) < 15:
            return None
        return "BULLISH" if bullish_score >= bearish_score else "BEARISH"

    def _fallback_direction(self, current_price: float) -> str:
        if self.h1_trend == "BULLISH":
            return "BULLISH"
        elif self.h1_trend == "BEARISH":
            return "BEARISH"
        elif len(self.m1_history) >= 3:
            last_3 = [c["close"] for c in self.m1_history[-3:]]
            if last_3[-1] > last_3[0]:
                return "BULLISH"
            else:
                return "BEARISH"
        elif len(self.tick_prices) >= 2:
            return "BULLISH" if self.tick_prices[-1] >= self.tick_prices[-2] else "BEARISH"
        return "BULLISH"

    def _calculate_direction_scores(self, direction: str, current_price: float) -> Dict[str, int]:
        scores = {"BOS": 0, "FVG": 0, "OTE": 0, "H1_TREND": 0, "MOMENTUM": 0}
        now = time.time()

        if self.last_bos and self.last_bos["type"] == direction:
            bos_age = now - self.last_bos["epoch"]
            if bos_age < 900:
                if bos_age < 300:
                    scores["BOS"] = 30
                elif bos_age < 600:
                    scores["BOS"] = 20
                else:
                    scores["BOS"] = 10

        for fvg in reversed(self.active_fvgs):
            if not fvg["filled"] and fvg["type"] == direction:
                fvg_age = now - fvg["epoch"]
                if fvg_age < 600:
                    if fvg_age < 180:
                        scores["FVG"] = 25
                    elif fvg_age < 360:
                        scores["FVG"] = 15
                    else:
                        scores["FVG"] = 8
                break

        if self.last_bos and self.last_bos.get("origin"):
            origin = self.last_bos["origin"]
            origin_price = origin["price"]
            extreme_price = self.last_bos.get("swing_high", {}).get("price") if self.last_bos["type"] == "BULLISH" else self.last_bos.get("swing_low", {}).get("price")
            if extreme_price:
                fib = self._calculate_fib_levels(origin_price, extreme_price)
                ote_low = min(fib["0.618"], fib["0.786"])
                ote_high = max(fib["0.618"], fib["0.786"])
                if ote_low <= current_price <= ote_high:
                    scores["OTE"] = 20
                elif abs(current_price - (ote_low + ote_high) / 2) < 0.30:
                    scores["OTE"] = 12
                elif abs(current_price - (ote_low + ote_high) / 2) < 0.60:
                    scores["OTE"] = 6

        if direction == "BULLISH" and self.h1_trend == "BULLISH":
            scores["H1_TREND"] = 15
        elif direction == "BEARISH" and self.h1_trend == "BEARISH":
            scores["H1_TREND"] = 15
        elif self.h1_trend == "NEUTRAL":
            scores["H1_TREND"] = 5

        if len(self.m1_history) >= 5:
            last_5 = [c["close"] for c in self.m1_history[-5:]]
            net_move = last_5[-1] - last_5[0]
            if direction == "BULLISH" and net_move > 0:
                scores["MOMENTUM"] = 10 if net_move > 0.15 else 5
            elif direction == "BEARISH" and net_move < 0:
                scores["MOMENTUM"] = 10 if net_move < -0.15 else 5

        if len(self.tick_prices) >= 10:
            recent = self.tick_prices[-10:]
            net_move = recent[-1] - recent[0]
            if direction == "BULLISH" and net_move > 0:
                scores["MOMENTUM"] = max(scores["MOMENTUM"], 10 if net_move > 0.05 else 5)
            elif direction == "BEARISH" and net_move < 0:
                scores["MOMENTUM"] = max(scores["MOMENTUM"], 10 if net_move < -0.05 else 5)

        return scores

    def _build_signal_message(self, direction, confidence, scores, current_price, problems, is_choppy, has_liquidity_sweep, price_source="TICK", sl_offset=3.0, tp1_offset=5.0, tp2_offset=10.0, tp3_offset=15.0, validity_minutes=15):
        emoji = "🟢 ACHAT (CALL)" if direction == "BULLISH" else "🔴 VENTE (PUT)"

        if direction == "BULLISH":
            sl_price = current_price - sl_offset
            tp1_price = current_price + tp1_offset
            tp2_price = current_price + tp2_offset
            tp3_price = current_price + tp3_offset
            entry_zone = f"{current_price - 0.10:.2f} - {current_price + 0.10:.2f}"
        else:
            sl_price = current_price + sl_offset
            tp1_price = current_price - tp1_offset
            tp2_price = current_price - tp2_offset
            tp3_price = current_price - tp3_offset
            entry_zone = f"{current_price - 0.10:.2f} - {current_price + 0.10:.2f}"

        rr1 = tp1_offset / sl_offset if sl_offset != 0 else 0
        rr2 = tp2_offset / sl_offset if sl_offset != 0 else 0
        rr3 = tp3_offset / sl_offset if sl_offset != 0 else 0

        bar_filled = int(confidence / 5)
        bar_empty = 20 - bar_filled
        confidence_bar = "█" * bar_filled + "░" * bar_empty

        if confidence >= 80:
            verdict = "🟢 <b>VERDICT: SIGNAL FORT</b> — Entree recommandee"
        elif confidence >= 50:
            verdict = "🟡 <b>VERDICT: SIGNAL MOYEN</b> — Prudence recommandee"
        else:
            verdict = "🟠 <b>VERDICT: SIGNAL FAIBLE</b> — Risque eleve"

        msg = f"📊 <b>SIGNAL XAUUSD</b>\n═══════════════════════\n{emoji}\n💵 Prix actuel: <code>{current_price:.2f}</code> USD\n"

        if price_source == "M1_FALLBACK":
            msg += "⚠️ <b>ALERTE:</b> Prix tick fige — Utilisation du close M1 (donnees pouvant etre legerement retardees)\n"

        msg += f"📈 Confiance: <b>{confidence}%</b>\n{confidence_bar}\n\n"

        msg += "<b>🔍 DIAGNOSTIC MARCHE:</b>\n"
        if is_choppy:
            msg += "⚠️ <b>Marche choppy</b> — Range etroit, faible volatilite\n"
        if has_liquidity_sweep:
            msg += "⚠️ <b>Prise de liquidite detectee</b> — Manipulation possible\n"
        if problems:
            msg += f"📋 <b>Problemes identifies ({len(problems)}):</b>\n"
            for p in problems[:5]:
                msg += f"   • {p}\n"
        else:
            msg += "✅ <b>Aucun probleme majeur</b>\n"
        msg += "\n"

        msg += "<b>📋 STRATEGIES ACTIVES:</b>\n"

        if scores["BOS"] >= 20:
            msg += f"✅ <b>BOS</b>: {scores['BOS']}% — Break frais\n"
        elif scores["BOS"] >= 10:
            msg += f"⚠️ <b>BOS</b>: {scores['BOS']}% — Break vieillissant\n"
        else:
            msg += f"❌ <b>BOS</b>: {scores['BOS']}% — Aucun break recent\n"

        if scores["FVG"] >= 15:
            msg += f"✅ <b>FVG</b>: {scores['FVG']}% — Gap valide\n"
        elif scores["FVG"] >= 8:
            msg += f"⚠️ <b>FVG</b>: {scores['FVG']}% — Gap vieux\n"
        else:
            msg += f"❌ <b>FVG</b>: {scores['FVG']}% — Aucun gap\n"

        if scores["OTE"] >= 15:
            msg += f"✅ <b>OTE Fibonacci</b>: {scores['OTE']}% — Dans la zone\n"
        elif scores["OTE"] >= 6:
            msg += f"⚠️ <b>OTE Fibonacci</b>: {scores['OTE']}% — Proche zone\n"
        else:
            msg += f"❌ <b>OTE Fibonacci</b>: {scores['OTE']}% — Hors zone\n"

        if scores["H1_TREND"] >= 12:
            msg += f"✅ <b>Tendance H1</b>: {scores['H1_TREND']}% — Alignee\n"
        elif scores["H1_TREND"] >= 5:
            msg += f"⚠️ <b>Tendance H1</b>: {scores['H1_TREND']}% — Neutre\n"
        else:
            msg += f"❌ <b>Tendance H1</b>: {scores['H1_TREND']}% — Contre-tendance\n"

        if scores["MOMENTUM"] >= 8:
            msg += f"✅ <b>Momentum</b>: {scores['MOMENTUM']}% — Fort\n"
        elif scores["MOMENTUM"] >= 4:
            msg += f"⚠️ <b>Momentum</b>: {scores['MOMENTUM']}% — Faible\n"
        else:
            msg += f"❌ <b>Momentum</b>: {scores['MOMENTUM']}% — Plat\n"

        msg += (f"\n<b>🎯 RECOMMANDATIONS:</b>\n"
                f"🚪 Zone d'entree: <code>{entry_zone}</code>\n"
                f"🛑 Stop Loss: <code>{sl_price:.2f}</code> (risque: {sl_offset:.2f}$)\n"
                f"🎯 Take Profit 1: <code>{tp1_price:.2f}</code> (R:R ~1:{rr1:.1f})\n"
                f"🎯 Take Profit 2: <code>{tp2_price:.2f}</code> (R:R ~1:{rr2:.1f})\n"
                f"🎯 Take Profit 3: <code>{tp3_price:.2f}</code> (R:R ~1:{rr3:.1f})\n"
                f"📊 Ratios: 1:{rr1:.1f} | 1:{rr2:.1f} | 1:{rr3:.1f}\n"
                f"⏳ Validite: <b>{validity_minutes} min</b> — Expire a {datetime.fromtimestamp(self.last_signal_expire, tz=timezone.utc).strftime('%H:%M:%S')} UTC\n\n"
                f"{verdict}\n"
                f"⏰ {self._now_ms()}")
        return msg

    async def _connect_api(self):
        try:
            self._log("CONNEXION", "Connexion Deriv...")
            if await self.api.connect():
                auth_resp = await self.api.authorize(TOKEN)
                if auth_resp and auth_resp.get("authorize"):
                    self.authorized = True
                    self.ws_connected = True
                    loginid = auth_resp["authorize"].get("loginid", "N/A")
                    self._log("CONNEXION", f"Autorise — {loginid}")
                    msg = (f"🤖 <b>Bot XAUUSD Signal Active</b>\n"
                           f"📡 Compte: <code>{loginid}</code>\n"
                           f"📊 Asset: {SYMBOL}\n"
                           f"⚡ Mode: INTERACTIF — Tapez /signal pour un signal\n"
                           f"⏰ {self._now_ms()}")
                    await self._send_telegram(msg)
                    await self._handle_help_command()
                    return True
            return False
        except Exception as e:
            self._log("ERREUR", f"Connexion: {str(e)}")
            return False

    async def _disconnect_api(self):
        try:
            await self.api.disconnect()
        except:
            pass
        self.ws_connected = False
        self.authorized = False

    async def _fetch_h1_history(self):
        try:
            if not self.api.connected or not self.authorized:
                return
            req = {
                "ticks_history": SYMBOL,
                "adjust_start_time": 1,
                "count": MAX_HISTORY_H1,
                "end": "latest",
                "start": 1,
                "style": "candles",
                "granularity": 3600
            }
            resp = await self.api.ticks_history(req)
            if resp and "candles" in resp:
                self.h1_history = []
                for c in resp["candles"]:
                    self.h1_history.append({
                        "epoch": int(c.get("epoch", 0)),
                        "open": float(c.get("open", 0)),
                        "high": float(c.get("high", 0)),
                        "low": float(c.get("low", 0)),
                        "close": float(c.get("close", 0))
                    })
                self.h1_history = self.h1_history[-MAX_HISTORY_H1:]
                self._evaluate_h1_trend()
        except Exception as e:
            self._log("ERREUR", f"Fetch H1: {str(e)}")

    async def _fetch_m1_history(self):
        try:
            if not self.api.connected or not self.authorized:
                return
            req = {
                "ticks_history": SYMBOL,
                "adjust_start_time": 1,
                "count": MAX_HISTORY_M1,
                "end": "latest",
                "start": 1,
                "style": "candles",
                "granularity": 60
            }
            resp = await self.api.ticks_history(req)
            if resp and "candles" in resp:
                self.m1_history = []
                for c in resp["candles"]:
                    self.m1_history.append({
                        "epoch": int(c.get("epoch", 0)),
                        "open": float(c.get("open", 0)),
                        "high": float(c.get("high", 0)),
                        "low": float(c.get("low", 0)),
                        "close": float(c.get("close", 0))
                    })
                self.m1_history = self.m1_history[-MAX_HISTORY_M1:]
                self._log("HISTORIQUE", f"{len(self.m1_history)} bougies M1 chargees")
                self._detect_m1_structure()
        except Exception as e:
            self._log("ERREUR", f"Fetch M1: {str(e)}")

    def _evaluate_h1_trend(self):
        if len(self.h1_history) < 5:
            self.h1_trend = "NEUTRAL"
            return
        h1_swings_high = []
        h1_swings_low = []
        for i in range(SWING_LOOKBACK, len(self.h1_history) - SWING_LOOKBACK):
            prev2 = self.h1_history[i-2]
            prev1 = self.h1_history[i-1]
            curr = self.h1_history[i]
            next1 = self.h1_history[i+1]
            next2 = self.h1_history[i+2]
            if curr["high"] > prev2["high"] and curr["high"] > prev1["high"] and curr["high"] > next1["high"] and curr["high"] > next2["high"]:
                h1_swings_high.append({"price": curr["high"]})
            if curr["low"] < prev2["low"] and curr["low"] < prev1["low"] and curr["low"] < next1["low"] and curr["low"] < next2["low"]:
                h1_swings_low.append({"price": curr["low"]})
        last_close = self.h1_history[-1]["close"]
        if h1_swings_high and last_close > h1_swings_high[-1]["price"]:
            self.h1_trend = "BULLISH"
            self._log("TREND", "H1 BULLISH")
        elif h1_swings_low and last_close < h1_swings_low[-1]["price"]:
            self.h1_trend = "BEARISH"
            self._log("TREND", "H1 BEARISH")
        else:
            self.h1_trend = "NEUTRAL"
            self._log("TREND", "H1 NEUTRAL")

    async def _subscribe_ticks(self):
        try:
            if not self.api.connected or not self.authorized:
                return
            self._log("TICK STREAM", f"Abonnement {SYMBOL}...")

            def on_tick(data):
                try:
                    if "tick" in data:
                        tick = data["tick"]
                        price = float(tick.get("quote", 0))
                        self.last_price = price
                        self.last_price_timestamp = time.time()
                        self.last_tick_received_time = time.time()
                        self.tick_prices.append(price)
                        if len(self.tick_prices) > 20:
                            self.tick_prices = self.tick_prices[-20:]
                        asyncio.create_task(self.tick_queue.put({
                            "epoch": int(tick.get("epoch", time.time())),
                            "price": price,
                            "id": tick.get("id", "")
                        }))
                except Exception:
                    pass

            await self.api.subscribe({"ticks": SYMBOL}, on_tick)
            self._log("TICK STREAM", "Flux actif")
            while not self.shutdown_event.is_set() and self.ws_connected:
                await asyncio.sleep(5)
        except Exception as e:
            self._log("ERREUR", f"Stream: {str(e)}")
            self.ws_connected = False

    async def _tick_consumer_worker(self):
        self._log("WORKER", "Worker M1 demarre")
        while not self.shutdown_event.is_set():
            try:
                tick = await asyncio.wait_for(self.tick_queue.get(), timeout=1.0)
                await self._process_tick(tick)
                self.tick_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                self._log("ERREUR", f"Consumer: {str(e)}")

    async def _process_tick(self, tick):
        try:
            epoch = tick["epoch"]
            price = tick["price"]
            current_minute = epoch // 60
            if self.current_minute is not None and current_minute != self.current_minute:
                if self.current_m1:
                    self._finalize_m1_candle()
            if self.current_minute is None or current_minute != self.current_minute:
                self.current_minute = current_minute
                self.current_m1 = {
                    "epoch": current_minute * 60,
                    "open": price, "high": price, "low": price,
                    "close": price, "volume": 1
                }
            else:
                self.current_m1["high"] = max(self.current_m1["high"], price)
                self.current_m1["low"] = min(self.current_m1["low"], price)
                self.current_m1["close"] = price
                self.current_m1["volume"] += 1
        except Exception as e:
            self._log("ERREUR", f"Process tick: {str(e)}")

    def _finalize_m1_candle(self):
        if not self.current_m1:
            return
        self.m1_history.append(self.current_m1.copy())
        self.m1_history = self.m1_history[-MAX_HISTORY_M1:]
        candle_time = datetime.fromtimestamp(self.current_m1["epoch"], tz=timezone.utc).strftime("%H:%M")
        self._log("OHLC", f"M1 {candle_time} O:{self.current_m1['open']:.2f} H:{self.current_m1['high']:.2f} L:{self.current_m1['low']:.2f} C:{self.current_m1['close']:.2f}")
        self._detect_m1_structure()
        self.current_m1 = None

    def _detect_m1_structure(self):
        if len(self.m1_history) < 5:
            return
        self._update_swings()
        self._detect_bos()
        self._detect_fvg()

    def _update_swings(self):
        n = len(self.m1_history)
        if n < 5:
            return
        check_idx = n - 3
        if check_idx < 2 or check_idx + 2 >= n:
            return
        prev2 = self.m1_history[check_idx - 2]
        prev1 = self.m1_history[check_idx - 1]
        curr = self.m1_history[check_idx]
        next1 = self.m1_history[check_idx + 1]
        next2 = self.m1_history[check_idx + 2]
        if curr["high"] > prev2["high"] and curr["high"] > prev1["high"] and curr["high"] > next1["high"] and curr["high"] > next2["high"]:
            swing = {"type": "HIGH", "price": curr["high"], "epoch": curr["epoch"], "idx": check_idx}
            if not self.swing_highs or self.swing_highs[-1]["epoch"] != swing["epoch"]:
                self.swing_highs.append(swing)
                self._log("SWING", f"HIGH {swing['price']:.2f}")
        if curr["low"] < prev2["low"] and curr["low"] < prev1["low"] and curr["low"] < next1["low"] and curr["low"] < next2["low"]:
            swing = {"type": "LOW", "price": curr["low"], "epoch": curr["epoch"], "idx": check_idx}
            if not self.swing_lows or self.swing_lows[-1]["epoch"] != swing["epoch"]:
                self.swing_lows.append(swing)
                self._log("SWING", f"LOW {swing['price']:.2f}")
        self.swing_highs = self.swing_highs[-50:]
        self.swing_lows = self.swing_lows[-50:]

    def _detect_bos(self):
        if len(self.m1_history) < 3:
            return
        last_candle = self.m1_history[-1]
        last_close = last_candle["close"]
        if self.swing_highs:
            last_sh = self.swing_highs[-1]
            if last_close > last_sh["price"]:
                if not self.last_bos or self.last_bos.get("epoch") != last_candle["epoch"] or self.last_bos.get("type") != "BULLISH":
                    origin = None
                    for sl in reversed(self.swing_lows):
                        if sl["epoch"] < last_sh["epoch"]:
                            origin = sl
                            break
                    self.last_bos = {"type": "BULLISH", "epoch": last_candle["epoch"], "close_price": last_close,
                                     "swing_high": last_sh, "origin": origin, "candle_idx": len(self.m1_history) - 1}
                    self._log("BOS", f"HAUSSIER {last_close:.2f} > {last_sh['price']:.2f}")
        if self.swing_lows:
            last_sl = self.swing_lows[-1]
            if last_close < last_sl["price"]:
                if not self.last_bos or self.last_bos.get("epoch") != last_candle["epoch"] or self.last_bos.get("type") != "BEARISH":
                    origin = None
                    for sh in reversed(self.swing_highs):
                        if sh["epoch"] < last_sl["epoch"]:
                            origin = sh
                            break
                    self.last_bos = {"type": "BEARISH", "epoch": last_candle["epoch"], "close_price": last_close,
                                     "swing_low": last_sl, "origin": origin, "candle_idx": len(self.m1_history) - 1}
                    self._log("BOS", f"BAISSIER {last_close:.2f} < {last_sl['price']:.2f}")

    def _detect_fvg(self):
        if len(self.m1_history) < 3 or not self.last_bos:
            return
        bos_idx = self.last_bos.get("candle_idx", 0)
        if bos_idx != len(self.m1_history) - 1:
            return
        i = len(self.m1_history) - 1
        if self.last_bos["type"] == "BULLISH" and i >= 2:
            if self.m1_history[i-2]["high"] < self.m1_history[i]["low"]:
                self.active_fvgs.append({"type": "BULLISH", "top": self.m1_history[i]["low"],
                    "bottom": self.m1_history[i-2]["high"], "epoch": self.m1_history[i]["epoch"],
                    "bos_epoch": self.last_bos["epoch"], "filled": False})
                self._log("FVG", f"HAUSSIER [{self.m1_history[i-2]['high']:.2f} -> {self.m1_history[i]['low']:.2f}]")
        elif self.last_bos["type"] == "BEARISH" and i >= 2:
            if self.m1_history[i-2]["low"] > self.m1_history[i]["high"]:
                self.active_fvgs.append({"type": "BEARISH", "top": self.m1_history[i-2]["low"],
                    "bottom": self.m1_history[i]["high"], "epoch": self.m1_history[i]["epoch"],
                    "bos_epoch": self.last_bos["epoch"], "filled": False})
                self._log("FVG", f"BAISSIER [{self.m1_history[i]['high']:.2f} -> {self.m1_history[i-2]['low']:.2f}]")
        self._purge_filled_fvgs()
        self.active_fvgs = self.active_fvgs[-20:]

    def _purge_filled_fvgs(self):
        if not self.m1_history:
            return
        current_price = self.m1_history[-1]["close"]
        for fvg in self.active_fvgs:
            if fvg["filled"]:
                continue
            if fvg["type"] == "BULLISH" and current_price <= fvg["bottom"]:
                fvg["filled"] = True
                self._log("FVG", f"H comble a {current_price:.2f}")
            elif fvg["type"] == "BEARISH" and current_price >= fvg["top"]:
                fvg["filled"] = True
                self._log("FVG", f"B comble a {current_price:.2f}")

    async def _heartbeat_task(self):
        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(60)
                now = time.time()
                price_age = now - self.last_price_timestamp
                tick_age = now - self.last_tick_received_time

                if not self.ws_connected:
                    self._log("HEARTBEAT", "WS deconnecte")
                else:
                    price_str = f"{self.last_price:.2f}" if self.last_price else "N/A"
                    self._log("STATUS", f"Trend H1:{self.h1_trend} | Prix:{price_str} | Bougies:{len(self.m1_history)} | PrixAge:{price_age:.0f}s | TickAge:{tick_age:.0f}s")

                if self.ws_connected and price_age > 180:
                    self._log("HEARTBEAT", f"CRITIQUE: Prix fige depuis {price_age:.0f}s > 180s — Forçage reconnexion WebSocket")
                    self.ws_connected = False

                if self.ws_connected and tick_age > 60:
                    self._log("HEARTBEAT", f"WARNING: Aucun tick recu depuis {tick_age:.0f}s > 60s — Flux probablement coupe, resubscribe force")
                    self.ws_connected = False

            except Exception as e:
                self._log("HEARTBEAT", f"Erreur: {str(e)}")

    async def _reconnect_routine(self):
        check_interval = 5
        fail_backoff = 10
        last_attempt = 0
        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(check_interval)
                if not self.ws_connected or not self.authorized:
                    now = time.time()
                    if now - last_attempt >= fail_backoff:
                        last_attempt = now
                        self._log("RECONNECT", "Tentative reconnexion...")
                        await self._disconnect_api()
                        await asyncio.sleep(2)
                        if await self._connect_api():
                            await self._fetch_h1_history()
                            await self._fetch_m1_history()
                            asyncio.create_task(self._subscribe_ticks())
                            fail_backoff = 10
                        else:
                            fail_backoff = min(fail_backoff * 2, 120)
                            self._log("RECONNECT", f"Echec connexion — prochaine tentative dans {fail_backoff}s")
            except Exception as e:
                self._log("ERREUR", f"Reconnect: {str(e)}")
                await asyncio.sleep(10)

    async def _shutdown(self, reason):
        self._log("SHUTDOWN", f"Arret: {reason}")
        await self._send_telegram(f"🔴 <b>Bot Signal arrete</b>\nRaison: {reason}")
        self.shutdown_event.set()
        await self._disconnect_api()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        sys.exit(0)

    async def run(self):
        self._log("BOOT", "═══════════════════════════════════════════")
        self._log("BOOT", "  XAUUSD SIGNAL BOT — Interactif Telegram")
        self._log("BOOT", "  Commandes: /signal /status /price /help")
        self._log("BOOT", "  Choix force: ACHAT ou VENTE avec %")
        self._log("BOOT", "  Connexion: WebSocket Deriv DIRECT")
        self._log("BOOT", "═══════════════════════════════════════════")

        if not await self._connect_api():
            self._log("FATAL", "Connexion echouee")
            return

        await self._fetch_h1_history()
        await self._fetch_m1_history()
        self.tasks = [
            asyncio.create_task(self._subscribe_ticks()),
            asyncio.create_task(self._tick_consumer_worker()),
            asyncio.create_task(self._telegram_command_listener()),
            asyncio.create_task(self._heartbeat_task()),
            asyncio.create_task(self._reconnect_routine()),
        ]

        self._log("BOOT", "Bot en ligne. En attente de commandes Telegram...")

        try:
            while not self.shutdown_event.is_set():
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            await self._shutdown("INTERRUPTION UTILISATEUR")
        except Exception as e:
            self._log("FATAL", f"Exception: {str(e)}")
            await self._shutdown("EXCEPTION CRITIQUE")


if __name__ == "__main__":
    nest_asyncio.apply()
    bot = XAUUSDSignalBot()
    try:
        asyncio.run(bot.run())
    except Exception as e:
        print(f"[FATAL] {str(e)}")
        sys.exit(1)
