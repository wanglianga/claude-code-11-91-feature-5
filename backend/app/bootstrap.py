"""容器启动引导：建表，仅在空库时灌入种子数据（重复执行安全，不重置已有数据）。

需要强制重置演示数据时运行：python -m app.seed
"""

from .database import SessionLocal, engine
from .models import Base, Site
from .seed import seed


def bootstrap() -> None:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.query(Site).count() == 0:
            db.close()
            seed()
        else:
            print("数据库已有数据，跳过种子灌入。")
    finally:
        db.close()


if __name__ == "__main__":
    bootstrap()
