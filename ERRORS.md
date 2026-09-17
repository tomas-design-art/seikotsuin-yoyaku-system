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

## 2026-09-16: 会話状態の入れ子の更新が DB に保存されていなかった（2026-08-15 から）

- 症状: 「いつも当院をご利用いただきありがとうございます。」が毎回付く。プロンプトの「挨拶は繰り返さない」も、9/8 に入れた「2通目以降は挨拶を削る」も効かない。
- 原因: `line_state._normalize_context` が `dict(context_data)` の浅いコピーだった。`context_data` は素の JSONB 列なので、SQLAlchemy は「代入された値が前の値と == で等しいか」で変更を判定する。入れ子のリスト/dict は読み込んだ値と共有されたままなので、`append_conversation_history` の append や `update_request` の update が読み込んだ値そのものを書き換え、代入しても新旧が等しくなり UPDATE が出ない。本物の PostgreSQL（asyncpg・JSONB）で確認: 会話履歴に assistant が1件も残らない／依頼の status・alternatives が残らない。直後に別の書き込みが続いても残らない。
- なぜ気づけなかったか: 既存テストは DB を `AsyncMock`、状態を `SimpleNamespace` で代用しており、SQLAlchemy の変更検出を通らない。
- 修正: `_normalize_context` を `copy.deepcopy` にした。
- 手順: **JSONB 列の入れ子を書き換える処理のテストは、本物の DB で「別セッションで読み直して残っているか」を見る。**モックでは緑になる。
- 状態: L3（`tests/test_line_state_persistence.py`。浅いコピーへ戻すと本命4件が落ちることを確認済み）

## 2026-09-16: 履歴が保存されるようになったことで表に出た3件

- **確定文の書き戻し**: 確定の分岐は `clear_user_draft` → `_compose_autopilot_reply("confirmed")` の順。返信が保存されるようになると、消した直後の履歴へ「ご予約を確定しました 9/10 14:00」が入り、次の会話の解析へ持ち込まれる。会話を終える5場面（confirmed / cancel_done / change_done / cancel_aborted / change_aborted）は履歴に書かない。L3。
- **挨拶の削りすぎ**: 旧正規表現は「いつも」から最初の「ありがとうございます」までを何でも削っていたので、「いつもの 全身・60分・時田でよろしいですか？ご連絡ありがとうございます。\nはい / いいえ」が「はい / いいえ」だけになった。挨拶の語（当院・ご利用・ご来院・お世話）必須・文の区切りをまたがない・20文字上限にした。骨格経路では削った後に `rejects()` をもう一度かけ、欠けたら削らずに送る。L3。
- **繰り返し検出の誤爆**: `_has_repeated_reply_opening`（先頭24文字が前回と同じなら作り直し→定型文＋院長通知）は履歴が無く一度も動いていなかった。挨拶は正規化後23文字なので、挨拶付き同士が「繰り返し」扱いになる。比較前に両方から挨拶を外す。L3。
- 鉄則: **壊れていた保存を直すと、「保存されないこと」を前提に動いていた箇所が一斉に動き出す。**直す前に、その値を読む箇所を全部洗う。

## 2026-09-16: 手元の PostgreSQL（Windows サービス）が接続を切り、実DBテストが落ちる

- 症状: `tests/test_line_inbox.py` と `tests/test_line_state_persistence.py` が `ConnectionResetError: [WinError 64]` で落ちる（手元の `DATABASE_URL` は localhost:5432）。
- 手順: 使い捨てのコンテナに向けて回す。
  `docker run -d --rm --name yoyaku-test-pg -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe -p 127.0.0.1:55432:5432 postgres:15`
  `DATABASE_URL="postgresql+asyncpg://postgres:probe@127.0.0.1:55432/probe?ssl=disable" pytest -q tests`
  起動直後は初期化で一度再起動するので、`select 1` が安定して通るまで待つ。この条件で 636 passed / 既知4 failed。
- 状態: L1（手元の PostgreSQL サービスの原因は未調査）

## 2026-09-16: 確認の「はい」が直前のメッセージと合成され、3回押しても効かなかった

- 症状: 「やっぱりキャンセルしたい」→[はい] で、同じキャンセル確認が3回返り、4回目でようやく実行された（実機 1:05〜1:06）。
- 原因: `line_debounce.merge_debounced_message` が10秒以内の連続メッセージを1本に繋げる。`_should_merge` は「直前に予約の意図があれば繋げる」判定なので、確認への「はい」も直前の「やっぱりキャンセルしたい」に繋がり、本文が `やっぱりキャンセルしたい\nはい` になっていた。確認の答えは `confirmation.read_answer` の判定順1「本文がちょうど はい／いいえ」で読むので、合成された本文はボタンの答えとして読めない。さらに会話履歴が保存されるようになったことで解析が文脈から日時を拾うようになり、判定順3「別の希望を述べている」に当たって UNCLEAR になった（だから毎回ではなく、拾った回だけ失敗する）。10秒の合成が切れた回だけ「はい」単独になり実行された。
- 修正: (1) はい/いいえ だけの返事は合成しない（`_BARE_YES_NO`）。(2) 確認待ちのモード（`_CONFIRM_FORM_MODES` の5つ）では合成そのものを行わず、バッファを捨てる。分割送信の合成は従来どおり残す。
- 手順: **本文の見た目で判定する処理を足すときは、その本文が途中で作り変えられていないかを先に確認する。**この合成は `_handle_text_message` の入口で本文を差し替えている。
- 状態: L3（`test_a_bare_yes_or_no_is_never_merged_into_the_previous_message` ほか3件。両方の層を無効化すると落ちることを確認済み）

## 2026-09-16: 会話の記録を直したら、AIが自分の過去の返事を読んで会話の進行を決め始めた

- 症状（実機 9:35〜9:37）: 「時田先生の空いているお時間は？」→「時田はあいにく予約がいっぱい」、「午後も？」→「午後も含め時田の枠はすべて埋まって」、「時田先生で、今日の午後2時頃！」→「本日午後の時田の枠は満席」。予約ボード上は時田の午後は空いていた。「今日できますか？」には候補を出さず「2026/09/16ですね…お時間は？」と聞き返した。
- 原因（構造）: 「解析AIに会話履歴を渡す」（8/18）と「AIが決めた返信目的 conversation_goal を状況より優先」（8/24）は、会話の記録が壊れていた期間（8/15〜9/16）に入っており、**本番では一度も本物の履歴を受け取らないまま調整されていた**。9/16 の `8f9ac3a` で記録を直した途端、AIが自分の誤った返事を読み、それに合わせて以後の返事を決めた。クラは「日付を引きずるか」しか確認せず、AIが会話の進行を決める側に回ることを見落とした。
- 原因（事実集め・8/18 からの既存バグ）: 空きの質問は名指しの担当を渡さず、全員から朝の早い順に3件だけ取っていたので、時田の枠がAIに渡らなかった。解析AIの date が非ISOだと候補探しを黙って飛ばしていた。
- 修正: 返信AIにも解析AIにも会話履歴を渡さず、記録もしない（9/16 以前に本番で動いていた条件に戻す）。挨拶の1日1回はコードの `last_greeted_on` で判定するので履歴は不要。空きの質問で名指しの担当の枠を探し、その担当が出勤か・空きがあるかをコードが数えて渡す。新規予約の候補と空きの質問は、いつもの担当の枠を先頭に（変更の交渉は時刻順のまま）。date は読める形なら YYYY-MM-DD に揃え、**読めない値は捨てない**（捨てると確認の「はい、20日の方で」がはいと読まれ、取消まで進む。レビューで検出）。
- 鉄則: **壊れていた入力を直すときは、その入力を「受け取るはずだった側」が、壊れていた間に何を前提に作られたかを先に洗う。**AIへの入力が増える変更は、AIに判断を任せる経路を増やす変更として扱う。
- 状態: L3（`test_the_parser_is_not_given_the_conversation_history`、`test_the_reply_ai_is_not_given_the_conversation_history`、`test_an_availability_question_searches_the_named_practitioners_slots`、`test_an_unreadable_date_in_a_confirmation_reply_is_still_a_different_wish` ほか。変更前のコードで新しいテストが落ちることを確認済み）

## 2026-09-17: RPA 用の一覧が「メニューなし・色つき」の予約1件で 500 になり、サロンボードへの転記が止まった

- 症状: `GET /api/hotpepper/pending-sync`・`reconcile-queue` が 500。院の RPA は「転記元 pending-sync 取得失敗: HTTP 500」で毎分失敗し、ブラウザも開かない。9/17 12:01 以降の予約（当日 18:30 を含む）が未転記。9/10 16:29〜19:57・9/14 09:54〜13:15 も同じ途切れ方。
- 原因: 3つの一覧（pending-sync / reservations-by-date / reconcile-queue）が `Reservation.color` を先読みしていなかった。`build_reservation_response` が色を読むと、非同期セッションでは遅延読み込み → `MissingGreenlet`。一覧に同じ色を持つメニューの予約があると `Menu.color`（lazy="joined"）で色がセッションに載って落ちないため、落ちたり直ったりした。引き金は 9/16 登録の「メニューなし・色1」の電話予約（2679）。コード自体は 2026-04 から。
- 見落とした理由: 既存テスト（test_hotpepper_api_endpoints.py）は DB を AsyncMock にしていて遅延読み込みを通らない。**`rpa_call_logs` は、エンドポイントが例外で落ちた 500 を記録しない**（BaseHTTPMiddleware で call_next が例外を投げ、記録の前に抜ける）。そのため「RPA が来ていない」と誤読した。
- 手順: 3つの一覧は `_response_load_options()` で色まで先読みする。`build_reservation_response` に渡す予約は、読む関連（patient / practitioner / menu / color）を全部先読みする。RPA の不調調査で `rpa_call_logs` に行が無いときは「来ていない」と「落ちて記録されなかった」の両方を疑い、エンドポイントを直接叩いてステータスを見る。
- 状態: L3（本物の PostgreSQL で「色つき・メニューなし」を再現する tests/test_hotpepper_rpa_queues_real_db.py。修正前に3件とも MissingGreenlet で落ちることを確認）。再発に備え、一覧は1件ずつ作り、作れない予約だけ飛ばして残りは渡す（飛ばした予約は /health の unlistable_pending に出る）。例外で落ちた回も rpa_call_logs に status 500・`_error` 付きで残す。どちらも修正前のコードで落ちるテストで固定。

## 2026-09-17: 先読みの設定をモジュールの読み込み時に作り、アプリの起動で mapper 初期化が落ちた（2回目）

- 症状: `selectinload(Reservation.color)` を hotpepper.py のモジュール定数にしたら、テスト収集で `ReservationColor failed to locate a name`。本番なら起動しない。
- 原因: 2026-09-04 の「部分 import で mapper 未登録」と同じ型。`selectinload(関連)` は作った時点で mapper の初期化を走らせる。ルーターの import 時点では ReservationColor がまだ登録されていない。
- 手順: `selectinload` などの関連を参照するオプションは、モジュール定数にせず関数の中で作る（クエリ実行時に作る）。
- 状態: L3（`import app.main` するテストの収集で必ず落ちる。今回もそれで検出）。2回目のため記録を残す。
