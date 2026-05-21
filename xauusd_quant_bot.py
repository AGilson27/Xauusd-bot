#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════
  XAUUSD QUANT BOT — Smart Money Concept Institutionnel
  Telegram Bot v2.0 — Temps réel Yahoo Finance
═══════════════════════════════════════════════════════════════
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple
from enum import Enum
from collections import deque

import numpy as np
import pandas as pd
import yfinance as yf

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

TOKEN = "8842089863:AAGkqa0yPvojFVYj9vJH3kBOIiNfpdjbCPg"
TICKER = "GC=F"  # XAUUSD sur Yahoo Finance

# Seuils institutionnels
SCORE_MIN_TRADE = 60
SCORE_PREMIUM = 85
RR_MIN = 1.5
RR_OPTIMAL = 2.0

# Timeframes pour l'analyse multi-TF
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]

# Sessions de trading
SESSION_ASIA = (0, 8)      # UTC
SESSION_LONDON = (8, 16)   # UTC
SESSION_NY = (13, 21)      # UTC

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
# LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)



# ─────────────────────────────────────────────────────────────
# CLASSES DE DONNÉES
# ─────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    structure_marche: float = 0.0      # 0-20
    liquidite_smc: float = 0.0         # 0-20
    alignement_mtf: float = 0.0        # 0-15
    volatilite_conditions: float = 0.0 # 0-15
    risk_reward: float = 0.0           # 0-15
    timing_entree: float = 0.0         # 0-10
    contexte_macro: float = 0.0        # 0-5

    @property
    def total(self) -> float:
        return (
            self.structure_marche +
            self.liquidite_smc +
            self.alignement_mtf +
            self.volatilite_conditions +
            self.risk_reward +
            self.timing_entree +
            self.contexte_macro
        )

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
# MÉMOIRE CONVERSATIONNELLE & UTILISATEUR
# ─────────────────────────────────────────────────────────────

class UserMemory:
    """Mémoire persistante par utilisateur Telegram."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.position: Optional[Position] = None
        self.historique_setups: deque = deque(maxlen=50)
        self.dernier_setup: Optional[Setup] = None
        self.preferences: Dict = {"alertes": True, "mode_agressif": False}

    def set_position(self, direction: Direction, prix: float, sl: float, tp: float, volume: float = 1.0):
        self.position = Position(direction, prix, sl, tp, volume)
        logger.info(f"[{self.user_id}] Position ouverte: {direction.value} @ {prix}")

    def close_position(self):
        if self.position:
            logger.info(f"[{self.user_id}] Position fermée. P&L: {self.position.pnl_pct:.2f}%")
        self.position = None

    def save_setup(self, setup: Setup):
        self.dernier_setup = setup
        self.historique_setups.append(asdict(setup))

    def get_context(self) -> str:
        if self.position:
            return f"Position {self.position.direction.value} @ {self.position.prix_entree} | P&L: {self.position.pnl_pct:.2f}%"
        return "Aucune position ouverte"

# Stockage global en mémoire (remplacer par Redis/DB en prod)
USER_MEMORIES: Dict[int, UserMemory] = {}

def get_user_memory(user_id: int) -> UserMemory:
    if user_id not in USER_MEMORIES:
        USER_MEMORIES[user_id] = UserMemory(user_id)
    return USER_MEMORIES[user_id]



# ─────────────────────────────────────────────────────────────
# MOTEUR DE DONNÉES MARCHÉ — TEMPS RÉEL
# ─────────────────────────────────────────────────────────────

class MarketEngine:
    """
    Moteur d'accès aux données marché via yfinance.
    Récupère données temps réel + historiques pour scoring.
    """

    def __init__(self, ticker: str = TICKER):
        self.ticker = ticker
        self.ticker_obj = yf.Ticker(ticker)
        self.last_data: Optional[pd.DataFrame] = None
        self.last_update: Optional[datetime] = None

    def get_realtime_price(self) -> Optional[dict]:
        """Prix temps réel (dernier tick disponible)."""
        try:
            info = self.ticker_obj.info
            hist = self.ticker_obj.history(period="1d", interval="1m")
            if hist.empty:
                return None

            last = hist.iloc[-1]
            return {
                "price": float(last["Close"]),
                "open": float(last["Open"]),
                "high": float(last["High"]),
                "low": float(last["Low"]),
                "volume": int(last["Volume"]),
                "timestamp": last.name.isoformat(),
                "change_pct": ((last["Close"] - hist.iloc[0]["Open"]) / hist.iloc[0]["Open"] * 100) if len(hist) > 1 else 0.0
            }
        except Exception as e:
            logger.error(f"Erreur prix temps réel: {e}")
            return None

    def get_multi_tf_data(self, period: str = "5d") -> Dict[str, pd.DataFrame]:
        """Télécharge données sur plusieurs timeframes."""
        data = {}
        intervals = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}

        for tf_name, interval in intervals.items():
            try:
                df = self.ticker_obj.history(period=period, interval=interval)
                if not df.empty:
                    data[tf_name] = df
            except Exception as e:
                logger.warning(f"Erreur TF {tf_name}: {e}")

        self.last_data = data
        self.last_update = datetime.utcnow()
        return data

    def get_current_session(self) -> str:
        """Détermine la session de trading actuelle (UTC)."""
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

# Instance globale du moteur marché
MARKET = MarketEngine()



# ─────────────────────────────────────────────────────────────
# INDICATEURS TECHNIQUES & SMART MONEY CONCEPTS (ICT)
# ─────────────────────────────────────────────────────────────

class TechnicalIndicators:
    """Tous les indicateurs techniques nécessaires au scoring."""

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
    def volume_profile(df: pd.DataFrame, bins: int = 20):
        """Calcule le Volume Profile (Point of Control)."""
        hist, edges = np.histogram(df["Close"], bins=bins, weights=df["Volume"])
        poc_idx = np.argmax(hist)
        poc = (edges[poc_idx] + edges[poc_idx + 1]) / 2
        return poc, edges, hist

    @staticmethod
    def detect_swing_points(df: pd.DataFrame, lookback: int = 5) -> Tuple[List, List]:
        """Détecte les swing highs et swing lows."""
        highs = df["High"].values
        lows = df["Low"].values

        swing_highs = []
        swing_lows = []

        for i in range(lookback, len(df) - lookback):
            # Swing High
            if all(highs[i] >= highs[i-j] for j in range(1, lookback+1)) and                all(highs[i] >= highs[i+j] for j in range(1, lookback+1)):
                swing_highs.append((i, highs[i]))

            # Swing Low
            if all(lows[i] <= lows[i-j] for j in range(1, lookback+1)) and                all(lows[i] <= lows[i+j] for j in range(1, lookback+1)):
                swing_lows.append((i, lows[i]))

        return swing_highs, swing_lows


class SmartMoneyConcepts:
    """
    Logique Smart Money / ICT avancée.
    Détection de liquidité, FVG, Order Blocks, Breaker Blocks.
    """

    @staticmethod
    def detect_liquidity_sweep(df: pd.DataFrame, swing_highs: List, swing_lows: List) -> dict:
        """
        Détecte un Liquidity Sweep (sweep des stops retail).
        Retourne: {'direction': 'BUY'/'SELL', 'swept_level': prix, 'confidence': 0-1}
        """
        if not swing_highs or not swing_lows:
            return {"detected": False}

        last_high = swing_highs[-1][1]
        last_low = swing_lows[-1][1]
        current_price = df["Close"].iloc[-1]

        # Sweep des highs (institutions prennent les stops des buyers)
        if current_price > last_high * 1.001:
            return {
                "detected": True,
                "direction": "SELL",
                "swept_level": last_high,
                "confidence": min((current_price - last_high) / last_high * 100 * 10, 1.0),
                "type": "LIQUIDITY_SWEEP_HIGH"
            }

        # Sweep des lows (institutions prennent les stops des sellers)
        if current_price < last_low * 0.999:
            return {
                "detected": True,
                "direction": "BUY",
                "swept_level": last_low,
                "confidence": min((last_low - current_price) / last_low * 100 * 10, 1.0),
                "type": "LIQUIDITY_SWEEP_LOW"
            }

        return {"detected": False}

    @staticmethod
    def detect_fvg(df: pd.DataFrame) -> List[dict]:
        """
        Détecte les Fair Value Gaps (FVG).
        Un FVG haussier: Low[i+2] > High[i]
        Un FVG baissier: High[i+2] < Low[i]
        """
        fvgs = []
        for i in range(len(df) - 3):
            # FVG haussier
            if df["Low"].iloc[i+2] > df["High"].iloc[i]:
                fvgs.append({
                    "type": "BULLISH_FVG",
                    "top": df["Low"].iloc[i+2],
                    "bottom": df["High"].iloc[i],
                    "index": i,
                    "mid": (df["Low"].iloc[i+2] + df["High"].iloc[i]) / 2
                })

            # FVG baissier
            elif df["High"].iloc[i+2] < df["Low"].iloc[i]:
                fvgs.append({
                    "type": "BEARISH_FVG",
                    "top": df["Low"].iloc[i],
                    "bottom": df["High"].iloc[i+2],
                    "index": i,
                    "mid": (df["Low"].iloc[i] + df["High"].iloc[i+2]) / 2
                })

        return fvgs

    @staticmethod
    def detect_order_blocks(df: pd.DataFrame) -> List[dict]:
        """
        Détecte les Order Blocks (OB) — zones où les institutions ont accumulé.
        OB haussier: dernière bougie baissière avant une impulsion haussière
        OB baissier: dernière bougie haussière avant une impulsion baissière
        """
        obs = []
        closes = df["Close"].values
        opens = df["Open"].values
        highs = df["High"].values
        lows = df["Low"].values

        for i in range(2, len(df) - 1):
            # OB haussier: bougie baissière suivie d'impulsion haussière
            if closes[i-1] < opens[i-1] and closes[i] > opens[i] and closes[i] > closes[i-1] * 1.003:
                obs.append({
                    "type": "BULLISH_OB",
                    "high": highs[i-1],
                    "low": lows[i-1],
                    "index": i-1,
                    "mid": (highs[i-1] + lows[i-1]) / 2
                })

            # OB baissier: bougie haussière suivie d'impulsion baissière
            elif closes[i-1] > opens[i-1] and closes[i] < opens[i] and closes[i] < closes[i-1] * 0.997:
                obs.append({
                    "type": "BEARISH_OB",
                    "high": highs[i-1],
                    "low": lows[i-1],
                    "index": i-1,
                    "mid": (highs[i-1] + lows[i-1]) / 2
                })

        return obs

    @staticmethod
    def detect_breaker_blocks(df: pd.DataFrame, obs: List[dict]) -> List[dict]:
        """
        Détecte les Breaker Blocks (anciens OB inversés).
        """
        breakers = []
        current_price = df["Close"].iloc[-1]

        for ob in obs[-10:]:  # 10 derniers OB
            if ob["type"] == "BULLISH_OB" and current_price < ob["low"]:
                breakers.append({
                    "type": "BEARISH_BREAKER",
                    "level": ob["mid"],
                    "original_ob": ob
                })
            elif ob["type"] == "BEARISH_OB" and current_price > ob["high"]:
                breakers.append({
                    "type": "BULLISH_BREAKER",
                    "level": ob["mid"],
                    "original_ob": ob
                })

        return breakers

    @staticmethod
    def detect_choch_bos(df: pd.DataFrame) -> dict:
        """
        Détecte CHoCH (Change of Character) et BOS (Break of Structure).
        """
        highs = df["High"].values
        lows = df["Low"].values

        if len(highs) < 10:
            return {"choch": False, "bos": False}

        # BOS: break d'un swing high/low précédent
        last_swing_high = max(highs[-10:-1])
        last_swing_low = min(lows[-10:-1])

        current_high = highs[-1]
        current_low = lows[-1]

        bos_bullish = current_high > last_swing_high
        bos_bearish = current_low < last_swing_low

        # CHoCH: break interne de structure (moins fort)
        choch_bullish = current_high > highs[-3] and current_low < lows[-3]
        choch_bearish = current_low < lows[-3] and current_high > highs[-3]

        return {
            "bos_bullish": bos_bullish,
            "bos_bearish": bos_bearish,
            "choch_bullish": choch_bullish,
            "choch_bearish": choch_bearish
        }



# ─────────────────────────────────────────────────────────────
# SCORING QUANTITATIF INSTITUTIONNEL
# ─────────────────────────────────────────────────────────────

class QuantScorer:
    """
    Moteur de scoring basé sur 7 critères institutionnels.
    Score total: 0-100
    """

    @staticmethod
    def score_structure_marche(df: pd.DataFrame) -> float:
        """
        0-20: Structure de marché (tendance, structure, swing points)
        """
        score = 0.0

        # Tendance via EMA
        ema20 = TechnicalIndicators.ema(df["Close"], 20)
        ema50 = TechnicalIndicators.ema(df["Close"], 50)

        if len(ema20) > 50:
            last_ema20 = ema20.iloc[-1]
            last_ema50 = ema50.iloc[-1]
            last_close = df["Close"].iloc[-1]

            # Alignement EMA
            if last_ema20 > last_ema50:
                score += 8  # Tendance haussière
            elif last_ema20 < last_ema50:
                score += 8  # Tendance baissière

            # Prix au-dessus/dessous EMA20
            if abs(last_close - last_ema20) / last_ema20 < 0.002:
                score += 4  # Prix proche EMA (consolidation)
            elif last_close > last_ema20:
                score += 6  # Prix au-dessus EMA20
            else:
                score += 2

            # Structure swing points
            swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df)
            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                # HH-HL ou LL-LH
                last_sh = swing_highs[-1][1]
                prev_sh = swing_highs[-2][1]
                last_sl = swing_lows[-1][1]
                prev_sl = swing_lows[-2][1]

                if last_sh > prev_sh and last_sl > prev_sl:
                    score += 6  # HH-HL = tendance haussière forte
                elif last_sh < prev_sh and last_sl < prev_sl:
                    score += 6  # LL-LH = tendance baissière forte
                else:
                    score += 2  # Structure confuse

        return min(score, 20)

    @staticmethod
    def score_liquidite_smc(df: pd.DataFrame) -> float:
        """
        0-20: Liquidité & Smart Money Concepts
        """
        score = 0.0

        # Détection sweep
        swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df, lookback=3)
        sweep = SmartMoneyConcepts.detect_liquidity_sweep(df, swing_highs, swing_lows)

        if sweep["detected"]:
            score += 10 * sweep["confidence"]

        # FVG proche du prix actuel
        fvgs = SmartMoneyConcepts.detect_fvg(df)
        current_price = df["Close"].iloc[-1]

        for fvg in fvgs[-5:]:
            if fvg["bottom"] < current_price < fvg["top"]:
                score += 5
                break

        # Order Blocks proches
        obs = SmartMoneyConcepts.detect_order_blocks(df)
        for ob in obs[-5:]:
            if ob["low"] < current_price < ob["high"]:
                score += 5
                break

        return min(score, 20)

    @staticmethod
    def score_alignement_mtf(data_dict: Dict[str, pd.DataFrame]) -> float:
        """
        0-15: Alignement multi-timeframe
        """
        score = 0.0
        directions = []

        for tf_name, df in data_dict.items():
            if len(df) < 50:
                continue
            ema20 = TechnicalIndicators.ema(df["Close"], 20)
            ema50 = TechnicalIndicators.ema(df["Close"], 50)

            if len(ema20) > 50 and len(ema50) > 50:
                if ema20.iloc[-1] > ema50.iloc[-1]:
                    directions.append("UP")
                else:
                    directions.append("DOWN")

        if not directions:
            return 0.0

        # Comptage
        up_count = directions.count("UP")
        down_count = directions.count("DOWN")
        total = len(directions)

        # Alignement fort (>80% dans une direction)
        if up_count / total >= 0.8 or down_count / total >= 0.8:
            score = 15
        elif up_count / total >= 0.6 or down_count / total >= 0.6:
            score = 10
        elif up_count / total >= 0.5 or down_count / total >= 0.5:
            score = 5

        return score

    @staticmethod
    def score_volatilite_conditions(df: pd.DataFrame) -> float:
        """
        0-15: Volatilité & conditions de marché
        """
        score = 0.0

        if len(df) < 20:
            return 0.0

        atr = TechnicalIndicators.atr(df)
        last_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-20:].mean()

        # Volatilité adéquate (ni trop faible ni trop élevée)
        atr_ratio = last_atr / avg_atr if avg_atr > 0 else 1.0

        if 0.8 <= atr_ratio <= 1.5:
            score += 8  # Volatilité normale
        elif 0.5 <= atr_ratio < 0.8:
            score += 4  # Volatilité faible
        elif atr_ratio > 1.5:
            score += 3  # Volatilité élevée (risque)
        else:
            score += 1  # Volatilité très faible

        # Bollinger Bands expansion/contraction
        upper, sma, lower = TechnicalIndicators.bollinger_bands(df["Close"])
        bb_width = (upper.iloc[-1] - lower.iloc[-1]) / sma.iloc[-1] if sma.iloc[-1] > 0 else 0

        if 0.005 < bb_width < 0.02:
            score += 7  # Expansion normale
        elif bb_width < 0.005:
            score += 2  # Squeeze (pré-breakout)
        else:
            score += 3  # Expansion extrême

        return min(score, 15)

    @staticmethod
    def score_risk_reward(direction: Direction, entry: float, sl: float, tp: float) -> float:
        """
        0-15: Risk/Reward
        """
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
        else:
            return max(rr * 5, 0)

    @staticmethod
    def score_timing_entree(df: pd.DataFrame, direction: Direction) -> float:
        """
        0-10: Timing d'entrée (éviter entrée tardive)
        """
        score = 0.0

        if len(df) < 10:
            return 0.0

        # RSI pour timing
        rsi = TechnicalIndicators.rsi(df["Close"])
        last_rsi = rsi.iloc[-1]

        if direction == Direction.BUY:
            if 30 < last_rsi < 50:
                score += 5  # Zone de valeur
            elif 50 < last_rsi < 70:
                score += 3  # Momentum
            else:
                score += 1
        elif direction == Direction.SELL:
            if 50 < last_rsi < 70:
                score += 5  # Zone de valeur
            elif 30 < last_rsi < 50:
                score += 3
            else:
                score += 1

        # Éviter entrée après impulsion violente
        last_3_candles = df["Close"].iloc[-3:].pct_change().abs().sum()
        if last_3_candles < 0.005:
            score += 5  # Consolidation = bon timing
        elif last_3_candles < 0.01:
            score += 3
        else:
            score += 1  # Impulsion = mauvais timing

        return min(score, 10)

    @staticmethod
    def score_contexte_macro() -> float:
        """
        0-5: Contexte macro (simplifié - à enrichir avec news API)
        """
        hour = datetime.utcnow().hour

        # Éviter trading pendant les news majeures (8:30 NY, 14:00 UTC)
        if hour in [8, 9, 13, 14]:
            return 2  # Période de news

        # Session overlap = meilleure liquidité
        if 8 <= hour <= 17:
            return 5

        return 3


class RegimeClassifier:
    """Classification du régime de marché."""

    @staticmethod
    def classify(df: pd.DataFrame) -> Regime:
        """
        Classifie le marché en: TREND, RANGE, EXPANSION, CHAOS
        """
        if len(df) < 50:
            return Regime.CHAOS

        # ADX-like proxy via ATR et EMA
        atr = TechnicalIndicators.atr(df)
        ema20 = TechnicalIndicators.ema(df["Close"], 20)
        ema50 = TechnicalIndicators.ema(df["Close"], 50)

        last_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-20:].mean()

        # Distance EMA20/50
        ema_dist = abs(ema20.iloc[-1] - ema50.iloc[-1]) / ema50.iloc[-1] if ema50.iloc[-1] > 0 else 0

        # Bollinger width
        upper, sma, lower = TechnicalIndicators.bollinger_bands(df["Close"])
        bb_width = (upper.iloc[-1] - lower.iloc[-1]) / sma.iloc[-1] if sma.iloc[-1] > 0 else 0

        # Trend: EMA alignées + ATR stable + BB expansion modérée
        if ema_dist > 0.005 and 0.8 <= last_atr / avg_atr <= 1.3 and bb_width > 0.008:
            return Regime.TREND

        # Range: EMA proches + ATR faible + BB contraction
        if ema_dist < 0.003 and last_atr / avg_atr < 0.8 and bb_width < 0.008:
            return Regime.RANGE

        # Expansion: ATR élevé + BB expansion extrême
        if last_atr / avg_atr > 1.5 and bb_width > 0.015:
            return Regime.EXPANSION

        # Sinon: CHAOS
        return Regime.CHAOS



# ─────────────────────────────────────────────────────────────
# GÉNÉRATEUR DE SETUPS & GESTION DE TRADE
# ─────────────────────────────────────────────────────────────

class SetupGenerator:
    """
    Génère des setups de trading basés sur le scoring quant
    et la logique Smart Money.
    """

    @staticmethod
    def generate_setup(data_dict: Dict[str, pd.DataFrame]) -> Setup:
        """
        Analyse le marché et génère un setup complet avec scoring.
        """
        setup = Setup()

        # Récupère le timeframe principal (1h ou 15m)
        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)

        if df is None or len(df) < 50:
            setup.raison_refus = "Données insuffisantes"
            return setup

        # Classification du régime
        regime = RegimeClassifier.classify(df)
        setup.regime = regime

        if regime == Regime.CHAOS:
            setup.raison_refus = "Régime CHAOS — Interdiction de trader"
            return setup

        # Analyse Smart Money
        swing_highs, swing_lows = TechnicalIndicators.detect_swing_points(df)
        sweep = SmartMoneyConcepts.detect_liquidity_sweep(df, swing_highs, swing_lows)
        fvgs = SmartMoneyConcepts.detect_fvg(df)
        obs = SmartMoneyConcepts.detect_order_blocks(df)
        choch_bos = SmartMoneyConcepts.detect_choch_bos(df)

        current_price = df["Close"].iloc[-1]
        atr = TechnicalIndicators.atr(df).iloc[-1]

        # Détermination de la direction
        direction = Direction.NONE
        entry = current_price
        sl = 0.0
        tp = 0.0
        raison = ""

        # Logique 1: Liquidity Sweep + FVG/OB
        if sweep["detected"]:
            direction = Direction.BUY if sweep["direction"] == "BUY" else Direction.SELL

            if direction == Direction.BUY:
                # SL sous le sweep low, TP vers dernier swing high
                sl = sweep["swept_level"] - atr * 1.5
                if swing_highs:
                    tp = max([sh[1] for sh in swing_highs[-3:]])
                else:
                    tp = current_price + atr * 3
            else:
                # SL au-dessus du sweep high, TP vers dernier swing low
                sl = sweep["swept_level"] + atr * 1.5
                if swing_lows:
                    tp = min([sl[1] for sl in swing_lows[-3:]])
                else:
                    tp = current_price - atr * 3

            raison = f"Liquidity Sweep {sweep['type']} — Retour à l'équilibre attendu"

        # Logique 2: FVG non comblé
        elif fvgs:
            last_fvg = fvgs[-1]
            if last_fvg["type"] == "BULLISH_FVG" and current_price > last_fvg["top"]:
                direction = Direction.BUY
                entry = last_fvg["mid"]
                sl = last_fvg["bottom"] - atr
                tp = current_price + atr * 2.5
                raison = "FVG haussier — Retest de la zone d'équilibre"
            elif last_fvg["type"] == "BEARISH_FVG" and current_price < last_fvg["bottom"]:
                direction = Direction.SELL
                entry = last_fvg["mid"]
                sl = last_fvg["top"] + atr
                tp = current_price - atr * 2.5
                raison = "FVG baissier — Retest de la zone d'équilibre"

        # Logique 3: Order Block + CHoCH/BOS
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

        # Logique 4: Mean Reversion en RANGE
        elif regime == Regime.RANGE:
            upper_bb, sma, lower_bb = TechnicalIndicators.bollinger_bands(df["Close"])
            if current_price >= upper_bb.iloc[-1] * 0.998:
                direction = Direction.SELL
                entry = current_price
                sl = current_price + atr * 1.5
                tp = sma.iloc[-1]
                raison = "Range — Prix au-dessus BB supérieure (mean reversion)"
            elif current_price <= lower_bb.iloc[-1] * 1.002:
                direction = Direction.BUY
                entry = current_price
                sl = current_price - atr * 1.5
                tp = sma.iloc[-1]
                raison = "Range — Prix sous BB inférieure (mean reversion)"

        # Logique 5: Breakout en EXPANSION
        elif regime == Regime.EXPANSION:
            if choch_bos["bos_bullish"]:
                direction = Direction.BUY
                entry = current_price
                sl = current_price - atr * 2
                tp = current_price + atr * 4
                raison = "Expansion haussière — Breakout de structure"
            elif choch_bos["bos_bearish"]:
                direction = Direction.SELL
                entry = current_price
                sl = current_price + atr * 2
                tp = current_price - atr * 4
                raison = "Expansion baissière — Breakdown de structure"

        # Si aucune logique valide
        if direction == Direction.NONE:
            setup.raison_refus = "Aucune logique institutionnelle détectée — WAIT"
            return setup

        # Calcul du RR
        if direction == Direction.BUY:
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp

        rr = reward / risk if risk != 0 else 0

        # Refus si RR insuffisant
        if rr < RR_MIN:
            setup.raison_refus = f"RR insuffisant ({rr:.2f} < {RR_MIN}) — WAIT"
            return setup

        # SCORING COMPLET
        setup.direction = direction
        setup.prix_entree = round(entry, 2)
        setup.stop_loss = round(sl, 2)
        setup.take_profit = round(tp, 2)
        setup.rr = round(rr, 2)
        setup.session = MARKET.get_current_session()
        setup.invalidation = f"Break de {sl} invalide le setup"

        # Calcul des 7 scores
        setup.score.structure_marche = QuantScorer.score_structure_marche(df)
        setup.score.liquidite_smc = QuantScorer.score_liquidite_smc(df)
        setup.score.alignement_mtf = QuantScorer.score_alignement_mtf(data_dict)
        setup.score.volatilite_conditions = QuantScorer.score_volatilite_conditions(df)
        setup.score.risk_reward = QuantScorer.score_risk_reward(direction, entry, sl, tp)
        setup.score.timing_entree = QuantScorer.score_timing_entree(df, direction)
        setup.score.contexte_macro = QuantScorer.score_contexte_macro()

        # Qualité
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
        """
        Évalue une position en cours et recommande une action.
        """
        position.update_pnl(current_price)
        pnl = position.pnl_pct

        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)

        if df is None or len(df) < 20:
            return Action.HOLD, "Données insuffisantes pour évaluation"

        atr = TechnicalIndicators.atr(df).iloc[-1]

        # 1. Stop Loss touché
        if position.direction == Direction.BUY and current_price <= position.stop_loss:
            return Action.CLOSE, "Stop Loss atteint — Fermeture obligatoire"
        if position.direction == Direction.SELL and current_price >= position.stop_loss:
            return Action.CLOSE, "Stop Loss atteint — Fermeture obligatoire"

        # 2. Take Profit atteint
        if position.direction == Direction.BUY and current_price >= position.take_profit:
            return Action.CLOSE, "Take Profit atteint — Fermeture complète"
        if position.direction == Direction.SELL and current_price <= position.take_profit:
            return Action.CLOSE, "Take Profit atteint — Fermeture complète"

        # 3. Move SL to Break-Even (+1% en faveur)
        if not position.sl_moved_be and pnl >= 1.0:
            return Action.MOVE_SL_BE, f"P&L +{pnl:.2f}% — Déplacer SL au BE"

        # 4. Partial Close (+2% en faveur)
        if not position.partial_closed and pnl >= 2.0:
            return Action.PARTIAL_CLOSE, f"P&L +{pnl:.2f}% — Fermeture partielle 50%"

        # 5. Structure invalide (CHoCH contre la position)
        choch_bos = SmartMoneyConcepts.detect_choch_bos(df)
        if position.direction == Direction.BUY and choch_bos["choch_bearish"]:
            return Action.CLOSE, "CHoCH baissier — Structure invalide, fermeture"
        if position.direction == Direction.SELL and choch_bos["choch_bullish"]:
            return Action.CLOSE, "CHoCH haussier — Structure invalide, fermeture"

        # 6. Régime CHAOS
        regime = RegimeClassifier.classify(df)
        if regime == Regime.CHAOS:
            return Action.CLOSE, "Régime CHAOS — Fermeture protective"

        # 7. RR restant intéressant ?
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
# FORMATAGE DES RÉPONSES TELEGRAM
# ─────────────────────────────────────────────────────────────

class TelegramFormatter:
    """Formate les réponses pour Telegram (mobile-friendly)."""

    @staticmethod
    def format_setup(setup: Setup, current_price: float) -> str:
        """Format complet d'un setup de trading."""

        if setup.qualite == Qualite.REFUSE:
            return f"""
📊 *XAUUSD QUANT — ANALYSE*

❌ *DÉCISION: WAIT*

📉 *Score: {setup.score.total:.0f}/100*
⭐ *Qualité: REFUSÉ*

🚫 *Raison:*
{setup.raison_refus}

💰 *Prix actuel: {current_price:.2f}*
🕐 *Session: {setup.session}*

*Pas de trade = décision valide*
"""

        emoji_dir = "🟢" if setup.direction == Direction.BUY else "🔴"
        emoji_qualite = "🌟" if setup.qualite == Qualite.A_PLUS else "⭐" if setup.qualite == Qualite.A else "📊"

        return f"""
📊 *XAUUSD QUANT — SETUP {emoji_dir} {setup.direction.value}*

📈 *Score: {setup.score.total:.0f}/100*
{emoji_qualite} *Qualité: {setup.qualite.value}*

📍 *Contexte:*
• Régime: {setup.regime.value}
• Session: {setup.session}
• Prix actuel: {current_price:.2f}

🧠 *Analyse:*
{setup.raison_refus if setup.raison_refus else "Setup validé par scoring quant"}

📌 *Plan:*
• Entrée: `{setup.prix_entree:.2f}`
• Stop Loss: `{setup.stop_loss:.2f}`
• Take Profit: `{setup.take_profit:.2f}`
• RR: `1:{setup.rr:.2f}`

📊 *Détail Scoring:*
• Structure: {setup.score.structure_marche:.0f}/20
• Liquidité/SMC: {setup.score.liquidite_smc:.0f}/20
• Alignement MTF: {setup.score.alignement_mtf:.0f}/15
• Volatilité: {setup.score.volatilite_conditions:.0f}/15
• Risk/Reward: {setup.score.risk_reward:.0f}/15
• Timing: {setup.score.timing_entree:.0f}/10
• Macro: {setup.score.contexte_macro:.0f}/5

⏱️ *Timing:*
Entrée sur confirmation — éviter l'impulsion tardive

🔁 *Gestion:*
• SL → BE à +1%
• Partial close à +2%
• Invalidation: {setup.invalidation}

🚫 *Risque:*
Ce trade peut échouer si la structure change avant l'entrée.
"""

    @staticmethod
    def format_position_update(position: Position, action: Action, reason: str, current_price: float) -> str:
        """Format de mise à jour d'une position."""

        emoji_pnl = "🟢" if position.pnl_pct >= 0 else "🔴"
        emoji_action = {
            Action.HOLD: "⏸️",
            Action.CLOSE: "❌",
            Action.MOVE_SL_BE: "🛡️",
            Action.PARTIAL_CLOSE: "✂️",
            Action.WAIT: "⏳"
        }.get(action, "❓")

        return f"""
📊 *XAUUSD — MISE À JOUR POSITION*

{emoji_pnl} *Position: {position.direction.value}*
• Entrée: {position.prix_entree:.2f}
• Prix actuel: {current_price:.2f}
• P&L: {position.pnl_pct:+.2f}%

{emoji_action} *Action recommandée: {action.value}*

📝 *Raison:*
{reason}

📌 *Niveaux:*
• SL: {position.stop_loss:.2f}
• TP: {position.take_profit:.2f}

🕐 *Ouverture:* {position.timestamp_ouverture[:16]}
"""

    @staticmethod
    def format_market_overview(price_data: dict, data_dict: Dict[str, pd.DataFrame]) -> str:
        """Vue d'ensemble du marché."""

        primary_tf = "1h" if "1h" in data_dict else "15m"
        df = data_dict.get(primary_tf)

        if df is None or len(df) < 20:
            return f"""
📊 *XAUUSD — VUE MARCHÉ*

💰 *Prix: {price_data['price']:.2f}*
📈 *Change: {price_data['change_pct']:+.2f}%*

⚠️ Données insuffisantes pour analyse complète.
"""

        regime = RegimeClassifier.classify(df)
        session = MARKET.get_current_session()

        ema20 = TechnicalIndicators.ema(df["Close"], 20).iloc[-1]
        ema50 = TechnicalIndicators.ema(df["Close"], 50).iloc[-1]
        rsi = TechnicalIndicators.rsi(df["Close"]).iloc[-1]
        atr = TechnicalIndicators.atr(df).iloc[-1]

        trend = "HAUSSIÈRE 📈" if ema20 > ema50 else "BAISSIÈRE 📉"

        return f"""
📊 *XAUUSD — VUE MARCHÉ*

💰 *Prix: {price_data['price']:.2f}*
📈 *Change: {price_data['change_pct']:+.2f}%*
🕐 *Session: {session}*

📍 *Structure:*
• Régime: {regime.value}
• Tendance: {trend}
• EMA20: {ema20:.2f}
• EMA50: {ema50:.2f}

📊 *Indicateurs:*
• RSI: {rsi:.1f}
• ATR: {atr:.2f}

🧠 *Recommandation:*
Utilisez `/analyse` pour un setup complet
ou `/update` si vous êtes en position.
"""

    @staticmethod
    def format_help() -> str:
        return """
🤖 *XAUUSD QUANT BOT — AIDE*

Commandes disponibles:

📊 `/start` — Démarrer le bot
📊 `/analyse` — Analyse complète + setup
📊 `/update` — Mise à jour position en cours
📊 `/prix` — Prix actuel + vue marché
📊 `/buy [prix] [sl] [tp]` — Enregistrer position BUY
📊 `/sell [prix] [sl] [tp]` — Enregistrer position SELL
📊 `/close` — Fermer position en cours
📊 `/status` — État de votre position
📊 `/help` — Cette aide

*Rappel:*
• Score < 60 = NO TRADE
• 60-75 = setup faible
• 75-85 = setup correct
• 85+ = setup PREMIUM ✅

*RR minimum: 1:1.5 | RR optimal: ≥1:2*
"""



# ─────────────────────────────────────────────────────────────
# HANDLERS TELEGRAM
# ─────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /start"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    welcome = f"""
🤖 *XAUUSD QUANT BOT — ACTIVÉ*

Bienvenue, trader.

Je suis un système de trading quant institutionnel
spécialisé sur l'or (XAUUSD).

*Philosophie:*
→ Je suis un FILTRE, pas un générateur de trades
→ Je refuse la majorité des setups
→ Je protège votre capital

*Seuils:*
• Score < 60 → NO TRADE
• 60-75 → setup faible
• 75-85 → setup correct
• 85+ → setup PREMIUM ✅

Envoyez `/help` pour les commandes.
    """

    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /help"""
    await update.message.reply_text(
        TelegramFormatter.format_help(),
        parse_mode="Markdown"
    )


async def prix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /prix — Prix actuel + vue marché"""
    user_id = update.effective_user.id

    await update.message.reply_text("⏳ Récupération des données marché...")

    price_data = MARKET.get_realtime_price()
    if not price_data:
        await update.message.reply_text("❌ Erreur récupération prix. Réessayez.")
        return

    data_dict = MARKET.get_multi_tf_data(period="3d")

    msg = TelegramFormatter.format_market_overview(price_data, data_dict)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /analyse — Analyse complète + génération setup"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    await update.message.reply_text("⏳ Analyse quant en cours... Cela peut prendre 10-20s.")

    # Récupération données
    price_data = MARKET.get_realtime_price()
    if not price_data:
        await update.message.reply_text("❌ Erreur données marché. Réessayez.")
        return

    data_dict = MARKET.get_multi_tf_data(period="5d")

    # Génération setup
    setup = SetupGenerator.generate_setup(data_dict)
    setup.raison_refus = setup.raison_refus if setup.raison_refus else ""

    # Sauvegarde en mémoire
    memory.save_setup(setup)

    # Formatage et envoi
    msg = TelegramFormatter.format_setup(setup, price_data["price"])
    await update.message.reply_text(msg, parse_mode="Markdown")


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /update — Mise à jour position en cours"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    if not memory.position:
        await update.message.reply_text(
            "❌ *Aucune position enregistrée.*

"
            "Utilisez `/buy prix sl tp` ou `/sell prix sl tp` pour enregistrer.
"
            "Ou `/analyse` pour un nouveau setup.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("⏳ Évaluation de la position en cours...")

    # Récupération prix actuel
    price_data = MARKET.get_realtime_price()
    if not price_data:
        await update.message.reply_text("❌ Erreur prix. Réessayez.")
        return

    data_dict = MARKET.get_multi_tf_data(period="3d")

    # Évaluation
    action, reason = SetupGenerator.evaluate_position(
        memory.position,
        price_data["price"],
        data_dict
    )

    # Exécution de l'action recommandée
    if action == Action.CLOSE:
        memory.close_position()
    elif action == Action.MOVE_SL_BE:
        memory.position.stop_loss = memory.position.prix_entree
        memory.position.sl_moved_be = True
    elif action == Action.PARTIAL_CLOSE:
        memory.position.partial_closed = True

    # Formatage et envoi
    msg = TelegramFormatter.format_position_update(
        memory.position if memory.position else Position(Direction.NONE, 0, 0, 0),
        action,
        reason,
        price_data["price"]
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /buy prix sl tp — Enregistre position BUY"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❌ *Format incorrect*

"
            "Usage: `/buy prix sl tp [volume]`
"
            "Ex: `/buy 3345.20 3340.00 3355.00`",
            parse_mode="Markdown"
        )
        return

    try:
        prix = float(args[0])
        sl = float(args[1])
        tp = float(args[2])
        volume = float(args[3]) if len(args) > 3 else 1.0

        memory.set_position(Direction.BUY, prix, sl, tp, volume)

        rr = (tp - prix) / (prix - sl) if (prix - sl) != 0 else 0

        await update.message.reply_text(
            f"🟢 *Position BUY enregistrée*

"
            f"• Entrée: {prix:.2f}
"
            f"• SL: {sl:.2f}
"
            f"• TP: {tp:.2f}
"
            f"• RR: 1:{rr:.2f}
"
            f"• Volume: {volume}

"
            f"Utilisez `/update` pour le suivi en temps réel.",
            parse_mode="Markdown"
        )

    except ValueError:
        await update.message.reply_text("❌ Prix invalides. Utilisez des nombres.")


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /sell prix sl tp — Enregistre position SELL"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❌ *Format incorrect*

"
            "Usage: `/sell prix sl tp [volume]`
"
            "Ex: `/sell 3345.20 3350.00 3330.00`",
            parse_mode="Markdown"
        )
        return

    try:
        prix = float(args[0])
        sl = float(args[1])
        tp = float(args[2])
        volume = float(args[3]) if len(args) > 3 else 1.0

        memory.set_position(Direction.SELL, prix, sl, tp, volume)

        rr = (prix - tp) / (sl - prix) if (sl - prix) != 0 else 0

        await update.message.reply_text(
            f"🔴 *Position SELL enregistrée*

"
            f"• Entrée: {prix:.2f}
"
            f"• SL: {sl:.2f}
"
            f"• TP: {tp:.2f}
"
            f"• RR: 1:{rr:.2f}
"
            f"• Volume: {volume}

"
            f"Utilisez `/update` pour le suivi en temps réel.",
            parse_mode="Markdown"
        )

    except ValueError:
        await update.message.reply_text("❌ Prix invalides. Utilisez des nombres.")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /close — Ferme la position en cours"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    if not memory.position:
        await update.message.reply_text("❌ Aucune position à fermer.")
        return

    pnl = memory.position.pnl_pct
    memory.close_position()

    emoji = "🟢" if pnl >= 0 else "🔴"
    await update.message.reply_text(
        f"{emoji} *Position fermée*

"
        f"P&L final: {pnl:+.2f}%

"
        f"Capital préservé. Analysez avant de ré-entrer.",
        parse_mode="Markdown"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /status — État de la position"""
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    if not memory.position:
        await update.message.reply_text(
            "📭 *Aucune position ouverte*

"
            "Utilisez `/analyse` pour trouver un setup.",
            parse_mode="Markdown"
        )
        return

    price_data = MARKET.get_realtime_price()
    current_price = price_data["price"] if price_data else 0

    memory.position.update_pnl(current_price)
    pnl = memory.position.pnl_pct

    emoji = "🟢" if pnl >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} *POSITION ACTUELLE*

"
        f"Direction: {memory.position.direction.value}
"
        f"Entrée: {memory.position.prix_entree:.2f}
"
        f"Prix actuel: {current_price:.2f}
"
        f"P&L: {pnl:+.2f}%
"
        f"SL: {memory.position.stop_loss:.2f}
"
        f"TP: {memory.position.take_profit:.2f}
"
        f"SL→BE: {'✅' if memory.position.sl_moved_be else '❌'}
"
        f"Partial: {'✅' if memory.position.partial_closed else '❌'}

"
        f"`/update` pour évaluation en temps réel.",
        parse_mode="Markdown"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler texte libre — Interprétation des intentions"""
    text = update.message.text.lower().strip()
    user_id = update.effective_user.id
    memory = get_user_memory(user_id)

    # Mapping des intentions
    if any(word in text for word in ["analyse", "setup", "trade", "signal"]):
        await analyse_command(update, context)

    elif any(word in text for word in ["update", "mise à jour", "suivi", "position"]):
        if memory.position:
            await update_command(update, context)
        else:
            await update.message.reply_text(
                "📭 Pas de position en cours.
"
                "Envoyez `/analyse` pour un setup ou enregistrez une position."
            )

    elif any(word in text for word in ["prix", "price", "cours", "gold"]):
        await prix_command(update, context)

    elif any(word in text for word in ["fermer", "close", "clôturer", "sortir"]):
        await close_command(update, context)

    elif "buy" in text or "achat" in text or "long" in text:
        # Essaie d'extraire les prix du texte
        import re
        numbers = re.findall(r"[0-9]+\.?[0-9]*", text)
        if len(numbers) >= 3:
            context.args = numbers[:4]
            await buy_command(update, context)
        else:
            await update.message.reply_text(
                "🟢 Pour enregistrer un BUY:
"
                "`/buy prix sl tp`
"
                "Ex: `/buy 3345.20 3340.00 3355.00`"
            )

    elif "sell" in text or "vente" in text or "short" in text:
        import re
        numbers = re.findall(r"[0-9]+\.?[0-9]*", text)
        if len(numbers) >= 3:
            context.args = numbers[:4]
            await sell_command(update, context)
        else:
            await update.message.reply_text(
                "🔴 Pour enregistrer un SELL:
"
                "`/sell prix sl tp`
"
                "Ex: `/sell 3345.20 3350.00 3330.00`"
            )

    elif any(word in text for word in ["aide", "help", "commande", "comment"]):
        await help_command(update, context)

    else:
        await update.message.reply_text(
            "🤖 Je n'ai pas compris.

"
            "Essayez:
"
            "• `/analyse` — Nouveau setup
"
            "• `/update` — Suivi position
"
            "• `/prix` — Prix actuel
"
            "• `/help` — Toutes les commandes"
        )




# ─────────────────────────────────────────────────────────────
# SYSTÈME D'ALERTES AUTOMATIQUES
# ─────────────────────────────────────────────────────────────

async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Job périodique: scanne le marché et alerte si setup premium détecté.
    """
    for user_id, memory in USER_MEMORIES.items():
        if not memory.preferences.get("alertes", True):
            continue

        # Pas d'alerte si déjà en position
        if memory.position:
            continue

        try:
            data_dict = MARKET.get_multi_tf_data(period="3d")
            setup = SetupGenerator.generate_setup(data_dict)

            if setup.qualite == Qualite.A_PLUS and setup.score.total >= 85:
                price_data = MARKET.get_realtime_price()
                current_price = price_data["price"] if price_data else setup.prix_entree

                msg = f"""
🚨 *ALERTE SETUP PREMIUM 🌟*

{setup.direction.value} détecté sur XAUUSD!

📈 Score: {setup.score.total:.0f}/100
⭐ Qualité: A+

📌 Plan:
• Entrée: `{setup.prix_entree:.2f}`
• SL: `{setup.stop_loss:.2f}`
• TP: `{setup.take_profit:.2f}`
• RR: `1:{setup.rr:.2f}`

💰 Prix actuel: {current_price:.2f}

`/analyse` pour détails complets.
"""
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")

        except Exception as e:
            logger.warning(f"Erreur alerte user {user_id}: {e}")


# ─────────────────────────────────────────────────────────────
# GESTION ERREURS GLOBALE
# ─────────────────────────────────────────────────────────────

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestionnaire d'erreurs global."""
    logger.error(f"Exception: {context.error}", exc_info=True)

    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ *Erreur interne.*
"
            "Les données marché peuvent être indisponibles.
"
            "Réessayez dans 30 secondes.",
            parse_mode="Markdown"
        )


# ─────────────────────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────────────────────

def main():
    """Point d'entrée principal du bot."""

    # Création de l'application
    application = Application.builder().token(TOKEN).build()

    # Job d'alertes automatiques (toutes les 5 minutes)
    application.job_queue.run_repeating(alert_job, interval=300, first=60)

    # Handler d'erreurs global
    application.add_error_handler(error_handler)

    # Ajout des handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("analyse", analyse_command))
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CommandHandler("prix", prix_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("close", close_command))
    application.add_handler(CommandHandler("status", status_command))

    # Handler texte libre
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Démarrage
    logger.info("🤖 XAUUSD Quant Bot démarré")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════
# FIN DU BOT
# ═══════════════════════════════════════════════════════════════
