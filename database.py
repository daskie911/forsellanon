import aiosqlite
import os
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "./data/bot.db")


class Database:
    def __init__(self):
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Подключение и создание таблиц"""
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"✅ Database connected: {DB_PATH}")

    async def _create_tables(self):
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER,
                message_type TEXT NOT NULL,
                content TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (sender_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS message_mapping (
                recipient_id INTEGER NOT NULL,
                recipient_msg_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (recipient_id, recipient_msg_id)
            );

            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                blocked_at TEXT NOT NULL,
                PRIMARY KEY (blocker_id, blocked_id)
            );
        """)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
            logger.info("❌ Database closed")

    # ===== Пользователи =====

    async def upsert_user(self, user_id: int, username: str, first_name: str):
        """Создать или обновить пользователя"""
        await self.db.execute("""
            INSERT INTO users (user_id, username, first_name, first_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username, first_name, datetime.now().isoformat()))
        await self.db.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_users(self, limit: int = 10) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM users ORDER BY first_seen DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count_users(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            return row[0]

    # ===== Сообщения =====

    async def log_message(self, sender_id: int, recipient_id: int,
                          message_type: str, content: str):
        """Логировать сообщение"""
        await self.db.execute("""
            INSERT INTO messages (sender_id, recipient_id, message_type, content, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (sender_id, recipient_id, message_type, content[:200], datetime.now().isoformat()))
        await self.db.commit()

    async def count_messages(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM messages") as cursor:
            row = await cursor.fetchone()
            return row[0]

    async def count_user_sent(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]

    async def count_user_received(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]

    async def get_user_first_seen(self, user_id: int) -> Optional[str]:
        async with self.db.execute(
            "SELECT first_seen FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_recent_messages(self, limit: int = 5) -> list[dict]:
        async with self.db.execute("""
            SELECT m.*, u.first_name, u.username
            FROM messages m
            LEFT JOIN users u ON m.sender_id = u.user_id
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_top_users(self, limit: int = 10) -> list[dict]:
        """Топ пользователей по количеству отправленных сообщений"""
        async with self.db.execute("""
            SELECT u.user_id, u.username, u.first_name,
                   COUNT(m.id) as msg_count
            FROM users u
            LEFT JOIN messages m ON u.user_id = m.sender_id
            GROUP BY u.user_id
            ORDER BY msg_count DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ===== Маппинг сообщений =====

    async def add_mapping(self, recipient_id: int, recipient_msg_id: int, sender_id: int):
        """Создать связь: сообщение у получателя → отправитель"""
        await self.db.execute("""
            INSERT OR REPLACE INTO message_mapping
            (recipient_id, recipient_msg_id, sender_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (recipient_id, recipient_msg_id, sender_id, datetime.now().isoformat()))
        await self.db.commit()

    async def get_sender(self, recipient_id: int, recipient_msg_id: int) -> Optional[int]:
        """Найти отправителя по reply"""
        async with self.db.execute(
            "SELECT sender_id FROM message_mapping WHERE recipient_id = ? AND recipient_msg_id = ?",
            (recipient_id, recipient_msg_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def cleanup_old_mappings(self, days: int = 30):
        """Удалить маппинги старше N дней"""
        from datetime import timedelta
        threshold = (datetime.now() - timedelta(days=days)).isoformat()
        await self.db.execute(
            "DELETE FROM message_mapping WHERE created_at < ?", (threshold,)
        )
        await self.db.commit()
        logger.info(f"🧹 Old mappings cleaned (older than {days} days)")

    # ===== Блокировки =====

    async def block_user(self, blocker_id: int, blocked_id: int):
        """Заблокировать анонимного отправителя"""
        await self.db.execute("""
            INSERT OR IGNORE INTO blocks (blocker_id, blocked_id, blocked_at)
            VALUES (?, ?, ?)
        """, (blocker_id, blocked_id, datetime.now().isoformat()))
        await self.db.commit()
        logger.info(f"🚫 User {blocker_id} blocked {blocked_id}")

    async def unblock_user(self, blocker_id: int, blocked_id: int):
        """Разблокировать"""
        await self.db.execute(
            "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
            (blocker_id, blocked_id)
        )
        await self.db.commit()
        logger.info(f"✅ User {blocker_id} unblocked {blocked_id}")

    async def is_blocked(self, blocker_id: int, blocked_id: int) -> bool:
        """Проверить, заблокирован ли отправитель"""
        async with self.db.execute(
            "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
            (blocker_id, blocked_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def get_blocked_list(self, blocker_id: int) -> list[int]:
        """Список заблокированных user_id"""
        async with self.db.execute(
            "SELECT blocked_id FROM blocks WHERE blocker_id = ?", (blocker_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def count_blocked(self, blocker_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM blocks WHERE blocker_id = ?", (blocker_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]


# Глобальный экземпляр
db = Database()
