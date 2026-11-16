# PulBot — Telegram referral bot (Render uchun tayyor)

PulBot — do'st taklif qilib so'm ishlash botidir: kunlik bonus va streak,
ikki darajali referral, haftalik reyting va konkurs, pul yechish (Uzcard/Humo/telefon puli),
homiy kanallar orqali daromad, to'liq admin panel (narxlar, foydalanuvchilar, xavfsizlik,
broadcast, CSV eksport). Barcha inline va oddiy tugmalar **rangli** (yashil/qizil/ko'k).

> **Start bonusi:** standart holatda **o'chirilgan (0)** — `/start` uchun pul berilmaydi.
> Xohlasangiz admin panel → «Narxlar va bonuslar» → «Start bonus» orqali yoqish mumkin.

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `PulBot.py` | Botning o'zi |
| `requirements.txt` | Kerakli kutubxonalar |
| `render.yaml` | Render Blueprint (avtomatik konfiguratsiya) |
| `.github/workflows/keepalive.yml` | 24/7 keep-alive: har 5 daqiqada `/health` ni ping qiladi |

## Lokal sinov

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
set BOT_TOKEN=123456:ABC...  # Windows (cmd)
set OWNER_ID=123456789
python PulBot.py
```

## Render'da ishga tushirish (eng oson yo'l — Blueprint)

1. **Bot token** oling: Telegram'da [@BotFather](https://t.me/BotFather) → `/newbot` → tokenni saqlang.
2. **O'z ID'ingizni** oling: [@userinfobot](https://t.me/userinfobot) ga `/start` yozing.
3. Ushbu papkani GitHub'ga yuklang (quyidagi "GitHub'ga yuklash" bo'limiga qarang).
4. [Render Dashboard](https://dashboard.render.com) → **New +** → **Blueprint** → repozitoriyangizni tanlang → Apply.
   - `render.yaml` avtomatik o'qiladi; faqat `BOT_TOKEN` va `OWNER_ID` ni so'raydi.
5. Deploy tugagach loglarde `Health-check server port 8080 da ishga tushdi` va `Run polling for bot` yozuvlari chiqadi.

### Qo'lda yaratish (Blueprint'siz)

1. Render Dashboard → **New +** → **Web Service** → GitHub repo tanlang.
2. Sozlamalar:
   - Runtime: **Python 3**
   - Build Command: `pip install --no-cache-dir -r requirements.txt`
   - Start Command: `python PulBot.py`
   - Health Check Path: `/health`
3. **Environment** bo'limida qo'shing:
   - `BOT_TOKEN` = @BotFather tokeni
   - `OWNER_ID` = sizning Telegram ID
4. **Create Web Service** → kutib turing → tayyor.

## GitHub'ga yuklash (git o'rnatilmagan bo'lsa)

1. [github.com/new](https://github.com/new) → repo nomi `pulbot` → **Create repository**.
2. **uploading an existing file** havolasini bosing → `PulBot.py`, `requirements.txt`, `render.yaml`, `.gitignore`, `README.md` fayllarini tashlang → **Commit changes**.

## Muhim eslatmalar

- **24/7 ishlash (bepul plan)**: Render bepul service 15 daqiqa trafiksiz uxlab qoladi. Repo ichidagi
  `.github/workflows/keepalive.yml` har 5 daqiqada `https://<service>.onrender.com/health` ni ping qiladi —
  shu tufayli bot doim uyg'oq. GitHub'da **Actions → Render keep-alive → Enable workflow** bo'lishi shart
  (repo public bo'lsa daqiqalar cheksiz). Qo'shimcha kafolat uchun [UptimeRobot](https://uptimerobot.com)
  da ham xuddi shu manzilga 5 daqiqali monitor qo'shish mumkin.
- **Baza saqlanadi (zaxira)**: `bot.db` Render'ning vaqtinchalik diskida bo'ladi, shuning uchun bot uni
  yopiq GitHub repo'ga (`BACKUP_REPO`) har 10 daqiqada va har to'xtashdan oldin zaxiralaydi, startda esa
  o'zi tiklaydi. Buning uchun Render → Environment'da `BACKUP_REPO` (masalan `user/pulbot-data`) va
  `BACKUP_TOKEN` (repo'ga yozish huquqi bor token) bo'lishi kerak. Bu o'zgaruvchilar bo'lmasa — zaxira o'chirilgan.
- **Homiy kanal** qo'shishdan oldin botni o'sha kanalga **admin** qiling, aks holda obuna tekshiruvi ishlamaydi (bot bunday kanalni e'tiborsiz qoldiradi va xato bermaydi).
- **Admin panel**: `/admin` buyrug'i. Rollar: `owner` (hamma narsa), `moderator` (xavfsizlik, broadcast, foydalanuvchilar), `accountant` (yechish so'rovlari, hisobotlar). Admin qo'shish: `/admin` → ⚙️ Sozlamalar → 👮 Adminlar boshqaruvi.
- **Tugma ranglari**: Telegram Bot API 9.4 + aiogram 3.31 kerak (requirements.txt'da ko'rsatilgan). Eski Telegram messenger versiyalarida ranglar ko'rinmaydi (oddiy tugmalar bo'lib chiqadi).
