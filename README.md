# Ozod Hosting

Telegram bot orqali foydalanuvchi loyihalarini deploy va boshqarish tizimi.

## Tuzilma

- `main.py` - bot, API, SQLite, ichki queue worker, local process runner hammasi bitta faylda
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
- `render.yaml` Render free uchun moslangan
- Docker runner o'chirilgan, default rejim local process runner
- static website, Python app va Node.js app deploy ishlaydi
- Render free filesystem ephemeral, shu sabab SQLite va `projects/` redeploy/restartdan keyin saqlanmaydi
- Render env vars ichida kamida `BOT_TOKEN`, `API_TOKEN`, `PUBLIC_BASE_URL` ni to'ldiring
- `PUBLIC_BASE_URL` sifatida Render service URL ni yozing

## Asosiy imkoniyatlar

- GitHub yoki ZIP orqali loyiha yaratish
- Static website, Python app, Node.js app deploy
- Start, pause, restart, delete
- Logs yuklab olish
- GitHub commit o'zgarsa auto-redeploy
- SQLite bilan yengil local persistence
- Redis talab qilinmaydi
- Node.js va Python detection
- Frontend build bo'lsa static output saqlash
