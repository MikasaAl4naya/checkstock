# Отчёт о финальном предрелизном аудите проекта CheckStock

Проведён комплексный аудит кодовой базы, схем данных, инвариантов бизнес-логики и инфраструктуры проекта.

---

### Шаг 1. Зачистка следов промпта и мусора
1. **Зачистка комментариев и текста:**
   - Во всех файлах (`app/models.py`, `app/services.py`, `app/main.py`, `app/templates/index.html`, `tests/test_warehouse.py`, `README.md`) удалены любые маркеры промпта/ТЗ («КРАЕВОЙ СЦЕНАРИЙ», «25% ОЦЕНКИ», «По ТЗ», «Для ревьюера», «Шаг 1/2» и т.п.).
   - Все формулировки заменены на лаконичные инженерные описания предметной области (архивация складов, идемпотентность операций, обработка локального дефицита, аудит движений).
2. **Файл `.gitignore`:**
   - Создан чистый промышленный `.gitignore` в корне репозитория (включает `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.env`, `.venv/`, `.idea/`, `.vscode/`, `*.db`, `*.sqlite3`, `postgres_data/`, `*.log`).
3. **Отсутствие мёртвого кода и заглушек:**
   - Проверено поиском по всей кодовой базе: заглушки `TODO`, `FIXME` и пустые `pass` отсутствуют.

---

### Шаг 2. Результаты самопроверки по чек-листу бизнес-логики
- **FBS Резерв и Отправка:**
  - При создании резерва `StockBalance.physical_qty` остаётся неизменным, растёт `reserved_fbs_qty += qty`, соответственно `available_qty` падает на `qty`.
  - При отправке клиенту (`ship_fbs`) одновременно списываются `physical_qty -= qty` и `reserved_fbs_qty -= qty` (`available_qty` не изменяется повторно), а статус переходит в `SHIPPED_TO_CLIENT` (блок «В пути клиентам FBS»).
  - При отмене резерва (`cancel_fbs`) уменьшается `reserved_fbs_qty -= qty`, возвращая товар в `available_qty`.
- **Перемещение между ФФ (`TRANSFER`):**
  - При отправке со склада А товар списывается с физического остатка А и получает статус `IN_TRANSIT` («В пути между ФФ»).
  - При приёмке на складе Б блокируется операция, начисляется на физический остаток Б и переводится в `COMPLETED`. Запрещено перемещение на тот же склад (`source_id == target_id`).
- **Поставка FBO:**
  - При отправке списывается с физического остатка ФФ и получает статус `IN_TRANSIT_TO_MP` («В пути на МП (FBO)»).
  - При приёмке маркетплейсом статус переходит в `COMPLETED`, фиксируя товар на балансе маркетплейса.
- **Обработка нехватки (дефицита):**
  - При нехватке на выбранном складе при наличии остатка в сети операция не списывает сток, а возвращает структурированный ответ с расчётом дефицита, списком доноров и быстрой кнопкой оформления перемещения недостающего количества в 1 клик. При глобальной нехватке выводится блокирующий алерт.
- **Архивация склада (Soft Delete):**
  - Физический `DELETE` запрещен. Склад получает `is_active = False` и `deleted_at = utcnow()`.
  - Склад исключается из списков для новых приходов и резервов, но остатки не теряются, помечаются бейджем «УДАЛЁН / АРХИВ», и доступна кнопка вывоза зависшего остатка на активный склад через обычный `TRANSFER`.
- **Идемпотентность:**
  - Все подтверждения защищены конечным автоматом статусов и `SELECT ... FOR UPDATE`. Повторный вызов возвращает `already_processed=True` и жёлтое предупреждение без повторного списания или начисления.
- **Переименование склада:**
  - Корректно сохраняет все внешние ключи, остатки и аудит-лог.

---

### Шаг 3. Прогон тестов и проверка HTTP-ответа
1. **Тесты:** `docker compose exec app pytest -v` → **9 из 9 тестов пройдены успешно за 1.49s**.
2. **HTTP Ответ:** Запрос `GET http://localhost:8000` отдаёт **`200 OK`** с корректным рендерингом Jinja2.

---

### Шаг 4. Материалы для финального ревью

#### 1. Полное дерево файлов проекта
```text
checkstock/
├── app/
│   ├── __init__.py
│   ├── config.py          # Настройки приложения и подключение к БД
│   ├── database.py        # Инициализация движка SQLAlchemy и сессий
│   ├── models.py          # Модели таблиц, CheckConstraint и связи
│   ├── schemas.py         # Pydantic-схемы валидации запросов и ответов
│   ├── services.py        # Бизнес-логика, транзакции и блокировки FOR UPDATE
│   ├── main.py            # Маршруты веб-интерфейса и JSON API
│   └── templates/
│       └── index.html     # Дашборд (Jinja2, HTMX, Tailwind CSS)
├── tests/
│   └── test_warehouse.py  # 9 интеграционных тестов бизнес-логики
├── .gitignore             # Исключение временных и сгенерированных файлов
├── Dockerfile             # Сборка контейнера приложения
├── docker-compose.yml     # Запуск базы PostgreSQL 16 и веб-сервиса
├── requirements.txt       # Зависимости Python
├── pytest.ini             # Конфигурация тестов
├── AUDIT_REPORT.md        # Сводный отчёт по проекту
└── README.md              # Документация
```

---

#### 2. Полный текст файла `README.md`

```markdown
# CheckStock — Где сейчас товар?

Система складского учёта и отслеживания остатков между фулфилментами (ФФ), клиентскими заказами по FBS и поставками FBO на склады маркетплейсов.

Стек: FastAPI, SQLAlchemy 2.0 (asyncpg), PostgreSQL 16, Jinja2, HTMX, Tailwind CSS, Pytest.

---

## 1. Быстрый запуск

Запуск сервисов (PostgreSQL 16 и FastAPI):

```bash
docker compose up --build
```

- Веб-интерфейс: [http://localhost:8000](http://localhost:8000)
- Документация API (Swagger): [http://localhost:8000/docs](http://localhost:8000/docs)

### Запуск тестов

```bash
docker compose exec app pytest -v
```
*(или локально: `pytest -v`)*

---

## 2. Архитектура и движение остатков

### 2.1. Формула остатков
На каждом складе для каждого артикула хранятся два счётчика:
1. `physical_qty` — сколько штук лежит на полке склада (включая собранные, но ещё не отгруженные заказы FBS).
2. `reserved_fbs_qty` — сколько штук заблокировано под оформленные заказы FBS.
3. `available_qty = physical_qty - reserved_fbs_qty` — вычисляемое поле: сколько товара доступно для новых резервов FBS, поставок FBO или перемещений между складами.

| Действие | physical_qty | reserved_fbs_qty | available_qty | Где отображается товар |
|---|---|---|---|---|
| **Приход (INBOUND)** | $+X$ | $0$ | $+X$ | На полке ФФ |
| **Резерв FBS** | $0$ | $+X$ | $-X$ | На полке ФФ (в резерве) |
| **Отправка FBS клиенту** | $-X$ | $-X$ | $0$ | В пути к клиенту FBS |
| **Отмена резерва FBS** | $0$ | $-X$ | $+X$ | На полке ФФ (доступно) |
| **Перемещение: отправка** | $-X$ | $0$ | $-X$ | В пути между ФФ |
| **Перемещение: приёмка** | $+X$ (на целевом) | $0$ | $+X$ (на целевом) | На полке целевого ФФ |
| **FBO: отправка** | $-X$ | $0$ | $-X$ | В пути на склад МП |
| **FBO: приёмка на МП** | $0$ | $0$ | $0$ | Принято на баланс МП |

### 2.2. Защита от отрицательных остатков и взаимоблокировок
- **Ограничения в БД (`CheckConstraint`):**
  - `physical_qty >= 0`
  - `reserved_fbs_qty >= 0`
  - `physical_qty >= reserved_fbs_qty` (резерв не может превышать физический остаток).
- **Пессимистические блокировки (`SELECT ... FOR UPDATE`):** операции с остатками и документами выполняются внутри транзакций `async with session.begin():` с блокировкой строк.
- **Защита от дедлоков:** при одновременной блокировке нескольких складов (например, при перемещении) строки запрашиваются строго по возрастанию `warehouse_id`.

### 2.3. Идемпотентность подтверждений
- У каждой операции (`StockOperation`) есть ключ `operation_uid` и статус.
- При повторном нажатии на кнопку подтверждения (приёмка перемещения, отправка FBS или приёмка FBO) сервис проверяет статус через `FOR UPDATE`. Если операция уже проведена (`COMPLETED` или `SHIPPED_TO_CLIENT`), остатки не меняются повторно, а система возвращает флаг `already_processed=True` и предупреждение:
  > «Защита от дублей: операция #{id} уже проведена ранее, остатки не изменились».
- Для быстрой проверки в интерфейс выведена кнопка «Тест повторного подтверждения (Idempotency Check)».

### 2.4. Мягкое удаление складов (Soft Delete)
- Склады не удаляются физически из базы (`DELETE FROM warehouses` исключён).
- При удалении склад получает статус архива: `is_active = False`, `deleted_at = utcnow()`.
- Товар и история движений сохраняются.
- На архивный склад нельзя оформить новый приход или отправить перемещение.
- При этом остатки с архивного склада можно вывезти на любой действующий склад (в интерфейсе рядом с архивным складом есть кнопка для быстрого перемещения).

### 2.5. Обработка нехватки на складе при наличии остатка в сети
Если запрошено $N$ шт., а на выбранном складе доступно $M < N$:
1. Если суммарно по остальным действующим складам товара хватает:
   - Операция не проводится.
   - В интерфейсе появляется блок с расчётом дефицита и двумя быстрыми действиями:
     - создать перемещение недостающих шт. со склада-донора;
     - провести операцию только на доступные $M$ шт.
2. Если товара не хватает по всей сети:
   - Система блокирует действие и сообщает о необходимости оформить приход от поставщика.

---

## 3. Сценарий проверки в браузере

Откройте [http://localhost:8000](http://localhost:8000) и выполните шаги:

1. **Сверьте стартовые остатки:**
   - `SKU-101 Органайзер`: Север = 10, Юг = 6 (всего 16).
   - `SKU-102 Контейнер`: Север = 5, Юг = 0 (всего 5).
   - `SKU-103 Корзина`: Север = 0, Юг = 4 (всего 4).
   *(Кнопка «Сбросить БД к эталону» в шапке возвращает эти значения).*

2. **Проверьте резерв и отправку FBS:**
   - В блоке «Заказ FBS» выберите склад «Север», артикул «SKU-101», 3 шт., заказ 1001 и нажмите «Зарезервировать под заказ FBS».
   - На складе «Север»: Физически = 10, В резерве = 3, Доступно = 7.
   - В таблице «Активные операции» нажмите «Отправить клиенту».
   - На складе «Север»: Физически = 7, В резерве = 0, Доступно = 7. В карточке «В пути к клиенту FBS» отображаются 3 шт.

3. **Проверьте подсказку донора при нехватке:**
   - В блоке «Поставка FBO» выберите склад «Север», товар «SKU-101», укажите 10 шт. (доступно 7, а на Юге лежит 6).
   - Появится блок с сообщением: «На складе «Север» доступно только 7 шт. (не хватает 3 шт.). Но этот товар доступен на других ФФ: Юг — 6 шт.».
   - Нажмите кнопку «Оформить перемещение недостающих 3 шт. со склада «Юг» на склад «Север»».
   - 3 шт. перейдут в статус «В пути между ФФ».

4. **Проверьте архивацию склада с остатком:**
   - В списке складов нажмите «Удалить» у склада «Юг».
   - Появится уведомление: «Склад «Юг» переведён в архив. На нём числится товар — остатки и история полностью сохранены.».
   - Склад «Юг» получит бейдж «УДАЛЁН / АРХИВ», его остатки останутся в таблице, а вверху отобразится баннер с возможностью вывоза.

5. **Проверьте защиту от повторного списания:**
   - В таблице «Защита от повторного подтверждения» нажмите кнопку «Тест повторного подтверждения (Idempotency Check)» у любой завершённой операции.
   - Появится жёлтое предупреждение: «Защита от дублей: операция #ID уже проведена ранее, остатки не изменились». Числа на остатках останутся прежними.

---

## 4. Структура проекта

```text
checkstock/
├── app/
│   ├── __init__.py
│   ├── config.py          # Настройки приложения и подключение к БД
│   ├── database.py        # Инициализация движка SQLAlchemy и сессий
│   ├── models.py          # Модели таблиц, CheckConstraint и связи
│   ├── schemas.py         # Pydantic-схемы валидации запросов и ответов
│   ├── services.py        # Бизнес-логика, транзакции и блокировки FOR UPDATE
│   ├── main.py            # Маршруты веб-интерфейса и JSON API
│   └── templates/
│       └── index.html     # Дашборд (Jinja2, HTMX, Tailwind CSS)
├── tests/
│   └── test_warehouse.py  # 9 интеграционных тестов бизнес-логики
├── .gitignore             # Исключение временных и сгенерированных файлов
├── Dockerfile             # Сборка контейнера приложения
├── docker-compose.yml     # Запуск базы PostgreSQL 16 и веб-сервиса
├── requirements.txt       # Зависимости Python
├── pytest.ini             # Конфигурация тестов
├── AUDIT_REPORT.md        # Сводный отчёт по проекту
└── README.md              # Документация
```
```

---

#### 3. Полный текст файла `app/models.py`

```python
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
```

---

#### 4. Ключевые функции бизнес-логики из `app/services.py`

##### А. Расчёт и выявление дефицита (`check_deficit`):
```python
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
```

##### Б. Идемпотентная приёмка перемещения с блокировкой строк (`accept_transfer`):
```python
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
```

##### В. Резервирование и отправка FBS (`create_fbs_reserve`, `ship_fbs_to_client`):
```python
@staticmethod
async def create_fbs_reserve(
    session: AsyncSession,
    warehouse_id: int,
    sku: str,
    qty: int,
    order_number: Optional[str] = "1001",
    operation_uid: Optional[str] = None
) -> OperationResult:
    balance = await WarehouseService.get_or_create_balance(
        session, warehouse.id, product.id, lock=True
    )

    if balance.available_qty < qty:
        deficit_info = await WarehouseService.check_deficit(...)
        return OperationResult(success=False, deficit_info=deficit_info, message=...)

    # Резервируем без списания physical_qty
    balance.reserved_fbs_qty += qty
    return OperationResult(success=True, operation_id=op.id, message=..., snapshot=snap)

@staticmethod
async def ship_fbs_to_client(session: AsyncSession, operation_id: int) -> OperationResult:
    stmt = select(StockOperation).where(StockOperation.id == operation_id)
    stmt = apply_for_update(stmt, session)
    op = (await session.execute(stmt)).scalar_one_or_none()

    if op.status in (OperationStatus.SHIPPED_TO_CLIENT, OperationStatus.COMPLETED):
        return OperationResult(
            success=False,
            already_processed=True,
            operation_id=op.id,
            message=f"Защита от дублей: Заказ FBS #{op.id} уже был отправлен клиенту ранее. Повторное списание заблокировано."
        )

    balance = await WarehouseService.get_or_create_balance(
        session, op.source_warehouse_id, op.product_id, lock=True
    )
    # Одновременное списание physical и reserved
    balance.physical_qty -= op.qty
    balance.reserved_fbs_qty -= op.qty
    op.status = OperationStatus.SHIPPED_TO_CLIENT
    return OperationResult(success=True, operation_id=op.id, message=..., snapshot=snap)
```

##### Г. Безопасное мягкое удаление склада (`soft_delete_warehouse`):
```python
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

    stmt = select(func.sum(StockBalance.physical_qty)).where(StockBalance.warehouse_id == warehouse_id)
    res = await session.execute(stmt)
    total_remaining = res.scalar() or 0

    msg = (
        f"Склад «{wh.name}» переведён в архив (удалён из списка для новых приходов). "
        f"На нём числится {total_remaining} шт. товара — остатки и история полностью сохранены."
    )
    return OperationResult(success=True, message=msg)
```

---

#### 5. Вывод терминала с прохождением `pytest -v`

```text
root@checkstock-app-1:/app# pytest -v
============================= test session starts ==============================
platform linux -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0 -- /usr/local/bin/python3.12
cachedir: .pytest_cache
rootdir: /app
configfile: pytest.ini
plugins: anyio-4.16.0, asyncio-1.5.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 9 items

tests/test_warehouse.py::test_1_initial_benchmark_seeding PASSED         [ 11%]
tests/test_warehouse.py::test_2_inbound_increases_physical_and_available PASSED [ 22%]
tests/test_warehouse.py::test_3_fbs_full_lifecycle PASSED                [ 33%]
tests/test_warehouse.py::test_4_transfer_full_lifecycle PASSED           [ 44%]
tests/test_warehouse.py::test_5_fbo_full_lifecycle PASSED                [ 55%]
tests/test_warehouse.py::test_6_deficit_donor_suggestion_and_global_deficit PASSED [ 66%]
tests/test_warehouse.py::test_7_warehouse_soft_delete_preserves_stock_and_allows_evacuation PASSED [ 77%]
tests/test_warehouse.py::test_8_idempotency_prevents_duplicate_confirmations PASSED [ 88%]
tests/test_warehouse.py::test_9_cannot_transfer_to_same_warehouse PASSED [100%]

============================== 9 passed in 1.49s ===============================
```
