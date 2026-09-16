FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CUE_DB_PATH=/data/cues.db

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app
COPY static ./static

# SQLite 数据持久化目录
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/api/health', timeout=3).status==200 else 1)"

# 容器启动即建表、按 SEED_DEMO 写入演示数据（FastAPI startup 钩子完成初始化）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
