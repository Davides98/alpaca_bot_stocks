"""
Configurazione centralizzata del bot azionario (pydantic-settings).

Ogni campo può essere sovrascritto da una variabile d'ambiente con lo stesso nome
(o da un file `.env` nella root del progetto). Le credenziali sono `SecretStr`:
non vengono mai mostrate in chiaro da `repr()`/log.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Il bot opera ESCLUSIVAMENTE in paper trading. Non è un parametro configurabile.
PAPER_TRADING: bool = True

DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "JPM", "COST",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # su GitHub un Secret/Variable non definito arriva come stringa vuota -> uso il default
        case_sensitive=True,
    )

    # ------------------------------------------------------------------ #
    # Credenziali (solo da env / .env)
    # ------------------------------------------------------------------ #
    ALPACA_API_KEY: SecretStr = SecretStr("")
    ALPACA_SECRET_KEY: SecretStr = SecretStr("")
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: SecretStr = SecretStr("")

    # ------------------------------------------------------------------ #
    # Modalità di esecuzione
    # ------------------------------------------------------------------ #
    DRY_RUN: bool = False  # False = invia ordini (sempre e solo sull'account PAPER)
    IGNORE_MARKET_HOURS: bool = False  # efficace solo se DRY_RUN=true
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------ #
    # Modalità sessione (un job resta attivo per tutta la sessione di mercato)
    # ------------------------------------------------------------------ #
    SESSION_MODE: bool = False  # False = run singolo (locale / manuale); True = ciclo continuo (GitHub automatico)
    REFRESH_INTERVAL_SEC: int = Field(default=60, ge=30, le=600)  # equity + P&L + stop dinamici
    TRADE_INTERVAL_MIN: int = Field(default=5, ge=1, le=120)  # screening + nuovi trade
    SESSION_MAX_MINUTES: int = Field(default=340, ge=5, le=350)  # i job GitHub durano al massimo 360 min
    SESSION_WAIT_FOR_OPEN_MIN: int = Field(default=20, ge=0, le=120)

    # ------------------------------------------------------------------ #
    # Universo e dati
    # ------------------------------------------------------------------ #
    WATCHLIST: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    HISTORY_TRADING_DAYS: int = Field(default=300, ge=120, le=1000)  # >= 252 + HV_WINDOW per il Vol Rank a 1 anno
    # Feed per prezzi in tempo reale (ultimo trade / quote): "iex" è gratuito, "sip" richiede l'abbonamento.
    STOCK_DATA_FEED: str = "iex"
    # Feed per le barre daily. Il piano gratuito include i dati SIP con almeno 15 minuti di ritardo:
    # volumi completi (IEX vede solo ~2-3% del volume USA). Se SIP non è disponibile si ripiega su IEX.
    BARS_DATA_FEED: str = "sip"
    SIP_DELAY_MIN: int = Field(default=16, ge=0, le=60)

    # Filtri di liquidità
    MIN_PRICE: float = Field(default=10.0, ge=1.0)
    MIN_AVG_DOLLAR_VOLUME: float = Field(default=50_000_000.0, ge=0)  # controvalore medio a 20 giorni (solo con barre SIP)
    MAX_QUOTE_SPREAD_PCT: float = Field(default=0.005, gt=0, le=0.05)  # spread bid-ask oltre cui la quote è ignorata

    # ------------------------------------------------------------------ #
    # Regime detector
    # ------------------------------------------------------------------ #
    EMA_FAST: int = 20
    EMA_SLOW: int = 50
    ADX_PERIOD: int = 14
    ADX_TREND_THRESHOLD: float = 20.0
    ATR_PERIOD: int = 14
    RSI_PERIOD: int = 14
    HV_WINDOW: int = 20
    VOL_RANK_HIGH_THRESHOLD: float = Field(default=50.0, ge=0, le=100)  # Vol Rank >= soglia -> HIGH_VOL

    # ------------------------------------------------------------------ #
    # Segnali di ingresso
    # ------------------------------------------------------------------ #
    BREAKOUT_LOOKBACK: int = Field(default=20, ge=5, le=120)  # canale di Donchian (massimi/minimi a N giorni)
    BREAKOUT_MAX_EXTENSION_ATR: float = Field(default=0.75, gt=0, le=5)  # non insegue un breakout già lontano
    PULLBACK_MAX_DIST_ATR: float = Field(default=1.0, gt=0, le=5)  # distanza massima dalla EMA20
    PULLBACK_RSI_MAX: float = Field(default=50.0, ge=20, le=80)  # long: RSI <= soglia; short: RSI >= 100 - soglia
    BB_PERIOD: int = 20
    BB_STD: float = Field(default=2.0, gt=0, le=4)

    # ------------------------------------------------------------------ #
    # Stop e target (in multipli di ATR e di R = distanza dello stop)
    # ------------------------------------------------------------------ #
    BREAKOUT_STOP_ATR: float = Field(default=2.0, gt=0, le=10)
    PULLBACK_STOP_ATR: float = Field(default=1.5, gt=0, le=10)
    MEAN_REVERSION_STOP_ATR: float = Field(default=1.25, gt=0, le=10)
    TAKE_PROFIT_R_TREND: float = Field(default=3.0, gt=0, le=20)  # target dei trade di trend = 3R
    MIN_REWARD_RISK: float = Field(default=0.8, gt=0, le=10)  # mean reversion: target (media) >= 0.8R
    # Gestione dinamica dello stop (solo in avanti, mai indietro)
    BREAKEVEN_AT_R: float = Field(default=1.0, ge=0, le=10)  # 0 = disattivato
    TRAIL_START_R: float = Field(default=1.5, ge=0, le=20)  # 0 = disattivato; solo strategie di trend
    TRAIL_ATR_MULT: float = Field(default=2.5, gt=0, le=10)
    # Uscite temporali e di regime
    MAX_HOLDING_DAYS_TREND: int = Field(default=30, ge=1, le=365)
    MAX_HOLDING_DAYS_MR: int = Field(default=10, ge=1, le=365)
    EXIT_ON_TREND_REVERSAL: bool = True

    # ------------------------------------------------------------------ #
    # Screener
    # ------------------------------------------------------------------ #
    MIN_OPPORTUNITY_SCORE: float = Field(default=40.0, ge=0, le=100)

    # ------------------------------------------------------------------ #
    # Risk management
    # ------------------------------------------------------------------ #
    MAX_RISK_PER_TRADE_PCT: float = 0.01  # perdita massima (allo stop) per trade, in frazione dell'equity
    MAX_POSITION_PCT: float = Field(default=0.20, gt=0, le=1.0)  # controvalore massimo di una posizione
    MAX_GROSS_EXPOSURE_PCT: float = Field(default=1.0, gt=0, le=2.0)  # long + short in valore assoluto (1.0 = no leva)
    MAX_OPEN_POSITIONS: int = Field(default=5, ge=1, le=30)
    MAX_NEW_TRADES_PER_RUN: int = Field(default=2, ge=1, le=10)
    ALLOW_SHORT: bool = True  # False = solo strategie long (le short diventano NO_TRADE)
    STOP_LOSS_COOLDOWN_HOURS: int = Field(default=24, ge=0, le=240)

    # ------------------------------------------------------------------ #
    # Finestre di mercato
    # ------------------------------------------------------------------ #
    NO_TRADE_AFTER_OPEN_MIN: int = Field(default=15, ge=0, le=120)  # niente nuovi trade né stop dinamici
    NO_NEW_TRADES_BEFORE_CLOSE_MIN: int = Field(default=15, ge=0, le=120)

    # ------------------------------------------------------------------ #
    # Esecuzione
    # ------------------------------------------------------------------ #
    CHASE_INTERVAL_SEC: int = Field(default=10, ge=2, le=120)
    MAX_CHASE_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    MAX_ENTRY_SLIPPAGE_PCT: float = Field(default=0.002, ge=0, le=0.05)  # 0.2% oltre il prezzo di riferimento
    MAX_EXIT_SLIPPAGE_PCT: float = Field(default=0.005, ge=0, le=0.05)
    ORDER_POLL_SEC: float = 1.5

    # ------------------------------------------------------------------ #
    # Performance
    # ------------------------------------------------------------------ #
    INITIAL_CAPITAL: float = 100_000.0

    # ------------------------------------------------------------------ #
    # Validatori
    # ------------------------------------------------------------------ #
    @field_validator("WATCHLIST", mode="before")
    @classmethod
    def _parse_watchlist(cls, value: Any) -> Any:
        """Accetta sia 'SPY,QQQ' sia '["SPY","QQQ"]' sia una lista Python."""
        if isinstance(value, str):
            cleaned = value.strip().strip("[]")
            items = [s.strip().strip("'\"").upper() for s in cleaned.split(",")]
        elif isinstance(value, (list, tuple)):
            items = [str(s).strip().upper() for s in value]
        else:
            return value
        seen: list[str] = []
        for s in items:
            if s and s not in seen:
                seen.append(s)
        return seen

    @field_validator(
        "MAX_RISK_PER_TRADE_PCT", "MAX_POSITION_PCT", "MAX_GROSS_EXPOSURE_PCT",
        "MAX_ENTRY_SLIPPAGE_PCT", "MAX_EXIT_SLIPPAGE_PCT", "MAX_QUOTE_SPREAD_PCT",
        mode="before",
    )
    @classmethod
    def _parse_percent(cls, value: Any) -> Any:
        """Accetta '1%' come 0.01 (oltre al formato decimale 0.01)."""
        if isinstance(value, str) and value.strip().endswith("%"):
            return float(value.strip().rstrip("%").replace(",", ".")) / 100.0
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.EMA_FAST >= self.EMA_SLOW:
            raise ValueError("EMA_FAST deve essere minore di EMA_SLOW")
        if not 0 < self.MAX_RISK_PER_TRADE_PCT <= 0.05:
            raise ValueError("MAX_RISK_PER_TRADE_PCT deve essere in (0, 0.05]")
        if not self.WATCHLIST:
            raise ValueError("WATCHLIST vuota")
        return self

    # ------------------------------------------------------------------ #
    # Helper
    # ------------------------------------------------------------------ #
    @property
    def alpaca_configured(self) -> bool:
        return bool(self.ALPACA_API_KEY.get_secret_value() and self.ALPACA_SECRET_KEY.get_secret_value())

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value())

    def secret_values(self) -> list[str]:
        """Valori segreti da mascherare nei log."""
        values = [
            self.ALPACA_API_KEY.get_secret_value(),
            self.ALPACA_SECRET_KEY.get_secret_value(),
            self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
        ]
        return [v for v in values if v and len(v) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
