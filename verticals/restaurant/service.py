from __future__ import annotations

import json
import logging
import os
import uuid
from dotenv import load_dotenv
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as url_error
from urllib import request as url_request

from .config_schema import MenuItem, RestaurantConfig
from .tools import calculate_total, find_menu_item, load_config
from services.menu_api_client import get_menu

load_dotenv()

MENU_KEYWORDS = {"menu", "cardapio"}
PROMO_KEYWORDS = {"promo", "promocao", "promocoes"}
FINISH_KEYWORDS = {"finalizar", "fechar", "encerrar", "checkout"}
CONFIRM_KEYWORDS = {"sim", "confirmar", "confirmo", "ok", "pode"}
EDIT_KEYWORDS = {"editar", "mudar", "alterar", "nao", "cancelar", "voltar"}
REMOVE_KEYWORDS = {"remover", "tirar", "excluir", "deletar"}

NUMBER_WORDS = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "onze": 11,
    "doze": 12,
}

GENERIC_ITEM_TOKENS = {
    "pizza",
    "pizzas",
    "bebida",
    "bebidas",
    "lanche",
    "lanches",
    "combo",
    "combos",
    "sabor",
    "sabores",
    "tamanho",
    "media",
    "grande",
}
ORDER_CREATE_URL_DEFAULT = "https://pizzaria-demo.onrender.com/orders/public"
CHECKOUT_URL_DEFAULT = "https://pizzaria-demo.onrender.com/api/orders/checkout"

#ORDER_CREATE_URL_DEFAULT = "http://localhost:8000/orders/public"
#CHECKOUT_URL_DEFAULT = "http://localhost:8000/api/orders/checkout"

logger = logging.getLogger(__name__)


def get_restaurant_id() -> Optional[str]:
    return os.getenv("RESTAURANT_ID")


class ConversationStep(str, Enum):
    ORDERING = "ordering"
    CONFIRMATION = "confirmation"
    AWAITING_NAME = "awaiting_name"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_PHONE = 'awaiting_phone'
    AWAITING_PAYMENT = "awaiting_payment"
    ORDER_COMPLETED = "order_completed"

class IntentType(str, Enum):
    SHOW_MENU = "show_menu"
    SHOW_PROMOS = "show_promos"
    FINISH = "finish"
    CONFIRM = "confirm"
    EDIT = "edit"
    ADD_ITEM = "add_item"
    REMOVE_ITEM = "remove_item"
    NEW_ORDER = "new_order"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Intent:
    type: IntentType
    item: Optional[MenuItem] = None
    quantity: int = 1


@dataclass(frozen=True)
class IndexedItem:
    item: MenuItem
    category: str
    id_norm: str
    name_norm: str
    tokens: Tuple[str, ...]


def _strip_accents(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def _normalize_text(text: str) -> str:
    text = _strip_accents(text.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _tokenize(text: str) -> List[str]:
    normalized = _normalize_text(text)
    return normalized.split() if normalized else []


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _token_matches_item(token: str, item_token: str) -> bool:
    token = _singularize(token)
    item_token = _singularize(item_token)
    if token == item_token:
        return True
    if len(token) > 3 and item_token.startswith(token):
        return True
    if len(item_token) > 3 and token.startswith(item_token):
        return True
    return False


def _format_price(value: Any) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "R$ --"
    formatted = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def _menu_text_from_menu(menu: Dict[str, List[MenuItem]]) -> str:
    if not menu:
        return "Nenhum produto cadastrado no momento."

    lines: List[str] = []
    for category in sorted(menu.keys(), key=lambda name: name.lower()):
        items = menu.get(category) or []
        if not items:
            continue
        items_sorted = sorted(items, key=lambda item: item.name.lower())
        lines.append(f"{category.upper()}:")
        for item in items_sorted:
            name = item.name.strip()
            if not name:
                continue
            description = (item.description or "").strip()
            price_text = _format_price(item.price)
            line = f"• {name} — {price_text}"
            if description:
                line += f" — {description}"
            lines.append(line)
        lines.append("")

    if not lines:
        return "Nenhum produto cadastrado no momento."

    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _build_menu_from_api(menu_data: Optional[dict] = None) -> Dict[str, List[MenuItem]]:
    menu = menu_data if menu_data is not None else get_menu()
    print("[assistant] resposta bruta da API:", menu)
    if not menu:
        logger.warning("[assistant] menu da API vazio")
        return {"categories": []}

    def _coerce_price(raw_value: Any) -> Optional[float]:
        if raw_value is None:
            return None
        try:
            raw_text = str(raw_value).strip()
            if not raw_text:
                return None
            raw_text = raw_text.replace(",", ".")
            return float(raw_text)
        except (TypeError, ValueError):
            return None

    def _build_item(product: Dict[str, Any]) -> Optional[MenuItem]:
        if not isinstance(product, dict):
            return None
        item_name = str(product.get("name") or "").strip()
        if not item_name:
            return None
        price = _coerce_price(product.get("price"))
        if price is None:
            return None
        raw_id = product.get("id")
        item_id = str(raw_id) if raw_id is not None else item_name
        description = str(product.get("description") or "").strip()
        return MenuItem(
            id=item_id,
            name=item_name,
            price=price,
            description=description,
        )

    api_menu: Dict[str, List[MenuItem]] = {}
    total_items = 0

    if isinstance(menu, dict) and isinstance(menu.get("categories"), list):
        categories_list = [category for category in menu.get("categories") if isinstance(category, dict)]
        for category in categories_list:
            name = str(category.get("name") or category.get("category") or "").strip()
            if not name:
                continue
            products = category.get("products") or category.get("items") or []
            if not isinstance(products, list):
                continue
            items: List[MenuItem] = []
            for product in products:
                item = _build_item(product)
                if item is None:
                    continue
                items.append(item)
            if items:
                items.sort(key=lambda item: item.name.lower())
                api_menu[name] = items
                total_items += len(items)
    elif isinstance(menu, dict) and isinstance(menu.get("menu"), list):
        entries = [entry for entry in menu.get("menu") if isinstance(entry, dict)]
        for entry in entries:
            name = str(entry.get("category") or entry.get("name") or "").strip()
            if not name:
                continue
            products = entry.get("items") or entry.get("products") or []
            if not isinstance(products, list):
                continue
            items: List[MenuItem] = []
            for product in products:
                item = _build_item(product)
                if item is None:
                    continue
                items.append(item)
            if items:
                items.sort(key=lambda item: item.name.lower())
                api_menu[name] = items
                total_items += len(items)
    elif isinstance(menu, dict) and isinstance(menu.get("items"), list):
        products = menu.get("items") or []
        category_map: Dict[str, List[MenuItem]] = {}
        for product in products:
            if not isinstance(product, dict):
                continue
            category_name = str(product.get("category") or "Itens").strip()
            item = _build_item(product)
            if item is None:
                continue
            category_map.setdefault(category_name, []).append(item)
        for category_name, items in category_map.items():
            if not items:
                continue
            items.sort(key=lambda item: item.name.lower())
            api_menu[category_name] = items
            total_items += len(items)
    elif isinstance(menu, list):
        category_map: Dict[str, List[MenuItem]] = {}
        for product in menu:
            if not isinstance(product, dict):
                continue
            category_name = str(product.get("category") or "Itens").strip()
            item = _build_item(product)
            if item is None:
                continue
            category_map.setdefault(category_name, []).append(item)
        for category_name, items in category_map.items():
            if not items:
                continue
            items.sort(key=lambda item: item.name.lower())
            api_menu[category_name] = items
            total_items += len(items)

    print("[assistant] produtos processados:", total_items)
    logger.info("[assistant] categorias processadas: %s", len(api_menu))
    if not api_menu:
        logger.warning("[assistant] menu da API vazio")
        return {"categories": []}
    return api_menu


def _build_item_index(config: RestaurantConfig) -> List[IndexedItem]:
    items: List[IndexedItem] = []
    for category, entries in config.menu.items():
        for item in entries:
            id_norm = _normalize_text(item.id)
            name_norm = _normalize_text(item.name)
            tokens = tuple(
                token
                for token in _tokenize(item.name)
                if token not in GENERIC_ITEM_TOKENS
            )
            items.append(
                IndexedItem(
                    item=item,
                    category=category,
                    id_norm=id_norm,
                    name_norm=name_norm,
                    tokens=tokens,
                )
            )
    return items


def _build_item_index_from_menu(menu: Dict[str, List[MenuItem]]) -> List[IndexedItem]:
    items: List[IndexedItem] = []
    for category, entries in menu.items():
        for item in entries:
            id_norm = _normalize_text(item.id)
            name_norm = _normalize_text(item.name)
            tokens = tuple(
                token
                for token in _tokenize(item.name)
                if token not in GENERIC_ITEM_TOKENS
            )
            items.append(
                IndexedItem(
                    item=item,
                    category=category,
                    id_norm=id_norm,
                    name_norm=name_norm,
                    tokens=tokens,
                )
            )
    return items


def _build_item_index_from_api(menu_data: Optional[dict] = None) -> List[IndexedItem]:
    menu = _build_menu_from_api(menu_data)
    if not menu:
        return []
    return _build_item_index_from_menu(menu)


def _menu_text(config: RestaurantConfig) -> str:
    return _menu_text_from_menu(config.menu)


def _menu_is_empty(menu: Dict[str, List[MenuItem]]) -> bool:
    if not menu:
        return True
    return all(not items for items in menu.values())


def _match_item(text: str, indexed_items: List[IndexedItem]) -> Optional[IndexedItem]:
    normalized_text = _normalize_text(text)
    tokens = _tokenize(text)

    for indexed in indexed_items:
        if indexed.id_norm and indexed.id_norm in normalized_text:
            return indexed

    for indexed in indexed_items:
        if indexed.name_norm and indexed.name_norm in normalized_text:
            return indexed

    best_score = 0
    best_item: Optional[IndexedItem] = None
    for indexed in indexed_items:
        if not indexed.tokens:
            continue
        matches = 0
        for token in tokens:
            if any(_token_matches_item(token, item_token) for item_token in indexed.tokens):
                matches += 1
        if matches >= max(1, min(2, len(indexed.tokens))) and matches > best_score:
            best_score = matches
            best_item = indexed

    return best_item


def _quantity_from_tokens(tokens: List[str]) -> Optional[int]:
    for token in tokens:
        if token.isdigit():
            return max(int(token), 1)
        token = _singularize(token)
        if token in NUMBER_WORDS:
            return NUMBER_WORDS[token]
    return None


def _extract_quantity(text: str, indexed: Optional[IndexedItem]) -> int:
    tokens = _tokenize(text)
    if not tokens:
        return 1

    if indexed:
        item_tokens = list(indexed.tokens) + [indexed.id_norm]
        for i, token in enumerate(tokens):
            if any(_token_matches_item(token, item_token) for item_token in item_tokens if item_token):
                window_start = max(0, i - 2)
                qty = _quantity_from_tokens(tokens[window_start:i])
                if qty:
                    return qty

    match = re.search(r"\b(\d+)\s*x\b", _normalize_text(text))
    if match:
        return max(int(match.group(1)), 1)

    qty = _quantity_from_tokens(tokens)
    return qty or 1


def _looks_like_phone(value: str) -> bool:
    digits = re.sub(r"\D+", "", value)
    return 10 <= len(digits) <= 13


def _normalize_phone(value: str) -> Optional[str]:
    digits = re.sub(r"\D+", "", value)
    if 10 <= len(digits) <= 13:
        return digits
    return None


def _extract_name(text: str) -> Optional[str]:
    match = re.search(r"\b(meu nome e|me chamo|sou)\s+(.+)", _normalize_text(text))
    if match:
        return match.group(2).strip().title()
    return None


def _extract_address(text: str) -> Optional[str]:
    match = re.search(r"\b(endereco|endereço)\s*(e|é|:)?\s+(.+)", text, re.IGNORECASE)
    if match:
        return match.group(3).strip()
    return None


def _is_valid_name(name: str) -> bool:
    cleaned = name.strip()
    if len(cleaned) < 2:
        return False
    return any(char.isalpha() for char in cleaned)


def _is_valid_address(address: str) -> bool:
    return len(address.strip()) >= 6


def _pretty_hours(hours: str) -> str:
    intervals = [segment.strip() for segment in hours.split(",") if segment.strip()]
    pretty: List[str] = []
    for interval in intervals:
        if "-" in interval:
            start, end = [part.strip() for part in interval.split("-", 1)]
            pretty.append(f"{start} as {end}")
        else:
            pretty.append(interval)
    return " e ".join(pretty)


def _closed_message(config: RestaurantConfig, now: datetime) -> str:
    day_key = now.strftime("%A").lower()
    hours = config.opening_hours.get(day_key)
    if hours:
        return f"Estamos fechados agora. Nosso horario de hoje e {_pretty_hours(hours)}."
    return "Estamos fechados agora."


def _response_with_notice(text: str, notice: Optional[str]) -> str:
    if not notice:
        return text
    return f"{notice}\n\n{text}"


def _create_order(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    api_key = os.getenv("INTERNAL_API_KEY", "")
    if not api_key:
        return None, "INTERNAL_API_KEY nao configurada."

    url = os.getenv("ORDER_CREATE_URL", ORDER_CREATE_URL_DEFAULT)
    body = json.dumps(payload).encode("utf-8")
    request = url_request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-API-KEY", api_key)

    response_body = ""
    status = None
    max_attempts = 3
    timeout_seconds = 30

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("Payload enviado para criacao de pedido (tentativa %s): %s", attempt, payload)
            with url_request.urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                status = response.status
            break
        except url_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8") if exc.fp else ""
            logger.error(
                "Erro HTTP ao criar pedido (status=%s): %s", exc.code, response_body
            )
            return None, "Nao consegui registrar seu pedido agora. Tente novamente."
        except url_error.URLError as exc:
            reason_text = str(getattr(exc, "reason", exc))
            logger.error("Falha de conexao ao criar pedido: %s", reason_text)
            if attempt < max_attempts:
                continue
            return None, "Nao consegui conectar ao servidor. Tente novamente em alguns instantes."
        except TimeoutError:
            logger.error("Timeout ao criar pedido (tentativa %s).", attempt)
            if attempt < max_attempts:
                continue
            return None, "A requisicao demorou demais. Tente novamente em alguns instantes."
        except Exception:
            logger.exception("Erro inesperado ao criar o pedido.")
            return None, "Nao consegui registrar seu pedido agora. Tente novamente."

    logger.info("Resposta criar pedido (status=%s): %s", status, response_body)

    if status not in {200, 201}:
        logger.error("Status inesperado ao criar pedido: %s", status)
        return None, "Nao consegui registrar seu pedido agora. Tente novamente."

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        logger.error("Resposta invalida do servico de pedidos: %s", response_body)
        return None, "Nao consegui registrar seu pedido agora. Tente novamente."

    print("BACKEND RESPONSE:", data)

    if data.get("open") is False:
        message = data.get("message") or "Estamos fechados agora."
        logger.info("Backend informou fechado: %s", message)
        return None, message

    order_id = data.get("order_id") or data.get("id")
    if not order_id:
        logger.error("Servico de pedidos nao retornou order_id: %s", data)
        return None, "Nao consegui registrar seu pedido agora. Tente novamente."

    return int(order_id), None


def _create_checkout(order_id: int) -> Tuple[Optional[str], Optional[str]]:
    api_key = os.getenv("INTERNAL_API_KEY", "")
    if not api_key:
        return None, "INTERNAL_API_KEY nao configurada."

    base_url = os.getenv("CHECKOUT_URL", CHECKOUT_URL_DEFAULT).rstrip("/")
    url = f"{base_url}/{order_id}"
    request = url_request.Request(url, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-API-KEY", api_key)

    try:
        logger.info("Solicitando checkout para order_id=%s", order_id)
        with url_request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8")
            status = response.status
    except url_error.HTTPError as exc:
        response_body = exc.read().decode("utf-8") if exc.fp else ""
        logger.error(
            "Erro HTTP ao criar checkout (status=%s): %s", exc.code, response_body
        )
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."
    except url_error.URLError as exc:
        logger.error("Falha de conexao ao criar checkout: %s", exc.reason)
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."
    except Exception:
        logger.exception("Erro inesperado ao criar o checkout.")
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."

    logger.info("Resposta checkout (status=%s): %s", status, response_body)

    if status not in {200, 201}:
        logger.error("Status inesperado ao criar checkout: %s", status)
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        logger.error("Resposta invalida do servico de checkout: %s", response_body)
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."

    print("BACKEND RESPONSE:", data)

    if data.get("open") is False:
        message = data.get("message") or "Estamos fechados agora."
        logger.info("Backend informou fechado no checkout: %s", message)
        return None, message

    checkout_url = data.get("checkout_url")
    if not checkout_url:
        logger.error("Servico de checkout nao retornou link: %s", data)
        return None, "Nao consegui gerar o link de pagamento agora. Tente novamente."

    return checkout_url, None


def parse_intent(message: str, indexed_items: List[IndexedItem]) -> Intent:
    text = _normalize_text(message)
    tokens = set(text.split())

    if "novo pedido" in text:
        return Intent(IntentType.NEW_ORDER)

    if tokens & MENU_KEYWORDS or any(keyword in text for keyword in MENU_KEYWORDS):
        return Intent(IntentType.SHOW_MENU)
    if tokens & PROMO_KEYWORDS or any(keyword in text for keyword in PROMO_KEYWORDS):
        return Intent(IntentType.SHOW_PROMOS)
    if tokens & FINISH_KEYWORDS or any(keyword in text for keyword in FINISH_KEYWORDS):
        return Intent(IntentType.FINISH)
    if tokens & CONFIRM_KEYWORDS or any(keyword in text for keyword in CONFIRM_KEYWORDS):
        return Intent(IntentType.CONFIRM)
    if tokens & EDIT_KEYWORDS or any(keyword in text for keyword in EDIT_KEYWORDS):
        return Intent(IntentType.EDIT)

    indexed = _match_item(message, indexed_items)
    if indexed:
        quantity = _extract_quantity(message, indexed)
        if tokens & REMOVE_KEYWORDS or any(keyword in text for keyword in REMOVE_KEYWORDS):
            return Intent(IntentType.REMOVE_ITEM, indexed.item, quantity)
        return Intent(IntentType.ADD_ITEM, indexed.item, quantity)

    return Intent(IntentType.UNKNOWN)


class CartManager:
    def __init__(self, config: RestaurantConfig, cart_state: List[Dict[str, Any]]) -> None:
        self.config = config
        self.cart_state = cart_state

    def add(self, item_id: str, quantity: int) -> None:
        quantity = max(int(quantity), 1)
        for entry in self.cart_state:
            if entry.get("id") == item_id:
                entry["quantity"] = int(entry.get("quantity", 1)) + quantity
                return
        self.cart_state.append({"id": item_id, "quantity": quantity})

    def remove(self, item_id: str, quantity: int) -> bool:
        quantity = max(int(quantity), 1)
        for entry in list(self.cart_state):
            if entry.get("id") == item_id:
                current = int(entry.get("quantity", 1))
                if quantity >= current:
                    self.cart_state.remove(entry)
                else:
                    entry["quantity"] = current - quantity
                return True
        return False

    def has_items(self) -> bool:
        for entry in self.cart_state:
            item_id = entry.get("id")
            if not item_id:
                continue
            if find_menu_item(self.config, item_id) is not None:
                return True
        return False

    def items(self) -> List[Tuple[MenuItem, int]]:
        result: List[Tuple[MenuItem, int]] = []
        for entry in self.cart_state:
            item_id = entry.get("id")
            if not item_id:
                continue
            item = find_menu_item(self.config, item_id)
            if item is None:
                continue
            quantity = max(int(entry.get("quantity", 1)), 1)
            result.append((item, quantity))
        return result

    def total(self) -> float:
        return calculate_total(self.cart_state, self.config)

    def summary_text(self) -> str:
        lines: List[str] = ["Resumo do pedido:"]
        for item, quantity in self.items():
            subtotal = item.price * quantity
            lines.append(f"• {quantity}x {item.name} — R$ {subtotal:.2f}")
        lines.append(f"Taxa de entrega: R$ {float(self.config.delivery_fee):.2f}")
        lines.append(f"Total: R$ {self.total():.2f}")
        return "\n".join(lines)

    def has_beverage(self, item_index: List[IndexedItem]) -> bool:
        for entry in self.cart_state:
            item_id = entry.get("id")
            if not item_id:
                continue
            for indexed in item_index:
                if indexed.item.id == item_id and _is_beverage_category(indexed.category):
                    return True
        return False


def _is_beverage_category(category: str) -> bool:
    label = category.lower()
    return "bebida" in label or "drink" in label or "refri" in label or "refrigerante" in label


def _find_beverage_item(item_index: List[IndexedItem]) -> Optional[MenuItem]:
    for indexed in item_index:
        if _is_beverage_category(indexed.category):
            return indexed.item
    return None


def _coerce_step(state: Dict[str, Any]) -> ConversationStep:
    step_value = state.get("step")
    if step_value:
        try:
            return ConversationStep(step_value)
        except ValueError:
            return ConversationStep.ORDERING

    if state.get("awaiting_confirmation"):
        return ConversationStep.CONFIRMATION
    awaiting_info = state.get("awaiting_info")
    if awaiting_info == "name":
        return ConversationStep.AWAITING_NAME
    if awaiting_info == "address":
        return ConversationStep.AWAITING_ADDRESS
    if awaiting_info == "payment":
        return ConversationStep.AWAITING_PAYMENT
    return ConversationStep.ORDERING


class ConversationManager:
    def __init__(self, config: RestaurantConfig, state: Dict[str, Any], restaurant_slug: str) -> None:
        self.config = config
        self.state = state
        self.restaurant_slug = restaurant_slug
        self.cart = CartManager(config, state.setdefault("cart", []))
        self.customer_info: Dict[str, Any] = state.setdefault("customer_info", {})
        menu_data = get_menu()
        api_menu = _build_menu_from_api(menu_data)
        if api_menu.get("categories") == [] and len(api_menu) == 1:
            self.config.menu = {}
            self.item_index = []
        else:
            self.config.menu = api_menu
            self.item_index = _build_item_index_from_menu(self.config.menu) if self.config.menu else []
        print("[assistant] item index carregado da API")

        self.step = _coerce_step(state)
        self.state["step"] = self.step.value

        # Limpa flags antigas para manter o estado consistente
        self.state.pop("awaiting_confirmation", None)
        self.state.pop("awaiting_info", None)
        self.state.pop("confirmed", None)

        self.closed_notice = None

    def reload_menu_index(self) -> None:
        menu_data = get_menu()
        api_menu = _build_menu_from_api(menu_data)
        if api_menu.get("categories") == [] and len(api_menu) == 1:
            self.config.menu = {}
            self.item_index = []
        else:
            self.config.menu = api_menu
            self.item_index = _build_item_index_from_menu(self.config.menu) if self.config.menu else []
        print("[assistant] item index carregado da API")

    def handle_message(self, message: str) -> Dict[str, Any]:
        intent = parse_intent(message, self.item_index)

        if intent.type == IntentType.NEW_ORDER:
            return self._start_new_order()

        normalized = _normalize_text(message)

        if self.step == ConversationStep.AWAITING_PAYMENT:
            return self._build_response(
                "Seu pedido esta aguardando confirmacao de pagamento.\n"
                "Assim que for aprovado, avisaremos aqui.\n\n"
                "Se quiser cancelar e comecar novamente, digite 'novo pedido'."
            )

        if self.step == ConversationStep.ORDER_COMPLETED:
            if "novo pedido" in normalized:
                self.state.clear()
                self.state["session_id"] = str(uuid.uuid4())
                self.state["step"] = ConversationStep.ORDERING.value
                return self._build_response(
                    "Perfeito! Vamos comecar um novo pedido.\n"
                    "Digite 'menu' para ver as opcoes."
                )

            if "status" in normalized:
                order_id = self.state.get("order_id")
                return self._build_response(
                    f"O pedido #{order_id} ja esta confirmado e pago ?"
                )

            return self._build_response(
                f"?? Pedido #{self.state.get('order_id')} confirmado!\n"
                "Obrigado pela preferencia ??\n\n"
                "Para fazer um novo pedido, digite 'novo pedido'."
            )

        if self.step in {ConversationStep.AWAITING_NAME, ConversationStep.AWAITING_ADDRESS, ConversationStep.AWAITING_PHONE}:
            return self._handle_customer_info(message, intent)

        if self.step == ConversationStep.CONFIRMATION:
            return self._handle_confirmation(intent, message)

        if self.step == ConversationStep.ORDERING and intent.type == IntentType.FINISH:
            return self._finalize_order()

        return self._handle_general(intent, message)

    def _handle_customer_info(self, message: str, intent: Intent) -> Dict[str, Any]:
        # Durante a coleta de dados, nao processa carrinho nem upsell.

        # -------------------------
        # NOME
        # -------------------------
        if self.step == ConversationStep.AWAITING_NAME:
            name = _extract_name(message) or message.strip()
            if not _is_valid_name(name):
                return self._build_response("Nao consegui entender seu nome. Pode repetir?")

            self.customer_info["name"] = name
            self.step = ConversationStep.AWAITING_ADDRESS
            self.state["step"] = self.step.value
            return self._build_response("Obrigado! Qual o endereco para entrega?")

        # -------------------------
        # ENDEREÇO
        # -------------------------
        if self.step == ConversationStep.AWAITING_ADDRESS:
            address = _extract_address(message) or message.strip()
            if not _is_valid_address(address):
                return self._build_response("Endereco invalido. Pode enviar novamente?")

            self.customer_info["address"] = address
            self.step = ConversationStep.AWAITING_PHONE
            self.state["step"] = self.step.value
            return self._build_response(
                "Perfeito! Qual o seu WhatsApp para enviarmos atualizacoes do pedido?"
            )

        # -------------------------
        # TELEFONE
        # -------------------------
        if self.step == ConversationStep.AWAITING_PHONE:
            phone = _normalize_phone(message)
            if not phone:
                return self._build_response("Telefone invalido. Pode enviar novamente?")

            self.customer_info["phone"] = phone
            return self._finalize_order()

        # Fallback de segurança
        return self._build_response("Posso te ajudar com algo mais?")

    def _start_new_order(self) -> Dict[str, Any]:
        self.state.clear()
        self.state["session_id"] = str(uuid.uuid4())
        self.state["cart"] = []
        self.state["customer_info"] = {}
        self.step = ConversationStep.ORDERING
        self.state["step"] = self.step.value
        self.cart = CartManager(self.config, self.state["cart"])
        self.customer_info = self.state["customer_info"]
        return self._build_response(
            "Perfeito! Vamos comecar um novo pedido.\n"
            "Digite 'menu' para ver as opcoes."
        )

    def _handle_confirmation(self, intent: Intent, message: str) -> Dict[str, Any]:
        if _menu_is_empty(self.config.menu):
            self.step = ConversationStep.ORDERING
            self.state["step"] = self.step.value
            return self._build_response("Nenhum produto cadastrado no momento.")

        if intent.type == IntentType.CONFIRM:
            return self._finalize_order()
        if intent.type == IntentType.EDIT:
            self.step = ConversationStep.ORDERING
            self.state["step"] = self.step.value
            return self._build_response(
                "Sem problemas. Diga o item que deseja adicionar ou remover."
            )
        if intent.type in {IntentType.ADD_ITEM, IntentType.REMOVE_ITEM}:
            response = self._handle_general(intent, message)
            summary = self.cart.summary_text()
            summary += "\n\nVoce confirma? (sim/nao)"
            response["text"] = f"{response['text']}\n\n{summary}"
            return response

        return self._build_response(
            "Para finalizar, responda 'sim'. Para mudar o pedido, diga 'editar'."
        )

    def _handle_general(self, intent: Intent, message: str) -> Dict[str, Any]:
        if _menu_is_empty(self.config.menu):
            if intent.type in {
                IntentType.SHOW_MENU,
                IntentType.ADD_ITEM,
                IntentType.REMOVE_ITEM,
                IntentType.FINISH,
                IntentType.SHOW_PROMOS,
                IntentType.CONFIRM,
            }:
                return self._build_response("Nenhum produto cadastrado no momento.")

        if intent.type == IntentType.SHOW_MENU:
            return self._build_response(_menu_text(self.config))

        if intent.type == IntentType.SHOW_PROMOS:
            if self.config.promotions:
                promo_lines = ["Promocoes de hoje:"]
                for promo in self.config.promotions:
                    promo_lines.append(f"• {promo.message}")
                return self._build_response("\n".join(promo_lines))
            return self._build_response("No momento nao temos promocoes ativas.")

        if intent.type == IntentType.ADD_ITEM and intent.item:
            self.cart.add(intent.item.id, intent.quantity)
            total = self.cart.total()
            response_lines = [
                f"Adicionado: {intent.quantity}x {intent.item.name}.",
                f"Total parcial (com entrega): R$ {total:.2f}.",
            ]

            for promo in self.config.promotions:
                if promo.trigger == intent.item.id:
                    response_lines.append(promo.message)

            if not self.cart.has_beverage(self.item_index):
                beverage = _find_beverage_item(self.item_index)
                if beverage:
                    response_lines.append(
                        f"Quer adicionar {beverage.name} por R$ {beverage.price:.2f} para completar seu pedido?"
                    )

            response_lines.append("Se quiser finalizar, diga 'finalizar'.")
            return self._build_response(" ".join(response_lines))

        if intent.type == IntentType.REMOVE_ITEM and intent.item:
            removed = self.cart.remove(intent.item.id, intent.quantity)
            if not removed:
                return self._build_response("Nao encontrei esse item no seu carrinho.")
            if not self.cart.has_items():
                return self._build_response(
                    "Removi o item. Seu carrinho esta vazio."
                )
            total = self.cart.total()
            return self._build_response(
                f"Removi {intent.quantity}x {intent.item.name}. Total atual: R$ {total:.2f}."
            )

        if intent.type == IntentType.FINISH:
            if not self.cart.has_items():
                return self._build_response(
                    "Seu carrinho esta vazio. Escolha um item do cardapio para continuar."
                )
            self.step = ConversationStep.CONFIRMATION
            self.state["step"] = self.step.value
            summary = self.cart.summary_text()
            summary += "\n\nVoce confirma? (sim/nao)"
            return self._build_response(summary)

        if intent.type == IntentType.CONFIRM:
            return self._build_response(
                "Se deseja finalizar, diga 'finalizar' e eu resumo o pedido."
            )

        return self._build_response(
            "Consigo te ajudar com o cardapio, adicionar itens ou finalizar o pedido. "
            "Diga 'menu' para ver os itens."
        )

    def _missing_info_prompt(self) -> str:
        if not self.customer_info.get("name"):
            return "Antes de finalizar, preciso do seu nome."
        if not self.customer_info.get("address"):
            return "Antes de finalizar, preciso do seu endereco."
        return ""

    def _finalize_order(self) -> Dict[str, Any]:
        if not self.cart.has_items():
            self.step = ConversationStep.ORDERING
            self.state["step"] = self.step.value
            return self._build_response(
                "Seu carrinho esta vazio. Escolha um item do cardapio."
            )

        if not self.customer_info.get("name"):
            self.step = ConversationStep.AWAITING_NAME
            self.state["step"] = self.step.value
            return self._build_response("Qual seu nome?")

        if not self.customer_info.get("address"):
            self.step = ConversationStep.AWAITING_ADDRESS
            self.state["step"] = self.step.value
            return self._build_response("Qual o endereco para entrega?")

        normalized_phone = _normalize_phone(self.customer_info.get("phone", ""))
        if not normalized_phone:
            self.step = ConversationStep.AWAITING_PHONE
            self.state["step"] = self.step.value
            return self._build_response('Qual o seu WhatsApp para enviarmos atualizações do pedido ?')
        self.customer_info["phone"] = normalized_phone

        restaurant_id = self.state.get("restaurant_id") or get_restaurant_id()
        if not restaurant_id:
            raise Exception("Restaurant ID not configured")

        print("DEBUG RESTAURANT ID:", restaurant_id)
        delivery_fee = 5.00 # Valor Fixo temporário
        items_payload = []
        for item, quantity in self.cart.items():
            try:
                safe_qty = int(quantity)
            except (TypeError, ValueError):
                continue
            if safe_qty < 1:
                continue
            try:
                unit_price = float(item.price)
            except (TypeError, ValueError):
                continue
            if unit_price <= 0:
                continue
            items_payload.append(
                {
                    "product_id": item.id if item.id else None,
                    "product_name": item.name,
                    "quantity": safe_qty,
                    "unit_price": unit_price,
                }
            )

        if not items_payload:
            self.step = ConversationStep.ORDERING
            self.state["step"] = self.step.value
            return self._build_response("Seu carrinho esta vazio. Escolha um item do cardapio.")

        items_payload.append(
            {
                "product_id": None,
                "product_name": "Taxa de entrega",
                "quantity": 1,
                "unit_price": delivery_fee
            }
        )
        payload = {
            "customer_name": self.customer_info.get("name"),
            "customer_phone": normalized_phone,
            "session_id": self.state.get("session_id"),
            "restaurant_id": int(restaurant_id),
            "delivery_fee": float(self.config.delivery_fee),
            "items": items_payload,
        }

        order_id = self.state.get("order_id")
        if not order_id:
            order_id, error_message = _create_order(payload)
            if error_message:
                return self._build_response(error_message)
            self.state["order_id"] = order_id

        checkout_url, error_message = _create_checkout(int(order_id))
        if error_message:
            return self._build_response(error_message)

        self.step = ConversationStep.AWAITING_PAYMENT
        self.state["step"] = self.step.value

       # text = "Perfeito! Aqui esta seu link para pagamento:\n" + checkout_url
        text = "Perfeito! Escolha a forma de pagamento:"
        buttons = [
            {"title": "Crédito", "url": checkout_url},
            {"title": "Débito", "url": checkout_url},
            {"title": "Pix", "url": checkout_url},
        ]
        return self._build_response(text, checkout_url=checkout_url, buttons=buttons)

    def _build_response(
        self,
        text: str,
        checkout_url: Optional[str] = None,
        buttons: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        final_text = _response_with_notice(text, self.closed_notice)
        payload: Dict[str, Any] = {
            "text": final_text,
            "message": final_text,
            "response": final_text,
            "state": self.state,
        }
        if "order_id" in self.state:
            payload["order_id"] = self.state["order_id"]
        if checkout_url:
            payload["checkout_url"] = checkout_url
        if buttons:
            payload["buttons"] = buttons
        return payload


class RestaurantService:
    def __init__(self, config_path: str | Path) -> None:
        # Carrega o arquivo de configuracao para uso em todo o fluxo
        self.config_path = Path(config_path)
        self.config = load_config(str(self.config_path))
        self.restaurant_slug = self.config_path.stem

    def process_message(self, message: str, state: Dict[str, Any]) -> Dict[str, Any]:
        # Estado compartilhado do pedido (carrinho e dados do cliente)
        if state is None:
            state = {}
        if not state.get("session_id"):
            state["session_id"] = str(uuid.uuid4())

        # Se estiver fechado, encerra imediatamente sem alterar o estado.
        # Atualiza telefone se vier no texto em qualquer momento
        if message and _looks_like_phone(message):
            normalized_phone = _normalize_phone(message)
            if normalized_phone:
                state.setdefault("customer_info", {})["phone"] = normalized_phone

        manager = ConversationManager(self.config, state, self.restaurant_slug)
        return manager.handle_message(message)
