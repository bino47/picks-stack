FROM python:3.12-slim
WORKDIR /app
COPY scripts/ /app/scripts/
COPY seed/ /app/seed/
COPY start.sh /app/start.sh
RUN pip install --no-cache-dir numpy
ENV DATA_ROOT=/var/data
# env at deploy time: BETSAPI_KEY (never baked into the image)
CMD ["bash", "start.sh"]
