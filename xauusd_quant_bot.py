#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD Quant Bot v2.0 — Telebot Edition
Compatible PythonAnywhere & pyTelegramBotAPI
"""

import os
import re
import json
import time
import threading
import logging
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple
from enum import Enum
from collections import deque

import numpy as np
import pandas as pd
import yfinance as yf
import telebot
from telebot import types

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

TOKEN = "8842089863:AAGkqa0yPvojFVYj9vJH3kBOIiNfpdjbCPg"
TICKER = "GC=F"
SCORE_MIN_TRADE = 60
SCORE_PREMIUM = 85
RR_MIN = 1.5
RR_OPTIMAL = 2.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────

class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"

class Regime(Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    EXPANSION = "EXPANSION"
    CHAOS = "CHAOS"

class Qualite(Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    REFUSE = "REFUSE"

class Action(Enum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    MOVE_SL_BE = "MOVE_SL_BREAK_EVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    WAIT = "WAIT"

# ─────────────────────────────────────────────────────────────
# CLASSES DE DONNEES
# ─────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    structure_marche: float = 0.0
    liquidite_smc: float = 0.0
    alignement_mtf: float = 0.0
    volatilite_conditions: float = 0.0
    risk_reward: float = 0.0
    timing_entree: float = 0.0
    contexte_macro: float = 0.0

    @property
    def total(self) -> float:
        return (self.structure_marche + self.liquidite_smc + self.alignement_mtf +
                self.volatilite_conditions + self.risk_reward + self.timing_entree + self.contexte_macro)

@dataclass
class Setup:
    direction: Direction = Direction.NONE
    prix_entree: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    rr: float = 0.0
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    qualite: Qualite = Qualite.REFUSE
    regime: Regime = Regime.CHAOS
    session: str = ""
    invalidation: str = ""
    raison_refus: str = ""
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

@dataclass
class Position:
    direction: Direction
    prix_entree: float
    stop_loss: float
    take_profit: float
    volume: float = 1.0
    timestamp_ouverture: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    partial_closed: bool = False
    sl_moved_be: bool = False
    pnl_pct: float = 0.0

    def update_pnl(self, prix_actuel: float):
        if self.direction == Direction.BUY:
            self.pnl_pct = (prix_actuel - self.prix_entree) / self.prix_entree * 100
        elif self.direction == Direction.SELL:
            self.pnl_pct = (self.prix_entree - prix_actuel) / self.prix_entree * 100

# ─────────────────────────────────────────────────────────────
# MEMOIRE UTILISATEUR
# ─────────────────────────────────────────────────────────────

class UserMemory:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.position: Optional[Position] = None
        self.historique_setups: deque = deque(maxlen=50)
        self.dernier_setup: Optional[Setup] = None
        self.preferences: Dict = {"alertes": True, "mode_agressif": False}

    def set_position(self, direction: Direction, prix: float, sl: float, tp: float, volume: float = 1.0):
        self.position = Position(direction, prix, sl, tp, volume)
        logger.info("[%s] Position ouverte: %s @ %s", self.user_id, direction.value, prix)

    def close_position(self):
        if self.position:
            logger.info("[%s] Position fermee. P&L: %.2f%%", self.user_id, self.position.pnl_pct)
        self.position = None

    def save_setup(self, setup: Setup):
        self.dernier_setup = setup
        self.historique_setups.append(asdict(setup))

    def get_context(self) -> str:
        if self.position:
            return f"Position {self.position.direction.value} @ {self.position.prix_entree} | P&L: {self.position.pnl_pct:.2f}%"
        return "Aucune position ouverte"

USER_MEMORIES: Dict[int, UserMemory] = {}

def get_user_memory(user_id: int) -> UserMemory:
    if user_id not in USER_MEMORIES:
        USER_MEMORIES[user_id] = UserMemory(user_id)
    return USER_MEMORIES[user_id]

# ─────────────────────────────────────────────────────────────
# MOTEUR DE DONNEES MARCHE
# ─────────────────────────────────────────────────────────────

class MarketEngine:
    def __init__(self, ticker: str = TICKER):
        self.ticker = ticker
        self.ticker_obj = yf.Ticker(ticker)
        self.last_data: Optional[Dict[str, pd.DataFrame]] = None
        self.last_update: Optional[datetime] = None

    def get_realtime_price(self) -> Optional[dict]:
        try:
            hist = self.ticker_obj.history(period="1d", interval="1m")
            if hist.empty:
                return None
            last = hist.iloc[-1]
            first = hist.iloc[0]
            return {
                "price": float(last["Close"]),
                "open": float(last["Open"]),
                "high": float(last["High"]),
                "low": float(last["Low"]),
                "volume": int(last["Volume"]),
                "timestamp": str(last.name),
                "change_pct": ((last["Close"] - first["Open"]) / first["Open"] * 100) if len(hist) > 1 else 0.0
            }
        except Exception as e:
            logger.error("Erreur prix temps reel: %s", e)
            return None

    def get_multi_tf_data(self, period: str = "5d") -> Dict[str, pd.DataFrame]:
        data = {}
        intervals = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}
        for tf_name, interval in intervals.items():
            try:
                df = self.ticker_obj.history(period=period, interval=interval)
                if not df.empty:
                    data[tf_name] = df
            except Exception as e:
                logger.warning("Erreur TF %s: %s", tf_name, e)
        self.last_data = data
        self.last_update = datetime.utcnow()
        return data

    def get_current_session(self) -> str:
        hour = datetime.utcnow().hour
        if 0 <= hour < 8:
            return "ASIE (Tokyo/Sydney)"
        elif 8 <= hour < 13:
            return "LONDON (Ouverture)"
        elif 13 <= hour < 17:
            return "NEW YORK (Overlap London-NY)"
        elif 17 <= hour < 21:
            return "NEW YORK (Solo)"
        else:
            return "LOW LIQUIDITY (Fermeture)"

MARKET = MarketEngine()

# ─────────────────────────────────────────────────────────────
# INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────

class TechnicalIndicators:
    @staticmethod
    def sma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(window=period).mean()

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - df["Close"].shift()).abs()
        low_close = (df["Low"] - df["Close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
        sma = series.rolling(window=period).mean()
        rolling_std = series.rolling(window=period).std()
        upper = sma + (rolling_std * std)
        lower = sma - (rolling_std * std)
        return upper, sma, lower

    @staticmethod
    def detect_swing_points(df: pd.DataFrame, lookback: int = 5) -> Tuple[List, List]:
        highs = df["High"].values
        lows = df["Low"].values
        swing_highs = []
        swing_lows = []
        for i in range(lookback, len(df) - lookback):
            if all(highs[i] >= highs[i-j] for j in range(1, lookback+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1, lookback+1)):
                swing_highs.append((i, float(highs[i])))
            if all(lows[i] <= lows[i-j] for j in range(1, lookback+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1, lookback+1)):
                swing_lows.append((i, float(lows[i])))
        return swing_highs, swing_lows

# ─────────────────────────────────────────────────────────────
# SMART MONEY CONCEPTS
# ─────────────────────────────────────────────────────────────

class SmartMoneyConcepts:
    @staticmethod
    def detect_liquidity_sweep(df: pd.DataFrame, swing_highs: List, swing_lows: List) -> dict:
        if not swing_highs or not swing_lows:
            return {"detected": False}
        last_high = swing_highs[-1][1]
        last_low = swing_lows[-1][1]
        current_price = float(df["Close"].iloc[-1])
        if current_price > last_high * 1.001:
            return {"detected": True, "direction": "SELL", "swept_level": last_high,
                    "confidence": min((current_price - last_high) / last_high * 100 * 10, 1.0),
                    "type": "LIQUIDITY_SWEEP_HIGH"}
        if current_price < last_low * 0.999:
            return {"detected": True, "direction": "BUY", "swept_level": last_low,
                    "confidence": min((last_low - current_price) / last_low * 100 * 10, 1.0),
                    "type": "LIQUIDITY_SWEEP_LOW"}
        return {"detected": False}

    @staticmethod
    def detect_fvg(df: pd.DataFrame) -> List[dict]:
        fvgs = []
        for i in range(len(df) - 3):
            if df["Low"].iloc[i+2] > df["High"].iloc[i]:
                fvgs.append({"type": "BULLISH_FVG", "top": float(df["Low"].iloc[i+2]),
                             "bottom": float(df["High"].iloc[i]), "index": i,
                             "mid": (float(df["Low"].iloc[i+2]) + float(df["High"].iloc[i])) / 2})
            elif df["High"].iloc[i+2] < df["Low"].iloc[i]:
                fvgs.append({"type": "BEARISH_FVG", "top": float(df["Low"].iloc[i]),
                             "bottom": float(df["High"].iloc[i+2]), "index": i,
                             "mid": (float(df["Low"].iloc[i]) + float(df["High"].iloc[i+2])) / 2})
        return fvgs

    @staticmethod
    def detect_order_blocks(df: pd.DataFrame) -> List[dict]:
        obs = []
        for i in range(2, len(df) - 1):
            c_prev = float(df["Close"].iloc[i-1])
            o_prev = float(df["Open"].iloc[i-1])
            c_curr = float(df["Close"].iloc[i])
            if c_prev < o_prev and c_curr > o_prev and c_curr > c_prev * 1.003:
                obs.append({"type": "BULLISH_OB", "high": float(df["High"].iloc[i-1]),
                            "low": float(df["Low"].iloc[i-1]), "index": i-1,
                            "mid": (float(df["High"].iloc[i-1]) + float(df["Low"].iloc[i-1])) / 2})
            elif c_prev > o_prev and c_curr < o_prev and c_curr < c_prev * 0.997:
                obs.append({"type": "BEARISH_OB", "high": float(df["High"].iloc[i-1]),
                            "low": float(df["Low"].iloc[i-1]), "index": i-1,
                            "mid": (float(df["High"].iloc[i-1]) + float(df["Low"].iloc[i-1])) / 2})
        return obs

    @staticmethod
    def detect_choch_bos(df: pd.DataFrame) -> dict:
        highs = df["High"].values
        lows = df["Low"].values
        if len(highs) < 10:
            return {"choch": False, "bos": False}
        last_swing_high = max(highs[-10:-1])
        last_swing_low = min(lows[-10:-1])
        return {
            "bos_bullish": float(highs[-1]) > float(last_swing_high),
            "bos_bearish": float(lows[-1]) < float(last_swing_low),
            "choch_bullish": float(highs[-1]) > float(highs[-3]) and float(lows[-1]) < float(lows[-3]),
            "choch_bearish": float(lows[-1]) < float(lows[-3]) and float(highs[-1]) > float(highs[-3])
        }

# ─────────────────────────────────────────────────────────────
# SCORING QUANT
# ─────────────────────────────────────────────────────────────

class QuantScorer:
    @staticmethod
    def score_structure_marche(df: pd.DataFrame) -> float:
        score = 0.0
        ema20 = TechnicalIndicators.ema(df["Close"], 20)
        ema50 = TechnicalIndicators.ema(df["Close"], 50)
        if len(ema20) > 50:
            last_ema20 = float(ema20.iloc[-1])
            last_ema50 = float(ema50.iloc[-1])
            last_close = float(df["Close"].iloc[-1])
            if last_ema20 > last_ema50:
                score += 8
            elif last_ema20 < last_ema50:
                score += 8
            if abs(last_close - last_ema20) / last_ema20 < 0.002:
                score += 4
            elif last_close > last_ema20:
                score += 6
            else:
                score += 2
            swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df)
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                last_sh = swing_highs[-1][1]
                prev_sh = swing_highs[-2][1]
                last_sl = swing_lows[-1][1]
                prev_sl = swing_lows[-2][1]
                if last_sh > prev_sh and last_sl > prev_sl:
                    score += 6
                elif last_sh < prev_sh and last_sl < prev_sl:
                    score += 6
                else:
                    score += 2
        return min(score, 20)

    @staticmethod
    def score_liquidite_smc(df: pd.DataFrame) -> float:
        score = 0.0
        swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df, lookback=3)
        sweep = SmartMoneyConcepts.detect_liquidity_sweep(df, swing_highs, swing_lows)
        if sweep["detected"]:
            score += 10 * sweep["confidence"]
        fvgs = SmartMoneyConcepts.detect_fvg(df)
        current_price = float(df["Close"].iloc[-1])
        for fvg in fvgs[-5:]:
            if fvg["bottom"] < current_price < fvg["top"]:
                score += 5
                break
        obs = SmartMoneyConcepts.detect_order_blocks(df)
        for ob in obs[-5:]:
            if ob["low"] < current_price < ob["high"]:
                score += 5
                break
        return min(score, 20)

    @staticmethod
    def score_alignement_mtf(data_dict: Dict[str, pd.DataFrame]) -> float:
        directions = []
        for tf_name, df in data_dict.items():
            if len(df) < 50:
                continue
            ema20 = TechnicalIndicators.ema(df["Close"], 20)
            ema50 = TechnicalIndicators.ema(df["Close"], 50)
            if len(ema20) > 50 and len(ema50) > 50:
                if float(ema20.iloc[-1]) > float(ema50.iloc[-1]):
                    directions.append("UP")
                else:
                    directions.append("DOWN")
        if not directions:
            return 0.0
        up_count = directions.count("UP")
        down_count = directions.count("DOWN")
        total = len(directions)
        if up_count / total >= 0.8 or down_count / total >= 0.8:
            return 15
        elif up_count / total >= 0.6 or down_count / total >= 0.6:
            return 10
        elif up_count / total >= 0.5 or down_count / total >= 0.5:
            return 5
        return 0.0

    @staticmethod
    def score_volatilite_conditions(df: pd.DataFrame) -> float:
        score = 0.0
        if len(df) < 20:
            return 0.0
        atr = TechnicalIndicators.atr(df)
        last_atr = float(atr.iloc[-1])
        avg_atr = float(atr.iloc[-20:].mean())
        atr_ratio = last_atr / avg_atr if avg_atr > 0 else 1.0
        if 0.8 <= atr_ratio <= 1.5:
            score += 8
        elif 0.5 <= atr_ratio < 0.8:
            score += 4
        elif atr_ratio > 1.5:
            score += 3
        else:
            score += 1
        upper, sma, lower = TechnicalIndicators.bollinger_bands(df["Close"])
        bb_width = (float(upper.iloc[-1]) - float(lower.iloc[-1])) / float(sma.iloc[-1]) if float(sma.iloc[-1]) > 0 else 0
        if 0.005 < bb_width < 0.02:
            score += 7
        elif bb_width < 0.005:
            score += 2
        else:
            score += 3
        return min(score, 15)

    @staticmethod
    def score_risk_reward(direction: Direction, entry: float, sl: float, tp: float) -> float:
        if direction == Direction.NONE or sl == entry:
            return 0.0
        if direction == Direction.BUY:
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp
        if risk <= 0:
            return 0.0
        rr = reward / risk
        if rr >= RR_OPTIMAL:
            return 15
        elif rr >= RR_MIN:
            return 10 + (rr - RR_MIN) / (RR_OPTIMAL - RR_MIN) * 5
        elif rr >= 1.0:
            return 5 + (rr - 1.0) / (RR_MIN - 1.0) * 5
        return max(rr * 5, 0)

    @staticmethod
    def score_timing_entree(df: pd.DataFrame, direction: Direction) -> float:
        score = 0.0
        if len(df) < 10:
            return 0.0
        rsi = TechnicalIndicators.rsi(df["Close"])
        last_rsi = float(rsi.iloc[-1])
        if direction == Direction.BUY:
            if 30 < last_rsi < 50:
                score += 5
            elif 50 < last_rsi < 70:
                score += 3
            else:
                score += 1
        elif direction == Direction.SELL:
            if 50 < last_rsi < 70:
                score += 5
            elif 30 < last_rsi < 50:
                score += 3
            else:
                score += 1
        last_3 = df["Close"].iloc[-3:].pct_change().abs().sum()
        if last_3 < 0.005:
            score += 5
        elif last_3 < 0.01:
            score += 3
        else:
            score += 1
        return min(score, 10)

    @staticmethod
    def score_contexte_macro() -> float:
        hour = datetime.utcnow().hour
        if hour in [8, 9, 13, 14]:
            return 2
        if 8 <= hour <= 17:
            return 5
        return 3

# ─────────────────────────────────────────────────────────────
# CLASSIFICATION REGIME
# ─────────────────────────────────────────────────────────────

class RegimeClassifier:
    @staticmethod
    def classify(df: pd.DataFrame) -> Regime:
        if len(df) < 50:
            return Regime.CHAOS
        atr = TechnicalIndicators.atr(df)
        ema20 = TechnicalIndicators.ema(df["Close"], 20)
        ema50 = TechnicalIndicators.ema(df["Close"], 50)
        last_atr = float(atr.iloc[-1])
        avg_atr = float(atr.iloc[-20:].mean())
        ema_dist = abs(float(ema20.iloc[-1]) - float(ema50.iloc[-1])) / float(ema50.iloc[-1]) if float(ema50.iloc[-1]) > 0 else 0
        upper, sma, lower = TechnicalIndicators.bollinger_bands(df["Close"])
        bb_width = (float(upper.iloc[-1]) - float(lower.iloc[-1])) / float(sma.iloc[-1]) if float(sma.iloc[-1]) > 0 else 0
        if ema_dist > 0.005 and 0.8 <= last_atr / avg_atr <= 1.3 and bb_width > 0.008:
            return Regime.TREND
        if ema_dist < 0.003 and last_atr / avg_atr < 0.8 and bb_width < 0.008:
            return Regime.RANGE
        if last_atr / avg_atr > 1.5 and bb_width > 0.015:
            return Regime.EXPANSION
        return Regime.CHAOS

# ─────────────────────────────────────────────────────────────
# GENERATEUR DE SETUPS
# ─────────────────────────────────────────────────────────────

class SetupGenerator:
    @staticmethod
    def generate_setup(data_dict: Dict[str, pd.DataFrame]) -> Setup:
        setup = Setup()
        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)
        if df is None or len(df) < 50:
            setup.raison_refus = "Donnees insuffisantes"
            return setup
        regime = RegimeClassifier.classify(df)
        setup.regime = regime
        if regime == Regime.CHAOS:
            setup.raison_refus = "Regime CHAOS — Interdiction de trader"
            return setup
        swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df)
        sweep = SmartMoneyConcepts.detect_liquidity_sweep(df, swing_highs, swing_lows)
        fvgs = SmartMoneyConcepts.detect_fvg(df)
        obs = SmartMoneyConcepts.detect_order_blocks(df)
        choch_bos = SmartMoneyConcepts.detect_choch_bos(df)
        current_price = float(df["Close"].iloc[-1])
        atr = float(TechnicalIndicators.atr(df).iloc[-1])
        direction = Direction.NONE
        entry = current_price
        sl = 0.0
        tp = 0.0
        raison = ""

        if sweep["detected"]:
            direction = Direction.BUY if sweep["direction"] == "BUY" else Direction.SELL
            if direction == Direction.BUY:
                sl = sweep["swept_level"] - atr * 1.5
                if swing_highs:
                    tp = max([sh[1] for sh in swing_highs[-3:]])
                else:
                    tp = current_price + atr * 3
            else:
                sl = sweep["swept_level"] + atr * 1.5
                if swing_lows:
                    tp = min([sl[1] for sl in swing_lows[-3:]])
                else:
                    tp = current_price - atr * 3
            raison = f"Liquidity Sweep {sweep['type']} — Retour a l'equilibre attendu"
        elif fvgs:
            last_fvg = fvgs[-1]
            if last_fvg["type"] == "BULLISH_FVG" and current_price > last_fvg["top"]:
                direction = Direction.BUY
                entry = last_fvg["mid"]
                sl = last_fvg["bottom"] - atr
                tp = current_price + atr * 2.5
                raison = "FVG haussier — Retest de la zone d'equilibre"
            elif last_fvg["type"] == "BEARISH_FVG" and current_price < last_fvg["bottom"]:
                direction = Direction.SELL
                entry = last_fvg["mid"]
                sl = last_fvg["top"] + atr
                tp = current_price - atr * 2.5
                raison = "FVG baissier — Retest de la zone d'equilibre"
        elif obs and (choch_bos["choch_bullish"] or choch_bos["bos_bullish"]):
            last_ob = [o for o in obs if o["type"] == "BULLISH_OB"]
            if last_ob:
                ob = last_ob[-1]
                if current_price > ob["low"] and current_price < ob["high"] * 1.002:
                    direction = Direction.BUY
                    entry = ob["mid"]
                    sl = ob["low"] - atr
                    tp = current_price + atr * 2
                    raison = "Order Block haussier + BOS — Accumulation institutionnelle"
        elif obs and (choch_bos["choch_bearish"] or choch_bos["bos_bearish"]):
            last_ob = [o for o in obs if o["type"] == "BEARISH_OB"]
            if last_ob:
                ob = last_ob[-1]
                if current_price < ob["high"] and current_price > ob["low"] * 0.998:
                    direction = Direction.SELL
                    entry = ob["mid"]
                    sl = ob["high"] + atr
                    tp = current_price - atr * 2
                    raison = "Order Block baissier + BOS — Distribution institutionnelle"
        elif regime == Regime.RANGE:
            upper_bb, sma, lower_bb = TechnicalIndicators.bollinger_bands(df["Close"])
            if current_price >= float(upper_bb.iloc[-1]) * 0.998:
                direction = Direction.SELL
                entry = current_price
                sl = current_price + atr * 1.5
                tp = float(sma.iloc[-1])
                raison = "Range — Prix au-dessus BB superieure (mean reversion)"
            elif current_price <= float(lower_bb.iloc[-1]) * 1.002:
                direction = Direction.BUY
                entry = current_price
                sl = current_price - atr * 1.5
                tp = float(sma.iloc[-1])
                raison = "Range — Prix sous BB inferieure (mean reversion)"
        elif regime == Regime.EXPANSION:
            if choch_bos["bos_bullish"]:
                direction = Direction.BUY
                entry = current_price
                sl = current_price - atr * 2
                tp = current_price + atr * 4
                raison = "Expansion haussiere — Breakout de structure"
            elif choch_bos["bos_bearish"]:
                direction = Direction.SELL
                entry = current_price
                sl = current_price + atr * 2
                tp = current_price - atr * 4
                raison = "Expansion baissiere — Breakdown de structure"

        if direction == Direction.NONE:
            setup.raison_refus = "Aucune logique institutionnelle detectee — WAIT"
            return setup

        if direction == Direction.BUY:
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp
        rr = reward / risk if risk != 0 else 0
        if rr < RR_MIN:
            setup.raison_refus = f"RR insuffisant ({rr:.2f} < {RR_MIN}) — WAIT"
            return setup

        setup.direction = direction
        setup.prix_entree = round(entry, 2)
        setup.stop_loss = round(sl, 2)
        setup.take_profit = round(tp, 2)
        setup.rr = round(rr, 2)
        setup.session = MARKET.get_current_session()
        setup.invalidation = f"Break de {sl} invalide le setup"
        setup.score.structure_marche = QuantScorer.score_structure_marche(df)
        setup.score.liquidite_smc = QuantScorer.score_liquidite_smc(df)
        setup.score.alignement_mtf = QuantScorer.score_alignement_mtf(data_dict)
        setup.score.volatilite_conditions = QuantScorer.score_volatilite_conditions(df)
        setup.score.risk_reward = QuantScorer.score_risk_reward(direction, entry, sl, tp)
        setup.score.timing_entree = QuantScorer.score_timing_entree(df, direction)
        setup.score.contexte_macro = QuantScorer.score_contexte_macro()

        total = setup.score.total
        if total >= 85:
            setup.qualite = Qualite.A_PLUS
        elif total >= 75:
            setup.qualite = Qualite.A
        elif total >= 60:
            setup.qualite = Qualite.B
        elif total >= 50:
            setup.qualite = Qualite.C
        else:
            setup.qualite = Qualite.REFUSE
            setup.raison_refus = f"Score trop faible ({total:.0f}/100) — WAIT"
        return setup

    @staticmethod
    def evaluate_position(position: Position, current_price: float, data_dict: Dict[str, pd.DataFrame]) -> Tuple[Action, str]:
        position.update_pnl(current_price)
        pnl = position.pnl_pct
        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)
        if df is None or len(df) < 20:
            return Action.HOLD, "Donnees insuffisantes pour evaluation"
        atr = float(TechnicalIndicators.atr(df).iloc[-1])
        if position.direction == Direction.BUY and current_price <= position.stop_loss:
            return Action.CLOSE, "Stop Loss atteint — Fermeture obligatoire"
        if position.direction == Direction.SELL and current_price >= position.stop_loss:
            return Action.CLOSE, "Stop Loss atteint — Fermeture obligatoire"
        if position.direction == Direction.BUY and current_price >= position.take_profit:
            return Action.CLOSE, "Take Profit atteint — Fermeture complete"
        if position.direction == Direction.SELL and current_price <= position.take_profit:
            return Action.CLOSE, "Take Profit atteint — Fermeture complete"
        if not position.sl_moved_be and pnl >= 1.0:
            return Action.MOVE_SL_BE, f"P&L +{pnl:.2f}% — Deplacer SL au BE"
        if not position.partial_closed and pnl >= 2.0:
            return Action.PARTIAL_CLOSE, f"P&L +{pnl:.2f}% — Fermeture partielle 50%"
        choch_bos = SmartMoneyConcepts.detect_choch_bos(df)
        if position.direction == Direction.BUY and choch_bos["choch_bearish"]:
            return Action.CLOSE, "CHoCH baissier — Structure invalide, fermeture"
        if position.direction == Direction.SELL and choch_bos["choch_bullish"]:
            return Action.CLOSE, "CHoCH haussier — Structure invalide, fermeture"
        regime = RegimeClassifier.classify(df)
        if regime == Regime.CHAOS:
            return Action.CLOSE, "Regime CHAOS — Fermeture protective"
        if position.direction == Direction.BUY:
            remaining_reward = position.take_profit - current_price
            remaining_risk = current_price - position.stop_loss
        else:
            remaining_reward = current_price - position.take_profit
            remaining_risk = position.stop_loss - current_price
        remaining_rr = remaining_reward / remaining_risk if remaining_risk > 0 else 0
        if remaining_rr < 0.5 and pnl > 0.5:
            return Action.CLOSE, f"RR restant faible ({remaining_rr:.2f}) — Fermeture prudente"
        return Action.HOLD, f"Position valide — P&L: {pnl:.2f}% | RR restant: {remaining_rr:.2f}"

# ─────────────────────────────────────────────────────────────
# FORMATAGE TELEGRAM
# ─────────────────────────────────────────────────────────────

class TelegramFormatter:
    @staticmethod
    def format_setup(setup: Setup, current_price: float) -> str:
        if setup.qualite == Qualite.REFUSE:
            return ("*XAUUSD QUANT — ANALYSE*\n\n"
                    "*DECISION: WAIT*\n\n"
                    f"*Score: {setup.score.total:.0f}/100*\n"
                    "*Qualite: REFUSE*\n\n"
                    "*Raison:*\n"
                    f"{setup.raison_refus}\n\n"
                    f"*Prix actuel: {current_price:.2f}*\n"
                    f"*Session: {setup.session}*\n\n"
                    "*Pas de trade = decision valide*")

        emoji_dir = "BUY" if setup.direction == Direction.BUY else "SELL"
        emoji_qual = "A+" if setup.qualite == Qualite.A_PLUS else "A" if setup.qualite == Qualite.A else "B"

        return (f"*XAUUSD QUANT — SETUP {emoji_dir}*\n\n"
                f"*Score: {setup.score.total:.0f}/100*\n"
                f"*Qualite: {emoji_qual}*\n\n"
                "*Contexte:*\n"
                f"• Regime: {setup.regime.value}\n"
                f"• Session: {setup.session}\n"
                f"• Prix actuel: {current_price:.2f}\n\n"
                "*Analyse:*\n"
                f"{setup.raison_refus if setup.raison_refus else 'Setup valide par scoring quant'}\n\n"
                "*Plan:*\n"
                f"• Entree: `{setup.prix_entree:.2f}`\n"
                f"• Stop Loss: `{setup.stop_loss:.2f}`\n"
                f"• Take Profit: `{setup.take_profit:.2f}`\n"
                f"• RR: `1:{setup.rr:.2f}`\n\n"
                "*Detail Scoring:*\n"
                f"• Structure: {setup.score.structure_marche:.0f}/20\n"
                f"• Liquidite/SMC: {setup.score.liquidite_smc:.0f}/20\n"
                f"• Alignement MTF: {setup.score.alignement_mtf:.0f}/15\n"
                f"• Volatilite: {setup.score.volatilite_conditions:.0f}/15\n"
                f"• Risk/Reward: {setup.score.risk_reward:.0f}/15\n"
                f"• Timing: {setup.score.timing_entree:.0f}/10\n"
                f"• Macro: {setup.score.contexte_macro:.0f}/5\n\n"
                "*Timing:*\n"
                "Entree sur confirmation\n\n"
                "*Gestion:*\n"
                "• SL -> BE a +1%\n"
                "• Partial close a +2%\n"
                f"• Invalidation: {setup.invalidation}\n\n"
                "*Risque:*\n"
                "Ce trade peut echouer si la structure change.")

    @staticmethod
    def format_position_update(position: Position, action: Action, reason: str, current_price: float) -> str:
        pnl = position.pnl_pct
        emoji_pnl = "+" if pnl >= 0 else ""
        return (f"*XAUUSD — MISE A JOUR POSITION*\n\n"
                f"*Position: {position.direction.value}*\n"
                f"• Entree: {position.prix_entree:.2f}\n"
                f"• Prix actuel: {current_price:.2f}\n"
                f"• P&L: {emoji_pnl}{pnl:.2f}%\n\n"
                f"*Action: {action.value}*\n\n"
                "*Raison:*\n"
                f"{reason}\n\n"
                "*Niveaux:*\n"
                f"• SL: {position.stop_loss:.2f}\n"
                f"• TP: {position.take_profit:.2f}")

    @staticmethod
    def format_market_overview(price_data: dict, data_dict: Dict[str, pd.DataFrame]) -> str:
        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)
        if df is None or len(df) < 20:
            return (f"*XAUUSD — VUE MARCHE*\n\n"
                    f"*Prix: {price_data['price']:.2f}*\n"
                    f"*Change: {price_data['change_pct']:+.2f}%*\n\n"
                    "Donnees insuffisantes pour analyse complete.")
        regime = RegimeClassifier.classify(df)
        session = MARKET.get_current_session()
        ema20 = float(TechnicalIndicators.ema(df["Close"], 20).iloc[-1])
        ema50 = float(TechnicalIndicators.ema(df["Close"], 50).iloc[-1])
        rsi = float(TechnicalIndicators.rsi(df["Close"]).iloc[-1])
        atr = float(TechnicalIndicators.atr(df).iloc[-1])
        trend = "HAUSSIERE" if ema20 > ema50 else "BAISSIERE"
        return (f"*XAUUSD — VUE MARCHE*\n\n"
                f"*Prix: {price_data['price']:.2f}*\n"
                f"*Change: {price_data['change_pct']:+.2f}%*\n"
                f"*Session: {session}*\n\n"
                "*Structure:*\n"
                f"• Regime: {regime.value}\n"
                f"• Tendance: {trend}\n"
                f"• EMA20: {ema20:.2f}\n"
                f"• EMA50: {ema50:.2f}\n\n"
                "*Indicateurs:*\n"
                f"• RSI: {rsi:.1f}\n"
                f"• ATR: {atr:.2f}\n\n"
                "*Recommandation:*\n"
                "Utilisez /analyse pour un setup complet\n"
                "ou /update si vous etes en position.")

# ─────────────────────────────────────────────────────────────
# BOT TELEGRAM (telebot)
# ─────────────────────────────────────────────────────────────

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["start"])
def cmd_start(message):
    welcome = ("*XAUUSD QUANT BOT — ACTIVE*\n\n"
               "Bienvenue, trader.\n\n"
               "Je suis un systeme de trading quant institutionnel\n"
               "specialise sur l'or (XAUUSD).\n\n"
               "*Philosophie:*\n"
               "-> Je suis un FILTRE, pas un generateur de trades\n"
               "-> Je refuse la majorite des setups\n"
               "-> Je protege votre capital\n\n"
               "*Seuils:*\n"
               "• Score < 60 -> NO TRADE\n"
               "• 60-75 -> setup faible\n"
               "• 75-85 -> setup correct\n"
               "• 85+ -> setup PREMIUM\n\n"
               "Envoyez /help pour les commandes.")
    bot.send_message(message.chat.id, welcome, parse_mode="Markdown")

@bot.message_handler(commands=["help"])
def cmd_help(message):
    help_text = ("*XAUUSD QUANT BOT — AIDE*\n\n"
                 "Commandes disponibles:\n\n"
                 "/analyse — Analyse complete + setup\n"
                 "/update — Mise a jour position en cours\n"
                 "/prix — Prix actuel + vue marche\n"
                 "/buy prix sl tp — Enregistrer position BUY\n"
                 "/sell prix sl tp — Enregistrer position SELL\n"
                 "/close — Fermer position\n"
                 "/status — Etat de la position\n"
                 "/help — Cette aide\n\n"
                 "*Rappel:*\n"
                 "• Score < 60 = NO TRADE\n"
                 "• 60-75 = setup faible\n"
                 "• 75-85 = setup correct\n"
                 "• 85+ = setup PREMIUM\n\n"
                 "RR minimum: 1:1.5 | RR optimal: >=1:2")
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=["prix"])
def cmd_prix(message):
    bot.send_message(message.chat.id, "Recuperation des donnees marche...")
    price_data = MARKET.get_realtime_price()
    if not price_data:
        bot.send_message(message.chat.id, "Erreur recuperation prix. Reessayez.")
        return
    data_dict = MARKET.get_multi_tf_data(period="3d")
    msg = TelegramFormatter.format_market_overview(price_data, data_dict)
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=["analyse"])
def cmd_analyse(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    bot.send_message(message.chat.id, "Analyse quant en cours... 10-20s.")
    price_data = MARKET.get_realtime_price()
    if not price_data:
        bot.send_message(message.chat.id, "Erreur donnees marche. Reessayez.")
        return
    data_dict = MARKET.get_multi_tf_data(period="5d")
    setup = SetupGenerator.generate_setup(data_dict)
    memory.save_setup(setup)
    msg = TelegramFormatter.format_setup(setup, price_data["price"])
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=["update"])
def cmd_update(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    if not memory.position:
        bot.send_message(message.chat.id,
            "Aucune position enregistree.\n\n"
            "Utilisez /buy prix sl tp ou /sell prix sl tp\n"
            "Ou /analyse pour un nouveau setup.")
        return
    bot.send_message(message.chat.id, "Evaluation de la position en cours...")
    price_data = MARKET.get_realtime_price()
    if not price_data:
        bot.send_message(message.chat.id, "Erreur prix. Reessayez.")
        return
    data_dict = MARKET.get_multi_tf_data(period="3d")
    action, reason = SetupGenerator.evaluate_position(memory.position, price_data["price"], data_dict)
    if action == Action.CLOSE:
        memory.close_position()
    elif action == Action.MOVE_SL_BE:
        memory.position.stop_loss = memory.position.prix_entree
        memory.position.sl_moved_be = True
    elif action == Action.PARTIAL_CLOSE:
        memory.position.partial_closed = True
    msg = TelegramFormatter.format_position_update(
        memory.position if memory.position else Position(Direction.NONE, 0, 0, 0),
        action, reason, price_data["price"])
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=["buy"])
def cmd_buy(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    args = message.text.split()[1:]
    if len(args) < 3:
        bot.send_message(message.chat.id,
            "Format incorrect\n\n"
            "Usage: /buy prix sl tp [volume]\n"
            "Ex: /buy 3345.20 3340.00 3355.00")
        return
    try:
        prix = float(args[0])
        sl = float(args[1])
        tp = float(args[2])
        volume = float(args[3]) if len(args) > 3 else 1.0
        memory.set_position(Direction.BUY, prix, sl, tp, volume)
        rr = (tp - prix) / (prix - sl) if (prix - sl) != 0 else 0
        bot.send_message(message.chat.id,
            f"Position BUY enregistree\n\n"
            f"• Entree: {prix:.2f}\n"
            f"• SL: {sl:.2f}\n"
            f"• TP: {tp:.2f}\n"
            f"• RR: 1:{rr:.2f}\n"
            f"• Volume: {volume}\n\n"
            "Utilisez /update pour le suivi en temps reel.")
    except ValueError:
        bot.send_message(message.chat.id, "Prix invalides. Utilisez des nombres.")

@bot.message_handler(commands=["sell"])
def cmd_sell(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    args = message.text.split()[1:]
    if len(args) < 3:
        bot.send_message(message.chat.id,
            "Format incorrect\n\n"
            "Usage: /sell prix sl tp [volume]\n"
            "Ex: /sell 3345.20 3350.00 3330.00")
        return
    try:
        prix = float(args[0])
        sl = float(args[1])
        tp = float(args[2])
        volume = float(args[3]) if len(args) > 3 else 1.0
        memory.set_position(Direction.SELL, prix, sl, tp, volume)
        rr = (prix - tp) / (sl - prix) if (sl - prix) != 0 else 0
        bot.send_message(message.chat.id,
            f"Position SELL enregistree\n\n"
            f"• Entree: {prix:.2f}\n"
            f"• SL: {sl:.2f}\n"
            f"• TP: {tp:.2f}\n"
            f"• RR: 1:{rr:.2f}\n"
            f"• Volume: {volume}\n\n"
            "Utilisez /update pour le suivi en temps reel.")
    except ValueError:
        bot.send_message(message.chat.id, "Prix invalides. Utilisez des nombres.")

@bot.message_handler(commands=["close"])
def cmd_close(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    if not memory.position:
        bot.send_message(message.chat.id, "Aucune position a fermer.")
        return
    pnl = memory.position.pnl_pct
    memory.close_position()
    emoji = "+" if pnl >= 0 else ""
    bot.send_message(message.chat.id,
        f"Position fermee\n\n"
        f"P&L final: {emoji}{pnl:.2f}%\n\n"
        "Capital preserve. Analysez avant de re-entrer.")

@bot.message_handler(commands=["status"])
def cmd_status(message):
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    if not memory.position:
        bot.send_message(message.chat.id,
            "Aucune position ouverte\n\n"
            "Utilisez /analyse pour trouver un setup.")
        return
    price_data = MARKET.get_realtime_price()
    current_price = price_data["price"] if price_data else 0
    memory.position.update_pnl(current_price)
    pnl = memory.position.pnl_pct
    emoji = "+" if pnl >= 0 else ""
    bot.send_message(message.chat.id,
        f"POSITION ACTUELLE\n\n"
        f"Direction: {memory.position.direction.value}\n"
        f"Entree: {memory.position.prix_entree:.2f}\n"
        f"Prix actuel: {current_price:.2f}\n"
        f"P&L: {emoji}{pnl:.2f}%\n"
        f"SL: {memory.position.stop_loss:.2f}\n"
        f"TP: {memory.position.take_profit:.2f}\n"
        f"SL->BE: {'Oui' if memory.position.sl_moved_be else 'Non'}\n"
        f"Partial: {'Oui' if memory.position.partial_closed else 'Non'}\n\n"
        "/update pour evaluation en temps reel.")

@bot.message_handler(func=lambda m: True)
def text_handler(message):
    text = message.text.lower().strip()
    user_id = message.from_user.id
    memory = get_user_memory(user_id)
    if any(w in text for w in ["analyse", "setup", "trade", "signal"]):
        cmd_analyse(message)
    elif any(w in text for w in ["update", "mise a jour", "suivi", "position"]):
        if memory.position:
            cmd_update(message)
        else:
            bot.send_message(message.chat.id,
                "Pas de position en cours.\n"
                "Envoyez /analyse pour un setup ou enregistrez une position.")
    elif any(w in text for w in ["prix", "price", "cours", "gold"]):
        cmd_prix(message)
    elif any(w in text for w in ["fermer", "close", "cloturer", "sortir"]):
        cmd_close(message)
    elif "buy" in text or "achat" in text or "long" in text:
        nums = re.findall(r"[0-9]+\.?[0-9]*", text)
        if len(nums) >= 3:
            message.text = "/buy " + " ".join(nums[:4])
            cmd_buy(message)
        else:
            bot.send_message(message.chat.id,
                "Pour enregistrer un BUY:\n"
                "/buy prix sl tp\n"
                "Ex: /buy 3345.20 3340.00 3355.00")
    elif "sell" in text or "vente" in text or "short" in text:
        nums = re.findall(r"[0-9]+\.?[0-9]*", text)
        if len(nums) >= 3:
            message.text = "/sell " + " ".join(nums[:4])
            cmd_sell(message)
        else:
            bot.send_message(message.chat.id,
                "Pour enregistrer un SELL:\n"
                "/sell prix sl tp\n"
                "Ex: /sell 3345.20 3350.00 3330.00")
    elif any(w in text for w in ["aide", "help", "commande", "comment"]):
        cmd_help(message)
    else:
        bot.send_message(message.chat.id,
            "Je n'ai pas compris.\n\n"
            "Essayez:\n"
            "• /analyse — Nouveau setup\n"
            "• /update — Suivi position\n"
            "• /prix — Prix actuel\n"
            "• /help — Toutes les commandes")

# ─────────────────────────────────────────────────────────────
# ALERTES AUTOMATIQUES (thread separe)
# ─────────────────────────────────────────────────────────────

def alert_loop():
    while True:
        try:
            for user_id, memory in list(USER_MEMORIES.items()):
                if not memory.preferences.get("alertes", True):
                    continue
                if memory.position:
                    continue
                data_dict = MARKET.get_multi_tf_data(period="3d")
                setup = SetupGenerator.generate_setup(data_dict)
                if setup.qualite == Qualite.A_PLUS and setup.score.total >= 85:
                    price_data = MARKET.get_realtime_price()
                    current_price = price_data["price"] if price_data else setup.prix_entree
                    msg = (f"*ALERTE SETUP PREMIUM*\n\n"
                           f"{setup.direction.value} detecte sur XAUUSD!\n\n"
                           f"*Score: {setup.score.total:.0f}/100*\n"
                           "*Qualite: A+*\n\n"
                           "*Plan:*\n"
                           f"• Entree: `{setup.prix_entree:.2f}`\n"
                           f"• SL: `{setup.stop_loss:.2f}`\n"
                           f"• TP: `{setup.take_profit:.2f}`\n"
                           f"• RR: `1:{setup.rr:.2f}`\n\n"
                           f"*Prix actuel: {current_price:.2f}*\n\n"
                           "/analyse pour details complets.")
                    try:
                        bot.send_message(user_id, msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.warning("Erreur envoi alerte user %s: %s", user_id, e)
        except Exception as e:
            logger.error("Erreur alert loop: %s", e)
        time.sleep(300)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("XAUUSD Quant Bot demarrage...")
    alert_thread = threading.Thread(target=alert_loop, daemon=True)
    alert_thread.start()
    logger.info("Thread d'alertes demarre")
    logger.info("Polling Telegram...")
    bot.polling(none_stop=True, interval=1, timeout=20)
