from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from sqlalchemy import select, and_

from app.database import Base, get_db
from app.main import app
from app.models import Warehouse, Product, StockBalance, StockOperation, OperationType, OperationStatus
from app.services import WarehouseService

# Тестовая база SQLite в памяти для быстрых изолированных тестов
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_test_db():
    # Пересоздание таблиц перед каждым тестом
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Заполнение эталонными данными
    async with TestSessionLocal() as session:
        async with session.begin():
            await WarehouseService.seed_benchmark_data(session, force_reset=True)

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def async_client():
    try:
        from httpx import ASGITransport
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    except ImportError:
        async with AsyncClient(app=app, base_url="http://testserver") as client:
            yield client


# =====================================================================
# 1. ТЕСТ НАЧАЛЬНОГО СИДИНГА ЭТАЛОННЫХ ДАННЫХ
# =====================================================================
@pytest.mark.asyncio
async def test_1_initial_benchmark_seeding(async_client: AsyncClient):
    """
    Проверяет начальное состояние остатков складов:
    - SKU-101 Органайзер: Север = 10, Юг = 6 (Всего 16)
    - SKU-102 Контейнер: Север = 5, Юг = 0 (Всего 5)
    - SKU-103 Корзина: Север = 0, Юг = 4 (Всего 4)
    """
    resp = await async_client.get("/api/stock/detailed")
    assert resp.status_code == 200
    rows = resp.json()

    stock_map = {(r["warehouse_name"], r["sku"]): r for r in rows}

    assert stock_map[("Север", "SKU-101")]["physical_qty"] == 10
    assert stock_map[("Север", "SKU-101")]["available_qty"] == 10
    assert stock_map[("Юг", "SKU-101")]["physical_qty"] == 6
    assert stock_map[("Юг", "SKU-101")]["available_qty"] == 6

    assert stock_map[("Север", "SKU-102")]["physical_qty"] == 5
    assert stock_map[("Юг", "SKU-102")]["physical_qty"] == 0

    assert stock_map[("Север", "SKU-103")]["physical_qty"] == 0
    assert stock_map[("Юг", "SKU-103")]["physical_qty"] == 4

    # Проверка сводных показателей KPI
    kpi_resp = await async_client.get("/api/kpis")
    kpis = kpi_resp.json()
    assert kpis["total_physical"] == 25
    assert kpis["total_available"] == 25
    assert kpis["total_reserved_fbs"] == 0


# =====================================================================
# 2. ТЕСТ ПРИХОДА ТОВАРА (INBOUND)
# =====================================================================
@pytest.mark.asyncio
async def test_2_inbound_increases_physical_and_available(async_client: AsyncClient):
    """Приход товара увеличивает physical_qty и available_qty."""
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()

    inbound_resp = await async_client.post(
        "/api/inbound",
        json={"warehouse_id": wh_north.id, "sku": "SKU-102", "qty": 10}
    )
    assert inbound_resp.status_code == 200
    res = inbound_resp.json()
    assert res["success"] is True

    # Проверяем, что на Севере стало 5 + 10 = 15 шт.
    async with TestSessionLocal() as session:
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-102"))).scalar_one()
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 15
        assert bal.available_qty == 15


# =====================================================================
# 3. ПОЛНЫЙ ЦИКЛ FBS (РЕЗЕРВ -> ОТПРАВКА КЛИЕНТУ -> ОТМЕНА РЕЗЕРВА)
# =====================================================================
@pytest.mark.asyncio
async def test_3_fbs_full_lifecycle(async_client: AsyncClient):
    """
    Жизненный цикл заказа FBS:
    1. Резерв блокирует available_qty, не уменьшая physical_qty.
    2. Отправка клиенту списывает physical и reserved, переводя заказ в SHIPPED_TO_CLIENT.
    3. Доставка клиенту переводит операцию в COMPLETED.
    4. Отмена резерва до отправки возвращает available_qty в исходное состояние.
    """
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()

    # Резервирование 3 шт. SKU-101 на складе Север
    fbs_resp = await async_client.post(
        "/api/fbs",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 3, "order_number": "WB-777"}
    )
    assert fbs_resp.status_code == 200
    fbs_res = fbs_resp.json()
    assert fbs_res["success"] is True
    op_id = fbs_res["operation_id"]

    # Проверка остатков: физически 10, в резерве 3, доступно 7
    async with TestSessionLocal() as session:
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 10
        assert bal.reserved_fbs_qty == 3
        assert bal.available_qty == 7

    # Отправка заказа клиенту
    ship_resp = await async_client.post(f"/api/operations/{op_id}/action?action=ship_fbs")
    assert ship_resp.status_code == 200
    assert ship_resp.json()["success"] is True

    # Проверка после отгрузки: физически 7, в резерве 0, доступно 7
    async with TestSessionLocal() as session:
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 7
        assert bal.reserved_fbs_qty == 0
        assert bal.available_qty == 7

        op = (await session.execute(select(StockOperation).where(StockOperation.id == op_id))).scalar_one()
        assert op.status == OperationStatus.SHIPPED_TO_CLIENT

    # Подтверждение доставки клиенту
    deliv_resp = await async_client.post(f"/api/operations/{op_id}/action?action=deliver_fbs")
    assert deliv_resp.status_code == 200
    assert deliv_resp.json()["success"] is True

    # Проверка отмены резерва до отгрузки: резервируем 2 шт. и отменяем
    res2_resp = await async_client.post(
        "/api/fbs",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 2, "order_number": "WB-888"}
    )
    op2_id = res2_resp.json()["operation_id"]

    cancel_resp = await async_client.post(f"/api/operations/{op2_id}/action?action=cancel_fbs")
    assert cancel_resp.status_code == 200

    async with TestSessionLocal() as session:
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 7
        assert bal.reserved_fbs_qty == 0
        assert bal.available_qty == 7


# =====================================================================
# 4. ПОЛНЫЙ ЦИКЛ ПЕРЕМЕЩЕНИЯ МЕЖДУ ФФ (TRANSFER)
# =====================================================================
@pytest.mark.asyncio
async def test_4_transfer_full_lifecycle(async_client: AsyncClient):
    """Отправка с Севера списывает с Севера и переводит в IN_TRANSIT, приёмка на Юге начисляет на Юг."""
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
        wh_south = (await session.execute(select(Warehouse).where(Warehouse.name == "Юг"))).scalar_one()
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()

    # Отправка 4 шт. SKU-101 с Севера на Юг
    trf_resp = await async_client.post(
        "/api/transfer",
        json={
            "source_warehouse_id": wh_north.id,
            "target_warehouse_id": wh_south.id,
            "sku": "SKU-101",
            "qty": 4
        }
    )
    assert trf_resp.status_code == 200
    trf_res = trf_resp.json()
    assert trf_res["success"] is True
    op_id = trf_res["operation_id"]

    # Проверяем остаток на Севере (было 10 -> стало 6) и товар в пути
    async with TestSessionLocal() as session:
        bal_n = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        bal_s = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_south.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal_n.physical_qty == 6
        assert bal_s.physical_qty == 6  # Еще не доехало

        op = (await session.execute(select(StockOperation).where(StockOperation.id == op_id))).scalar_one()
        assert op.status == OperationStatus.IN_TRANSIT

    # Приёмка на Юге
    accept_resp = await async_client.post(f"/api/operations/{op_id}/action?action=accept_transfer")
    assert accept_resp.status_code == 200
    assert accept_resp.json()["success"] is True

    # Проверяем Юг: 6 + 4 = 10 шт.
    async with TestSessionLocal() as session:
        bal_s = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_south.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal_s.physical_qty == 10
        assert bal_s.available_qty == 10


# =====================================================================
# 5. ПОЛНЫЙ ЦИКЛ FBO (ОТПРАВКА НА МП -> ПРИЁМКА НА МП)
# =====================================================================
@pytest.mark.asyncio
async def test_5_fbo_full_lifecycle(async_client: AsyncClient):
    """Отправка списывает с ФФ и переводит в IN_TRANSIT_TO_MP, приёмка закрывает поставку на баланс МП."""
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-102"))).scalar_one()

    # На Севере 5 шт. SKU-102. Отправляем 2 шт. на склад WB Коледино
    fbo_resp = await async_client.post(
        "/api/fbo",
        json={"warehouse_id": wh_north.id, "sku": "SKU-102", "qty": 2, "destination": "WB Коледино"}
    )
    assert fbo_resp.status_code == 200
    op_id = fbo_resp.json()["operation_id"]

    # Проверяем: на Севере осталось 3 шт., поставка в пути
    async with TestSessionLocal() as session:
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 3
        assert bal.available_qty == 3

    # Приёмка на МП
    acc_resp = await async_client.post(f"/api/operations/{op_id}/action?action=accept_fbo")
    assert acc_resp.status_code == 200

    # Проверяем сводку: принято на МП = 2 шт.
    sum_resp = await async_client.get("/api/stock/summary")
    summaries = {s["sku"]: s for s in sum_resp.json()}
    assert summaries["SKU-102"]["accepted_fbo"] == 2
    assert summaries["SKU-102"]["total_company"] == 5


# =====================================================================
# 6. ОБРАБОТКА ДЕФИЦИТА НА СКЛАДЕ ПРИ НАЛИЧИИ СТОКА В СЕТИ
# =====================================================================
@pytest.mark.asyncio
async def test_6_deficit_donor_suggestion_and_global_deficit(async_client: AsyncClient):
    """
    Попытка отправить/зарезервировать 12 шт. SKU-101 со склада Север:
    На Севере только 10, но на Юге есть 6 (суммарно 16 >= 12).
    Операция блокируется, возвращается deficit_info: дефицит 2 шт., донор Юг (6 шт.).
    Также проверяется сценарий глобальной нехватки (запрос 25 шт. при наличии 16 в сети).
    """
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()

    # Попытка зарезервировать 12 шт.
    deficit_resp = await async_client.post(
        "/api/fbs",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 12}
    )
    assert deficit_resp.status_code == 200
    res = deficit_resp.json()
    assert res["success"] is False
    assert res["deficit_info"] is not None

    d = res["deficit_info"]
    assert d["available_on_source"] == 10
    assert d["deficit"] == 2
    assert d["is_resolvable"] is True
    assert len(d["donor_warehouses"]) == 1
    assert d["donor_warehouses"][0]["warehouse_name"] == "Юг"
    assert d["donor_warehouses"][0]["available_qty"] == 6

    # Остаток на Севере не изменился
    async with TestSessionLocal() as session:
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()
        bal = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal.physical_qty == 10
        assert bal.reserved_fbs_qty == 0

    # Проверка глобальной нехватки (запрос 25 шт. при доступных 16 по всей сети)
    global_resp = await async_client.post(
        "/api/fbs",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 25}
    )
    gres = global_resp.json()
    assert gres["success"] is False
    assert gres["deficit_info"]["is_resolvable"] is False
    assert "Глобальная нехватка" in gres["message"]


# =====================================================================
# 7. АРХИВАЦИЯ СКЛАДА С ОСТАТКАМИ (SOFT DELETE) И ВЫВОЗ ТОВАРА
# =====================================================================
@pytest.mark.asyncio
async def test_7_warehouse_soft_delete_preserves_stock_and_allows_evacuation(async_client: AsyncClient):
    """
    Архивация склада Север с остатками (10 шт. SKU-101 и 5 шт. SKU-102):
    - Склад помечается is_active=False.
    - Физического удаления не происходит (строки в БД сохранены).
    - Остатки сохранены.
    - Разрешено оформить перемещение с архивного склада на активный Юг.
    - Приход на удаленный склад блокируется.
    """
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
        wh_south = (await session.execute(select(Warehouse).where(Warehouse.name == "Юг"))).scalar_one()

    # Удаляем склад Север
    del_resp = await async_client.post(f"/api/warehouses/{wh_north.id}/delete")
    assert del_resp.status_code == 200

    # Проверяем, что склад в архиве, но остатки целы
    async with TestSessionLocal() as session:
        wh_check = (await session.execute(select(Warehouse).where(Warehouse.id == wh_north.id))).scalar_one()
        assert wh_check.is_active is False
        assert wh_check.deleted_at is not None

        prod_101 = (await session.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()
        bal_101 = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_north.id, StockBalance.product_id == prod_101.id)
            )
        )).scalar_one()
        assert bal_101.physical_qty == 10

    # Попытка нового прихода на удаленный склад блокируется
    bad_inbound = await async_client.post(
        "/api/inbound",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 5}
    )
    assert bad_inbound.json()["success"] is False
    assert "архиве" in bad_inbound.json()["message"]

    # Вывоз остатков с удаленного Севера на активный Юг РАЗРЕШЕН
    evac_resp = await async_client.post(
        "/api/transfer",
        json={
            "source_warehouse_id": wh_north.id,
            "target_warehouse_id": wh_south.id,
            "sku": "SKU-101",
            "qty": 10
        }
    )
    assert evac_resp.status_code == 200
    assert evac_resp.json()["success"] is True


# =====================================================================
# 8. ПРОВЕРКА ИДЕМПОТЕНТНОСТИ ОПЕРАЦИЙ (ЗАЩИТА ОТ ПОВТОРНОГО СПИСАНИЯ)
# =====================================================================
@pytest.mark.asyncio
async def test_8_idempotency_prevents_duplicate_confirmations(async_client: AsyncClient):
    """
    Двойной вызов подтверждения приёмки перемещения, отправки FBS и приёмки FBO
    НЕ меняет остатки и возвращает already_processed=True.
    """
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
        wh_south = (await session.execute(select(Warehouse).where(Warehouse.name == "Юг"))).scalar_one()
        prod = (await session.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()

    # 1. Перемещение: отправляем 2 шт.
    trf = await async_client.post(
        "/api/transfer",
        json={
            "source_warehouse_id": wh_north.id,
            "target_warehouse_id": wh_south.id,
            "sku": "SKU-101",
            "qty": 2
        }
    )
    op_id = trf.json()["operation_id"]

    # Первый вызов приёмки — успешно (+2 шт. на Юг)
    call1 = await async_client.post(f"/api/operations/{op_id}/action?action=accept_transfer")
    assert call1.status_code == 200
    assert call1.json()["success"] is True

    # Второй повторный вызов — защита от дублей!
    call2 = await async_client.post(f"/api/operations/{op_id}/action?action=accept_transfer")
    assert call2.status_code == 200
    res2 = call2.json()
    assert res2["success"] is False
    assert res2["already_processed"] is True
    assert "Защита от дублей" in res2["message"]

    # Проверяем, что на Юге ровно 6 + 2 = 8 шт., а не 10!
    async with TestSessionLocal() as session:
        bal_s = (await session.execute(
            select(StockBalance).where(
                and_(StockBalance.warehouse_id == wh_south.id, StockBalance.product_id == prod.id)
            )
        )).scalar_one()
        assert bal_s.physical_qty == 8

    # 2. Двойная отправка FBS
    fbs = await async_client.post(
        "/api/fbs",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 1, "order_number": "DUB-1"}
    )
    fbs_op_id = fbs.json()["operation_id"]

    # Первая отправка
    fbs_c1 = await async_client.post(f"/api/operations/{fbs_op_id}/action?action=ship_fbs")
    assert fbs_c1.json()["success"] is True

    # Вторая отправка того же заказа
    fbs_c2 = await async_client.post(f"/api/operations/{fbs_op_id}/action?action=ship_fbs")
    assert fbs_c2.json()["already_processed"] is True

    # 3. Двойная приёмка FBO
    fbo = await async_client.post(
        "/api/fbo",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 1, "destination": "Коледино"}
    )
    fbo_op_id = fbo.json()["operation_id"]

    # Первая приёмка на МП
    fbo_c1 = await async_client.post(f"/api/operations/{fbo_op_id}/action?action=accept_fbo")
    assert fbo_c1.json()["success"] is True

    # Вторая приёмка на МП
    fbo_c2 = await async_client.post(f"/api/operations/{fbo_op_id}/action?action=accept_fbo")
    assert fbo_c2.json()["already_processed"] is True


# =====================================================================
# 9. ТЕСТ ИНВАРИАНТА: ЗАПРЕТ ПЕРЕМЕЩЕНИЯ НА ТОТ ЖЕ СКЛАД
# =====================================================================
@pytest.mark.asyncio
async def test_9_cannot_transfer_to_same_warehouse(async_client: AsyncClient):
    """Запрещено перемещение на тот же самый склад (source == target)."""
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()

    resp = await async_client.post(
        "/api/transfer",
        json={
            "source_warehouse_id": wh_north.id,
            "target_warehouse_id": wh_north.id,
            "sku": "SKU-101",
            "qty": 1
        }
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is False
    assert "Запрещено перемещение на тот же самый склад" in res["message"]


# =====================================================================
# 10. ТЕСТ ВОССТАНОВЛЕНИЯ СКЛАДА ИЗ АРХИВА (RESTORE SOFT-DELETE)
# =====================================================================
@pytest.mark.asyncio
async def test_10_warehouse_restore_lifecycle(async_client: AsyncClient):
    """
    Жизненный цикл восстановления склада из архива:
    1. Архивируем активный склад (is_active=False).
    2. Восстанавливаем склад из архива (is_active=True, deleted_at=None).
    3. Повторная попытка восстановления возвращает already_processed=True.
    4. После восстановления склад снова принимает новые приходы (inbound).
    """
    async with TestSessionLocal() as session:
        wh_north = (await session.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()

    # 1. Архивация
    del_resp = await async_client.post(f"/api/warehouses/{wh_north.id}/delete")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    async with TestSessionLocal() as session:
        wh_archived = (await session.execute(select(Warehouse).where(Warehouse.id == wh_north.id))).scalar_one()
        assert wh_archived.is_active is False
        assert wh_archived.deleted_at is not None

    # 2. Восстановление из архива
    restore_resp = await async_client.post(f"/api/warehouses/{wh_north.id}/restore")
    assert restore_resp.status_code == 200
    res_restore = restore_resp.json()
    assert res_restore["success"] is True
    assert "успешно восстановлен" in res_restore["message"]

    async with TestSessionLocal() as session:
        wh_restored = (await session.execute(select(Warehouse).where(Warehouse.id == wh_north.id))).scalar_one()
        assert wh_restored.is_active is True
        assert wh_restored.deleted_at is None

    # 3. Повторное восстановление (идемпотентность / статус)
    dup_restore = await async_client.post(f"/api/warehouses/{wh_north.id}/restore")
    assert dup_restore.status_code == 200
    assert dup_restore.json()["already_processed"] is True
    assert "уже является активным" in dup_restore.json()["message"]

    # 4. Проверка возможности прихода товара на восстановленный склад
    inbound_resp = await async_client.post(
        "/api/inbound",
        json={"warehouse_id": wh_north.id, "sku": "SKU-101", "qty": 3}
    )
    assert inbound_resp.status_code == 200
    assert inbound_resp.json()["success"] is True

