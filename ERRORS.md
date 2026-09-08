# 開発エラー台帳

## 2026-09-04: ホストPowerShellでpytestを直接実行できない

- 症状: `pytest` がコマンドとして認識されない。
- 原因: Pythonテスト環境はホストではなくDockerバックエンド側にある。
- 手順: このリポジトリのbackendテストはCompose定義を確認し、`docker compose run --rm backend pytest ...` で実行する。
- 状態: L2（誤った復旧手順を追試で修正。以後、空のpackage importを全モデル登録とみなさない）

## 2026-09-04: backend全体テストの既知4失敗

- 症状: 全体テストは `4 failed / 401 passed`。失敗は `test_daily_report` 1件、`test_integration` 1件、`test_patients` 2件。
- 原因: SQLiteがPostgreSQL専用JSONBを生成できない既存のテスト分離問題と、既存AsyncMockのcoroutine取り扱い。
- 判定: 変更前の `main` と同じ4テストが同じ原因で失敗する。LINE対象テストは `137 passed`。
- 最新確認: LINE inbox段階導入後は `4 failed / 411 passed`、LINE対象は `145 passed`。失敗名は同一。
- 手順: LINE変更の回帰判定では、上記4件以外の失敗増加がないことを比較する。
- 状態: 既知ベースライン（今回の変更対象外）

## 2026-09-04: `python -c` で複数行async関数がSyntaxError

- 症状: PowerShellから`python -c`へセミコロン後の`async def`と文字列`\n`を渡すと構文エラーになる。
- 原因: compound statementはセミコロン後に置けず、PowerShell文字列内の`\n`もPythonコードの改行にならない。
- 手順: 複数行の実行確認はPowerShellのhere-stringを`docker compose run --rm -T backend python -`へ標準入力する。
- 状態: L1（初回記録）

## 2026-09-04: 部分importしたDBスニペットでSQLAlchemy mapper未登録

- 症状: `app.api.line`だけをimportしてORMクエリを実行すると、`ReservationColor`を解決できず`InvalidRequestError`になる。
- 原因: 通常のFastAPI起動時に行われる全モデルimportを通さず、SQLAlchemy mapper registryが不完全な状態でクエリをコンパイルした。
- 追試: `app.models.__init__`は空なので、`import app.models`だけでは登録されない。
- 手順: DBスニペットは参照する関連モデル（例: `patient`, `practitioner`, `reservation_color`, `reservation_series`）を明示importするか、全モデルを読み込む実API経路で検証する。
- 状態: L1（初回記録）

## 2026-09-04: 共有ターミナルのcwd変化で`git -C ..`がリポジトリ外を参照

- 症状: ターミナルが既にリポジトリルートへ移動していたため、`git -C .. diff`が親フォルダを参照して`Not a git repository`になった。
- 原因: 前回コマンドのcwdが維持される共有ターミナルで、`..`をbackend基準だと仮定した。
- 手順: Git操作前に`$repo = git rev-parse --show-toplevel`でルートを取得し、以後は`git -C $repo ...`を使う。
- 状態: L2（相対cwd前提の再発。以後ルートをコマンドで確定する）

## 2026-09-04: デプロイ前の`DATABASE_URL`がローカル接続だった

- 症状: URLは設定済みだが`IsLocal=True`で、本番のautopilot対象者件数確認に使用できなかった。
- 追加エラー: psqlは`ssl`クエリパラメータを受け付けず、`ssl=disable`が残ると`invalid URI query parameter`になる。
- 手順: DB操作前に接続先を値非表示でlocal/Render判定する。psqlではasyncpgスキームを変換し、`ssl=require|disable`も`sslmode=require|disable`へ変換する。
- 状態: push前で停止。本番影響なし。

## 2026-09-04: PowerShellの`foreach`直後のpipeでParserError

- 症状: 1行内の`foreach (...) { ... } | Format-Table`で`An empty pipe element is not allowed`になった。
- 原因: 文としての`foreach`出力をそのまま同一構文のpipelineへ接続した。
- 手順: 結果を`$rows = @(...foreach...)`へ格納してから`$rows | Format-Table`する。
- 状態: L1（初回記録）

## 2026-09-04: 確認待ち中の「はい」が「相槌」と判定され無言で捨てられた

- 症状: 予約確定→キャンセル対象をpostbackで選択→「はい」で、取消が実行されず返信も無し。イベントは `done/attempts=1/エラー無し`。会話状態は `autopilot_cancel_confirm` のまま残った。
- 原因: `api/line.py` の「予約確定直後の感謝・締めには返信しない」ショートカットが、**確認待ちモードでも発火**した。直前の確定で `recent_completed_booking` が残っており、Geminiが「はい」を `reply_action=no_reply / intent=other / has_reservation_intent=false` と判定 → `clear_recent_completed_booking()` して無言 `return`。取消分岐まで到達しなかった。
- 特定方法: 本番DBの `line_user_states.context_data` から `recent_completed_booking` が消えていた。このキーを消すコードは本番経路に1箇所（`clear_recent_completed_booking`）しかない。
- 手順: 会話の近道（no_reply・自動確定・自動スキップ）を足すときは、**確認待ちモードを必ず除外する**。`current_mode not in _AUTOPILOT_BOOKING_MODES` を条件に入れる。
- 状態: L3（回帰テスト `test_cancel_confirmation_survives_recent_booking_no_reply_shortcut` で固定）

## 2026-09-04: 8秒重複判定のプロセス内stateがテスト間で漏れる

- 症状: 新テスト追加だけで、無関係な既存テスト `test_autopilot_cancel_failure_replies_and_keeps_confirmation_active` が落ちた。単体では通る。
- 原因: `line_debounce._RECENT_MESSAGES` はモジュールレベルのdictで、`(user_id, 本文)` を8秒保持する。同じ `U-autopilot` + `はい` を使うテストが8秒以内に走ると、後のテストが黙殺される。
- 手順: LINEのテストは **user_id をテストごとに変える**。同じIDと同じ本文を共有しない。
- 状態: L1（初回記録。dedup自体を撤去できたらこの項目は消す）

## 2026-09-08: 確認の「はい」判定が部分一致で、別の希望を述べた返事で実行していた

- 症状: 枠確認中に「15時でお願いします」と打つと、提示済みの **14:00 で予約が確定**した。「16時に変更でお願いします」も同じ。取消・変更の確認でも同型。
- 原因: `_is_affirmative` が bool で「わからない」を表現できず、マーカーの部分一致で判定していた（`"お願い"` が肯定語）。実測で `"ありがとうございます！よろしくお願いします"` も肯定。解析側の `_rule_polarity` にも `お願いします` が入っているため、**polarity を見るだけでは止まらない**。
- 手順: 確認の判定は `app/services/confirmation.py` の `read_answer`（YES/NO/UNCLEAR）を通す。**`looks_affirmative` を単独で使わない。** 判定順（ボタン→否定→別の希望→肯定→UNCLEAR）は仕様なので入れ替えない。マーカーから語を削るのは直し方として誤り。
- 状態: L3（`tests/test_confirmation.py` と `test_a_new_time_during_the_*_does_not_*` で固定）

## 2026-09-08: 予約直前の「検算」が自分自身を比べていた

- 症状: 事故 #2572 の再発防止として入れた `matches_slot` が、引数4つとも `picked` 由来で常に一致。防ぎたい食い違いを原理的に検出できなかった。単体テストは独立した引数を渡していたので緑だった。
- 手順: 検算は**必ず別の経路から取り直した値**と突き合わせる。同じ dict の別表現を比べない。選択には `offered_slots.picked_record` で出どころ（offer_id / index）を付け、`verify_pick` で提示を引き直して照合する。
- 補足: **関数が正しくても呼び方が誤っていることがある。** 単体テストが緑なことは、その呼び出し方を何も保証しない。
- 状態: L3（`test_booking_stops_when_the_offer_changed_between_choosing_and_confirming` で固定）

## 2026-09-08: 候補0件のとき覚え書きを [] で上書きし、再検索への逃げ道を塞いでいた

- 症状: 日付だけ受けて候補が0件だったとき、`merge_user_draft` が `if candidates:` の**外**にあり `autopilot_offered_slots: []` を書いていた。これが落ちると「候補提示済み」フラグが消え、枠確認中に「もっと早く」と言われても再検索へ逃がせない（唯一の道）。
- 手順: draft の `merge_user_draft` は「実際にその状態になったとき」だけ呼ぶ。`merge_user_draft` は `None`/`""` を無視するが **`[]` と `{}` は書き込む**。空で上書きするつもりが無いなら、呼ぶ位置を条件の内側へ。
- 状態: L1

## 2026-09-08（未修正）: 「1時間30分」が 30分 と解釈される

- 症状: `_extract_duration_minutes("1時間30分でお願いします")` が **30** を返す。`_STATED_DURATION` のルックビハインド `(?<![時:：\d])` が「間」を弾かないため。「1時間」単独は `None`（安全側）。
- 影響: `parsed["duration_minutes"]` が取れないとき（Gemini 障害時の正規表現フォールバック）だけ通る経路。希望より短い枠で候補が組まれる。
- 状態: **未修正。** 「N時間M分」「N時間半」を先に拾う分岐が要る。着手時は `_extract_duration_minutes("1時間半で") == 90` も併せて固定すること。

## 2026-09-08: 複数LINE workerが同じイベントと会話を並列処理した

- 症状: 患者への同一返信が2通ずつ届き、キャンセル確認へ「はい」と答えても同じ確認が繰り返された。Renderログでは受信直後に `LINE reply failed: 400 Invalid reply token`、続いて `LINE push fallback sent (reason=reply_api_failed)` が発生。APSchedulerの同一実行時刻にも重複したログがあった。
- 原因: `claim_pending_events()` がpending行をロックせずSELECTしてからprocessingへ更新していた。`_LINE_EVENT_WORKER_LOCK` とAPSchedulerの `max_instances=1` はプロセス内でしか効かず、複数プロセス・コンテナのschedulerが同じDB行を同時にclaimできた。片方がreply tokenを使った後、もう片方が同じtokenで400となり、同文をpushした。別イベントも並列処理されるため、同一患者の会話状態とキャンセル手順の順序保証も失われた。
- 修正: claim SELECTへ `FOR UPDATE SKIP LOCKED` を追加。同一患者の古いpending/processingイベントが残る間は後続をclaimしない `NOT EXISTS` 条件も追加した。異なる行を同時にclaimする競合は、claimトランザクション内だけの患者単位advisory lockで原子化する。長時間のworker lockは接続だけ切れた際に排他を失うため採用しない。上限到達のprocessing行はstaleになるまで障壁にし、その後failedへ終端化して後続を解放する。
- 検証: `tests/test_line_inbox.py` で同一行の競合、同一患者の通常順序、古いイベントの遅着、同値/NULL timestamp、5回目処理中の停止復旧を実PostgreSQLで確認済み（7件成功）。LINE関連340件成功。backend全体は619件成功・既知4件失敗で、新規失敗なし。
- 鉄則: **会話キューの直列性をプロセス内ロックだけで保証しない。** DB行claimを原子的にし、同一患者の先行イベントをDB条件で障壁にする。
- 状態: L3（行ロック・短時間の患者claim lock・先行イベント障壁・実DB回帰テストで固定）

## 2026-09-08: 実DBロックテストが全体実行時だけ別イベントループで失敗

- 症状: `test_only_one_process_can_hold_the_line_worker_lease` は単独成功したが、全体スイートでは `Future attached to a different loop` で失敗した。
- 原因: モジュール共有のAsyncEngineが、先に実行された別イベントループのasyncpg接続をpoolに保持していた。テストがそのEngineを直接再利用したため、pytestの現在ループと接続のループが食い違った。
- 修正: `app.database.engine.url` から実DBテスト専用のAsyncEngineを `NullPool` で作る。ランダム名の専用schema内にキューテーブルを作り、テスト終了時にschemaごと削除する。アプリ共有Engineのpoolと開発DBの既存LINEイベントは使わない。
- 手順: 複数イベントループを作る全体スイートでasyncpgを実測するテストは、アプリ共有Engineのpoolを再利用しない。
- 状態: L1

## 2026-09-08: キューテストが削除済みのEngine参照で失敗

- 症状: `test_line_inbox.py` の実DBテスト2件が `module 'app.services.line_inbox' has no attribute 'engine'` で失敗した。
- 原因: advisory lock撤去で `line_inbox` からEngine importも削除したが、テストの接続URL取得元を直していなかった。
- 修正: Engineの所有元 `app.database.engine` をテストから明示参照する。
- 状態: L1

## 2026-09-08: 遅着した古いLINEイベントがprocessing障壁をすり抜けた

- 症状: 未commit中の患者単位claim lockでは競合を止められたが、先にclaimした新しいイベントをcommitした後、遅着した古いイベントがclaimされた。
- 原因: 先行障壁が「候補より古いpending/processing」だけを見ており、候補より新しいtimestampのprocessing行を見ていなかった。
- 修正: 同一患者の別processing行はtimestamp順に関係なく後続claimの障壁にする。短時間claim lockは未commit同士の競合防止として併用する。
- 併発: 復旧テストに `sqlalchemy.select` のimport漏れがあり `NameError`。テスト側へ明示importした。
- 状態: L3（遅着イベント実DBテストで固定）
