FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monetary_flow monetary_flow
RUN useradd -m mf && mkdir -p /app/data && chown mf /app/data
USER mf
ENV MF_DATA_DIR=/app/data TZ=Asia/Kolkata
EXPOSE 8080
CMD ["uvicorn", "monetary_flow.app:app", "--host", "0.0.0.0", "--port", "8080"]
