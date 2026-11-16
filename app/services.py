from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from sqlalchemy import select, func, and_, or_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Shop,
    Warehouse,
    Product,
    StockBalance,
    StockOperation,
    MovementHistory,
    OperationType,
    OperationStatus,
)
from app.schemas import (
    OperationResult,
    DeficitInfo,
    DonorWarehouseInfo,
    StockBalanceRow,
    SkuSummaryRow,
    KPICards,
    StockOperationRead,
    MovementHistoryRead,
    WarehouseRead,
    ProductRead,
)


def apply_for_update(stmt, session: AsyncSession):
    """Безопасное применение SELECT FOR UPDATE, если диалект СУБД поддерживает блокировку строк (PostgreSQL)."""
    if session.bind and session.bind.dialect.name == "sqlite":
        return stmt
    return stmt.with_for_update()


def make_snapshot_str(wh_name: str, physical: int, reserved: int) -> str:
    return f"{wh_name}: физ {physical}, рез {reserved}, дост {physical - reserved}"


class WarehouseService:

    @staticmethod
    async def get_or_create_shop(session: AsyncSession) -> Shop:
        result = await session.execute(select(Shop).limit(1))
        shop = result.scalar_one_or_none()
        if not shop:
            shop = Shop(name="Основной магазин WB")
            session.add(shop)
            await session.flush()
        return shop

    @staticmethod
    async def get_product_by_sku(session: AsyncSession, sku: str) -> Optional[Product]:
        result = await session.execute(select(Product).where(Product.sku == sku.strip()))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_warehouse_by_id(session: AsyncSession, warehouse_id: int) -> Optional[Warehouse]:
        result = await session.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create_balance(
        session: AsyncSession,
        warehouse_id: int,
        product_id: int,
        lock: bool = True
    ) -> StockBalance:
        stmt = select(StockBalance).where(
            and_(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.product_id == product_id
            )
        )
        if lock:
            stmt = apply_for_update(stmt, session)
        result = await session.execute(stmt)
        balance = result.scalar_one_or_none()

        if not balance:
            balance = StockBalance(
                warehouse_id=warehouse_id,
                product_id=product_id,
                physical_qty=0,
                reserved_fbs_qty=0
            )
            session.add(balance)
            await session.flush()
        return balance

    @staticmethod
    async def check_deficit(
        session: AsyncSession,
        product: Product,
        source_wh: Warehouse,
        requested_qty: int,
        available_on_source: int,
        original_op_type: str = "",
        target_warehouse_id: Optional[int] = None,
        destination: Optional[str] = None
    ) -> DeficitInfo:
        deficit = requested_qty - available_on_source

        # Поиск активных складов-доноров
        wh_stmt = select(Warehouse).where(
            and_(
                Warehouse.is_active == True,
                Warehouse.id != source_wh.id
            )
        )
        wh_result = await session.execute(wh_stmt)
        other_warehouses = wh_result.scalars().all()

        donors: List[DonorWarehouseInfo] = []
        total_other_available = 0

        for wh in other_warehouses:
            bal_stmt = select(StockBalance).where(
                and_(
                    StockBalance.warehouse_id == wh.id,
                    StockBalance.product_id == product.id
                )
            )
            bal_res = await session.execute(bal_stmt)
            bal = bal_res.scalar_one_or_none()
            avail = bal.available_qty if bal else 0
            if avail > 0:
                donors.append(
                    DonorWarehouseInfo(
                        warehouse_id=wh.id,
                        warehouse_name=wh.name,
                        available_qty=avail
                    )
                )
                total_other_available += avail

        # Сортировка доноров по убыванию доступного остатка
        donors.sort(key=lambda d: d.available_qty, reverse=True)
        total_available_network = available_on_source + total_other_available
        is_resolvable = total_available_network >= requested_qty

        return DeficitInfo(
            sku=product.sku,
            product_name=product.name,
            requested_qty=requested_qty,
            source_warehouse_id=source_wh.id,
            source_warehouse_name=source_wh.name,
            available_on_source=available_on_source,
            deficit=deficit,
            total_available_network=total_available_network,
            is_resolvable=is_resolvable,
            donor_warehouses=donors,
            original_op_type=original_op_type,
            target_warehouse_id=target_warehouse_id,
            destination=destination
        )

    # -------------------------------------------------------------
    # 1. Оприходование товара (INBOUND)
    # -------------------------------------------------------------
    @staticmethod
    async def process_inbound(
        session: AsyncSession,
        warehouse_id: int,
        sku: str,
        qty: int,
        operation_uid: Optional[str] = None
    ) -> OperationResult:
        if qty <= 0:
            return OperationResult(success=False, message="Количество должно быть больше нуля.")

        warehouse = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not warehouse:
            return OperationResult(success=False, message="Склад не найден.")
        if not warehouse.is_active:
            return OperationResult(
                success=False,
                message=f"Склад «{warehouse.name}» находится в архиве. Приход на архивный склад запрещён!"
            )

        product = await WarehouseService.get_product_by_sku(session, sku)
        if not product:
            return OperationResult(success=False, message=f"Товар с артикулом {sku} не найден.")

        uid = operation_uid or f"INB-{uuid.uuid4().hex[:12].upper()}"

        balance = await WarehouseService.get_or_create_balance(
            session, warehouse.id, product.id, lock=True
        )
        balance.physical_qty += qty

        op = StockOperation(
            operation_uid=uid,
            op_type=OperationType.INBOUND,
            status=OperationStatus.COMPLETED,
            product_id=product.id,
            source_warehouse_id=None,
            target_warehouse_id=warehouse.id,
            marketplace_destination=None,
            qty=qty
        )
        session.add(op)
        await session.flush()

        snap = make_snapshot_str(warehouse.name, balance.physical_qty, balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="ПРИХОД",
            product_id=product.id,
            qty=qty,
            from_location="Поставщик",
            to_location=warehouse.name,
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Приход {qty} шт. «{product.name}» ({product.sku}) на склад «{warehouse.name}» успешно проведён.",
            snapshot=snap
        )

    # -------------------------------------------------------------
    # 2. Перемещение между складами (TRANSFER)
    # -------------------------------------------------------------
    @staticmethod
    async def start_transfer(
        session: AsyncSession,
        source_warehouse_id: int,
        target_warehouse_id: int,
        sku: str,
        qty: int,
        operation_uid: Optional[str] = None
    ) -> OperationResult:
        if qty <= 0:
            return OperationResult(success=False, message="Количество должно быть больше нуля.")

        if source_warehouse_id == target_warehouse_id:
            return OperationResult(
                success=False,
                message="Запрещено перемещение на тот же самый склад (склад-отправитель совпадает со складом-получателем)!"
            )

        source_wh = await WarehouseService.get_warehouse_by_id(session, source_warehouse_id)
        if not source_wh:
            return OperationResult(success=False, message="Склад-отправитель не найден.")

        target_wh = await WarehouseService.get_warehouse_by_id(session, target_warehouse_id)
        if not target_wh:
            return OperationResult(success=False, message="Склад-получатель не найден.")
        if not target_wh.is_active:
            return OperationResult(
                success=False,
                message=f"Склад-получатель «{target_wh.name}» находится в архиве. Перемещение на удалённый склад запрещено!"
            )

        product = await WarehouseService.get_product_by_sku(session, sku)
        if not product:
            return OperationResult(success=False, message=f"Товар с артикулом {sku} не найден.")

        # Блокировка строки остатка на складе-отправителе
        source_balance = await WarehouseService.get_or_create_balance(
            session, source_wh.id, product.id, lock=True
        )

        if source_balance.available_qty < qty:
            deficit_info = await WarehouseService.check_deficit(
                session=session,
                product=product,
                source_wh=source_wh,
                requested_qty=qty,
                available_on_source=source_balance.available_qty,
                original_op_type="TRANSFER",
                target_warehouse_id=target_warehouse_id
            )
            if deficit_info.is_resolvable:
                donors_text = ", ".join(
                    [f"{d.warehouse_name} — {d.available_qty} шт." for d in deficit_info.donor_warehouses]
                )
                msg = (
                    f"На складе «{source_wh.name}» доступно только {deficit_info.available_on_source} шт. "
                    f"(не хватает {deficit_info.deficit} шт. для заявки на {qty} шт.). "
                    f"Но этот товар доступен на других ФФ: {donors_text}."
                )
            else:
                msg = (
                    f"Глобальная нехватка товара {product.sku}! Запрошено {qty} шт., "
                    f"на складе «{source_wh.name}» доступно {deficit_info.available_on_source} шт., "
                    f"а суммарно по всем складам сети доступно всего {deficit_info.total_available_network} шт. "
                    f"Требуется оформить приход от поставщика."
                )
            return OperationResult(
                success=False,
                message=msg,
                deficit_info=deficit_info
            )

        # Списание физического остатка с отправителя
        source_balance.physical_qty -= qty

        uid = operation_uid or f"TRF-{uuid.uuid4().hex[:12].upper()}"
        op = StockOperation(
            operation_uid=uid,
            op_type=OperationType.TRANSFER,
            status=OperationStatus.IN_TRANSIT,
            product_id=product.id,
            source_warehouse_id=source_wh.id,
            target_warehouse_id=target_wh.id,
            marketplace_destination=None,
            qty=qty
        )
        session.add(op)
        await session.flush()

        snap = make_snapshot_str(source_wh.name, source_balance.physical_qty, source_balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="ПЕРЕМЕЩЕНИЕ_ОТПРАВКА",
            product_id=product.id,
            qty=qty,
            from_location=source_wh.name,
            to_location=f"В пути между ФФ ({source_wh.name} → {target_wh.name})",
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Отправлено перемещение {qty} шт. «{product.sku}» со склада «{source_wh.name}» на склад «{target_wh.name}». Товар в пути.",
            snapshot=snap
        )

    @staticmethod
    async def accept_transfer(session: AsyncSession, operation_id: int) -> OperationResult:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse),
            selectinload(StockOperation.target_warehouse)
        ).where(StockOperation.id == operation_id)
        stmt = apply_for_update(stmt, session)
        res = await session.execute(stmt)
        op = res.scalar_one_or_none()

        if not op:
            return OperationResult(success=False, message="Операция перемещения не найдена.")

        if op.op_type != OperationType.TRANSFER:
            return OperationResult(success=False, message="Операция не является перемещением между ФФ.")

        # Проверка идемпотентности
        if op.status == OperationStatus.COMPLETED:
            return OperationResult(
                success=False,
                already_processed=True,
                operation_id=op.id,
                message=f"Защита от дублей: Перемещение #{op.id} уже было принято ранее. Повторное начисление заблокировано, остатки не изменились."
            )

        if op.status != OperationStatus.IN_TRANSIT:
            return OperationResult(
                success=False,
                message=f"Операция #{op.id} имеет статус {op.status.value}, приёмка невозможна."
            )

        target_balance = await WarehouseService.get_or_create_balance(
            session, op.target_warehouse_id, op.product_id, lock=True
        )
        target_balance.physical_qty += op.qty
        op.status = OperationStatus.COMPLETED

        snap = make_snapshot_str(op.target_warehouse.name, target_balance.physical_qty, target_balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="ПЕРЕМЕЩЕНИЕ_ПРИЕМКА",
            product_id=op.product_id,
            qty=op.qty,
            from_location=f"В пути между ФФ ({op.source_warehouse.name} → {op.target_warehouse.name})",
            to_location=op.target_warehouse.name,
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Перемещение #{op.id} принято на складе «{op.target_warehouse.name}» (+{op.qty} шт.).",
            snapshot=snap
        )

    # -------------------------------------------------------------
    # 3. Заказы FBS (резерв и отгрузка)
    # -------------------------------------------------------------
    @staticmethod
    async def create_fbs_reserve(
        session: AsyncSession,
        warehouse_id: int,
        sku: str,
        qty: int,
        order_number: Optional[str] = "1001",
        operation_uid: Optional[str] = None
    ) -> OperationResult:
        if qty <= 0:
            return OperationResult(success=False, message="Количество должно быть больше нуля.")

        warehouse = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not warehouse:
            return OperationResult(success=False, message="Склад не найден.")
        if not warehouse.is_active:
            return OperationResult(
                success=False,
                message=f"Склад «{warehouse.name}» находится в архиве. Резерв на удалённом складе запрещён!"
            )

        product = await WarehouseService.get_product_by_sku(session, sku)
        if not product:
            return OperationResult(success=False, message=f"Товар с артикулом {sku} не найден.")

        balance = await WarehouseService.get_or_create_balance(
            session, warehouse.id, product.id, lock=True
        )

        if balance.available_qty < qty:
            deficit_info = await WarehouseService.check_deficit(
                session=session,
                product=product,
                source_wh=warehouse,
                requested_qty=qty,
                available_on_source=balance.available_qty,
                original_op_type="FBS",
                destination=order_number
            )
            if deficit_info.is_resolvable:
                donors_text = ", ".join(
                    [f"{d.warehouse_name} — {d.available_qty} шт." for d in deficit_info.donor_warehouses]
                )
                msg = (
                    f"На складе «{warehouse.name}» доступно только {deficit_info.available_on_source} шт. "
                    f"(не хватает {deficit_info.deficit} шт. для заявки на {qty} шт.). "
                    f"Но этот товар доступен на других ФФ: {donors_text}."
                )
            else:
                msg = (
                    f"Глобальная нехватка товара {product.sku}! Запрошено {qty} шт., "
                    f"на складе «{warehouse.name}» доступно {deficit_info.available_on_source} шт., "
                    f"а суммарно по всем складам сети доступно всего {deficit_info.total_available_network} шт. "
                    f"Требуется оформить приход от поставщика."
                )
            return OperationResult(
                success=False,
                message=msg,
                deficit_info=deficit_info
            )

        # Резервирование без изменения физического остатка
        balance.reserved_fbs_qty += qty

        order_str = f"Заказ FBS #{order_number or '1001'}"
        uid = operation_uid or f"FBS-{uuid.uuid4().hex[:12].upper()}"

        op = StockOperation(
            operation_uid=uid,
            op_type=OperationType.FBS,
            status=OperationStatus.RESERVED,
            product_id=product.id,
            source_warehouse_id=warehouse.id,
            target_warehouse_id=None,
            marketplace_destination=order_str,
            qty=qty
        )
        session.add(op)
        await session.flush()

        snap = make_snapshot_str(warehouse.name, balance.physical_qty, balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBS_РЕЗЕРВ",
            product_id=product.id,
            qty=qty,
            from_location=warehouse.name,
            to_location=f"Резерв FBS на {warehouse.name}",
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Резерв FBS {qty} шт. «{product.sku}» успешно создан на складе «{warehouse.name}» ({order_str}).",
            snapshot=snap
        )

    @staticmethod
    async def ship_fbs_to_client(session: AsyncSession, operation_id: int) -> OperationResult:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse)
        ).where(StockOperation.id == operation_id)
        stmt = apply_for_update(stmt, session)
        res = await session.execute(stmt)
        op = res.scalar_one_or_none()

        if not op:
            return OperationResult(success=False, message="Операция FBS не найдена.")

        if op.op_type != OperationType.FBS:
            return OperationResult(success=False, message="Операция не является заказом FBS.")

        # Проверка идемпотентности
        if op.status in (OperationStatus.SHIPPED_TO_CLIENT, OperationStatus.COMPLETED):
            return OperationResult(
                success=False,
                already_processed=True,
                operation_id=op.id,
                message=f"Защита от дублей: Заказ FBS #{op.id} уже был отправлен клиенту ранее. Повторное списание заблокировано."
            )

        if op.status != OperationStatus.RESERVED:
            return OperationResult(
                success=False,
                message=f"Невозможно отправить заказ: текущий статус #{op.id} — {op.status.value}."
            )

        balance = await WarehouseService.get_or_create_balance(
            session, op.source_warehouse_id, op.product_id, lock=True
        )
        # Одновременное списание физического и зарезервированного остатка
        balance.physical_qty -= op.qty
        balance.reserved_fbs_qty -= op.qty
        op.status = OperationStatus.SHIPPED_TO_CLIENT

        snap = make_snapshot_str(op.source_warehouse.name, balance.physical_qty, balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBS_ОТПРАВКА_КЛИЕНТУ",
            product_id=op.product_id,
            qty=op.qty,
            from_location=f"Резерв FBS на {op.source_warehouse.name}",
            to_location="В пути к клиенту (FBS)",
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Заказ FBS #{op.id} ({op.marketplace_destination}) отправлен клиенту. Списано со склада {op.qty} шт.",
            snapshot=snap
        )

    @staticmethod
    async def cancel_fbs_reserve(session: AsyncSession, operation_id: int) -> OperationResult:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse)
        ).where(StockOperation.id == operation_id)
        stmt = apply_for_update(stmt, session)
        res = await session.execute(stmt)
        op = res.scalar_one_or_none()

        if not op:
            return OperationResult(success=False, message="Операция FBS не найдена.")

        if op.op_type != OperationType.FBS:
            return OperationResult(success=False, message="Операция не является заказом FBS.")

        # Проверка идемпотентности
        if op.status == OperationStatus.CANCELLED:
            return OperationResult(
                success=False,
                already_processed=True,
                operation_id=op.id,
                message=f"Защита от дублей: Резерв FBS #{op.id} уже был отменён ранее."
            )

        if op.status != OperationStatus.RESERVED:
            return OperationResult(
                success=False,
                message=f"Отменить можно только заказ в статусе RESERVED (текущий: {op.status.value})."
            )

        balance = await WarehouseService.get_or_create_balance(
            session, op.source_warehouse_id, op.product_id, lock=True
        )
        # Снятие резерва без изменения физического остатка
        balance.reserved_fbs_qty -= op.qty
        op.status = OperationStatus.CANCELLED

        snap = make_snapshot_str(op.source_warehouse.name, balance.physical_qty, balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBS_ОТМЕНА_РЕЗЕРВА",
            product_id=op.product_id,
            qty=op.qty,
            from_location=f"Резерв FBS на {op.source_warehouse.name}",
            to_location=op.source_warehouse.name,
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Резерв FBS #{op.id} отменён. {op.qty} шт. возвращены в доступный остаток.",
            snapshot=snap
        )

    @staticmethod
    async def deliver_fbs_order(session: AsyncSession, operation_id: int) -> OperationResult:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse)
        ).where(StockOperation.id == operation_id)
        stmt = apply_for_update(stmt, session)
        res = await session.execute(stmt)
        op = res.scalar_one_or_none()

        if not op:
            return OperationResult(success=False, message="Операция FBS не найдена.")

        if op.op_type != OperationType.FBS:
            return OperationResult(success=False, message="Операция не является заказом FBS.")

        if op.status == OperationStatus.COMPLETED:
            return OperationResult(
                success=False,
                already_processed=True,
                operation_id=op.id,
                message=f"Защита от дублей: Заказ FBS #{op.id} уже был доставлен клиенту ранее."
            )

        if op.status != OperationStatus.SHIPPED_TO_CLIENT:
            return OperationResult(
                success=False,
                message=f"Подтвердить доставку можно только для заказа в пути (текущий статус: {op.status.value})."
            )

        op.status = OperationStatus.COMPLETED
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBS_ДОСТАВЛЕНО",
            product_id=op.product_id,
            qty=op.qty,
            from_location="В пути к клиенту (FBS)",
            to_location="Клиент (FBS)",
            snapshot_after="Заказ вручен клиенту"
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Заказ FBS #{op.id} успешно вручен клиенту. Операция закрыта.",
            snapshot="Вручено клиенту"
        )

    # -------------------------------------------------------------
    # 4. Поставки FBO на маркетплейс
    # -------------------------------------------------------------
    @staticmethod
    async def start_fbo_shipment(
        session: AsyncSession,
        warehouse_id: int,
        sku: str,
        qty: int,
        destination: str = "Склад WB Коледино",
        operation_uid: Optional[str] = None
    ) -> OperationResult:
        if qty <= 0:
            return OperationResult(success=False, message="Количество должно быть больше нуля.")

        warehouse = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not warehouse:
            return OperationResult(success=False, message="Склад не найден.")
        if not warehouse.is_active:
            return OperationResult(
                success=False,
                message=f"Склад «{warehouse.name}» находится в архиве. Отправка FBO с архивного склада запрещена!"
            )

        product = await WarehouseService.get_product_by_sku(session, sku)
        if not product:
            return OperationResult(success=False, message=f"Товар с артикулом {sku} не найден.")

        balance = await WarehouseService.get_or_create_balance(
            session, warehouse.id, product.id, lock=True
        )

        if balance.available_qty < qty:
            deficit_info = await WarehouseService.check_deficit(
                session=session,
                product=product,
                source_wh=warehouse,
                requested_qty=qty,
                available_on_source=balance.available_qty,
                original_op_type="FBO",
                destination=destination
            )
            if deficit_info.is_resolvable:
                donors_text = ", ".join(
                    [f"{d.warehouse_name} — {d.available_qty} шт." for d in deficit_info.donor_warehouses]
                )
                msg = (
                    f"На складе «{warehouse.name}» доступно только {deficit_info.available_on_source} шт. "
                    f"(не хватает {deficit_info.deficit} шт. для заявки на {qty} шт.). "
                    f"Но этот товар доступен на других ФФ: {donors_text}."
                )
            else:
                msg = (
                    f"Глобальная нехватка товара {product.sku}! Запрошено {qty} шт., "
                    f"на складе «{warehouse.name}» доступно {deficit_info.available_on_source} шт., "
                    f"а суммарно по всем складам сети доступно всего {deficit_info.total_available_network} шт. "
                    f"Требуется оформить приход от поставщика."
                )
            return OperationResult(
                success=False,
                message=msg,
                deficit_info=deficit_info
            )

        # Списание физического остатка со склада
        balance.physical_qty -= qty

        dest = destination.strip() or "Склад WB Коледино"
        uid = operation_uid or f"FBO-{uuid.uuid4().hex[:12].upper()}"

        op = StockOperation(
            operation_uid=uid,
            op_type=OperationType.FBO,
            status=OperationStatus.IN_TRANSIT_TO_MP,
            product_id=product.id,
            source_warehouse_id=warehouse.id,
            target_warehouse_id=None,
            marketplace_destination=dest,
            qty=qty
        )
        session.add(op)
        await session.flush()

        snap = make_snapshot_str(warehouse.name, balance.physical_qty, balance.reserved_fbs_qty)
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBO_ОТПРАВКА_НА_МП",
            product_id=product.id,
            qty=qty,
            from_location=warehouse.name,
            to_location=f"В пути на МП (FBO) [{dest}]",
            snapshot_after=snap
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Поставка FBO {qty} шт. «{product.sku}» со склада «{warehouse.name}» на «{dest}» оформлена. Товар в пути на маркетплейс.",
            snapshot=snap
        )

    @staticmethod
    async def accept_fbo_shipment(session: AsyncSession, operation_id: int) -> OperationResult:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse)
        ).where(StockOperation.id == operation_id)
        stmt = apply_for_update(stmt, session)
        res = await session.execute(stmt)
        op = res.scalar_one_or_none()

        if not op:
            return OperationResult(success=False, message="Операция FBO не найдена.")

        if op.op_type != OperationType.FBO:
            return OperationResult(success=False, message="Операция не является поставкой FBO.")

        # Проверка идемпотентности
        if op.status == OperationStatus.COMPLETED:
            return OperationResult(
                success=False,
                already_processed=True,
                operation_id=op.id,
                message=f"Защита от дублей: Поставка FBO #{op.id} уже была принята на МП ранее, повторное подтверждение заблокировано."
            )

        if op.status != OperationStatus.IN_TRANSIT_TO_MP:
            return OperationResult(
                success=False,
                message=f"Подтвердить приёмку на МП можно только для поставки со статусом IN_TRANSIT_TO_MP (текущий: {op.status.value})."
            )

        op.status = OperationStatus.COMPLETED

        dest = op.marketplace_destination or "Склад МП"
        history = MovementHistory(
            operation_id=op.id,
            action_type="FBO_ПРИЕМКА_МП",
            product_id=op.product_id,
            qty=op.qty,
            from_location=f"В пути на МП (FBO) [{dest}]",
            to_location=f"Принято на баланс МП ({dest})",
            snapshot_after=f"Принято на баланс МП ({dest})"
        )
        session.add(history)
        await session.flush()

        return OperationResult(
            success=True,
            operation_id=op.id,
            message=f"Поставка FBO #{op.id} принята маркетплейсом ({dest}). {op.qty} шт. зачислены на баланс МП.",
            snapshot=f"Принято на МП {dest}"
        )

    # -------------------------------------------------------------
    # 5. Обработка действий операций и проверка идемпотентности
    # -------------------------------------------------------------
    @staticmethod
    async def execute_operation_action(
        session: AsyncSession,
        operation_id: int,
        action: str
    ) -> OperationResult:
        action_clean = action.strip().lower()

        # Повторное подтверждение (проверка защиты от дублирования)
        if action_clean in ("test_repeat", "repeat", "idempotency_test"):
            # Определение типа операции и запуск соответствующего подтверждения
            op_stmt = select(StockOperation).where(StockOperation.id == operation_id)
            res = await session.execute(op_stmt)
            op = res.scalar_one_or_none()
            if not op:
                return OperationResult(success=False, message="Операция не найдена.")

            if op.op_type == OperationType.TRANSFER:
                return await WarehouseService.accept_transfer(session, operation_id)
            elif op.op_type == OperationType.FBS:
                if op.status in (OperationStatus.SHIPPED_TO_CLIENT, OperationStatus.COMPLETED):
                    return await WarehouseService.ship_fbs_to_client(session, operation_id)
                elif op.status == OperationStatus.CANCELLED:
                    return await WarehouseService.cancel_fbs_reserve(session, operation_id)
                else:
                    return await WarehouseService.ship_fbs_to_client(session, operation_id)
            elif op.op_type == OperationType.FBO:
                return await WarehouseService.accept_fbo_shipment(session, operation_id)
            elif op.op_type == OperationType.INBOUND:
                return OperationResult(
                    success=False,
                    already_processed=True,
                    operation_id=op.id,
                    message=f"Защита от дублей: Приход #{op.id} уже проведён ранее, остатки не изменились."
                )

        if action_clean == "accept_transfer":
            return await WarehouseService.accept_transfer(session, operation_id)
        elif action_clean == "ship_fbs":
            return await WarehouseService.ship_fbs_to_client(session, operation_id)
        elif action_clean == "cancel_fbs":
            return await WarehouseService.cancel_fbs_reserve(session, operation_id)
        elif action_clean == "deliver_fbs":
            return await WarehouseService.deliver_fbs_order(session, operation_id)
        elif action_clean == "accept_fbo":
            return await WarehouseService.accept_fbo_shipment(session, operation_id)
        else:
            return OperationResult(success=False, message=f"Неизвестное действие: {action}")

    # -------------------------------------------------------------
    # 6. Управление складами и безопасное удаление (Soft Delete)
    # -------------------------------------------------------------
    @staticmethod
    async def create_warehouse(session: AsyncSession, name: str) -> OperationResult:
        name_clean = name.strip()
        if not name_clean:
            return OperationResult(success=False, message="Название склада не может быть пустым.")

        shop = await WarehouseService.get_or_create_shop(session)

        # Проверка существования склада с таким же именем
        existing = await session.execute(
            select(Warehouse).where(
                and_(
                    Warehouse.shop_id == shop.id,
                    Warehouse.name == name_clean
                )
            )
        )
        if existing.scalar_one_or_none():
            return OperationResult(success=False, message=f"Склад с названием «{name_clean}» уже существует.")

        wh = Warehouse(shop_id=shop.id, name=name_clean, is_active=True)
        session.add(wh)
        await session.flush()
        return OperationResult(success=True, message=f"Склад «{name_clean}» успешно создан.")

    @staticmethod
    async def rename_warehouse(session: AsyncSession, warehouse_id: int, new_name: str) -> OperationResult:
        new_name_clean = new_name.strip()
        if not new_name_clean:
            return OperationResult(success=False, message="Новое название склада не может быть пустым.")

        wh = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not wh:
            return OperationResult(success=False, message="Склад не найден.")

        old_name = wh.name
        wh.name = new_name_clean
        await session.flush()
        return OperationResult(
            success=True,
            message=f"Склад «{old_name}» успешно переименован в «{new_name_clean}». Все остатки и история сохранены."
        )

    @staticmethod
    async def soft_delete_warehouse(session: AsyncSession, warehouse_id: int) -> OperationResult:
        wh = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not wh:
            return OperationResult(success=False, message="Склад не найден.")

        if not wh.is_active:
            return OperationResult(
                success=False,
                already_processed=True,
                message=f"Склад «{wh.name}» уже находится в архиве."
            )

        wh.is_active = False
        wh.deleted_at = datetime.now(timezone.utc)
        await session.flush()

        # Подсчёт суммарного физического остатка на складе
        stmt = select(func.sum(StockBalance.physical_qty)).where(StockBalance.warehouse_id == warehouse_id)
        res = await session.execute(stmt)
        total_remaining = res.scalar() or 0

        # Уведомление о сохранении остатков и истории при архивации
        msg = (
            f"Склад «{wh.name}» переведён в архив (удалён из списка для новых приходов). "
            f"На нём числится {total_remaining} шт. товара — остатки и история полностью сохранены."
        )
        return OperationResult(success=True, message=msg)

    @staticmethod
    async def restore_warehouse(session: AsyncSession, warehouse_id: int) -> OperationResult:
        wh = await WarehouseService.get_warehouse_by_id(session, warehouse_id)
        if not wh:
            return OperationResult(success=False, message="Склад не найден.")

        if wh.is_active:
            return OperationResult(
                success=False,
                already_processed=True,
                message=f"Склад «{wh.name}» уже является активным."
            )

        wh.is_active = True
        wh.deleted_at = None
        await session.flush()
        return OperationResult(
            success=True,
            message=f"Склад «{wh.name}» успешно восстановлен из архива и снова доступен для всех операций."
        )

    # -------------------------------------------------------------
    # 7. Запросы аналитики и сводных данных дашборда
    # -------------------------------------------------------------
    @staticmethod
    async def get_kpis(session: AsyncSession) -> KPICards:
        # Суммарный физический остаток на всех складах (активных и архивных)
        phys_stmt = select(func.coalesce(func.sum(StockBalance.physical_qty), 0))
        total_physical = (await session.execute(phys_stmt)).scalar() or 0

        # Доступный остаток на активных складах
        avail_stmt = select(
            func.coalesce(func.sum(StockBalance.physical_qty - StockBalance.reserved_fbs_qty), 0)
        ).join(Warehouse).where(Warehouse.is_active == True)
        total_available = (await session.execute(avail_stmt)).scalar() or 0

        # Суммарный резерв под заказы FBS
        res_stmt = select(func.coalesce(func.sum(StockBalance.reserved_fbs_qty), 0))
        total_reserved = (await session.execute(res_stmt)).scalar() or 0

        # В пути между складами ФФ
        ff_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
            and_(
                StockOperation.op_type == OperationType.TRANSFER,
                StockOperation.status == OperationStatus.IN_TRANSIT
            )
        )
        in_transit_ff = (await session.execute(ff_stmt)).scalar() or 0

        # В пути на склад маркетплейса (FBO)
        fbo_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
            and_(
                StockOperation.op_type == OperationType.FBO,
                StockOperation.status == OperationStatus.IN_TRANSIT_TO_MP
            )
        )
        in_transit_fbo = (await session.execute(fbo_stmt)).scalar() or 0

        # В пути к покупателю (FBS)
        fbs_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
            and_(
                StockOperation.op_type == OperationType.FBS,
                StockOperation.status == OperationStatus.SHIPPED_TO_CLIENT
            )
        )
        in_transit_fbs = (await session.execute(fbs_stmt)).scalar() or 0

        return KPICards(
            total_physical=total_physical,
            total_available=total_available,
            total_reserved_fbs=total_reserved,
            in_transit_ff=in_transit_ff,
            in_transit_fbo=in_transit_fbo,
            in_transit_fbs=in_transit_fbs
        )

    @staticmethod
    async def get_detailed_stock_rows(session: AsyncSession) -> List[StockBalanceRow]:
        stmt = select(StockBalance).options(
            selectinload(StockBalance.warehouse),
            selectinload(StockBalance.product)
        ).join(Warehouse).join(Product).order_by(Product.sku, Warehouse.name)
        res = await session.execute(stmt)
        balances = res.scalars().all()

        rows = []
        for b in balances:
            rows.append(
                StockBalanceRow(
                    balance_id=b.id,
                    warehouse_id=b.warehouse_id,
                    warehouse_name=b.warehouse.name,
                    warehouse_is_active=b.warehouse.is_active,
                    product_id=b.product_id,
                    sku=b.product.sku,
                    product_name=b.product.name,
                    physical_qty=b.physical_qty,
                    reserved_fbs_qty=b.reserved_fbs_qty,
                    available_qty=b.available_qty
                )
            )
        return rows

    @staticmethod
    async def get_sku_summary_rows(session: AsyncSession) -> List[SkuSummaryRow]:
        prod_stmt = select(Product).order_by(Product.sku)
        prod_res = await session.execute(prod_stmt)
        products = prod_res.scalars().all()

        summary_rows = []
        for p in products:
            # Физический и зарезервированный остатки по всем складам
            bal_stmt = select(
                func.coalesce(func.sum(StockBalance.physical_qty), 0),
                func.coalesce(func.sum(StockBalance.reserved_fbs_qty), 0)
            ).where(StockBalance.product_id == p.id)
            bal_res = (await session.execute(bal_stmt)).first()
            sum_phys = bal_res[0] if bal_res else 0
            sum_res = bal_res[1] if bal_res else 0
            sum_avail = sum_phys - sum_res

            # В пути между ФФ
            ff_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
                and_(
                    StockOperation.product_id == p.id,
                    StockOperation.op_type == OperationType.TRANSFER,
                    StockOperation.status == OperationStatus.IN_TRANSIT
                )
            )
            in_transit_ff = (await session.execute(ff_stmt)).scalar() or 0

            # В пути на МП
            fbo_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
                and_(
                    StockOperation.product_id == p.id,
                    StockOperation.op_type == OperationType.FBO,
                    StockOperation.status == OperationStatus.IN_TRANSIT_TO_MP
                )
            )
            in_transit_fbo = (await session.execute(fbo_stmt)).scalar() or 0

            # В пути к клиенту
            fbs_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
                and_(
                    StockOperation.product_id == p.id,
                    StockOperation.op_type == OperationType.FBS,
                    StockOperation.status == OperationStatus.SHIPPED_TO_CLIENT
                )
            )
            in_transit_fbs = (await session.execute(fbs_stmt)).scalar() or 0

            # Принято маркетплейсом
            fbo_acc_stmt = select(func.coalesce(func.sum(StockOperation.qty), 0)).where(
                and_(
                    StockOperation.product_id == p.id,
                    StockOperation.op_type == OperationType.FBO,
                    StockOperation.status == OperationStatus.COMPLETED
                )
            )
            accepted_fbo = (await session.execute(fbo_acc_stmt)).scalar() or 0

            total_company = sum_phys + in_transit_ff + in_transit_fbo + in_transit_fbs + accepted_fbo

            summary_rows.append(
                SkuSummaryRow(
                    product_id=p.id,
                    sku=p.sku,
                    product_name=p.name,
                    sum_physical=sum_phys,
                    sum_reserved=sum_res,
                    sum_available=sum_avail,
                    in_transit_ff=in_transit_ff,
                    in_transit_fbo=in_transit_fbo,
                    in_transit_fbs=in_transit_fbs,
                    accepted_fbo=accepted_fbo,
                    total_company=total_company
                )
            )
        return summary_rows

    @staticmethod
    async def get_archived_stock_warnings(session: AsyncSession) -> List[dict]:
        stmt = select(StockBalance).options(
            selectinload(StockBalance.warehouse),
            selectinload(StockBalance.product)
        ).join(Warehouse).where(
            and_(
                Warehouse.is_active == False,
                StockBalance.physical_qty > 0
            )
        )
        res = await session.execute(stmt)
        balances = res.scalars().all()

        warnings = []
        for b in balances:
            warnings.append({
                "warehouse_id": b.warehouse_id,
                "warehouse_name": b.warehouse.name,
                "product_id": b.product_id,
                "sku": b.product.sku,
                "product_name": b.product.name,
                "physical_qty": b.physical_qty,
                "available_qty": b.available_qty
            })
        return warnings

    @staticmethod
    async def get_active_operations(session: AsyncSession) -> List[StockOperation]:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse),
            selectinload(StockOperation.target_warehouse)
        ).where(
            StockOperation.status.in_([
                OperationStatus.RESERVED,
                OperationStatus.IN_TRANSIT,
                OperationStatus.SHIPPED_TO_CLIENT,
                OperationStatus.IN_TRANSIT_TO_MP
            ])
        ).order_by(StockOperation.id.desc())
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_recent_completed_operations(session: AsyncSession, limit: int = 8) -> List[StockOperation]:
        stmt = select(StockOperation).options(
            selectinload(StockOperation.product),
            selectinload(StockOperation.source_warehouse),
            selectinload(StockOperation.target_warehouse)
        ).where(
            StockOperation.status.in_([
                OperationStatus.COMPLETED,
                OperationStatus.CANCELLED
            ])
        ).order_by(StockOperation.id.desc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_history_records(
        session: AsyncSession,
        sku_filter: Optional[str] = None,
        warehouse_filter: Optional[str] = None,
        limit: int = 150
    ) -> List[MovementHistory]:
        stmt = select(MovementHistory).options(
            selectinload(MovementHistory.product),
            selectinload(MovementHistory.operation)
        ).join(Product)

        if sku_filter and sku_filter.strip():
            stmt = stmt.where(Product.sku == sku_filter.strip())

        if warehouse_filter and warehouse_filter.strip():
            wf = f"%{warehouse_filter.strip()}%"
            stmt = stmt.where(
                or_(
                    MovementHistory.from_location.ilike(wf),
                    MovementHistory.to_location.ilike(wf),
                    MovementHistory.snapshot_after.ilike(wf)
                )
            )

        stmt = stmt.order_by(MovementHistory.id.desc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_all_warehouses(session: AsyncSession) -> List[Warehouse]:
        stmt = select(Warehouse).order_by(Warehouse.is_active.desc(), Warehouse.name)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_active_warehouses(session: AsyncSession) -> List[Warehouse]:
        stmt = select(Warehouse).where(Warehouse.is_active == True).order_by(Warehouse.name)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_all_products(session: AsyncSession) -> List[Product]:
        stmt = select(Product).order_by(Product.sku)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    # -------------------------------------------------------------
    # 8. Инициализация эталонных тестовых данных
    # -------------------------------------------------------------
    @staticmethod
    async def seed_benchmark_data(session: AsyncSession, force_reset: bool = False):
        if force_reset:
            # Очистка таблиц в корректном порядке зависимостей
            await session.execute(delete(MovementHistory))
            await session.execute(delete(StockOperation))
            await session.execute(delete(StockBalance))
            await session.execute(delete(Warehouse))
            await session.execute(delete(Product))
            await session.execute(delete(Shop))
            await session.flush()

        shop_stmt = select(Shop).limit(1)
        shop = (await session.execute(shop_stmt)).scalar_one_or_none()
        if not shop:
            shop = Shop(name="Основной магазин WB")
            session.add(shop)
            await session.flush()

        # Создание эталонных складов
        wh_north_stmt = select(Warehouse).where(Warehouse.name == "Север")
        wh_north = (await session.execute(wh_north_stmt)).scalar_one_or_none()
        if not wh_north:
            wh_north = Warehouse(shop_id=shop.id, name="Север", is_active=True)
            session.add(wh_north)
            await session.flush()
        else:
            wh_north.is_active = True
            wh_north.deleted_at = None

        wh_south_stmt = select(Warehouse).where(Warehouse.name == "Юг")
        wh_south = (await session.execute(wh_south_stmt)).scalar_one_or_none()
        if not wh_south:
            wh_south = Warehouse(shop_id=shop.id, name="Юг", is_active=True)
            session.add(wh_south)
            await session.flush()
        else:
            wh_south.is_active = True
            wh_south.deleted_at = None

        # Эталонные товары и остатки:
        # SKU-101 — Органайзер: Север = 10, Юг = 6
        # SKU-102 — Контейнер: Север = 5, Юг = 0
        # SKU-103 — Корзина: Север = 0, Юг = 4
        products_spec = [
            ("SKU-101", "Органайзер", 10, 6),
            ("SKU-102", "Контейнер", 5, 0),
            ("SKU-103", "Корзина", 0, 4),
        ]

        for sku, name, qty_north, qty_south in products_spec:
            prod_stmt = select(Product).where(Product.sku == sku)
            prod = (await session.execute(prod_stmt)).scalar_one_or_none()
            if not prod:
                prod = Product(sku=sku, name=name)
                session.add(prod)
                await session.flush()

            # Инициализация записей остатков
            bal_north = await WarehouseService.get_or_create_balance(
                session, wh_north.id, prod.id, lock=False
            )
            bal_south = await WarehouseService.get_or_create_balance(
                session, wh_south.id, prod.id, lock=False
            )

            # Оприходование остатков через операции прихода
            if force_reset or (bal_north.physical_qty == 0 and bal_south.physical_qty == 0):
                bal_north.physical_qty = 0
                bal_north.reserved_fbs_qty = 0
                bal_south.physical_qty = 0
                bal_south.reserved_fbs_qty = 0

                if qty_north > 0:
                    await WarehouseService.process_inbound(
                        session=session,
                        warehouse_id=wh_north.id,
                        sku=sku,
                        qty=qty_north,
                        operation_uid=f"INIT-NORTH-{sku}"
                    )
                if qty_south > 0:
                    await WarehouseService.process_inbound(
                        session=session,
                        warehouse_id=wh_south.id,
                        sku=sku,
                        qty=qty_south,
                        operation_uid=f"INIT-SOUTH-{sku}"
                    )

        await session.flush()
