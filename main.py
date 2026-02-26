from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from verticals.restaurant.service import RestaurantService

from pathlib import Path

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent

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
