import json
import logging
import os
import time
from typing import Any, Optional
from urllib import error as url_error
from urllib import request as url_request

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
TIMEOUT_SECONDS = 3
CACHE_TTL = 60
_menu_cache: Optional[dict] = None
_menu_cache_url: Optional[str] = None
_last_fetch = 0.0
logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    return os.getenv("PIZZARIA_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _get_json(path: str, base_url: Optional[str] = None) -> Optional[Any]:
    base = base_url or _get_base_url()
    url = f"{base}/{path.lstrip('/')}"
    try:
        with url_request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return None
            payload = response.read().decode("utf-8")
        return json.loads(payload)
    except (url_error.HTTPError, url_error.URLError, ValueError):
        return None
    except Exception:
        return None


def get_categories() -> Optional[list[dict]]:
    return _get_json("categories")


def get_products() -> Optional[list[dict]]:
    return _get_json("products")


def get_products_by_category(category_id: int) -> Optional[dict]:
    return _get_json(f"categories/{category_id}/products")


def get_menu() -> Optional[dict]:
    global _menu_cache, _menu_cache_url, _last_fetch
    base_url = _get_base_url()
    now = time.time()
    if (
        _menu_cache is not None
        and _menu_cache_url == base_url
        and (now - _last_fetch) < CACHE_TTL
    ):
        return _menu_cache

    logger.info("[assistant] carregando cardapio via API")
    print("[assistant] carregando cardapio via API")
    payload = _get_json("menu", base_url=base_url)
    if payload is None:
        return None

    _menu_cache = payload
    _menu_cache_url = base_url
    _last_fetch = now
    logger.info("[assistant] menu carregado com sucesso")
    print("[assistant] menu carregado com sucesso")
    return payload
