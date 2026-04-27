# Ozod Hosting

Telegram bot orqali foydalanuvchi loyihalarini Docker container ichida deploy va boshqarish tizimi.

## Tuzilma

- `main.py` - bot, API, SQLite, ichki queue worker, Docker runner hammasi bitta faylda
- `Dockerfile` - app image
- `requirements.txt` - Python dependency
- `projects/` - user project source va build output
- `data/ozod.sqlite` - SQLite baza
- `docker-compose.yml` - app + nginx

## Ishga tushirish

1. `.env.example` dan `.env` yarating
2. `BOT_TOKEN` va `API_TOKEN` ni to'ldiring
3. `docker compose up --build -d`

## Render Deploy

`render.yaml` qo'shilgan. Render Blueprint yoki oddiy Web Service orqali deploy qilsa bo'ladi.

Muhim:
- SQLite va `projects/` saqlanishi uchun persistent disk kerak
- `render.yaml` diskni `/data` ga mount qiladi
- Render env vars ichida kamida `BOT_TOKEN`, `API_TOKEN`, `PUBLIC_BASE_URL` ni to'ldiring
- `PUBLIC_BASE_URL` sifatida Render service URL ni yozing

## Asosiy imkoniyatlar

- GitHub yoki ZIP orqali loyiha yaratish
- Har loyiha uchun alohida container
- Start, pause, restart, delete
- Logs yuklab olish
- GitHub commit o'zgarsa auto-redeploy
- SQLite bilan yengil local persistence
- Redis talab qilinmaydi
- Node.js va Python detection
- Frontend build bo'lsa static output saqlash
