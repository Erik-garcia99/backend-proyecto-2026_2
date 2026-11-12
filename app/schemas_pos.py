from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SaleItemInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    product_id: UUID
    quantity: int = Field(gt=0, le=100000)


class PaymentInput(BaseModel):
    method: Literal['cash', 'card', 'transfer'] = 'cash'
    amount_received: Decimal | None = Field(default=None, ge=0)


class ProductOut(BaseModel):
    id: UUID
    sku: str
    name: str
    barcode: str | None = None
    sale_price: Decimal
    tax_rate: Decimal


class InventoryOut(BaseModel):
    product_id: UUID
    sku: str
    name: str
    barcode: str | None = None
    quantity_on_hand: int
    updated_at: str | None = None


class SaleCreate(BaseModel):
    customer_id: UUID | None = None
    items: list[SaleItemInput] = Field(min_length=1)
    payment: PaymentInput = PaymentInput()
    notes: str | None = Field(default=None, max_length=500)


class SaleItemOut(BaseModel):
    id: UUID
    product_id: UUID
    sku: str | None = None
    name: str | None = None
    quantity: int
    unit_price: Decimal
    subtotal: Decimal


class SaleOut(BaseModel):
    id: UUID
    sale_number: str
    status: str
    subtotal: Decimal
    tax: Decimal
    discount: Decimal
    total: Decimal
    payment_method: str
    customer_id: UUID | None = None
    created_at: str | None = None
    items: list[SaleItemOut] = []
