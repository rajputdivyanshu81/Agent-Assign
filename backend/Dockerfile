FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MINERVA_HEADLESS=true
ENV MINERVA_BROWSER_USER_DATA_DIR=/tmp/minerva-browser
ENV PORT=8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
