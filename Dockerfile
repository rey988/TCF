FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY tcf ./tcf
COPY tcf.py ./tcf.py
COPY tcf.config.json ./tcf.config.json

RUN mkdir -p /app/state

CMD ["python", "tcf.py", "--config", "tcf.config.json", "status"]
