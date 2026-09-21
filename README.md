# waterer

A Viam module that turns a `switch` component into a scheduled, dose-aware pump controller with hard safety caps.

Wraps any `rdk:component:switch` (smart plug, GPIO relay, etc.) with:

- **Dose-in-ml semantics** via a per-installation `ml_per_second` calibration constant.
- **Max runtime per dispense** — a single dispense can't run longer than this, no matter what.
- **Max daily volume** — total ml across all dispenses in a local day. Resets at midnight local time.
- **Named schedules** — HH:MM + days-of-week, one-per-day dedup, ≤30-minute catch-up window after a reboot.

## Model

| Model | API | Purpose |
|---|---|---|
| `viam:waterer:pump` | `rdk:component:generic` | Wraps a Switch dep with schedule + safety logic |

## Pump

Configuration and commands for the `viam:waterer:pump` model below.

### Configuration

```json
{
  "name": "pump",
  "type": "generic",
  "model": "viam:waterer:pump",
  "depends_on": ["my_plug"],
  "attributes": {
    "switch_name": "my_plug",
    "ml_per_second": 20.0,
    "max_runtime_seconds": 60,
    "max_daily_ml": 5000,
    "schedules": [
      {
        "name": "Morning",
        "time": "07:00",
        "dose_ml": 250,
        "days_of_week": [],
        "enabled": true
      }
    ]
  }
}
```

- `switch_name` (required) — name of the Switch component to drive. Must also appear in `depends_on`.
- `ml_per_second` (default `20.0`) — output rate of your pump. Calibrate once with `dispense_seconds` and update this value.
- `max_runtime_seconds` (default `60`) — hard cap on a single dispense. Refuses requests over this.
- `max_daily_ml` (default `5000`) — hard cap on the local-day total. Refuses requests that would exceed it.
- `schedules` — optional seed list. Only used when state is empty; runtime edits via `do_command` become the source of truth.

Each schedule: `{name, time (HH:MM), dose_ml, days_of_week?, enabled?}`. Empty or omitted `days_of_week` = every day; otherwise a list of ints 0..6 (Mon..Sun).

State persists to `~/.viam/waterer-<name>-state.json`.

### Commands

All via `do_command`.

### status

```json
{ "command": "status" }
```

Returns the current config, today's total dispensed, last dispense info, and the schedule list.

### dispense_seconds

```json
{ "command": "dispense_seconds", "seconds": 10 }
```

Runs the pump for N seconds. Refused if `seconds > max_runtime_seconds`, or if the daily total would exceed `max_daily_ml` (using `ml_per_second` to convert).

Response includes `started_at`, `finished_at`, computed ml, and updated daily total.

### dispense_ml

```json
{ "command": "dispense_ml", "ml": 250 }
```

Same as `dispense_seconds` but takes ml directly and converts via `ml_per_second`.

### stop

```json
{ "command": "stop" }
```

Force the switch off. Emergency cut. Does not modify daily total or state.

### add_schedule / update_schedule / delete_schedule

```json
{ "command": "add_schedule", "schedule": {
  "name": "Evening", "time": "19:00", "dose_ml": 250,
  "days_of_week": [0,1,2,3,4], "enabled": true
} }
```

```json
{ "command": "update_schedule", "schedule": { "id": "a1b2c3d4", "dose_ml": 300 } }
```

```json
{ "command": "delete_schedule", "id": "a1b2c3d4" }
```

### set_schedule_enabled

```json
{ "command": "set_schedule_enabled", "id": "a1b2c3d4", "enabled": false }
```

### reorder_schedules

```json
{ "command": "reorder_schedules", "ids": ["a1b2c3d4", "e5f6g7h8"] }
```

`ids` must include every existing schedule exactly once.

### Calibration

`ml_per_second` is the one calibration knob and it depends on your specific pump + tubing + reservoir height. Steps:

1. Prime the pump (submerge in reservoir, run for ~10s to expel air).
2. Route the output tube into a graduated container.
3. Send `{"command": "dispense_seconds", "seconds": 10}`.
4. Measure the water and divide by 10 to get ml/second.
5. Repeat twice, average.
6. Update `ml_per_second` in the config and save.

### Safety notes

- Set `max_runtime_seconds` to roughly **1.5× your longest single dose** in seconds. Tighter is safer.
- Set `max_daily_ml` to roughly **1.2× your intended daily total**. Prevents a runaway loop from draining the reservoir.
- Test the whole system for a week while you're home before trusting it unattended. A stuck-on pump is worst-case a full reservoir on the floor.

## Development

```bash
make setup    # create venv, install deps
make lint     # ruff + black --check
make test     # pytest
make package  # build module.tar.gz locally
```

## Releases

Merges to `main` auto-tag the next patch version, tar the module, and upload to the Viam registry. Requires `viam_key_id` and `viam_key_value` GitHub secrets.
