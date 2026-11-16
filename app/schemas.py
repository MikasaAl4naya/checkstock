from __future__ import annotations

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict


class WarehouseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class WarehouseUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class WarehouseRead(BaseModel):
    id: int
    shop_id: int
    name: str
    is_active: bool
    deleted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ProductRead(BaseModel):
    id: int
    sku: str
    name: str

    model_config = ConfigDict(from_attributes=True)


class InboundRequest(BaseModel):
    warehouse_id: int
    sku: str
    qty: int = Field(..., gt=0)
    operation_uid: Optional[str] = None


class TransferRequest(BaseModel):
    source_warehouse_id: int
    target_warehouse_id: int
    sku: str
    qty: int = Field(..., gt=0)
    operation_uid: Optional[str] = None


class FBSReserveRequest(BaseModel):
    warehouse_id: int
    sku: str
    qty: int = Field(..., gt=0)
    order_number: Optional[str] = "1001"
    operation_uid: Optional[str] = None


class FBOShipmentRequest(BaseModel):
    warehouse_id: int
    sku: str
    qty: int = Field(..., gt=0)
    destination: str = "Склад WB Коледино"
    operation_uid: Optional[str] = None


class DonorWarehouseInfo(BaseModel):
    warehouse_id: int
    warehouse_name: str
    available_qty: int


class DeficitInfo(BaseModel):
    sku: str
    product_name: str
    requested_qty: int
    source_warehouse_id: int
    source_warehouse_name: str
    available_on_source: int
    deficit: int
    total_available_network: int
    is_resolvable: bool
    donor_warehouses: List[DonorWarehouseInfo] = []
    original_op_type: str = ""
    target_warehouse_id: Optional[int] = None
    destination: Optional[str] = None


class OperationResult(BaseModel):
    success: bool
    message: str
    already_processed: bool = False
    deficit_info: Optional[DeficitInfo] = None
    operation_id: Optional[int] = None
    snapshot: Optional[str] = None


class StockBalanceRow(BaseModel):
    balance_id: int
    warehouse_id: int
    warehouse_name: str
    warehouse_is_active: bool
    product_id: int
    sku: str
    product_name: str
    physical_qty: int
    reserved_fbs_qty: int
    available_qty: int


class SkuSummaryRow(BaseModel):
    product_id: int
    sku: str
    product_name: str
    sum_physical: int
    sum_reserved: int
    sum_available: int
    in_transit_ff: int
    in_transit_fbo: int
    in_transit_fbs: int
    accepted_fbo: int
    total_company: int


class KPICards(BaseModel):
    total_physical: int
    total_available: int
    total_reserved_fbs: int
    in_transit_ff: int
    in_transit_fbo: int
    in_transit_fbs: int


class StockOperationRead(BaseModel):
    id: int
    operation_uid: str
    op_type: str
    status: str
    product_id: int
    sku: str
    product_name: str
    source_warehouse_id: Optional[int]
    source_warehouse_name: Optional[str]
    target_warehouse_id: Optional[int]
    target_warehouse_name: Optional[str]
    marketplace_destination: Optional[str]
    qty: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MovementHistoryRead(BaseModel):
    id: int
    created_at: datetime
    operation_id: Optional[int]
    action_type: str
    product_id: int
    sku: str
    product_name: str
    qty: int
    from_location: str
    to_location: str
    snapshot_after: str

    model_config = ConfigDict(from_attributes=True)
