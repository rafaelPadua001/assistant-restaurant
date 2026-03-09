from typing import Any, Dict

import re

from fastapi import FastAPI
from fastapi import Query
from fastapi.middleware.cors import CORSMiddleware
from threading import Lock
from datetime import datetime
from typing import List

from verticals.restaurant.service import RestaurantService

from pathlib import Path

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
notifications_store: dict[str, list[dict[str, Any]]] = {}
notifications_lock = Lock()
_SESSION_FLAGS: Dict[str, Dict[str, Any]] = {}

# CORS para permitir chamadas do frontend local (file:// ou http://localhost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/restaurant/{restaurant_id}/chat")
async def restaurant_chat(restaurant_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    message = body.get("message", "")
    state = body.get("state", {}) or {}

    session_id = str(state.get("session_id") or "").strip()
    if session_id:
        with notifications_lock:
            flags = _SESSION_FLAGS.get(session_id)
        if flags and flags.get("order_paid"):
            state["step"] = "order_completed"
            state["order_paid"] = True
            if flags.get("order_id") and not state.get("order_id"):
                state["order_id"] = flags["order_id"]
    service = RestaurantService(
        config_path=BASE_DIR / "verticals" / "restaurant" / "config" / f"{restaurant_id}.json"
    )
    result = service.process_message(message, state)

    # Atualiza state com o retorno do service, se houver
    state = result.get("state", state)

    message = (
        result.get("message")
        or result.get("text")
        or result.get("response")
        or ""
    )

    response: Dict[str, Any] = {
        "message": message,
        "state": state,
    }

    # Repassa campos adicionais quando existirem
    if "response" in result:
        response["response"] = result["response"]
    if "text" in result:
        response["text"] = result["text"]
    if "order_id" in result:
        response["order_id"] = result["order_id"]
    if "checkout_url" in result:
        response["checkout_url"] = result["checkout_url"]
    if "buttons" in result:
        response["buttons"] = result["buttons"]
    if "whatsapp_link" in result:
        response["whatsapp_link"] = result["whatsapp_link"]

    return response


@app.post("/assistant/notify")
async def assistant_notify(body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"status": "ignored"}

    status_value = str(body.get("status") or "").strip().upper()
    order_id_raw = body.get("order_id")
    message = str(body.get("message") or "").strip()
    if not message and order_id_raw is not None and status_value:
        message = f"Pagamento do pedido {order_id_raw} atualizado para {status_value.lower()}"

    payload: Dict[str, Any] = dict(body)
    payload["session_id"] = session_id
    payload["message"] = message
    payload["created_at"] = str(payload.get("created_at") or datetime.utcnow().isoformat())

    with notifications_lock:
        notifications_store.setdefault(session_id, []).append(payload)

    order_id_match = re.search(r"pedido\s*#(\d+)", message, re.IGNORECASE)
    if not status_value:
        status_match = re.search(r"status atualizado:\s*([A-Z0-9_]+)", message, re.IGNORECASE)
        if status_match:
            status_value = status_match.group(1).strip().upper()
    if status_value in {"PAID", "APPROVED", "APROVADO", "CONFIRMED", "PAGO"}:
        if isinstance(order_id_raw, int):
            order_id = order_id_raw
        elif isinstance(order_id_raw, str) and order_id_raw.isdigit():
            order_id = int(order_id_raw)
        elif order_id_match:
            order_id = int(order_id_match.group(1))
        else:
            order_id = None
        with notifications_lock:
            _SESSION_FLAGS[session_id] = {
                "order_paid": True,
                "order_id": order_id,
            }
    print("NOTIFY RECEBIDO:", body)
    print("NOTIFICATIONS:", notifications_store)
    return {"status": "ok"}


@app.get("/assistant/notifications")
async def assistant_notifications(session_id: str = Query(default="")) -> Dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"notifications": []}

    with notifications_lock:
        notifications = notifications_store.get(session_id, []).copy()
        notifications_store[session_id] = []

    return {"notifications": notifications}


@app.get("/assistant/notifications/{session_id}")
async def assistant_notifications_legacy(session_id: str) -> Dict[str, Any]:
    return await assistant_notifications(session_id=session_id)
