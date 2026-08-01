"""
Внутренняя авторизация сервисов HankeGo.
"""

from secrets import compare_digest
from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import get_settings


INTERNAL_API_KEY_HEADER = "X-Internal-API-Key"

internal_api_key_header = APIKeyHeader(
    name=INTERNAL_API_KEY_HEADER,
    auto_error=False,
    description=(
        "Секретный ключ для внутренних клиентов HankeGo."
    ),
)


async def verify_internal_api_key(
    provided_api_key: Annotated[
        str | None,
        Security(internal_api_key_header),
    ],
) -> None:
    """
    Проверяет секретный ключ внутреннего клиента.
    """

    settings = get_settings()

    expected_api_key = (
        settings.internal_api_key.get_secret_value()
    )

    candidate_api_key = provided_api_key or ""

    # compare_digest уменьшает риск timing-атак
    # при сравнении секретных значений.
    if not compare_digest(
        candidate_api_key,
        expected_api_key,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ошибка внутренней авторизации.",
            headers={
                "WWW-Authenticate": "ApiKey",
            },
        )