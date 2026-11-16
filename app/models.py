from __future__ import annotations

import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
    Enum as SAEnum,
    func
)
from sqlalchemy.orm import relationship

from app.database import Base


class OperationType(str, enum.Enum):
    INBOUND = "INBOUND"
    TRANSFER = "TRANSFER"
    FBS = "FBS"
    FBO = "FBO"


class OperationStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    IN_TRANSIT = "IN_TRANSIT"
    IN_TRANSIT_TO_MP = "IN_TRANSIT_TO_MP"
    SHIPPED_TO_CLIENT = "SHIPPED_TO_CLIENT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, default="Основной магазин WB")

    warehouses = relationship("Warehouse", back_populates="shop", cascade="all, delete-orphan")


class Warehouse(Base):
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("shop_id", "name", name="uq_shop_warehouse_name"),
    )

    shop = relationship("Shop", back_populates="warehouses")
    stock_balances = relationship("StockBalance", back_populates="warehouse")
    source_operations = relationship(
        "StockOperation",
        foreign_keys="StockOperation.source_warehouse_id",
        back_populates="source_warehouse"
    )
    target_operations = relationship(
        "StockOperation",
        foreign_keys="StockOperation.target_warehouse_id",
        back_populates="target_warehouse"
    )


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)

    stock_balances = relationship("StockBalance", back_populates="product")
    operations = relationship("StockOperation", back_populates="product")
    history_records = relationship("MovementHistory", back_populates="product")


class StockBalance(Base):
    __tablename__ = "stock_balances"

    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    physical_qty = Column(Integer, default=0, nullable=False)
    reserved_fbs_qty = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("physical_qty >= 0", name="check_physical_qty_non_negative"),
        CheckConstraint("reserved_fbs_qty >= 0", name="check_reserved_fbs_qty_non_negative"),
        CheckConstraint("physical_qty >= reserved_fbs_qty", name="check_physical_gte_reserved"),
        UniqueConstraint("warehouse_id", "product_id", name="uq_warehouse_product"),
    )

    warehouse = relationship("Warehouse", back_populates="stock_balances")
    product = relationship("Product", back_populates="stock_balances")

    @property
    def available_qty(self) -> int:
        return self.physical_qty - self.reserved_fbs_qty


class StockOperation(Base):
    __tablename__ = "stock_operations"

    id = Column(Integer, primary_key=True, index=True)
    operation_uid = Column(String(100), unique=True, index=True, nullable=False)
    op_type = Column(SAEnum(OperationType, native_enum=False), nullable=False)
    status = Column(SAEnum(OperationStatus, native_enum=False), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    source_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True)
    target_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True)
    marketplace_destination = Column(String(255), nullable=True)
    qty = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("qty > 0", name="check_operation_qty_positive"),
    )

    product = relationship("Product", back_populates="operations")
    source_warehouse = relationship("Warehouse", foreign_keys=[source_warehouse_id], back_populates="source_operations")
    target_warehouse = relationship("Warehouse", foreign_keys=[target_warehouse_id], back_populates="target_operations")
    history_records = relationship("MovementHistory", back_populates="operation")


class MovementHistory(Base):
    __tablename__ = "movement_history"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    operation_id = Column(Integer, ForeignKey("stock_operations.id"), nullable=True)
    action_type = Column(String(100), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty = Column(Integer, nullable=False)
    from_location = Column(String(255), nullable=False)
    to_location = Column(String(255), nullable=False)
    snapshot_after = Column(String(500), nullable=False)

    operation = relationship("StockOperation", back_populates="history_records")
    product = relationship("Product", back_populates="history_records")
