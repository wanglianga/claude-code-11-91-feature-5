import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev",
    )
    # 演示系统统一使用朴素本地时间（前端 datetime-local 与之对齐）
    app_name: str = "充电预约服务"
    cors_origins: list[str] = ["*"]
    # 候补一次性邀请的默认有效秒数（接口可用 invite_expires_seconds 覆盖）
    waitlist_invite_ttl_seconds: int = int(
        os.getenv("WAITLIST_INVITE_TTL_SECONDS", "120")
    )


settings = Settings()
