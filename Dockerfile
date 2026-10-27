# OfferPilot in one container: dashboard + API + autopilot, and the Telegram bots (see deploy/start.sh).
# Used by Fly.io (fly.toml). The database lives on a persistent volume at /data; on the very first start it is
# copied there from data/offerpilot.db, which is included in the build from your laptop (it is not in git).

# 1) Build the dashboard (React)
FROM node:20-slim AS dashboard
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# 2) Python runtime
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=dashboard /app/dashboard/dist ./dashboard/dist
RUN sed -i 's/\r$//' deploy/start.sh   # Windows line endings would break bash
EXPOSE 8080
CMD ["bash", "deploy/start.sh"]
