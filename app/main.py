from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional, List
from urllib.parse import urlencode

from fastapi import FastAPI, Depends, Request, Form, Query, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine, Base, get_db, async_session_factory
from app.models import (
    OperationType,
    OperationStatus,
    Warehouse,
    Product,
    StockOperation,
)
from app.schemas import (
    InboundRequest,
    TransferRequest,
    FBSReserveRequest,
    FBOShipmentRequest,
    WarehouseCreate,
    WarehouseUpdate,
    OperationResult,
    KPICards,
    StockBalanceRow,
    SkuSummaryRow,
)
from app.services import WarehouseService


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создание таблиц при запуске
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Инициализация начальных данных
    async with async_session_factory() as session:
        async with session.begin():
            await WarehouseService.seed_benchmark_data(session, force_reset=False)

    yield

    # Завершение работы соединения с БД
    await engine.dispose()


app = FastAPI(
    title="CheckStock — Где сейчас товар?",
    description="Промышленная система складского учёта и отслеживания перемещений товара",
    version="1.0.0",
    lifespan=lifespan
)

# Настройка шаблонов Jinja2
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


# -----------------------------------------------------------------
# Главная страница дашборда
# -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index_view(
    request: Request,
    msg: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    sku_filter: Optional[str] = Query(None),
    warehouse_filter: Optional[str] = Query(None),
    # Параметры разрешения дефицита
    deficit_sku: Optional[str] = Query(None),
    deficit_donor_id: Optional[int] = Query(None),
    deficit_donor_name: Optional[str] = Query(None),
    deficit_amount: Optional[int] = Query(None),
    deficit_source_id: Optional[int] = Query(None),
    deficit_source_name: Optional[str] = Query(None),
    deficit_available_source: Optional[int] = Query(None),
    deficit_op_type: Optional[str] = Query(None),
    deficit_dest: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    kpis = await WarehouseService.get_kpis(db)
    detailed_rows = await WarehouseService.get_detailed_stock_rows(db)
    summary_rows = await WarehouseService.get_sku_summary_rows(db)
    archived_warnings = await WarehouseService.get_archived_stock_warnings(db)
    active_operations = await WarehouseService.get_active_operations(db)
    recent_completed = await WarehouseService.get_recent_completed_operations(db)
    history_records = await WarehouseService.get_history_records(
        db, sku_filter=sku_filter, warehouse_filter=warehouse_filter
    )
    all_warehouses = await WarehouseService.get_all_warehouses(db)
    active_warehouses = await WarehouseService.get_active_warehouses(db)
    all_products = await WarehouseService.get_all_products(db)

    deficit_context = None
    if alert_type == "deficit" and deficit_sku:
        deficit_context = {
            "sku": deficit_sku,
            "donor_id": deficit_donor_id,
            "donor_name": deficit_donor_name,
            "amount": deficit_amount,
            "source_id": deficit_source_id,
            "source_name": deficit_source_name,
            "available_source": deficit_available_source,
            "op_type": deficit_op_type,
            "destination": deficit_dest
        }

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "kpis": kpis,
            "detailed_rows": detailed_rows,
            "summary_rows": summary_rows,
            "archived_warnings": archived_warnings,
            "active_operations": active_operations,
            "recent_completed": recent_completed,
            "history_records": history_records,
            "all_warehouses": all_warehouses,
            "active_warehouses": active_warehouses,
            "all_products": all_products,
            "msg": msg,
            "alert_type": alert_type,
            "deficit": deficit_context,
            "sku_filter": sku_filter or "",
            "warehouse_filter": warehouse_filter or ""
        }
    )


# -----------------------------------------------------------------
# Обработчики веб-форм
# -----------------------------------------------------------------
def redirect_with_alert(
    msg: str,
    alert_type: str = "success",
    deficit_info=None
) -> RedirectResponse:
    params = {"msg": msg, "alert_type": alert_type}
    if deficit_info and deficit_info.is_resolvable and deficit_info.donor_warehouses:
        best_donor = deficit_info.donor_warehouses[0]
        params.update({
            "deficit_sku": deficit_info.sku,
            "deficit_donor_id": best_donor.warehouse_id,
            "deficit_donor_name": best_donor.warehouse_name,
            "deficit_amount": deficit_info.deficit,
            "deficit_source_id": deficit_info.source_warehouse_id,
            "deficit_source_name": deficit_info.source_warehouse_name,
            "deficit_available_source": deficit_info.available_on_source,
            "deficit_op_type": deficit_info.original_op_type,
            "deficit_dest": deficit_info.destination or ""
        })
    query = urlencode(params)
    return RedirectResponse(url=f"/?{query}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/web/inbound")
async def web_inbound(
    warehouse_id: int = Form(...),
    sku: str = Form(...),
    qty: int = Form(...),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.process_inbound(
            session=db,
            warehouse_id=warehouse_id,
            sku=sku,
            qty=qty
        )
    return redirect_with_alert(
        msg=res.message,
        alert_type="success" if res.success else "error"
    )


@app.post("/web/transfer")
async def web_transfer(
    source_warehouse_id: int = Form(...),
    target_warehouse_id: int = Form(...),
    sku: str = Form(...),
    qty: int = Form(...),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.start_transfer(
            session=db,
            source_warehouse_id=source_warehouse_id,
            target_warehouse_id=target_warehouse_id,
            sku=sku,
            qty=qty
        )
    if res.deficit_info:
        alert_type = "deficit" if res.deficit_info.is_resolvable else "error"
        return redirect_with_alert(msg=res.message, alert_type=alert_type, deficit_info=res.deficit_info)
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/web/fbs")
async def web_fbs(
    warehouse_id: int = Form(...),
    sku: str = Form(...),
    qty: int = Form(...),
    order_number: Optional[str] = Form("1001"),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.create_fbs_reserve(
            session=db,
            warehouse_id=warehouse_id,
            sku=sku,
            qty=qty,
            order_number=order_number
        )
    if res.deficit_info:
        alert_type = "deficit" if res.deficit_info.is_resolvable else "error"
        return redirect_with_alert(msg=res.message, alert_type=alert_type, deficit_info=res.deficit_info)
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/web/fbo")
async def web_fbo(
    warehouse_id: int = Form(...),
    sku: str = Form(...),
    qty: int = Form(...),
    destination: str = Form("Склад WB Коледино"),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.start_fbo_shipment(
            session=db,
            warehouse_id=warehouse_id,
            sku=sku,
            qty=qty,
            destination=destination
        )
    if res.deficit_info:
        alert_type = "deficit" if res.deficit_info.is_resolvable else "error"
        return redirect_with_alert(msg=res.message, alert_type=alert_type, deficit_info=res.deficit_info)
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/operations/{op_id}/action")
async def execute_action(
    op_id: int,
    action: str = Form("accept_transfer"),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.execute_operation_action(
            session=db,
            operation_id=op_id,
            action=action
        )
    if res.already_processed:
        return redirect_with_alert(msg=res.message, alert_type="warning")
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/web/warehouses")
async def web_create_warehouse(
    name: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.create_warehouse(session=db, name=name)
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/web/warehouses/{warehouse_id}/rename")
async def web_rename_warehouse(
    warehouse_id: int,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.rename_warehouse(session=db, warehouse_id=warehouse_id, new_name=name)
    return redirect_with_alert(msg=res.message, alert_type="success" if res.success else "error")


@app.post("/web/warehouses/{warehouse_id}/delete")
async def web_delete_warehouse(
    warehouse_id: int,
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.soft_delete_warehouse(session=db, warehouse_id=warehouse_id)
    return redirect_with_alert(
        msg=res.message,
        alert_type="warning" if res.already_processed else "success"
    )


@app.post("/web/warehouses/{warehouse_id}/restore")
async def web_restore_warehouse(
    warehouse_id: int,
    db: AsyncSession = Depends(get_db)
):
    async with db.begin():
        res = await WarehouseService.restore_warehouse(session=db, warehouse_id=warehouse_id)
    return redirect_with_alert(
        msg=res.message,
        alert_type="warning" if res.already_processed else "success"
    )


@app.post("/reset-benchmark")
async def web_reset_benchmark(db: AsyncSession = Depends(get_db)):
    async with db.begin():
        await WarehouseService.seed_benchmark_data(session=db, force_reset=True)
    return redirect_with_alert(
        msg="База данных успешно сброшена к эталонным тестовым данным (Север: 10/5/0, Юг: 6/0/4)!",
        alert_type="success"
    )


# -----------------------------------------------------------------
# Эндпоинты API для внешних клиентов и тестов
# -----------------------------------------------------------------
@app.get("/api/kpis", response_model=KPICards)
async def api_get_kpis(db: AsyncSession = Depends(get_db)):
    return await WarehouseService.get_kpis(db)


@app.get("/api/stock/detailed", response_model=List[StockBalanceRow])
async def api_get_detailed(db: AsyncSession = Depends(get_db)):
    return await WarehouseService.get_detailed_stock_rows(db)


@app.get("/api/stock/summary", response_model=List[SkuSummaryRow])
async def api_get_summary(db: AsyncSession = Depends(get_db)):
    return await WarehouseService.get_sku_summary_rows(db)


@app.post("/api/inbound", response_model=OperationResult)
async def api_inbound(payload: InboundRequest, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.process_inbound(
            session=db,
            warehouse_id=payload.warehouse_id,
            sku=payload.sku,
            qty=payload.qty,
            operation_uid=payload.operation_uid
        )


@app.post("/api/transfer", response_model=OperationResult)
async def api_transfer(payload: TransferRequest, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.start_transfer(
            session=db,
            source_warehouse_id=payload.source_warehouse_id,
            target_warehouse_id=payload.target_warehouse_id,
            sku=payload.sku,
            qty=payload.qty,
            operation_uid=payload.operation_uid
        )


@app.post("/api/fbs", response_model=OperationResult)
async def api_fbs(payload: FBSReserveRequest, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.create_fbs_reserve(
            session=db,
            warehouse_id=payload.warehouse_id,
            sku=payload.sku,
            qty=payload.qty,
            order_number=payload.order_number,
            operation_uid=payload.operation_uid
        )


@app.post("/api/fbo", response_model=OperationResult)
async def api_fbo(payload: FBOShipmentRequest, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.start_fbo_shipment(
            session=db,
            warehouse_id=payload.warehouse_id,
            sku=payload.sku,
            qty=payload.qty,
            destination=payload.destination,
            operation_uid=payload.operation_uid
        )


@app.post("/api/operations/{op_id}/action", response_model=OperationResult)
async def api_action(op_id: int, action: str = Query("accept_transfer"), db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.execute_operation_action(
            session=db,
            operation_id=op_id,
            action=action
        )


@app.post("/api/warehouses", response_model=OperationResult)
async def api_create_warehouse(payload: WarehouseCreate, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.create_warehouse(session=db, name=payload.name)


@app.post("/api/warehouses/{warehouse_id}/delete", response_model=OperationResult)
async def api_delete_warehouse(warehouse_id: int, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.soft_delete_warehouse(session=db, warehouse_id=warehouse_id)


@app.post("/api/warehouses/{warehouse_id}/restore", response_model=OperationResult)
async def api_restore_warehouse(warehouse_id: int, db: AsyncSession = Depends(get_db)):
    async with db.begin():
        return await WarehouseService.restore_warehouse(session=db, warehouse_id=warehouse_id)


@app.post("/api/reset-benchmark", response_model=OperationResult)
async def api_reset_benchmark(db: AsyncSession = Depends(get_db)):
    async with db.begin():
        await WarehouseService.seed_benchmark_data(session=db, force_reset=True)
    return OperationResult(
        success=True,
        message="База данных сброшена к эталонным тестовым данным."
    )
