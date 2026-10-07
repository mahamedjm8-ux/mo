FROM node:22-bookworm-slim AS frontend
WORKDIR /app
COPY package*.json tsconfig.json vite.config.ts ./
COPY web ./web
RUN npm ci --no-audit --no-fund && npm run build

FROM python:3.12-slim
WORKDIR /app
COPY server ./server
COPY --from=frontend /app/dist ./dist
RUN useradd --create-home research && mkdir /app/.data && chown research:research /app/.data
USER research
ENV RESEARCH_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "server.app"]
