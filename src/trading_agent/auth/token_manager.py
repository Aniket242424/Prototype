"""
Token persistence with at-rest encryption (Fernet).

Storage strategy: Postgres `tokens` table holds the latest active token,
encrypted with TOKEN_ENCRYPTION_KEY (Fernet symmetric). One row per user.

Validity rule: Upstox tokens expire daily at 03:30 IST. We treat any token
issued before today's 03:30 IST cutoff as invalid even if Upstox would still
honor it briefly — fail-closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.auth.upstox_auth import UpstoxToken
from trading_agent.core.config import AppSettings
from trading_agent.core.exceptions import TokenExpiredError, TokenStorageError
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.infrastructure.models import TokenRow

log = get_logger(__name__)


@dataclass(frozen=True)
class StoredToken:
    user_id: str
    access_token: str  # decrypted
    issued_at: datetime
    is_valid_now: bool


class TokenManager:
    def __init__(self, settings: AppSettings):
        try:
            self._fernet = Fernet(settings.token_encryption_key.get_secret_value().encode())
        except Exception as e:
            raise TokenStorageError(
                "TOKEN_ENCRYPTION_KEY is invalid. Generate via: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from e

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as e:
            raise TokenStorageError("Token decryption failed — encryption key changed?") from e

    @staticmethod
    def _is_token_valid(issued_at: datetime) -> bool:
        """A token is valid only if it was issued AFTER the most recent 03:30 IST cutoff."""
        now = now_ist()
        cutoff = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now < cutoff:
            cutoff = cutoff.replace(day=now.day - 1) if now.day > 1 else cutoff
        return issued_at.astimezone(IST) >= cutoff

    async def save(self, session: AsyncSession, token: UpstoxToken) -> None:
        ciphertext = self._encrypt(token.access_token)
        row = await session.get(TokenRow, token.user_id)
        if row is None:
            row = TokenRow(
                user_id=token.user_id,
                broker=token.broker,
                access_token_encrypted=ciphertext,
                user_name=token.user_name,
                email=token.email,
                issued_at=datetime.fromisoformat(token.issued_at_ist_iso),
            )
            session.add(row)
        else:
            row.access_token_encrypted = ciphertext
            row.broker = token.broker
            row.user_name = token.user_name
            row.email = token.email
            row.issued_at = datetime.fromisoformat(token.issued_at_ist_iso)
        await session.commit()
        log.info("token.saved", user_id=token.user_id)

    async def load(self, session: AsyncSession, user_id: str) -> StoredToken | None:
        row = await session.get(TokenRow, user_id)
        if row is None:
            return None
        plaintext = self._decrypt(row.access_token_encrypted)
        return StoredToken(
            user_id=row.user_id,
            access_token=plaintext,
            issued_at=row.issued_at,
            is_valid_now=self._is_token_valid(row.issued_at),
        )

    async def get_valid_or_raise(self, session: AsyncSession, user_id: str) -> str:
        st = await self.load(session, user_id)
        if st is None:
            raise TokenExpiredError(f"No token stored for user_id={user_id}. Run `make auth`.")
        if not st.is_valid_now:
            raise TokenExpiredError(
                f"Token for user_id={user_id} expired (issued {st.issued_at.isoformat()}). Run `make auth`."
            )
        return st.access_token
