FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn==0.30.1 \
    openai==1.35.0 \
    tavily-python==0.3.3 \
    ddgs==6.3.7

COPY agent_server.py .

EXPOSE 8080

CMD ["python", "agent_server.py"]
