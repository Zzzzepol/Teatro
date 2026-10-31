FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-cloud.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements-cloud.txt \
    && groupadd --gid 10001 teatro \
    && useradd --uid 10001 --gid teatro --no-create-home teatro \
    && mkdir /data && chown teatro:teatro /data
COPY Teatro_Bot.py teatro_members.py teatro_workflows.py teatro_series.py series_chart.py ./
USER 10001:10001
CMD ["python", "Teatro_Bot.py"]
