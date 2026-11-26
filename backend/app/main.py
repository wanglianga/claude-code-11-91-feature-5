from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import operations, reservations, resources, search, waitlist

app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    description=(
        "充电预约演示系统：固定枪口 / 资源池两种预约粒度、事务级容量重算、"
        "幂等确认与维护影响分析；并支持候补排队、一次性空档邀请与"
        "FIFO / 兼容优先两种公平分配规则。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resources.router)
app.include_router(search.router)
app.include_router(reservations.router)
app.include_router(operations.router)
app.include_router(waitlist.router)
app.include_router(waitlist.policy_router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
