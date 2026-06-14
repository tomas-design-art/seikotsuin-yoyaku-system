"""RPA worker などからの /api/hotpepper/* への呼び出しを記録するテーブル。

audit_logs とは別系統。運用画面には出さず、AI/開発者が SQL で参照して
「RPA worker が生きているか/どんなパラメータで叩いているか」を診断する用途。
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON
from sqlalchemy.sql import func

from app.database import Base


class RpaCallLog(Base):
    __tablename__ = "rpa_call_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    endpoint = Column(String(200), nullable=False, index=True)
    method = Column(String(10), nullable=False)
    status_code = Column(Integer, nullable=True)
    query_params = Column(JSON, nullable=True)
    body_summary = Column(JSON, nullable=True)
    response_count = Column(Integer, nullable=True)
    response_ids = Column(JSON, nullable=True)  # 先頭20件のID
    duration_ms = Column(Integer, nullable=True)
    client_ip = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)
