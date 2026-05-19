import time
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Ограничение частоты сообщений.
    По умолчанию: максимум 8 сообщений за 60 секунд на пользователя.
    """

    def __init__(self, max_messages: int = 8, window_seconds: int = 60):
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        # user_id → список timestamp'ов
        self._requests: dict[int, list[float]] = defaultdict(list)

    def _cleanup(self, user_id: int):
        """Удалить устаревшие записи"""
        now = time.time()
        cutoff = now - self.window_seconds
        self._requests[user_id] = [
            t for t in self._requests[user_id] if t > cutoff
        ]

    def is_allowed(self, user_id: int) -> bool:
        """Проверить, может ли пользователь отправить сообщение"""
        self._cleanup(user_id)
        if len(self._requests[user_id]) >= self.max_messages:
            return False
        self._requests[user_id].append(time.time())
        return True

    def get_wait_time(self, user_id: int) -> int:
        """Сколько секунд ждать до следующего сообщения"""
        self._cleanup(user_id)
        if len(self._requests[user_id]) < self.max_messages:
            return 0
        oldest = self._requests[user_id][0]
        wait = int(self.window_seconds - (time.time() - oldest)) + 1
        return max(wait, 1)

    def reset(self, user_id: int):
        """Сбросить лимит (для админов)"""
        if user_id in self._requests:
            del self._requests[user_id]


# Глобальный экземпляр: 8 сообщений в минуту
rate_limiter = RateLimiter(max_messages=8, window_seconds=60)
