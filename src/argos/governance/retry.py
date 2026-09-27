"""Reintentos con backoff exponencial, solo para operaciones idempotentes (RNF-04, RF-21)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable


async def with_retry[T](
    fn: Callable[[int], Awaitable[T]],
    *,
    idempotent: bool,
    attempts: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Ejecuta `fn(attempt)`. Si no es idempotente, un único intento: el fallo se propaga y
    cualquier repetición requiere reconfirmación explícita."""
    max_attempts = attempts if idempotent else 1
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(attempt)
        except retry_on as exc:
            if attempt >= max_attempts or (retry_if is not None and not retry_if(exc)):
                raise
            if on_retry:
                on_retry(attempt, exc)
            await asyncio.sleep(base_delay * 2 ** (attempt - 1) * (1 + random.random() * 0.2))
    raise AssertionError("inalcanzable")
