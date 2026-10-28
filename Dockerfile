FROM python:3.12-slim
ARG VERSION=dev
ENV APP_VERSION=$VERSION
LABEL org.opencontainers.image.source="https://github.com/d56Wr/webshare-arr"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["python", "-u", "app.py"]
