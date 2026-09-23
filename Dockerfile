FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .

# Una variable por línea: si pones "\" al final de la última ENV, la línea EXPOSE se interpreta mal.
ENV HOST=0.0.0.0
ENV PORT=8098
ENV MEDIA_ROOT=/data/gopro
ENV USERS_FILE=/config/users.json

EXPOSE 8098

CMD ["python3", "-u", "app.py"]
