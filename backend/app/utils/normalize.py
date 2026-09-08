"""文字列正規化ユーティリティ.

用途が2つある。取り違えないこと。

- `normalize_search_text` … **検索・照合用**。ひらがな→カタカナまで踏み込む。
  氏名の名寄せのように「読みが同じなら同じ」と見なしたいときに使う。
- `normalize_input_text` … **入力の字幅を揃えるだけ**。NFKC と strip のみ。
  患者が全角で打った「１」「１４：００」を素直に読むためのもの。
"""

import re
import unicodedata

# ひらがな → カタカナ 変換テーブル (U+3041‒U+3096 → U+30A1‒U+30F6)
_HIRA = "".join(chr(c) for c in range(0x3041, 0x3097))
_KATA = "".join(chr(c) for c in range(0x30A1, 0x30F7))
_H2K_TABLE = str.maketrans(_HIRA, _KATA)

# PostgreSQL translate() で使えるよう、文字列としても公開
HIRA_CHARS = _HIRA
KATA_CHARS = _KATA


def normalize_search_text(text: str | None) -> str:
    """検索テキストを正規化する。

    処理内容:
      1. NFKC 正規化 — 半角カナ→全角カナ、全角英数→半角英数
      2. ひらがな→カタカナ統一
      3. 全角スペース→半角スペース、連続スペース除去
      4. strip + lower (英字の大小吸収)

    >>> normalize_search_text("やまだ")
    'ヤマダ'
    >>> normalize_search_text("ﾔﾏﾀﾞ")
    'ヤマダ'
    >>> normalize_search_text("ヤマダ")
    'ヤマダ'
    >>> normalize_search_text("ＹＡＭＡＤＡ")
    'yamada'
    """
    if not text:
        return ""
    # NFKC: 半角カナ→全角カナ, 全角英数→半角英数
    text = unicodedata.normalize("NFKC", text)
    # ひらがな→カタカナ
    text = text.translate(_H2K_TABLE)
    # スペース正規化
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def normalize_input_text(text: str | None) -> str:
    """患者が打った本文の字幅だけを揃える。NFKC と strip のみ。

    NFKC が「１」→「1」「：」→「:」「　」→「 」を吸収する。
    かなの統一も lower も **しない**。

    `normalize_search_text` をここに使ってはいけない。
    ひらがな→カタカナ変換が入るため「２番」が「2バン」になり、
    「番」「にします」「でお願いします」のような日本語の接尾辞と照合できなくなる。

    2026-09-08: 全角で「１」と答えた選択が候補選択として読まれず、
    同じ候補一覧を返し続けるループの一因になっていた。

    >>> normalize_input_text("１")
    '1'
    >>> normalize_input_text("１４：００")
    '14:00'
    >>> normalize_input_text("　2番　")
    '2番'
    """
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).strip()
