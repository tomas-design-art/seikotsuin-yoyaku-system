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
def _no_outbound_line_messages(monkeypatch):
    """開発者の .env には本物の LINE のトークンと管理者の LINE ID が入っている。

    そのままだと、送信を模擬していないテストが本物の LINE へ送る（AIの返信が定型文に
    落ちたときの「[LINE AI返信異常]」通知が管理者の LINE に届く、など）。2026-10-07 に気づいた。
    既定ではトークンを空にして送らない（line_reply は「未設定」として送信を飛ばす）。
    送る中身を確かめたいテストは、テスト内でトークンを差し替えて httpx を模擬すること。
    """
    monkeypatch.setattr("app.config.settings.line_channel_access_token", "", raising=False)
    # 開発者向けの通知（SOS・シャドー）は別のトークンで送るので、こちらも空にする
    monkeypatch.setattr("app.config.settings.line_channel_developer_access_token", "", raising=False)


@pytest.fixture(autouse=True)
def _no_autopilot_log_writes(monkeypatch):
    """LINE自動予約の記録は、開発者の DATABASE_URL へ書きに行かないよう止めておく。

    記録そのものの検証は tests/test_autopilot_log.py が使い捨ての schema に向けて有効にする。
    """
    monkeypatch.setattr("app.services.autopilot_log.ENABLED", False)
