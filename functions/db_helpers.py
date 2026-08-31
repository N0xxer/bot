from typing import Any, Optional, Dict
import aiosqlite
import os
import json

DB_PATH = "dbs/main.db"

ALLOWED_USER_FIELDS = {
    "user_id",
    "balance",
    "FIO",
    "functions",
    "photo",
    "last_collection",
    "last_work",
    "mandates"
}


async def init_db():
    """Создает необходимые таблицы при первом запуске бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                FIO TEXT DEFAULT '',
                functions TEXT DEFAULT NULL,
                photo TEXT DEFAULT NULL,
                last_collection INTEGER DEFAULT NULL,
                last_work INTEGER DEFAULT NULL,
                mandates INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                balance INTEGER DEFAULT 0,
                forum_id INTEGER DEFAULT NULL,
                company_type TEXT DEFAULT 'Бизнес'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS country (
                name TEXT PRIMARY KEY,
                population INTEGER DEFAULT 0,
                GDP INTEGER DEFAULT 0,
                value TEXT DEFAULT NULL,
                inflation REAL DEFAULT 0.0,
                unemployment REAL DEFAULT 0.0,
                tension REAL DEFAULT 0.0,
                stability REAL DEFAULT 0.0,
                legitimacy REAL DEFAULT 0.0,
                election_date TEXT DEFAULT NULL,
                president INTEGER DEFAULT NULL,
                date TEXT DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS regions (
                name TEXT PRIMARY KEY,
                population INTEGER DEFAULT 0,
                GDP INTEGER DEFAULT 0,
                inflation REAL DEFAULT 0.0,
                unemployment REAL DEFAULT 0.0,
                tension REAL DEFAULT 0.0,
                stability REAL DEFAULT 0.0,
                legitimacy REAL DEFAULT 0.0,
                election_date TEXT DEFAULT NULL,
                owner INTEGER DEFAULT NULL,
                balance INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS votings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                votes TEXT DEFAULT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS law_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                law_name TEXT NOT NULL,
                law_type TEXT NOT NULL,
                law_text TEXT NOT NULL,
                law_comment TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER,
                reject_reason TEXT,
                voting_poll_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def is_user_registered(user_id: int) -> bool:
    """Проверяет, зарегистрирован ли пользователь в базе данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT FIO FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row[0])


async def get_user_info(user_id: int, info_type: str) -> Any:
    """Получает информацию о пользователе из БД без создания новой записи."""
    if info_type not in ALLOWED_USER_FIELDS:
        raise ValueError(f"Недопустимое поле: {info_type}")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {info_type} FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row is not None else None


async def update_user_info(user_id: int, info_type: str, value: Any):
    """Обновляет информацию о пользователе."""
    if info_type not in ALLOWED_USER_FIELDS:
        raise ValueError(f"Недопустимое поле: {info_type}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {info_type} = ? WHERE user_id = ?", 
            (value, user_id)
        )
        await db.commit()


async def get_all_registered_users():
    """Возвращает список всех зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, FIO FROM users WHERE FIO != '' AND FIO IS NOT NULL") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ==========================================
#         ФУНКЦИИ ДЛЯ КОМПАНИЙ
# ==========================================


async def create_company_db(name: str, owner_id: int, forum_id: int, company_type: str) -> int:
    """Создает запись о компании и возвращает ее ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO companies (name, owner_id, balance, forum_id, company_type)
            VALUES (?, ?, 0, ?, ?)
            """,
            (name, owner_id, forum_id, company_type)
        )
        company_id = cursor.lastrowid
        await db.commit()
        return company_id


async def get_company_by_id(company_id: int) -> Optional[Dict[str, Any]]:
    """Получает данные компании по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def delete_company_db(company_id: int):
    """Удаляет компанию из базы данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        await db.commit()


async def update_company_balance(company_id: int, new_balance: int):
    """Обновляет баланс компании."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE companies SET balance = ? WHERE id = ?",
            (new_balance, company_id)
        )
        await db.commit()


async def get_all_companies():
    """Возвращает список всех зарегистрированных компаний."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, company_type, owner_id, balance FROM companies") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ==========================================
#         ФУНКЦИИ ДЛЯ РЕГИОНОВ
# ==========================================


async def get_region_by_name(region_name: str) -> Optional[Dict[str, Any]]:
    """Получает данные региона по названию."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM regions WHERE name = ?", (region_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None



async def update_region_balance(region_name: str, new_balance: int):
    """Обновляет баланс региона."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE regions SET balance = ? WHERE name = ?",
            (new_balance, region_name)
        )
        await db.commit()


async def init_regions_from_config():
    """Создает записи для каждого региона из конфига при старте."""
    config_path = "configs/regions_config.json"
    if not os.path.exists(config_path):
        return

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        regions_list = data.get("regions", [])

    async with aiosqlite.connect(DB_PATH) as db:
        for reg_name in regions_list:
            await db.execute(
                """
                INSERT INTO regions (name, balance, population, GDP, inflation, unemployment, tension, stability, legitimacy)
                VALUES (?, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
                ON CONFLICT(name) DO NOTHING
                """,
                (reg_name,)
            )
        await db.commit()


async def update_region_owner(region_name: str, owner_id: Optional[int]):
    """Закрепляет ID игрока за лидерством в регионе."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE regions SET owner = ? WHERE name = ?",
            (owner_id, region_name)
        )
        await db.commit()


# ==========================================
#         ФУНКЦИИ ДЛЯ ПОЛИТИКИ
# ==========================================


async def create_law_proposal(author_id: int, law_name: str, law_type: str, law_text: str, law_comment: Optional[str] = None) -> int:
    """Создает запись о новом законопроекте в БД и возвращает его ID."""
    async with aiosqlite.connect("dbs/main.db") as db:
        cursor = await db.execute(
            """
            INSERT INTO law_proposals (author_id, law_name, law_type, law_text, law_comment)
            VALUES (?, ?, ?, ?, ?)
            """,
            (author_id, law_name, law_type, law_text, law_comment)
        )
        await db.commit()
        return cursor.lastrowid

async def get_law_proposal(proposal_id: int) -> Optional[dict]:
    """Получает данные о законопроекте по его ID."""
    async with aiosqlite.connect("dbs/main.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM law_proposals WHERE id = ?", (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def update_law_proposal_status(proposal_id: int, status: str, reviewed_by: int, reject_reason: Optional[str] = None, voting_poll_id: Optional[int] = None):
    """Обновляет статус законопроекта (accepted / denied)."""
    async with aiosqlite.connect("dbs/main.db") as db:
        await db.execute(
            """
            UPDATE law_proposals
            SET status = ?, reviewed_by = ?, reject_reason = ?, voting_poll_id = ?
            WHERE id = ?
            """,
            (status, reviewed_by, reject_reason, voting_poll_id, proposal_id)
        )
        await db.commit()