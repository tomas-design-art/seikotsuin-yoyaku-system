import asyncio
import os
import asyncpg

async def create_staging_db():
    render_db_url = os.environ.get("DATABASE_URL")
    if not render_db_url:
        raise RuntimeError("DATABASE_URL 環境変数を設定してください。")
    
    print("データベースに接続中...")
    conn = await asyncpg.connect(render_db_url)
    
    try:
        print("coco_staging を作成しています...")
        await conn.execute('CREATE DATABASE coco_staging')
        print("✅ テスト用データベース (coco_staging) の作成に成功しました！")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print("⚠️ すでに coco_staging は存在しています！")
    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(create_staging_db())
