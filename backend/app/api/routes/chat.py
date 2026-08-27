"""The chat turn.

Thin on purpose: validation, call chat_service, map errors. Every interesting
decision -- screening, the turn limit, the gate, the audit rows -- lives in the
service, so this file stays boring and the flow is readable in one place.

No merchant key. The session id is the credential, and it is a uuid handed out
only in exchange for a slot token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core.db import RpcError
from app.services import chat_service

log = logging.getLogger("kirana.chat")

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatMessage(BaseModel):
    # Longer than sanitize's 500-char budget on purpose: an over-long message
    # should be truncated and screened, not rejected at the edge with a 422
    # the customer cannot interpret.
    message: str = Field(min_length=1, max_length=2000)


@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatMessage, db: DbDep) -> dict:
    try:
        return await chat_service.chat_turn(db, session_id, body.message)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": str(exc), "message": "No such session."},
        ) from exc
    except RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # A customer mid-negotiation gets a sentence, not a stack trace. The
        # detail goes to the logs where the merchant console can be checked.
        log.exception("chat turn failed for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "CHAT_UNAVAILABLE",
                "message": "I could not reach the shop's system just then. Try once more.",
            },
        ) from exc
