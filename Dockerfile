FROM python:3.11-slim

WORKDIR /app

# 设置 Python 输出不缓冲，保证云平台能实时查看日志
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY dmit_monitor.py .

EXPOSE 8080

CMD ["python", "dmit_monitor.py"]
