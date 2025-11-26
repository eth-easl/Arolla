FROM python:3.9-slim

WORKDIR /app

RUN pip install fastapi uvicorn boto3

COPY main.py .

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
