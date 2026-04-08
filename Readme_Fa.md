# ربات تلگرامی مدیریت VPS

این پروژه یک ربات تلگرامی برای اجرای عملیات شل روی VPS است که فقط برای یک **وایت‌لیست کوچک از کاربران مورد اعتماد** طراحی شده است.

کاربردهای اصلی:
- اجرای دستورهای شل
- نگه‌داری مسیر کاری مستقل برای هر کاربر
- اجرای زنده با PTY
- مشاهده خروجی زنده و tail
- آپلود و دانلود فایل
- نمایش SHA256 برای صحت فایل

## قابلیت‌ها

- کنترل دسترسی با `ALLOWED_USER_IDS`
- مسیر کاری جداگانه برای هر کاربر
- سشن زنده PTY (فقط یک سشن فعال برای هر کاربر)
- کنترل سشن: Stop، Ctrl+C، Ctrl+D، Enter
- پاک‌سازی escape sequence های ترمینال از خروجی
- حالت استریم خروجی با `/stream` یا `/live`
- آپلود فایل در مسیر کاری فعلی با تایید overwrite
- محدودیت اندازه آپلود + خطای شفاف برای فایل بزرگ
- ضد stale شدن دکمه‌های overwrite/cancel آپلود
- دانلود فایل با `/get <path>`
- نمایش SHA256 در آپلود/دانلود
- پیشنهاد دستورها با `/` (Bot Command Menu)
- کیبورد اکشن سریع (غیردائمی)
- دکمه‌های inline کمکی (`Help`, `Status`, `Tail`) برای پیام‌های usage/error
- لاگ عملیاتی پایه (startup/session/upload/get)

## دستورات

- `/help` نمایش راهنمای دوزبانه داخل بات
- `/id` نمایش شناسه تلگرام
- `/run <command>` اجرای دستور
- `/run cd <path>` تغییر مسیر کاری فعلی
- `/status` نمایش وضعیت سشن فعال
- `/tail` نمایش خروجی اخیر (یا آخرین سشن)
- `/stop` توقف سشن فعال
- `/ctrl c` ارسال Ctrl+C
- `/ctrl d` ارسال Ctrl+D (EOF)
- `/n` ارسال Enter/Newline
- `/clear` پاک کردن بافر خروجی سشن فعال
- `/stream on|off|toggle|status` مدیریت استریم خروجی
- `/live ...` نام جایگزین برای `/stream ...`
- `/get <path>` دریافت فایل از VPS

## کیبورد اکشن سریع

دکمه‌های فعلی پایین چت:
- `Status`
- `Tail`
- `Stop`
- `Ctrl+C`
- `Enter`
- `Clear`
- `Stream`
- `Help`

## مثال‌های Bash و Zsh

- شروع سشن bash:
  - `/run bash`
  - سپس پیام متنی ساده بفرستید: `pwd`
  - خروج: `/ctrl d` یا `/stop`

- شروع سشن zsh:
  - `/run zsh`
  - سپس پیام متنی ساده بفرستید: `whoami`
  - خروج: `/ctrl d` یا `/stop`

## نصب و اجرا

1. کلون و ورود به پروژه:

```bash
git clone <your-repo-url>
cd tg_vps_bot
```

2. ساخت محیط مجازی و نصب وابستگی‌ها:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. ساخت فایل `.env`:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ALLOWED_USER_IDS=123456789,987654321
DEFAULT_SHELL=/bin/bash
WORKDIR=/home/your-user
MAX_TAIL_LINES=30
MAX_UPLOAD_BYTES=20971520
LOG_LEVEL=INFO
```

4. اجرا:

```bash
python -m app.main
```

## اجرای سرویس با systemd (اختیاری)

فایل نمونه سرویس در مسیر `systemd/tg-vps-bot.service` قرار دارد.

روال معمول:

```bash
sudo cp systemd/tg-vps-bot.service /etc/systemd/system/tg-vps-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-vps-bot.service
sudo systemctl status tg-vps-bot.service
```

## نکات امنیتی

- این بات برای محیط trusted و وایت‌لیست کوچک طراحی شده است.
- بررسی وایت‌لیست را حذف نکنید.
- اطلاعات حساس مثل `.env` و توکن بات را داخل گیت کامیت نکنید.
- بهتر است سرویس با کاربر غیر root اجرا شود.

## ساختار پروژه

- `app/main.py` نقطه شروع برنامه
- `app/bot.py` هندلرها و UX تلگرام
- `app/command_runner.py` مدیریت فرآیند و PTY
- `app/session_manager.py` مدیریت state سشن و بافر خروجی
- `app/config.py` تنظیمات محیطی
- `systemd/tg-vps-bot.service` نمونه سرویس
