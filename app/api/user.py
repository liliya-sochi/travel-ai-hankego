"""
HTTP-маршруты пользователей HankeGo.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.user import (
    TelegramUserUpsertRequest,
    UserResponse,
)
from app.services.user import UserService, UserServiceError

router = APIRouter(
    prefix="/users",
)

SessionDependency = Annotated[
    AsyncSession,
    Depends(get_session),
]


@router.put(
    "/telegram",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Создать или обновить Telegram-пользователя",
)
async def upsert_telegram_user(
    request: TelegramUserUpsertRequest,
    session: SessionDependency,
) -> UserResponse:
    """
    Регистрирует Telegram-пользователя в HankeGo.

    Повторный запрос с тем же telegram_id обновляет имя,
    но не создаёт дублирующую запись.
    """

    service = UserService(session)

    try:
        user = await service.register_telegram_user(
            telegram_id=request.telegram_id,
            first_name=request.first_name,
        )

    except UserServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error

    return UserResponse.model_validate(user)
