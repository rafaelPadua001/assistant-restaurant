from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from .config_schema import MenuItem, RestaurantConfig
from .tools import (
    build_whatsapp_message,
    calculate_total,
    find_menu_item,
    is_open,
    load_config,
)


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


class ConversationStep(str, Enum):
    ORDERING = "ordering"
    CONFIRMATION = "confirmation"
    AWAITING_NAME = "awaiting_name"
    AWAITING_ADDRESS = "awaiting_address"


class IntentType(str, Enum):
    SHOW_MENU = "show_menu"
    SHOW_PROMOS = "show_promos"
    FINISH = "finish"
    CONFIRM = "confirm"
    EDIT = "edit"
    ADD_ITEM = "add_item"
    REMOVE_ITEM = "remove_item"
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


def _menu_category_emoji(category: str) -> str:
    label = category.lower()
    if "pizza" in label:
        return "🍕"
    if "bebida" in label or "drink" in label or "refri" in label or "refrigerante" in label:
        return "🥤"
    if "sobremesa" in label or "doce" in label:
        return "🍰"
    if "combo" in label:
        return "🎁"
    return "📋"


def _menu_text(config: RestaurantConfig) -> str:
    lines: List[str] = ["Cardapio:"]
    for category, items in config.menu.items():
        emoji = _menu_category_emoji(category)
        lines.append(f"\n{emoji} {category.upper()}:")
        for item in items:
            description = f" — {item.description}" if item.description else ""
            lines.append(
                f"• {item.id} — {item.name} (R$ {item.price:.2f}){description}"
            )
    return "\n".join(lines)


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
    match = re.search(r"\b(endereco|endereco)\s*(e|e|:)?\s+(.+)", text, re.IGNORECASE)
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


def parse_intent(message: str, indexed_items: List[IndexedItem]) -> Intent:
    text = _normalize_text(message)

    if any(keyword in text for keyword in MENU_KEYWORDS):
        return Intent(IntentType.SHOW_MENU)
    if any(keyword in text for keyword in PROMO_KEYWORDS):
        return Intent(IntentType.SHOW_PROMOS)
    if any(keyword in text for keyword in FINISH_KEYWORDS):
        return Intent(IntentType.FINISH)
    if any(keyword in text for keyword in CONFIRM_KEYWORDS):
        return Intent(IntentType.CONFIRM)
    if any(keyword in text for keyword in EDIT_KEYWORDS):
        return Intent(IntentType.EDIT)

    indexed = _match_item(message, indexed_items)
    if indexed:
        quantity = _extract_quantity(message, indexed)
        if any(keyword in text for keyword in REMOVE_KEYWORDS):
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
        return any(entry.get("id") for entry in self.cart_state)

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


def _response_with_notice(text: str, notice: Optional[str]) -> str:
    if not notice:
        return text
    return f"{notice}\n\n{text}"


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
    return ConversationStep.ORDERING


class ConversationManager:
    def __init__(self, config: RestaurantConfig, state: Dict[str, Any]) -> None:
        self.config = config
        self.state = state
        self.cart = CartManager(config, state.setdefault("cart", []))
        self.customer_info: Dict[str, Any] = state.setdefault("customer_info", {})
        self.item_index = _build_item_index(config)

        self.step = _coerce_step(state)
        self.state["step"] = self.step.value

        # Limpa flags antigas para manter o estado consistente
        self.state.pop("awaiting_confirmation", None)
        self.state.pop("awaiting_info", None)
        self.state.pop("confirmed", None)

        now = datetime.now()
        self.closed_notice = None if is_open(config, now) else _closed_message(config, now)

    def handle_message(self, message: str) -> Dict[str, Any]:
        intent = parse_intent(message, self.item_index)

        if self.step in {ConversationStep.AWAITING_NAME, ConversationStep.AWAITING_ADDRESS}:
            return self._handle_customer_info(message, intent)

        if self.step == ConversationStep.CONFIRMATION:
            return self._handle_confirmation(intent, message)

        return self._handle_general(intent, message)

    def _handle_customer_info(self, message: str, intent: Intent) -> Dict[str, Any]:
        # Permite que o usuario veja menu ou adicione itens sem quebrar o fluxo
        if intent.type in {
            IntentType.SHOW_MENU,
            IntentType.SHOW_PROMOS,
            IntentType.ADD_ITEM,
            IntentType.REMOVE_ITEM,
        }:
            response = self._handle_general(intent, message)
            reminder = self._missing_info_prompt()
            response["text"] = f"{response['text']}\n\n{reminder}"
            return response

        if intent.type == IntentType.CONFIRM:
            return self._build_response(
                "Antes de confirmar, preciso do seu nome e endereco."
            )

        if self.step == ConversationStep.AWAITING_NAME:
            name = _extract_name(message) or message.strip()
            if not _is_valid_name(name):
                return self._build_response("Nao consegui entender seu nome. Pode repetir?")
            self.customer_info["name"] = name
            self.step = ConversationStep.AWAITING_ADDRESS
            self.state["step"] = self.step.value
            return self._build_response("Obrigado! Qual o endereco para entrega?")

        address = _extract_address(message) or message.strip()
        if not _is_valid_address(address):
            return self._build_response("Endereco invalido. Pode enviar novamente?")
        self.customer_info["address"] = address
        self.step = ConversationStep.ORDERING
        self.state["step"] = self.step.value

        if self.state.get("pending_confirmation"):
            self.state.pop("pending_confirmation", None)
            return self._finalize_order()

        return self._build_response("Perfeito! Posso ajudar com mais algum item?")

    def _handle_confirmation(self, intent: Intent, message: str) -> Dict[str, Any]:
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
            self.state["pending_confirmation"] = True
            return self._build_response("Qual seu nome?")

        if not self.customer_info.get("address"):
            self.step = ConversationStep.AWAITING_ADDRESS
            self.state["step"] = self.step.value
            self.state["pending_confirmation"] = True
            return self._build_response("Qual o endereco para entrega?")

        phone = self.customer_info.get("phone")
        if phone and not _normalize_phone(phone):
            return self._build_response("Telefone invalido. Pode enviar novamente?")

        wa_message = build_whatsapp_message(self.config, self.cart.cart_state, self.customer_info)
        wa_link = f"https://wa.me/{self.config.whatsapp_number}?text={quote(wa_message)}"
        self.step = ConversationStep.ORDERING
        self.state["step"] = self.step.value
        self.state.pop("pending_confirmation", None)
        return self._build_response(
            "Pedido confirmado! Clique no link para enviar via WhatsApp.", whatsapp_link=wa_link
        )

    def _build_response(self, text: str, whatsapp_link: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "text": _response_with_notice(text, self.closed_notice),
            "state": self.state,
        }
        if whatsapp_link:
            payload["whatsapp_link"] = whatsapp_link
        return payload


class RestaurantService:
    def __init__(self, config_path: str) -> None:
        # Carrega o arquivo de configuracao para uso em todo o fluxo
        self.config = load_config(config_path)

    def process_message(self, message: str, state: Dict[str, Any]) -> Dict[str, Any]:
        # Estado compartilhado do pedido (carrinho e dados do cliente)
        if state is None:
            state = {}

        # Atualiza telefone se vier no texto em qualquer momento
        if message and _looks_like_phone(message):
            normalized_phone = _normalize_phone(message)
            if normalized_phone:
                state.setdefault("customer_info", {})["phone"] = normalized_phone

        manager = ConversationManager(self.config, state)
        return manager.handle_message(message)
