"""幂等依赖：以幂等键为单位做事务级串行化，首次请求的成功/业务失败均留存原始响应。"""

import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import IdempotencyRecord
from .services.reservation import _body_hash


class IdempotentConflict(Exception):
    """回放已存响应或回传首次业务失败时抛出，由路由转为对应 HTTP 响应。"""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body


def idempotent_endpoint(
    db: Session,
    key: str,
    path: str,
    payload: dict,
    work,
) -> dict:
    """执行一次幂等端点。

    - 先对幂等键取事务咨询锁：同键并发重复提交在此排队；
    - 已有记录则原样回放（响应码与响应体均为首次结果）；
    - 首次执行无论成功还是业务失败（如容量不足 409）都落库，
      业务写入通过保存点回滚，只保留幂等记录，
      之后的重复提交只能拿到原结果，不会再卖出或再拒绝一次。
    """
    lock_id = int(_body_hash({"k": key})[:15], 16)
    db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})

    rec = db.query(IdempotencyRecord).filter(
        IdempotencyRecord.idempotency_key == key
    ).one_or_none()
    if rec is not None:
        if rec.request_path != path:
            raise IdempotentConflict(409, {"detail": f"幂等键 {key} 已用于其他接口"})
        stored = json.loads(rec.response_body)
        if stored.get("_request_hash") != _body_hash(payload):
            raise IdempotentConflict(409, {"detail": f"幂等键 {key} 已用于不同的请求参数"})
        raise IdempotentConflict(rec.response_code, stored["body"])

    try:
        with db.begin_nested():
            body = work()
        code = 200
    except HTTPException as exc:
        code = exc.status_code
        body = {"detail": exc.detail}

    db.add(IdempotencyRecord(
        idempotency_key=key,
        request_path=path,
        response_code=code,
        response_body=json.dumps(
            {"_request_hash": _body_hash(payload), "body": jsonable_encoder(body)},
            ensure_ascii=False, default=str,
        ),
    ))
    db.commit()
    if code != 200:
        raise IdempotentConflict(code, body)
    return body
