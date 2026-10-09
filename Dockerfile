FROM python:3.12-slim
WORKDIR /app
COPY nemesis_bot.py .
ENV HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "nemesis_bot.py"]
