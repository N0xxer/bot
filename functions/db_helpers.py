import time
from typing import Any, Optional, Dict
import aiosqlite
import os
import json
from pathlib import Path
import random

# Приоритет переменной окружения хостинга (/app/data/main.db), локально — data/main.db
DB_PATH = os.getenv("DATABASE_PATH", str(Path("data/main.db").resolve()))

# Гарантируем создание папки для базы данных
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

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


async def create_company_db(name: str, owner_id: int, forum_id: Optional[int], company_type: str) -> int:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM law_proposals WHERE id = ?", (proposal_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_law_proposal_status(proposal_id: int, status: str, reviewed_by: int, reject_reason: Optional[str] = None, voting_poll_id: Optional[int] = None):
    """Обновляет статус законопроекта (accepted / denied)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE law_proposals
            SET status = ?, reviewed_by = ?, reject_reason = ?, voting_poll_id = ?
            WHERE id = ?
            """,
            (status, reviewed_by, reject_reason, voting_poll_id, proposal_id)
        )
        await db.commit()


# ==========================================
#              БАНКОВСКАЯ СИСТЕМА
# ==========================================



async def init_bank_db():
    """Инициализация расширенных таблиц банковской системы."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL DEFAULT 'коммерческий',
                owner_id INTEGER,
                balance INTEGER NOT NULL DEFAULT 0,
                interest_rate REAL NOT NULL DEFAULT 5.0,
                control_channel_id INTEGER DEFAULT NULL,
                log_channel_id INTEGER DEFAULT NULL,
                control_message_id INTEGER DEFAULT NULL,
                min_loan_amount INTEGER NOT NULL DEFAULT 100,
                max_loan_amount INTEGER NOT NULL DEFAULT 1000000,
                loan_approval_threshold INTEGER NOT NULL DEFAULT 100000,
                max_loans_per_user INTEGER NOT NULL DEFAULT 1,
                is_national BOOLEAN NOT NULL DEFAULT 0,
                max_accounts_per_user INTEGER NOT NULL DEFAULT 3,
                deposits_enabled BOOLEAN NOT NULL DEFAULT 0,
                deposit_interest_rate REAL NOT NULL DEFAULT 3.0,
                min_deposit_amount INTEGER NOT NULL DEFAULT 1000
            )
        """)

        # Безопасная миграция для уже существующей таблицы banks
        async with db.execute("PRAGMA table_info(banks)") as cursor:
            existing_cols = {row[1] for row in await cursor.fetchall()}

        bank_migrations = {
            "min_loan_amount": "INTEGER NOT NULL DEFAULT 100",
            "max_loan_amount": "INTEGER NOT NULL DEFAULT 1000000",
            "loan_approval_threshold": "INTEGER NOT NULL DEFAULT 100000",
            "max_loans_per_user": "INTEGER NOT NULL DEFAULT 1",
            "is_national": "BOOLEAN NOT NULL DEFAULT 0",
            "max_accounts_per_user": "INTEGER NOT NULL DEFAULT 3",
            "deposits_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "deposit_interest_rate": "REAL NOT NULL DEFAULT 3.0",
            "min_deposit_amount": "INTEGER NOT NULL DEFAULT 1000"
        }
        for col_name, col_def in bank_migrations.items():
            if col_name not in existing_cols:
                await db.execute(f"ALTER TABLE banks ADD COLUMN {col_name} {col_def}")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_accounts (
                account_number TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                bank_id INTEGER NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                is_frozen BOOLEAN NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                account_name TEXT DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (bank_id) REFERENCES banks(id)
            )
        """)
        # Миграция для bank_accounts (account_name)
        async with db.execute("PRAGMA table_info(bank_accounts)") as cursor:
            existing_acc_cols = {row[1] for row in await cursor.fetchall()}
        if "account_name" not in existing_acc_cols:
            await db.execute("ALTER TABLE bank_accounts ADD COLUMN account_name TEXT DEFAULT NULL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bank_id INTEGER NOT NULL,
                account_number TEXT NOT NULL,
                original_amount INTEGER NOT NULL,
                total_debt INTEGER NOT NULL,
                remaining_debt INTEGER NOT NULL,
                period_payment INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                next_payment_time INTEGER NOT NULL,
                is_closed BOOLEAN NOT NULL DEFAULT 0,
                borrower_bank_id INTEGER DEFAULT NULL,
                FOREIGN KEY (account_number) REFERENCES bank_accounts(account_number),
                FOREIGN KEY (bank_id) REFERENCES banks(id)
            )
        """)
        # Миграция для bank_loans (borrower_bank_id)
        async with db.execute("PRAGMA table_info(bank_loans)") as cursor:
            existing_loan_cols = {row[1] for row in await cursor.fetchall()}
        if "borrower_bank_id" not in existing_loan_cols:
            await db.execute("ALTER TABLE bank_loans ADD COLUMN borrower_bank_id INTEGER DEFAULT NULL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_loan_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bank_id INTEGER NOT NULL,
                account_number TEXT NOT NULL,
                amount INTEGER NOT NULL,
                days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                reviewed_by INTEGER DEFAULT NULL,
                message_id INTEGER DEFAULT NULL,
                borrower_bank_id INTEGER DEFAULT NULL,
                FOREIGN KEY (account_number) REFERENCES bank_accounts(account_number),
                FOREIGN KEY (bank_id) REFERENCES banks(id)
            )
        """)
        # Миграция для bank_loan_requests (borrower_bank_id)
        async with db.execute("PRAGMA table_info(bank_loan_requests)") as cursor:
            existing_req_cols = {row[1] for row in await cursor.fetchall()}
        if "borrower_bank_id" not in existing_req_cols:
            await db.execute("ALTER TABLE bank_loan_requests ADD COLUMN borrower_bank_id INTEGER DEFAULT NULL")

        # Таблица депозитов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bank_id INTEGER NOT NULL,
                account_number TEXT NOT NULL,
                amount INTEGER NOT NULL,
                interest_rate REAL NOT NULL,
                created_at INTEGER NOT NULL,
                last_payout_time INTEGER NOT NULL,
                is_closed BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY (account_number) REFERENCES bank_accounts(account_number),
                FOREIGN KEY (bank_id) REFERENCES banks(id)
            )
        """)

        # Таблица чёрного списка банков
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(bank_id, user_id),
                FOREIGN KEY (bank_id) REFERENCES banks(id)
            )
        """)

        await db.commit()

async def generate_unique_account_number(bank_id: int) -> str:
    """Генерация уникального расчетного счета."""
    async with aiosqlite.connect(DB_PATH) as db:
        while True:
            acc_num = f"{40800 + bank_id:05d}-{random.randint(1000, 9999)}"
            async with db.execute("SELECT 1 FROM bank_accounts WHERE account_number = ?", (acc_num,)) as cursor:
                if not await cursor.fetchone():
                    return acc_num

async def get_all_banks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banks") as cursor:
            return await cursor.fetchall()

async def get_national_bank():
    """Возвращает данные действующего Национального банка Резендии."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banks WHERE is_national = 1 LIMIT 1") as cursor:
            return await cursor.fetchone()

async def set_national_bank(bank_id: int):
    """Назначает банк Национальным банком Резендии, снимая статус с других."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE banks SET is_national = 0")
        await db.execute("UPDATE banks SET is_national = 1, type = 'государственный' WHERE id = ?", (bank_id,))
        await db.commit()

async def get_bank(bank_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banks WHERE id = ?", (bank_id,)) as cursor:
            return await cursor.fetchone()

async def get_bank_by_name(name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banks WHERE name = ?", (name,)) as cursor:
            return await cursor.fetchone()

async def get_account_by_number(account_number: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT a.*, b.name as bank_name, b.type as bank_type, b.interest_rate, b.log_channel_id
            FROM bank_accounts a
            JOIN banks b ON a.bank_id = b.id
            WHERE a.account_number = ?
        """, (account_number,)) as cursor:
            return await cursor.fetchone()

async def get_user_accounts(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT a.*, b.name as bank_name, b.type as bank_type 
            FROM bank_accounts a
            JOIN banks b ON a.bank_id = b.id
            WHERE a.user_id = ?
        """, (user_id,)) as cursor:
            return await cursor.fetchall()

async def get_bank_accounts(bank_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT a.*, u.FIO 
            FROM bank_accounts a
            LEFT JOIN users u ON a.user_id = u.user_id
            WHERE a.bank_id = ?
        """, (bank_id,)) as cursor:
            return await cursor.fetchall()

async def get_bank_total_issued_loans(bank_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT SUM(remaining_debt) FROM bank_loans WHERE bank_id = ? AND is_closed = 0",
            (bank_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] or 0


async def get_user_loans(user_id: int, only_active: bool = True):
    """Получить кредиты пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT l.*, b.name as bank_name, b.interest_rate
            FROM bank_loans l
            JOIN banks b ON l.bank_id = b.id
            WHERE l.user_id = ?
        """
        if only_active:
            query += " AND l.is_closed = 0"
        async with db.execute(query, (user_id,)) as cursor:
            return await cursor.fetchall()

async def get_loan_by_id(loan_id: int):
    """Получить конкретный кредит по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT l.*, b.name as bank_name, b.interest_rate, b.log_channel_id
            FROM bank_loans l
            JOIN banks b ON l.bank_id = b.id
            WHERE l.id = ?
        """, (loan_id,)) as cursor:
            return await cursor.fetchone()

async def get_loan_request_by_id(request_id: int):
    """Получить заявку на кредит по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.*, b.name as bank_name, b.interest_rate, b.log_channel_id, b.control_channel_id, b.balance as bank_balance
            FROM bank_loan_requests r
            JOIN banks b ON r.bank_id = b.id
            WHERE r.id = ?
        """, (request_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_bank_active_loans_count(user_id: int, bank_id: int) -> int:
    """Количество активных кредитов пользователя в конкретном банке."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM bank_loans WHERE user_id = ? AND bank_id = ? AND is_closed = 0",
            (user_id, bank_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def has_pending_loan_request(user_id: int, bank_id: int) -> bool:
    """Проверка наличия ожидающей рассмотрения заявки на кредит."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM bank_loan_requests WHERE user_id = ? AND bank_id = ? AND status = 'pending' LIMIT 1",
            (user_id, bank_id)
        ) as cursor:
            return bool(await cursor.fetchone())

async def get_account_active_loans(account_number: str):
    """Проверка наличия непогашенных кредитов, привязанных к счету."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bank_loans WHERE account_number = ? AND is_closed = 0",
            (account_number,)
        ) as cursor:
            return await cursor.fetchall()

async def close_bank_account(account_number: str) -> Optional[dict]:
    """
    Закрывает (удаляет) лицевой счет:
    Возвращает данные счета или None, если счет не найден.
    Если на счете были деньги, начисляет их на баланс пользователя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT a.*, b.name as bank_name, b.log_channel_id
            FROM bank_accounts a
            JOIN banks b ON a.bank_id = b.id
            WHERE a.account_number = ?
        """, (account_number,)) as cursor:
            account = await cursor.fetchone()

        if not account:
            return None

        account_dict = dict(account)

        # Выплачиваем остаток наличными, если баланс > 0
        if account_dict["balance"] > 0:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (account_dict["balance"], account_dict["user_id"])
            )

        # Удаляем счет
        await db.execute("DELETE FROM bank_accounts WHERE account_number = ?", (account_number,))
        await db.commit()
        return account_dict

async def get_bank_active_interbank_loans(bank_id: int):
    """Возвращает активные непогашенные межбанковские кредиты, взятые банком у Нацбанка."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT l.*, b.name as lender_bank_name
            FROM bank_loans l
            JOIN banks b ON l.bank_id = b.id
            WHERE l.borrower_bank_id = ? AND l.is_closed = 0
        """, (bank_id,)) as cursor:
            return await cursor.fetchall()

async def get_bank_interbank_loans_count(bank_id: int, lender_bank_id: int) -> int:
    """Количество активных межбанковских кредитов банка у конкретного банка-кредитора (Нацбанка)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM bank_loans WHERE borrower_bank_id = ? AND bank_id = ? AND is_closed = 0",
            (bank_id, lender_bank_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def has_pending_interbank_loan_request(bank_id: int, lender_bank_id: int) -> bool:
    """Проверка наличия ожидающей рассмотрения заявки на межбанковский кредит."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM bank_loan_requests WHERE borrower_bank_id = ? AND bank_id = ? AND status = 'pending' LIMIT 1",
            (bank_id, lender_bank_id)
        ) as cursor:
            return bool(await cursor.fetchone())

async def rename_bank_account(account_number: str, new_name: Optional[str]) -> bool:
    """Изменить название (метку) счета в банке."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bank_accounts SET account_name = ? WHERE account_number = ?",
            (new_name, account_number)
        )
        await db.commit()
        return True

async def get_user_bank_accounts_count(user_id: int, bank_id: int) -> int:
    """Получить количество открытых счетов пользователя в конкретном банке."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM bank_accounts WHERE user_id = ? AND bank_id = ?",
            (user_id, bank_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

# ================= ЧЁРНЫЙ СПИСОК БАНКА =================

async def add_to_bank_blacklist(bank_id: int, user_id: int, reason: str, added_by: int) -> bool:
    """Добавить пользователя в чёрный список банка."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO bank_blacklist (bank_id, user_id, reason, added_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(bank_id, user_id) DO UPDATE SET
                reason = excluded.reason,
                added_by = excluded.added_by,
                created_at = excluded.created_at
        """, (bank_id, user_id, reason, added_by, now))
        await db.commit()
        return True

async def remove_from_bank_blacklist(bank_id: int, user_id: int) -> bool:
    """Удалить пользователя из чёрного списка банка."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM bank_blacklist WHERE bank_id = ? AND user_id = ?",
            (bank_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_user_bank_blacklist_entry(bank_id: int, user_id: int):
    """Проверить, находится ли пользователь в ЧС банка. Возвращает запись или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bank_blacklist WHERE bank_id = ? AND user_id = ?",
            (bank_id, user_id)
        ) as cursor:
            return await cursor.fetchone()

async def get_bank_blacklist(bank_id: int):
    """Получить список пользователей в ЧС банка."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bank_blacklist WHERE bank_id = ? ORDER BY created_at DESC",
            (bank_id,)
        ) as cursor:
            return await cursor.fetchall()

# ================= ДЕПОЗИТЫ БАНКА =================

async def create_bank_deposit(user_id: int, bank_id: int, account_number: str, amount: int, interest_rate: float) -> int:
    """Открыть депозит."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO bank_deposits (user_id, bank_id, account_number, amount, interest_rate, created_at, last_payout_time, is_closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (user_id, bank_id, account_number, amount, interest_rate, now, now))
        deposit_id = cursor.lastrowid
        await db.commit()
        return deposit_id

async def get_user_deposits(user_id: int, only_active: bool = True):
    """Получить депозиты пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        clause = "AND d.is_closed = 0" if only_active else ""
        async with db.execute(f"""
            SELECT d.*, b.name as bank_name
            FROM bank_deposits d
            JOIN banks b ON d.bank_id = b.id
            WHERE d.user_id = ? {clause}
            ORDER BY d.created_at DESC
        """, (user_id,)) as cursor:
            return await cursor.fetchall()

async def get_bank_deposits_total(bank_id: int) -> int:
    """Сумма активных депозитов банка."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT SUM(amount) FROM bank_deposits WHERE bank_id = ? AND is_closed = 0",
            (bank_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0

async def get_deposit_by_id(deposit_id: int):
    """Получить депозит по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT d.*, b.name as bank_name, b.balance as bank_balance, b.log_channel_id
            FROM bank_deposits d
            JOIN banks b ON d.bank_id = b.id
            WHERE d.id = ?
        """, (deposit_id,)) as cursor:
            return await cursor.fetchone()

async def close_bank_deposit(deposit_id: int) -> bool:
    """Закрыть депозит."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bank_deposits SET is_closed = 1 WHERE id = ?", (deposit_id,))
        await db.commit()
        return True
