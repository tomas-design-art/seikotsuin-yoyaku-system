"""uvicorn の通り道の記録（アクセスログ）に、URL の ? より後ろを残さない。

画面の患者の検索（/api/patients/search?q=...）などは、? より後ろに患者の名前の一部や
電話番号が入る。uvicorn はアクセスログに ? より後ろまで書くので、パスだけにする
（入口の鍵の記録 app/middlewares/api_access.py と同じ考え方）。

uvicorn は access_logger.info('%s - "%s %s HTTP/%s" %d', 接続元, 方法, パスと問い合わせ, 版, 結果)
と書く（0.53 で確認）。3つ目の値が文字列で ? を含むときだけ、? より前に置き換える。
形の違う行はそのまま通す（記録そのものは落とさない）。
uvicorn の書き方が変わったら、tests/test_access_log_no_query.py が本物の uvicorn で気づく。
"""
from __future__ import annotations

import logging

ACCESS_LOGGER_NAME = "uvicorn.access"
_PATH_ARG_INDEX = 2


class DropQueryString(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > _PATH_ARG_INDEX:
            path = args[_PATH_ARG_INDEX]
            if isinstance(path, str) and "?" in path:
                record.args = (
                    args[:_PATH_ARG_INDEX]
                    + (path.split("?", 1)[0],)
                    + args[_PATH_ARG_INDEX + 1 :]
                )
        return True


_FILTER = DropQueryString()


def install() -> None:
    """uvicorn.access に付ける。何度呼んでも1つだけ。

    uvicorn はアプリを読み込む前にログの設定を済ませるので（workers が2以上でも、
    それぞれの子の中で設定→読み込みの順）、app.main を読み込んだときに付ければ効く。
    """
    logging.getLogger(ACCESS_LOGGER_NAME).addFilter(_FILTER)
