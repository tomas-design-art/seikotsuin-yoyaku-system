from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func

from app.database import Base


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    # 設定値は処理済みメールIDハッシュ一覧やJSONなど可変長データも格納するため TEXT とする。
    # （旧定義は VARCHAR(500) で、長いリスト保存時に StringDataRightTruncationError が発生していた）
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
