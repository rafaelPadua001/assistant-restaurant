from typing import Dict, List

from pydantic import BaseModel


class MenuItem(BaseModel):
    id: str
    name: str
    price: float
    description: str


class PromotionRule(BaseModel):
    trigger: str
    suggest: str
    message: str


class UpsellRule(BaseModel):
    condition: str
    suggest: str


class OpeningHours(BaseModel):
    monday: str
    tuesday: str
    wednesday: str
    thursday: str
    friday: str
    saturday: str
    sunday: str


class RestaurantConfig(BaseModel):
    name: str
    whatsapp_number: str
    delivery_fee: float
    opening_hours: Dict[str, str]
    menu: Dict[str, List[MenuItem]]
    promotions: List[PromotionRule]
    upsell_rules: List[UpsellRule]
