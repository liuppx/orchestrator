from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from .base import MarketSnapshot, Signal, Strategy


ACTION_HOLD = "hold"
ACTION_BUY = "buy"
ACTION_SELL = "sell"

DECISION_HOLD = "hold"
DECISION_BUY = "buy"
DECISION_SELL_HALF = "sell_half"
DECISION_SELL_FULL = "sell_full"


@dataclass(frozen=True)
class AuctionWaveSellRules:
    auction_lock_time: time
    auction_open_end_time: time
    zero_threshold_pct: float
    strong_open_threshold_pct: float
    afternoon_exit_time: time
    limit_up_threshold_pct: float
    lot_size: int

    @classmethod
    def from_config(cls, config: dict) -> "AuctionWaveSellRules":
        return cls(
            auction_lock_time=_parse_hhmm(str(config.get("auction_lock_time", "09:29")), "09:29"),
            auction_open_end_time=_parse_hhmm(str(config.get("auction_open_end_time", "09:35")), "09:35"),
            zero_threshold_pct=float(config.get("auction_zero_threshold_pct", 0.0)),
            strong_open_threshold_pct=float(config.get("auction_strong_open_threshold_pct", 3.0)),
            afternoon_exit_time=_parse_hhmm(str(config.get("afternoon_exit_time", "14:30")), "14:30"),
            limit_up_threshold_pct=float(config.get("limit_up_threshold_pct", 9.8)),
            lot_size=int(config.get("lot_size", 100) or 100),
        )

    def as_metadata(self) -> dict:
        return {
            "auction_lock_time": self.auction_lock_time.strftime("%H:%M"),
            "auction_open_end_time": self.auction_open_end_time.strftime("%H:%M"),
            "auction_zero_threshold_pct": self.zero_threshold_pct,
            "auction_strong_open_threshold_pct": self.strong_open_threshold_pct,
            "afternoon_exit_time": self.afternoon_exit_time.strftime("%H:%M"),
            "limit_up_threshold_pct": self.limit_up_threshold_pct,
            "lot_size": self.lot_size,
        }


def _parse_hhmm(value: str, fallback: str) -> time:
    raw = (value or fallback).strip()
    try:
        hour, minute = raw.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except Exception:
        hour, minute = fallback.split(":", 1)
        return time(hour=int(hour), minute=int(minute))


def _detect_phase(now: datetime, rules: AuctionWaveSellRules) -> str:
    current = now.time()
    if current < rules.auction_lock_time:
        return "pre_open"
    if current < time(9, 30):
        return "auction_lock"
    if current < rules.auction_open_end_time:
        return "auction_open"
    if current < rules.afternoon_exit_time:
        return "mid_session"
    return "afternoon_exit"


def _recent_returns(series: list[float]) -> list[float]:
    values: list[float] = []
    for prev, cur in zip(series, series[1:]):
        if prev:
            values.append((cur - prev) / prev)
    return values


def _round_lot(quantity: int, lot_size: int) -> int:
    if quantity <= 0:
        return 0
    if lot_size <= 1:
        return quantity
    return max(lot_size, (quantity // lot_size) * lot_size)


def _position_state(state: dict, config: dict, observed_at: datetime) -> tuple[dict, int]:
    next_state = dict(state or {})
    configured_position = int(config.get("position_quantity", 0) or 0)
    if "remaining_quantity" not in next_state:
        next_state["remaining_quantity"] = configured_position
    if "initial_quantity" not in next_state:
        next_state["initial_quantity"] = next_state["remaining_quantity"] or configured_position
    next_state.setdefault("position_opened", next_state.get("remaining_quantity", 0) > 0)
    next_state.setdefault("morning_half_sold", False)
    next_state.setdefault("afternoon_exit_done", False)
    next_state.setdefault("entry_day", observed_at.strftime("%Y-%m-%d"))
    return next_state, int(next_state.get("remaining_quantity", 0) or 0)


def _close_position(state: dict) -> None:
    state["remaining_quantity"] = 0
    state["position_opened"] = False
    state["afternoon_exit_done"] = True


def _with_decision(metadata: dict, action_type: str, rule_id: str, quantity: int) -> dict:
    metadata["decision"] = {
        "action_type": action_type,
        "rule_id": rule_id,
        "quantity": quantity,
    }
    return metadata


class AuctionWaveStrategy(Strategy):
    def evaluate(self, snapshot: MarketSnapshot, prior_state: dict | None = None) -> Signal:
        state = dict(prior_state or {})
        latest = snapshot.latest_price
        pre_close = snapshot.pre_close
        sell_rules = AuctionWaveSellRules.from_config(self.config)
        enable_buy = bool(self.config.get("enable_buy", False))
        observed_at = snapshot.observed_at or datetime.now()
        phase = _detect_phase(observed_at, sell_rules)
        active_lookback = int(self.config.get("active_lookback_days", 10))
        active_threshold_pct = float(self.config.get("active_threshold_pct", 4.0))

        metadata = {
            "strategy": "auction_wave",
            "observedAt": observed_at.isoformat(timespec="seconds"),
            "phase": phase,
            "sell_rules": sell_rules.as_metadata(),
            "action_enum": {
                "hold": DECISION_HOLD,
                "buy": DECISION_BUY,
                "sell_half": DECISION_SELL_HALF,
                "sell_full": DECISION_SELL_FULL,
            },
        }

        if latest is None or pre_close in (None, 0):
            metadata["next_state"] = state
            return Signal(ACTION_HOLD, "missing latest or pre-close price", 0, latest, _with_decision(metadata, DECISION_HOLD, "missing_price", 0))

        state, position_quantity = _position_state(state, self.config, observed_at)
        metadata["remaining_quantity"] = position_quantity
        pct = ((latest - pre_close) / pre_close) * 100.0
        metadata["change_pct"] = round(pct, 4)
        metadata["pre_close"] = pre_close

        if position_quantity > 0:
            auction_bucket = state.get("auction_bucket")
            if auction_bucket is None and phase != "pre_open":
                if pct < sell_rules.zero_threshold_pct:
                    auction_bucket = "below_zero"
                elif pct <= sell_rules.strong_open_threshold_pct:
                    auction_bucket = "zero_to_three"
                else:
                    auction_bucket = "above_three"
                state["auction_bucket"] = auction_bucket
                state["auction_bucket_locked_at"] = observed_at.isoformat(timespec="seconds")
                state["auction_bucket_source"] = "first_observed_after_09_29"

            is_limit_up = pct >= sell_rules.limit_up_threshold_pct
            metadata["auction_bucket"] = auction_bucket
            metadata["is_limit_up"] = is_limit_up
            metadata["position_state"] = {
                "remaining_quantity": state.get("remaining_quantity", 0),
                "initial_quantity": state.get("initial_quantity", 0),
                "morning_half_sold": bool(state.get("morning_half_sold")),
                "position_opened": bool(state.get("position_opened")),
                "entry_day": state.get("entry_day"),
                "auction_bucket_locked_at": state.get("auction_bucket_locked_at"),
            }

            if auction_bucket is None:
                metadata["next_state"] = state
                return Signal(
                    ACTION_HOLD,
                    f"waiting for {sell_rules.auction_lock_time.strftime('%H:%M')}+ auction observation to lock the opening bucket",
                    0,
                    latest,
                    _with_decision(metadata, DECISION_HOLD, "wait_for_auction_lock", 0),
                )

            if auction_bucket == "below_zero":
                _close_position(state)
                metadata["next_state"] = state
                return Signal(
                    ACTION_SELL,
                    f"auction bucket below {sell_rules.zero_threshold_pct:.2f}%, exit full position at {pct:.2f}%",
                    position_quantity,
                    latest,
                    _with_decision(metadata, DECISION_SELL_FULL, "auction_below_zero_full_exit", position_quantity),
                )

            if auction_bucket == "zero_to_three":
                base_quantity = int(state.get("initial_quantity", position_quantity) or position_quantity)
                half_qty = _round_lot(base_quantity // 2, sell_rules.lot_size) if base_quantity > 1 else base_quantity
                half_qty = min(position_quantity, half_qty or position_quantity)
                if phase in {"auction_lock", "auction_open"} and not state.get("morning_half_sold"):
                    state["morning_half_sold"] = True
                    state["remaining_quantity"] = max(0, position_quantity - half_qty)
                    state["position_opened"] = state["remaining_quantity"] > 0
                    metadata["next_state"] = state
                    return Signal(
                        ACTION_SELL,
                        f"auction in {sell_rules.zero_threshold_pct:.2f}%-{sell_rules.strong_open_threshold_pct:.2f}%, sell half on opening handling step, current {pct:.2f}%",
                        half_qty,
                        latest,
                        _with_decision(metadata, DECISION_SELL_HALF, "auction_zero_to_three_sell_half", half_qty),
                    )
                if phase in {"auction_open", "mid_session", "afternoon_exit"} and pct <= sell_rules.zero_threshold_pct and position_quantity > 0:
                    _close_position(state)
                    metadata["next_state"] = state
                    return Signal(
                        ACTION_SELL,
                        f"remaining half exits after price retraced to {sell_rules.zero_threshold_pct:.2f}% or below, current {pct:.2f}%",
                        position_quantity,
                        latest,
                        _with_decision(metadata, DECISION_SELL_FULL, "zero_to_three_retrace_full_exit", position_quantity),
                    )
                if phase == "afternoon_exit" and not is_limit_up and position_quantity > 0 and not state.get("afternoon_exit_done"):
                    _close_position(state)
                    metadata["next_state"] = state
                    return Signal(
                        ACTION_SELL,
                        f"afternoon forced exit at {sell_rules.afternoon_exit_time.strftime('%H:%M')} because not limit-up",
                        position_quantity,
                        latest,
                        _with_decision(metadata, DECISION_SELL_FULL, "afternoon_not_limit_up_full_exit", position_quantity),
                )

            if auction_bucket == "above_three":
                if phase in {"auction_open", "mid_session", "afternoon_exit"} and pct < sell_rules.strong_open_threshold_pct and position_quantity > 0:
                    _close_position(state)
                    metadata["next_state"] = state
                    return Signal(
                        ACTION_SELL,
                        f"auction >{sell_rules.strong_open_threshold_pct:.2f}% but intraday retraced below {sell_rules.strong_open_threshold_pct:.2f}%, exit full position at {pct:.2f}%",
                        position_quantity,
                        latest,
                        _with_decision(metadata, DECISION_SELL_FULL, "above_three_retrace_full_exit", position_quantity),
                    )
                if phase == "afternoon_exit" and not is_limit_up and position_quantity > 0 and not state.get("afternoon_exit_done"):
                    _close_position(state)
                    metadata["next_state"] = state
                    return Signal(
                        ACTION_SELL,
                        f"afternoon forced exit at {sell_rules.afternoon_exit_time.strftime('%H:%M')} because not limit-up",
                        position_quantity,
                        latest,
                        _with_decision(metadata, DECISION_SELL_FULL, "afternoon_not_limit_up_full_exit", position_quantity),
                    )

            metadata["next_state"] = state
            return Signal(
                ACTION_HOLD,
                f"holding position under {auction_bucket} rule, current {pct:.2f}%",
                0,
                latest,
                _with_decision(metadata, DECISION_HOLD, "position_hold_under_bucket_rule", 0),
            )

        # Buy side is intentionally approximate in v2. It adds activity and momentum
        # gates without pretending we already have full wave/board/ask-depth data.
        if enable_buy and len(snapshot.close_series) >= active_lookback:
            recent_window = snapshot.close_series[-active_lookback:]
            recent_high = max(recent_window)
            recent_low = min(recent_window)
            recent_returns = _recent_returns(recent_window)
            active_pct = (recent_high - recent_low) / recent_low * 100 if recent_low else 0
            avg_return_pct = (sum(abs(v) for v in recent_returns) / len(recent_returns) * 100) if recent_returns else 0
            resistance_lookback = int(self.config.get("resistance_lookback_days", max(active_lookback * 2, 20)))
            resistance_buffer_pct = float(self.config.get("resistance_buffer_pct", 1.0))
            resistance_window = snapshot.close_series[-resistance_lookback:] if len(snapshot.close_series) >= resistance_lookback else snapshot.close_series
            resistance_high = max(resistance_window) if resistance_window else recent_high
            breakout_buffer_pct = float(self.config.get("breakout_buffer_pct", 0.2))
            breakout_trigger = recent_high * (1 + breakout_buffer_pct / 100.0)
            not_near_resistance = latest >= resistance_high or latest <= resistance_high * (1 - resistance_buffer_pct / 100.0)
            metadata["buy_gate"] = {
                "active_pct": round(active_pct, 4),
                "avg_return_pct": round(avg_return_pct, 4),
                "recent_high": recent_high,
                "recent_low": recent_low,
                "lookback_days": active_lookback,
                "resistance_high": resistance_high,
                "resistance_lookback_days": resistance_lookback,
                "resistance_buffer_pct": resistance_buffer_pct,
                "breakout_trigger": round(breakout_trigger, 4),
                "breakout_buffer_pct": breakout_buffer_pct,
            }
            is_active = active_pct >= active_threshold_pct or avg_return_pct >= (active_threshold_pct / 2)
            near_breakout = latest >= breakout_trigger
            if phase in {"auction_open", "mid_session"} and is_active and near_breakout and not_near_resistance:
                buy_qty = int(self.config.get("quantity", 100))
                state["remaining_quantity"] = buy_qty
                state["initial_quantity"] = buy_qty
                state["auction_bucket"] = None
                state["morning_half_sold"] = False
                state["afternoon_exit_done"] = False
                state["position_opened"] = True
                state["entry_day"] = observed_at.strftime("%Y-%m-%d")
                state.pop("auction_bucket_locked_at", None)
                state.pop("auction_bucket_source", None)
                metadata["next_state"] = state
                metadata["buy_mode"] = "activity_plus_breakout_gate"
                return Signal(
                    ACTION_BUY,
                    "buy-side approximate gate passed: active recent window, breakout confirmed, no strong resistance",
                    buy_qty,
                    latest,
                    _with_decision(metadata, DECISION_BUY, "activity_breakout_buy_gate", buy_qty),
                )

        metadata["next_state"] = state
        return Signal(
            ACTION_HOLD,
            "no sell trigger and buy-side approximation did not pass",
            0,
            latest,
            _with_decision(metadata, DECISION_HOLD, "no_trigger", 0),
        )
