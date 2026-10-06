"""テスト全体の前提。

開発者の .env には本物の Gemini API キーが入っている。そのままだと単体テストが
ネットワークへ出てしまい、遅く・不安定になり、費用もかかる。返信の文面が
実行のたびに変わるため、テストの判定も揺れる。

既定では鍵を空にして、LLMを呼ばない状態で回す。
LLMの動きそのものを見たいテストは、テスト内で鍵を差し替えて httpx を模擬すること
（既存のテストはその形になっている）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_outbound_llm_calls(monkeypatch):
    monkeypatch.setattr("app.config.settings.gemini_api_key", "", raising=False)


@pytest.fixture(autouse=True)
def _no_autopilot_log_writes(monkeypatch):
    """LINE自動予約の記録は、開発者の DATABASE_URL へ書きに行かないよう止めておく。

    記録そのものの検証は tests/test_autopilot_log.py が使い捨ての schema に向けて有効にする。
    """
    monkeypatch.setattr("app.services.autopilot_log.ENABLED", False)
