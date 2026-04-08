from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from bot.database import SessionLocal
from bot.models import FriendRequest, Friendship, UserSettings


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def friend_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def user_exists(user_id: int) -> bool:
    async with SessionLocal() as session:
        us = await session.scalar(select(UserSettings.user_id).where(UserSettings.user_id == user_id).limit(1))
        return us is not None


async def user_id_by_profile_name(profile_name: str) -> int | None:
    name = (profile_name or "").strip()
    if not name:
        return None
    async with SessionLocal() as session:
        uid = await session.scalar(
            select(UserSettings.user_id).where(func.lower(UserSettings.profile_name) == name.lower()).limit(1)
        )
        return int(uid) if uid is not None else None


async def is_friend(user_a: int, user_b: int) -> bool:
    low, high = friend_pair(user_a, user_b)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high).limit(1)
        )
        return row is not None


async def remove_friend(user_a: int, user_b: int) -> bool:
    low, high = friend_pair(user_a, user_b)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high).limit(1)
        )
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def list_friends(user_id: int) -> list[int]:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Friendship).where(
                or_(Friendship.user_low_id == user_id, Friendship.user_high_id == user_id)
            )
        )
        out: list[int] = []
        for fr in rows.scalars().all():
            out.append(fr.user_high_id if fr.user_low_id == user_id else fr.user_low_id)
        out.sort()
        return out


async def resolve_profile_name(user_id: int) -> str:
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is not None and row.profile_name and row.profile_name.strip():
            return row.profile_name.strip()
    return f"Пользователь {str(user_id)[-4:]}"


async def list_incoming_requests(user_id: int) -> list[FriendRequest]:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(FriendRequest)
            .where(FriendRequest.to_user_id == user_id, FriendRequest.status == "pending")
            .order_by(FriendRequest.created_at.desc())
        )
        return rows.scalars().all()


async def create_friend_request(from_user_id: int, to_user_id: int) -> tuple[FriendRequest, bool]:
    """
    Создаёт заявку. Второй элемент — True, если создана новая pending-заявка
    (нужно уведомить получателя); False при дубликате или автопринятии.
    """
    if from_user_id == to_user_id:
        raise ValueError("cannot_add_self")
    if not await user_exists(to_user_id):
        raise ValueError("target_not_activated")
    if await is_friend(from_user_id, to_user_id):
        raise ValueError("already_friends")

    async with SessionLocal() as session:
        # если уже есть встречный запрос — сразу дружим
        opposite = await session.scalar(
            select(FriendRequest).where(
                FriendRequest.from_user_id == to_user_id,
                FriendRequest.to_user_id == from_user_id,
                FriendRequest.status == "pending",
            )
        )
        if opposite is not None:
            opposite.status = "accepted"
            opposite.responded_at = _utcnow()
            low, high = friend_pair(from_user_id, to_user_id)
            exists = await session.scalar(
                select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high).limit(1)
            )
            if exists is None:
                session.add(Friendship(user_low_id=low, user_high_id=high))
            req = FriendRequest(
                from_user_id=from_user_id,
                to_user_id=to_user_id,
                status="accepted",
                responded_at=_utcnow(),
            )
            session.add(req)
            await session.commit()
            await session.refresh(req)
            return req, False

        # повтор запроса не плодим
        existing = await session.scalar(
            select(FriendRequest).where(
                FriendRequest.from_user_id == from_user_id,
                FriendRequest.to_user_id == to_user_id,
                FriendRequest.status == "pending",
            )
        )
        if existing is not None:
            return existing, False

        req = FriendRequest(from_user_id=from_user_id, to_user_id=to_user_id, status="pending")
        session.add(req)
        await session.commit()
        await session.refresh(req)
        return req, True


async def respond_friend_request(request_id: int, user_id: int, accept: bool) -> tuple[FriendRequest, bool]:
    """Второй элемент True, если заявка была pending и ответ записан (один раз)."""
    async with SessionLocal() as session:
        req = await session.get(FriendRequest, request_id)
        if req is None or req.to_user_id != user_id:
            raise ValueError("request_not_found")
        if req.status != "pending":
            return req, False

        req.status = "accepted" if accept else "rejected"
        req.responded_at = _utcnow()
        if accept:
            low, high = friend_pair(req.from_user_id, req.to_user_id)
            exists = await session.scalar(
                select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high).limit(1)
            )
            if exists is None:
                session.add(Friendship(user_low_id=low, user_high_id=high))
        await session.commit()
        await session.refresh(req)
        return req, True
