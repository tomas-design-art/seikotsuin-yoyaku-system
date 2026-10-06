"""LINE自動予約の記録。患者からの1通（文・ボタン）ごとに1行。

院長・まことさんが実機で試したあと「何が起きたか」を、Render のログや3日で消える
受信記録（line_webhook_events）に頼らず確かめるためのもの。
監査ログ（audit_logs）とは分ける（2026-10-06 まことさん）。

消す仕組みは今は無い（autopilot の対象は2名だけ）。患者さんに開放する前に保存期間を決める。
"""
from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.database import Base


class LineAutopilotLog(Base):
    __tablename__ = "line_autopilot_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    line_user_id = Column(String(100), nullable=False, index=True)
    # 患者が消えても記録は残すので、外部キーにはしない
    patient_id = Column(Integer, nullable=True, index=True)
    webhook_event_id = Column(String(64), nullable=True)
    # text / postback
    event_type = Column(String(20), nullable=False)
    # 患者が送った文。ボタンなら postback の data
    received_text = Column(Text, nullable=True)
    mode_before = Column(String(50), nullable=True)
    mode_after = Column(String(50), nullable=True)
    # 会話状態（line_user_states.context_data）の前後。draft・提示中の候補を含む
    state_before = Column(JSON, nullable=True)
    state_after = Column(JSON, nullable=True)
    # 処理の途中で起きたこと（AIの読み取り・返信を作った場面と渡した事実・送ったメッセージ）
    steps = Column(JSON, nullable=False, default=list)
    # その患者のこれからの予約の前後と、その差分（作成・変更・取消）
    reservations_before = Column(JSON, nullable=True)
    reservations_after = Column(JSON, nullable=True)
    reservation_changes = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
