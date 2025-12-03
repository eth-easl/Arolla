FROM python:3.9-slim

WORKDIR /app

RUN pip install requests aiohttp

COPY traffic_gen.py .

CMD ["python", "-u", "traffic_gen.py"]
