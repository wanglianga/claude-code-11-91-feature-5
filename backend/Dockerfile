FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8000

# 先建表/灌种子（空库才灌），再启动 API
CMD ["sh", "-c", "python -m app.bootstrap && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
