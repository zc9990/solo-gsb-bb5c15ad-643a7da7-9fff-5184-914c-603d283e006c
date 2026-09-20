"""命令行入口：python -m app.init_db —— 初始化 SQLite 数据库。"""
from .database import DB_PATH, init_db

if __name__ == "__main__":
    init_db()
    print(f"数据库已就绪: {DB_PATH}")
