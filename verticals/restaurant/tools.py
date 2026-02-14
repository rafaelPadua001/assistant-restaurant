import json
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

from .config_schema import MenuItem, RestaurantConfig


DAY_ORDER = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def load_config(config_path: str) -> RestaurantConfig:
    with open(config_path, "r", encoding="utf-8") as file:
        data = json.load(file)
    return RestaurantConfig(**data)


def _parse_time(value: str) -> Optional[time]:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        return None


def _parse_intervals(hours: str) -> List[Tuple[time, time]]:
    intervals: List[Tuple[time, time]] = []
    for raw_interval in hours.split(","):
        interval = raw_interval.strip()
        if not interval or "-" not in interval:
            continue
        start_str, end_str = [part.strip() for part in interval.split("-", 1)]
        start = _parse_time(start_str)
        end = _parse_time(end_str)
        if start is None or end is None:
            continue
        intervals.append((start, end))
    return intervals


def _is_open_for_hours(hours: str, now_time: time) -> bool:
    normalized = hours.strip().lower()
    if not normalized or normalized in {"closed", "fechado"}:
        return False
    for start, end in _parse_intervals(hours):
        if end <= start:
            if now_time >= start or now_time < end:
                return True
        elif start <= now_time < end:
            return True
    return False


def _is_open_from_prev_day(hours: str, now_time: time) -> bool:
    if not hours:
        return False
    for start, end in _parse_intervals(hours):
        if end <= start and now_time < end:
            return True
    return False


def _previous_day(day_key: str) -> Optional[str]:
    if day_key not in DAY_ORDER:
        return None
    index = DAY_ORDER.index(day_key)
    return DAY_ORDER[index - 1]


def is_open(config: RestaurantConfig, now: datetime) -> bool:
    day_key = now.strftime("%A").lower()
    now_time = now.time()
    hours_today = config.opening_hours.get(day_key, "")
    if _is_open_for_hours(hours_today, now_time):
        return True
    prev_day = _previous_day(day_key)
    if not prev_day:
        return False
    hours_prev = config.opening_hours.get(prev_day, "")
    return _is_open_from_prev_day(hours_prev, now_time)


def find_menu_item(config: RestaurantConfig, item_id: str) -> Optional[MenuItem]:
    for items in config.menu.values():
        for item in items:
            if item.id == item_id:
                return item
    return None


def calculate_total(cart: List[Dict[str, Any]], config: RestaurantConfig) -> float:
    total = 0.0
    for entry in cart:
        item_id = entry.get("id")
        if not item_id:
            continue
        qty = entry.get("quantity", entry.get("qty", 1))
        try:
            quantity = int(qty)
        except (TypeError, ValueError):
            quantity = 1
        quantity = max(quantity, 1)
        item = find_menu_item(config, item_id)
        if item is None:
            continue
        total += item.price * quantity
    return total + float(config.delivery_fee)


def build_whatsapp_message(
    config: RestaurantConfig,
    cart: List[Dict[str, Any]],
    customer_info: Dict[str, Any],
) -> str:
    lines: List[str] = [f"Pedido - {config.name}"]

    name = customer_info.get("name")
    if name:
        lines.append(f"Cliente: {name}")
    phone = customer_info.get("phone")
    if phone:
        lines.append(f"Telefone: {phone}")
    address = customer_info.get("address")
    if address:
        lines.append(f"Endereco: {address}")
    payment = customer_info.get("payment_method")
    if payment:
        lines.append(f"Pagamento: {payment}")

    lines.append("Itens:")
    for entry in cart:
        item_id = entry.get("id")
        if not item_id:
            continue
        item = find_menu_item(config, item_id)
        if item is None:
            continue
        qty = entry.get("quantity", entry.get("qty", 1))
        try:
            quantity = int(qty)
        except (TypeError, ValueError):
            quantity = 1
        quantity = max(quantity, 1)
        subtotal = item.price * quantity
        lines.append(
            f"{quantity}x {item.name} (R$ {item.price:.2f}) = R$ {subtotal:.2f}"
        )

    lines.append(f"Taxa de entrega: R$ {float(config.delivery_fee):.2f}")
    total = calculate_total(cart, config)
    lines.append(f"Total: R$ {total:.2f}")

    notes = customer_info.get("notes")
    if notes:
        lines.append(f"Observacoes: {notes}")

    return "\n".join(lines)
