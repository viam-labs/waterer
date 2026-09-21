"""Scheduled pump controller with safety caps.

Wraps a Switch dependency (typically a smart plug or GPIO switch) with:
- ml-per-second calibration → dose-in-ml semantics.
- Hard cap on runtime per dispense.
- Hard cap on daily total volume.
- A schedule loop that fires named schedules at HH:MM on selected days.

State (schedules, daily total, last dispense) persists to
~/.viam/waterer-<name>-state.json so a module restart doesn't lose
today's total or forget an already-fired schedule.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.components.switch import Switch
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/waterer-{name}-state.json"
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_ML_PER_SECOND = 20.0
DEFAULT_MAX_RUNTIME_SECONDS = 60.0
DEFAULT_MAX_DAILY_ML = 5000.0
# How late a scheduled fire can still run after its HH:MM before we mark
# it missed instead. Keeps a Pi that just rebooted from firing a
# schedule that should have happened hours ago.
CATCH_UP_WINDOW_MIN = 30


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _normalize_days(days: Any) -> list[int]:
    """0..6 = Mon..Sun. Empty = every day.

    Viam serializes numeric config fields through protobuf's Value type
    as doubles, so `[0, 1, 2]` arrives as `[0.0, 1.0, 2.0]`. Coerce.
    """
    if days is None:
        return []
    if not isinstance(days, list):
        raise ValueError("`days_of_week` must be a list of integers 0..6")
    out = set()
    for d in days:
        if isinstance(d, bool):
            raise ValueError("`days_of_week` values must be integers 0..6")
        if isinstance(d, int):
            di = d
        elif isinstance(d, float) and d.is_integer():
            di = int(d)
        else:
            raise ValueError("`days_of_week` values must be integers 0..6")
        if not 0 <= di <= 6:
            raise ValueError("`days_of_week` values must be integers 0..6")
        out.add(di)
    return sorted(out)


def _parse_hhmm(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"`time` must be HH:MM, got {value!r}")
    h, m = value.split(":", 1)
    hi, mi = int(h), int(m)
    if not (0 <= hi < 24 and 0 <= mi < 60):
        raise ValueError(f"`time` out of range: {value!r}")
    return hi, mi


def _normalize_positive_number(name: str, value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"`{name}` must be a positive number")
    return float(value)


def _normalize_schedule(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    hi, mi = _parse_hhmm(raw.get("time"))
    dose_ml = _normalize_positive_number("dose_ml", raw.get("dose_ml"))
    return {
        "id": raw.get("id") or _new_id(),
        "name": str(raw.get("name") or f"Dispense {hi:02d}:{mi:02d}"),
        "time": f"{hi:02d}:{mi:02d}",
        "dose_ml": dose_ml,
        "days_of_week": _normalize_days(raw.get("days_of_week")),
        "enabled": bool(raw.get("enabled", True)),
        "last_fired_at": raw.get("last_fired_at"),
    }


def _empty_state() -> dict:
    return {
        "schedules": [],
        "daily_total": {"date": None, "ml": 0.0},
        "last_dispense": None,
    }


class Pump(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam-labs", "waterer"), "pump")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._switch: Switch | None = None
        self._switch_name: str = ""
        self._ml_per_second: float = DEFAULT_ML_PER_SECOND
        self._max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
        self._max_daily_ml: float = DEFAULT_MAX_DAILY_ML
        self._poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
        self._state_path: str = ""
        self._state: dict = _empty_state()
        self._state_lock: asyncio.Lock | None = None
        self._bg_task: asyncio.Task | None = None
        self._dispense_lock: asyncio.Lock | None = None

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Pump":
        p = cls(config.name)
        p.reconfigure(config, dependencies)
        return p

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        switch_name = attrs.get("switch_name")
        if not isinstance(switch_name, str) or not switch_name:
            raise ValueError("`switch_name` is required")
        for field, default in (
            ("ml_per_second", DEFAULT_ML_PER_SECOND),
            ("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS),
            ("max_daily_ml", DEFAULT_MAX_DAILY_ML),
        ):
            value = attrs.get(field)
            if value is not None:
                _normalize_positive_number(field, value)
            _ = default
        raw_schedules = attrs.get("schedules")
        if raw_schedules is not None:
            if not isinstance(raw_schedules, list):
                raise ValueError("`schedules` must be a list")
            for entry in raw_schedules:
                _normalize_schedule(entry)
        return [switch_name]

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._switch_name = str(attrs["switch_name"])
        self._ml_per_second = float(attrs.get("ml_per_second") or DEFAULT_ML_PER_SECOND)
        self._max_runtime_seconds = float(
            attrs.get("max_runtime_seconds") or DEFAULT_MAX_RUNTIME_SECONDS
        )
        self._max_daily_ml = float(attrs.get("max_daily_ml") or DEFAULT_MAX_DAILY_ML)
        self._poll_interval_sec = int(attrs.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC)
        self._state_path = str(
            attrs.get("state_path") or DEFAULT_STATE_PATH.format(name=config.name)
        )

        self._switch = None
        for name, resource in dependencies.items():
            if name.name == self._switch_name and isinstance(resource, Switch):
                self._switch = resource
                break
        if self._switch is None:
            raise RuntimeError(f"Switch dependency {self._switch_name!r} not found")

        self._state = self._load_and_seed_state(attrs)
        self._state_lock = asyncio.Lock()
        self._dispense_lock = asyncio.Lock()

        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._bg_loop())
        except RuntimeError:
            self._bg_task = None

    # ------------------------------------------------------------------
    # State persistence

    def _load_and_seed_state(self, attrs: dict) -> dict:
        loaded = self._load_state()
        state = _empty_state()
        if isinstance(loaded.get("schedules"), list):
            state["schedules"] = loaded["schedules"]
        if isinstance(loaded.get("daily_total"), dict):
            state["daily_total"] = {
                "date": loaded["daily_total"].get("date"),
                "ml": float(loaded["daily_total"].get("ml") or 0.0),
            }
        if isinstance(loaded.get("last_dispense"), dict):
            state["last_dispense"] = loaded["last_dispense"]
        if not state["schedules"]:
            state["schedules"] = self._seed_schedules_from_config(attrs)
        return state

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load state from %s: %s", path, e)
            return {}

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _seed_schedules_from_config(self, attrs: dict) -> list[dict]:
        raw = attrs.get("schedules")
        if not isinstance(raw, list):
            return []
        out = []
        for entry in raw:
            try:
                out.append(_normalize_schedule(entry))
            except ValueError as e:
                LOGGER.warning("skipping bad config schedule: %s", e)
        return out

    def _find_schedule(self, sid: str) -> dict | None:
        for s in self._state.get("schedules", []):
            if s.get("id") == sid:
                return s
        return None

    def _ensure_daily_total_current(self, today: date) -> None:
        dt = self._state.get("daily_total") or {"date": None, "ml": 0.0}
        if dt.get("date") != today.isoformat():
            self._state["daily_total"] = {"date": today.isoformat(), "ml": 0.0}

    # ------------------------------------------------------------------
    # Dispense (safety-checked)

    async def _dispense_seconds(self, seconds: float, source: str) -> dict:
        assert self._switch is not None
        assert self._state_lock is not None
        assert self._dispense_lock is not None

        seconds = float(seconds)
        if seconds <= 0:
            raise ValueError("`seconds` must be positive")
        if seconds > self._max_runtime_seconds:
            raise RuntimeError(
                f"requested {seconds:.1f}s exceeds max_runtime_seconds "
                f"({self._max_runtime_seconds:.1f}s)"
            )

        ml = seconds * self._ml_per_second
        now_local = datetime.now().astimezone()
        today = now_local.date()

        async with self._state_lock:
            self._ensure_daily_total_current(today)
            dt = self._state["daily_total"]
            if dt["ml"] + ml > self._max_daily_ml:
                raise RuntimeError(
                    f"dispense of {ml:.0f} ml would exceed daily cap "
                    f"({dt['ml']:.0f} already, cap {self._max_daily_ml:.0f})"
                )

        # Serialize actual pump runs — never overlap two dispenses.
        async with self._dispense_lock:
            started_at = datetime.now(UTC)
            try:
                await self._switch.set_position(1)
                await asyncio.sleep(seconds)
            finally:
                # Belt-and-suspenders: always try to turn off, even if
                # sleep was cancelled (module shutdown, reconfigure).
                try:
                    await self._switch.set_position(0)
                except Exception as e:
                    LOGGER.error("failed to turn switch off after dispense: %s", e)
            finished_at = datetime.now(UTC)

        async with self._state_lock:
            self._ensure_daily_total_current(today)
            self._state["daily_total"]["ml"] += ml
            self._state["last_dispense"] = {
                "at": finished_at.isoformat(),
                "seconds": seconds,
                "ml": ml,
                "source": source,
            }
            self._save_state()

        return {
            "ok": True,
            "seconds": seconds,
            "ml": ml,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "daily_total_ml": self._state["daily_total"]["ml"],
        }

    async def _dispense_ml(self, ml: float, source: str) -> dict:
        ml = float(ml)
        if ml <= 0:
            raise ValueError("`ml` must be positive")
        seconds = ml / self._ml_per_second
        return await self._dispense_seconds(seconds, source)

    async def _stop(self) -> dict:
        assert self._switch is not None
        await self._switch.set_position(0)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Background loop for schedules

    async def _bg_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("waterer tick failed: %s", e)
            await asyncio.sleep(self._poll_interval_sec)

    async def _tick(self) -> None:
        assert self._state_lock is not None
        now_local = datetime.now().astimezone()
        due = await self._pick_due_schedule(now_local)
        if due is None:
            return
        LOGGER.info(
            "firing schedule %s (%s) at %s",
            due.get("id"),
            due.get("name"),
            now_local.isoformat(),
        )
        try:
            await self._dispense_ml(due["dose_ml"], source=f"schedule:{due['id']}")
        except Exception as e:
            LOGGER.warning("scheduled dispense failed: %s", e)
            return
        async with self._state_lock:
            live = self._find_schedule(due["id"])
            if live is not None:
                live["last_fired_at"] = datetime.now(UTC).isoformat()
                self._save_state()

    async def _pick_due_schedule(self, now_local: datetime) -> dict | None:
        assert self._state_lock is not None
        async with self._state_lock:
            for schedule in list(self._state.get("schedules", [])):
                if not schedule.get("enabled", True):
                    continue
                if not self._is_due(schedule, now_local):
                    continue
                return dict(schedule)
        return None

    def _is_due(self, schedule: dict, now_local: datetime) -> bool:
        try:
            hi, mi = _parse_hhmm(schedule.get("time"))
        except ValueError:
            return False
        dows = schedule.get("days_of_week") or []
        if dows and now_local.weekday() not in dows:
            return False
        fire_today = now_local.replace(hour=hi, minute=mi, second=0, microsecond=0)
        if now_local < fire_today:
            return False
        age_min = (now_local - fire_today).total_seconds() / 60
        if age_min > CATCH_UP_WINDOW_MIN:
            return False
        last_iso = schedule.get("last_fired_at")
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso).astimezone(now_local.tzinfo)
                if last >= fire_today:
                    return False
            except ValueError:
                pass
        return True

    # ------------------------------------------------------------------
    # Schedule CRUD (via do_command)

    async def _add_schedule(self, payload: Any) -> dict:
        assert self._state_lock is not None
        if not isinstance(payload, dict):
            raise ValueError("schedule payload must be an object")
        schedule = _normalize_schedule({**payload, "id": _new_id()})
        async with self._state_lock:
            self._state["schedules"].append(schedule)
            self._save_state()
        return {"ok": True, "schedule": schedule}

    async def _update_schedule(self, payload: Any) -> dict:
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        async with self._state_lock:
            existing = self._find_schedule(str(payload["id"]))
            if existing is None:
                raise ValueError(f"no schedule with id={payload['id']!r}")
            merged = {**existing, **{k: v for k, v in payload.items() if v is not None}}
            normalized = _normalize_schedule(merged)
            normalized["id"] = existing["id"]
            if "last_fired_at" not in payload:
                normalized["last_fired_at"] = existing.get("last_fired_at")
            for i, s in enumerate(self._state["schedules"]):
                if s["id"] == existing["id"]:
                    self._state["schedules"][i] = normalized
                    break
            self._save_state()
        return {"ok": True, "schedule": normalized}

    async def _delete_schedule(self, payload: Any) -> dict:
        assert self._state_lock is not None
        sid = payload if isinstance(payload, str) else (payload or {}).get("id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("`id` is required")
        async with self._state_lock:
            before = len(self._state["schedules"])
            self._state["schedules"] = [s for s in self._state["schedules"] if s.get("id") != sid]
            if len(self._state["schedules"]) == before:
                raise ValueError(f"no schedule with id={sid!r}")
            self._save_state()
        return {"ok": True, "id": sid}

    async def _set_schedule_enabled(self, payload: Any) -> dict:
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        sid = str(payload["id"])
        enabled = bool(payload.get("enabled"))
        async with self._state_lock:
            live = self._find_schedule(sid)
            if live is None:
                raise ValueError(f"no schedule with id={sid!r}")
            live["enabled"] = enabled
            self._save_state()
        return {"ok": True, "id": sid, "enabled": enabled}

    async def _reorder_schedules(self, payload: Any) -> dict:
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
            raise ValueError("`ids` must be a list of schedule ids")
        wanted = [str(x) for x in payload["ids"]]
        async with self._state_lock:
            current = {s["id"]: s for s in self._state["schedules"]}
            if set(wanted) != set(current.keys()):
                raise ValueError("`ids` must include every existing schedule exactly once")
            self._state["schedules"] = [current[sid] for sid in wanted]
            self._save_state()
        return {"ok": True, "order": wanted}

    # ------------------------------------------------------------------
    # Status

    async def _status(self) -> dict:
        now_local = datetime.now().astimezone()
        assert self._state_lock is not None
        async with self._state_lock:
            self._ensure_daily_total_current(now_local.date())
            return {
                "kind": "waterer_pump",
                "switch_name": self._switch_name,
                "ml_per_second": self._ml_per_second,
                "max_runtime_seconds": self._max_runtime_seconds,
                "max_daily_ml": self._max_daily_ml,
                "daily_total": dict(self._state["daily_total"]),
                "last_dispense": (
                    dict(self._state["last_dispense"]) if self._state.get("last_dispense") else None
                ),
                "schedules": list(self._state.get("schedules", [])),
            }

    # ------------------------------------------------------------------
    # do_command

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        verb = command.get("command")
        if verb == "status":
            return await self._status()
        if verb == "dispense_seconds":
            return await self._dispense_seconds(command.get("seconds"), source="manual")
        if verb == "dispense_ml":
            return await self._dispense_ml(command.get("ml"), source="manual")
        if verb == "stop":
            return await self._stop()
        if verb == "add_schedule":
            return await self._add_schedule(command.get("schedule") or command)
        if verb == "update_schedule":
            return await self._update_schedule(command.get("schedule") or command)
        if verb == "delete_schedule":
            return await self._delete_schedule(command)
        if verb == "set_schedule_enabled":
            return await self._set_schedule_enabled(command)
        if verb == "reorder_schedules":
            return await self._reorder_schedules(command)
        raise ValueError(f"unknown command: {verb!r}")
