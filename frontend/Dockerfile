# =====================================================================
# frontend Dockerfile — multi-stage (Vite build → nginx static serve)
# =====================================================================

# ---- Stage 1: install deps + build ---------------------------------
FROM node:26-alpine AS builder

WORKDIR /app

# Install deps with the lockfile if present for deterministic builds.
COPY package.json ./
COPY bun.lock* package-lock.json* yarn.lock* pnpm-lock.yaml* ./

RUN \
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
  elif [ -f yarn.lock ];        then corepack enable && yarn install --frozen-lockfile; \
  elif [ -f pnpm-lock.yaml ];   then corepack enable && pnpm install --frozen-lockfile; \
  else npm install --no-audit --no-fund; fi

COPY . .

# Pin envs so the bundle hits nginx-prefixed paths in production.
ENV VITE_RECOMMENDATION_API_URL=/api/recommendations \
    VITE_CAMPAIGN_API_URL=/api/campaigns \
    VITE_MOBILE_API_URL=/api/mobile

RUN npm run build

# ---- Stage 2: nginx static serve ------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/dist        /usr/share/nginx/html
COPY            nginx.conf            /etc/nginx/nginx.conf

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --retries=10 \
    CMD wget -qO- http://localhost:3000/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
