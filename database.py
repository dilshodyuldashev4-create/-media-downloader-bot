import os
from typing import List, Optional

import asyncpg


class Database:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL не найден. Добавьте PostgreSQL в Railway "
                "и проверьте переменную DATABASE_URL."
            )

        self.pool = await asyncpg.create_pool(
            dsn=self.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )

        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    language_code TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("База данных не подключена. Сначала вызовите db.connect().")
        return self.pool

    async def add_subscriber(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> None:
        pool = self._require_pool()

        await pool.execute(
            """
            INSERT INTO subscribers (
                user_id,
                username,
                first_name,
                language_code,
                is_active,
                updated_at
            )
            VALUES ($1, $2, $3, $4, TRUE, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                language_code = EXCLUDED.language_code,
                is_active = TRUE,
                updated_at = NOW();
            """,
            user_id,
            username,
            first_name,
            language_code,
        )

    async def deactivate_subscriber(self, user_id: int) -> None:
        pool = self._require_pool()

        await pool.execute(
            """
            UPDATE subscribers
            SET is_active = FALSE, updated_at = NOW()
            WHERE user_id = $1;
            """,
            user_id,
        )

    async def get_active_subscribers(self) -> List[int]:
        pool = self._require_pool()

        rows = await pool.fetch(
            """
            SELECT user_id
            FROM subscribers
            WHERE is_active = TRUE
            ORDER BY created_at ASC;
            """
        )
        return [row["user_id"] for row in rows]

    async def count_active_subscribers(self) -> int:
        pool = self._require_pool()

        value = await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM subscribers
            WHERE is_active = TRUE;
            """
        )
        return int(value or 0)


db = Database()
