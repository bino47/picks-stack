FROM python:3.12-slim
WORKDIR /app
COPY board_host.py index.html /app/
ENV DB_PATH=/var/data/board.db
# env at deploy time: CARD_SOURCE, SETTLE_SOURCE, FIRST_ADMIN_USERNAME, FIRST_ADMIN_PASSWORD
CMD ["python", "board_host.py"]
