from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select

from bot.database import SessionLocal
from bot.models import FriendRequest, Friendship, Reminder, UserSettings


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def friend_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def user_exists(user_id: int) -> bool:
    async with SessionLocal() as session:
        us = await session.scalar(select(UserSettings.user_id).where(UserSettings.user_id == user_id).limit(1))
        if us is not None:
            return True
        rm = await session.scalar(select(Reminder.user_id).where(Reminder.user_id == user_id).limit(1))
        return rm is not None


async def is_friend(user_a: int, user_b: int) -> bool:
    low, high = friend_pair(user_a, user_b)
    async with SessionLocal() as session:
        row = await session.scalar(
            select(Friendship.id).where(Friendship.user_low_id == low, Friendship.user_high_id == high).limit(1)
        )
        return row is not None


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


async def list_incoming_requests(user_id: int) -> list[FriendRequest]:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(FriendRequest)
            .where(FriendRequest.to_user_id == user_id, FriendRequest.status == "pending")
            .order_by(FriendRequest.created_at.desc())
        )
        return rows.scalars().all()


async def create_friend_request(from_user_id: int, to_user_id: int) -> FriendRequest:
    if from_user_id == to_user_id:
        raise ValueError("cannot_add_self")
    if not await user_exists(to_user_id):
        raise ValueError("target_not_found")
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
            return req

        # повтор запроса не плодим
        existing = await session.scalar(
            select(FriendRequest).where(
                FriendRequest.from_user_id == from_user_id,
                FriendRequest.to_user_id == to_user_id,
                FriendRequest.status == "pending",
            )
        )
        if existing is not None:
            return existing

        req = FriendRequest(from_user_id=from_user_id, to_user_id=to_user_id, status="pending")
        session.add(req)
        await session.commit()
        await session.refresh(req)
        return req


async def respond_friend_request(request_id: int, user_id: int, accept: bool) -> FriendRequest:
    async with SessionLocal() as session:
        req = await session.get(FriendRequest, request_id)
        if req is None or req.to_user_id != user_id:
            raise ValueError("request_not_found")
        if req.status != "pending":
            return req

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
        return req
