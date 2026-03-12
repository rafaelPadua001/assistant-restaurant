from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from verticals.restaurant.service import RestaurantService
import verticals.restaurant.service as service_module
import services.menu_api_client as menu_api_client


@pytest.fixture(scope="session", autouse=True)
def _config_info() -> dict:
    root = Path(__file__).resolve().parents[1]
    source_dir = root / "verticals" / "restaurant" / "config"
    sources = sorted(source_dir.glob("*.json"))
    assert sources, "Nenhum arquivo de configuracao encontrado em verticals/restaurant/config"

    source = sources[0]
    return {"path": source, "restaurant_id": source.stem}


@pytest.fixture(autouse=True)
def _mock_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_order(_payload: dict):
        return (123, None)

    def _fake_checkout(_order_id: int):
        return ("http://checkout.test/abc", None)

    monkeypatch.setattr(service_module, "_create_order", _fake_order)
    monkeypatch.setattr(service_module, "_create_checkout", _fake_checkout)


@pytest.fixture(autouse=True)
def _mock_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "is_open", lambda *_args, **_kwargs: True)


@pytest.fixture(autouse=True)
def _mock_menu_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_menu():
        return {
            "categories": [
                {
                    "name": "Pizzas",
                    "products": [
                        {
                            "id": "calabresa",
                            "name": "Pizza Calabresa",
                            "price": 39.9,
                            "description": "Molho, mussarela e calabresa",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(service_module, "get_menu", _fake_menu)
    monkeypatch.setattr(menu_api_client, "get_menu", _fake_menu)


def test_restaurant_chat_flow(_config_info: dict) -> None:
    config_path = _config_info["path"]
    restaurant_id = _config_info["restaurant_id"]

    service = RestaurantService(str(config_path))
    first_category = next(iter(service.config.menu.values()))
    item_id = first_category[0].id

    client = TestClient(app)

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "menu", "state": {"restaurant_id": 1}},
    )
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert item_id.lower() in data["message"].lower()

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": item_id, "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["state"]["cart"][0]["id"] == item_id

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "finalizar", "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "checkout_url" not in data
    assert "Resumo do pedido" in data["message"]

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "sim", "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "nome" in data["message"].lower()

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "Joao", "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "endereco" in data["message"].lower()

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "Rua A, 123", "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "order_id" in data
    assert "checkout_url" in data
    assert "forma de pagamento" in data["message"].lower()


def test_menu_empty_from_api(_config_info: dict) -> None:
    config_path = _config_info["path"]
    restaurant_id = _config_info["restaurant_id"]

    def _empty_menu():
        return {"categories": []}

    service_module.get_menu = _empty_menu
    menu_api_client.get_menu = _empty_menu
    service = RestaurantService(str(config_path))
    assert service.config.menu == {}
    assert service_module._menu_text_from_menu(service.config.menu) == "Nenhum produto cadastrado no momento."

    client = TestClient(app)
    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "menu", "state": {"restaurant_id": 1}},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Nenhum produto cadastrado no momento."

    response = client.post(
        f"/restaurant/{restaurant_id}/chat",
        json={"message": "finalizar", "state": data["state"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Nenhum produto cadastrado no momento."
