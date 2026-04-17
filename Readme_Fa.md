# ربات تلگرامی مدیریت VPS

این پروژه یک ربات تلگرامی برای اجرای عملیات شل روی VPS است که فقط برای یک **وایت‌لیست کوچک از کاربران مورد اعتماد** طراحی شده است.

کاربردهای اصلی:
- اجرای دستورهای شل
- نگه‌داری مسیر کاری مستقل برای هر کاربر
- اجرای زنده با PTY
- مشاهده خروجی زنده و tail
- شروع سریع سشن شل از داخل UI تلگرام
- آپلود و دانلود فایل
- نمایش SHA256 برای صحت فایل

## قابلیت‌ها

- کنترل دسترسی با `ALLOWED_USER_IDS`
- مسیر کاری جداگانه برای هر کاربر
- ماندگاری تنظیمات هر کاربر (مثلاً وضعیت stream on/off)
- سشن زنده PTY (فقط یک سشن فعال برای هر کاربر)
- کنترل سشن: Stop، Ctrl+C، Ctrl+D، Enter
- پاک‌سازی escape sequence های ترمینال از خروجی
- حالت استریم خروجی با `/stream` یا `/live`
- شروع سریع `bash` و `zsh` از داخل دکمه‌های بات
- رندر هوشمند خروجی monospace با واحدهای کپی مناسب برای history، path، URL، key/value و stream frame
- آپلود فایل در مسیر کاری فعلی با تایید overwrite
- محدودیت اندازه آپلود + خطای شفاف برای فایل بزرگ
- ضد stale شدن دکمه‌های overwrite/cancel آپلود
- دانلود فایل با `/get <path>`
- نمایش SHA256 در آپلود/دانلود
- پیشنهاد دستورها با `/` (Bot Command Menu)
- کیبورد اکشن سریع ثابت
- دکمه‌های inline کمکی (`Help`, `Status`, `Tail`) برای پیام‌های usage/error
- لاگ عملیاتی پایه (startup/session/upload/get)

## دستورات

- `/help` نمایش راهنمای دوزبانه داخل بات
- `/id` نمایش شناسه تلگرام
- `/run <command>` اجرای دستور
- `/run cd <path>` تغییر مسیر کاری فعلی
- `/sessions [page]` نمایش لیست سشن‌ها (صفحه‌بندی)
- `/attach [session_id|suffix]` اتصال به سشن detached در حال اجرا
- `/detach` جدا شدن از سشن فعلی بدون توقف آن
- `/status [session_id|suffix]` نمایش وضعیت سشن فعال/هدف
- `/tail [session_id|suffix]` نمایش خروجی اخیر (یا آخرین سشن)
- `/stop [session_id|suffix]` توقف سشن فعال/هدف
- `/kill [session_id|suffix]` پایان فوری با تایید دو مرحله‌ای
- `/ctrl c` ارسال Ctrl+C
- `/ctrl d` ارسال Ctrl+D (EOF)
- `/n` ارسال Enter/Newline
- `/stream on|off|toggle|status` مدیریت استریم خروجی
- `/live ...` نام جایگزین برای `/stream ...`
- `/get <path>` دریافت فایل از VPS
- `/codex <task>` اجرای Codex به‌صورت non-interactive روی مسیر فعلی

رفتار بافر:
- در حالت stream `on` قبل از هر ورودی تعاملی جدید، بافر پاک می‌شود.
- در حالت stream `off` دستور `/tail` خروجی را نشان می‌دهد و همان بخش را مصرف (پاک) می‌کند.
- وضعیت stream برای هر کاربر در SQLite ذخیره می‌شود و تا وقتی کاربر تغییرش ندهد باقی می‌ماند.
- خروجی زنده به‌صورت frameهای پشت‌سرهم نمایش داده می‌شود و دکمه‌ها فقط روی آخرین frame فعال می‌مانند.
- خروجی‌های بلند هم بر اساس طول پیام و هم بودجه formatting تلگرام chunk می‌شوند.
- frameهای زنده قبل از خراب شدن formatting rollover می‌شوند تا حالت monospace و copy-friendly حفظ شود.

رفتار رندر خروجی:
- خط‌های شبیه history به شکل `شماره + کل دستور` رندر می‌شوند تا خود دستور با یک tap کپی شود.
- pathها، URLها، proxy linkها، hashها، UUIDها و موارد مشابه به‌صورت یک واحد کپی نمایش داده می‌شوند.
- خط‌های `key=value` یا `key: value` به‌صورت key + value رندر می‌شوند، نه کلمه‌به‌کلمه.
- خروجی‌های table-like و log-like هم تا جای ممکن ساختار monospace خوانا را حفظ می‌کنند.

رفتار Codex:
- با `/codex <task>`، Codex به‌صورت non-interactive در مسیر کاری فعلی شما اجرا می‌شود.
- دکمه شیشه‌ای `Codex` مسیر اصلی UX است و یک panel برای workspace و وضعیت Codex باز می‌کند.
- مسیر دیفالت Codex همان دایرکتوری‌ای است که کاربر هنگام زدن دکمه `Codex` در آن قرار دارد.
- از داخل panel می‌توان:
  - با ارسال مستقیم متن task را شروع کرد
  - حالت continue/new را تغییر داد
  - مدل را از لیست دکمه‌ای انتخاب کرد
  - سطح reasoning effort را انتخاب کرد (`low`/`medium`/`high` و قابل تنظیم)
  - مسیر را عوض کرد
  - وضعیت/session فعلی را دید
  - لاگ/Retry/Changes/Files/Patch آخرین run را گرفت
  - run فعال را cancel کرد
  - session فعلی را بست
- اگر سشن Codex فعال باشد و shell/PTTY فعالی نداشته باشید، متن ساده‌ی بعدی به‌صورت خودکار ادامه همان گفت‌وگوی Codex حساب می‌شود.
- اگر shell/PTTY فعال باشد، متن ساده همچنان اول به shell می‌رود و رفتار تعاملی فعلی حفظ می‌شود.
- علاوه بر دکمه‌های شیشه‌ای، زیر پیام نتیجه‌ی Codex هم inline actionهای بعد از اجرا (`Retry`، `Logs`، `Changes`، `Files`، `Export Patch`) نمایش داده می‌شود.
- مدل انتخاب‌شده هر Codex run در SQLite ذخیره می‌شود تا گزارش وضعیت/لاگ همیشه دقیق باشد.
- reasoning effort انتخاب‌شده هم برای هر Codex run در SQLite ذخیره می‌شود.
- لیست پیش‌فرض مدل‌ها (اگر env ست نشده باشد): `gpt-5.4`، `gpt-5.4-mini`، `gpt-5.3-codex`، `gpt-5.2`.
- لیست پیش‌فرض effortها (اگر env ست نشده باشد): `low`، `medium`، `high`، `xhigh`.

## کیبورد اکشن سریع

دکمه‌های فعلی پایین چت:
- `Status`
- `Tail`
- `Sessions`
- `Stop`
- `Detach`
- `Ctrl+C`
- `Ctrl+D`
- `Enter`
- `Stream`
- `Help`
- `Open Shell`
- `Codex`

اکشن‌های سریع شل:
- با `Open Shell` یک picker برای `zsh` و `bash` باز می‌شود.
- در `/start` هم دکمه‌های شروع سریع shell نمایش داده می‌شوند.
- در `/sessions` هم دکمه‌های `New zsh` و `New bash` برای ساخت سشن جدید وجود دارد.

## مثال‌های Bash و Zsh

- شروع سشن bash:
  - `/run bash`
  - سپس پیام متنی ساده بفرستید: `pwd`
  - خروج: `/ctrl d` یا `/stop`

- شروع سشن zsh:
  - `/run zsh`
  - سپس پیام متنی ساده بفرستید: `whoami`
  - خروج: `/ctrl d` یا `/stop`

- شروع shell از داخل UI تلگرام:
  - روی `Open Shell` بزنید
  - `zsh` یا `bash` را انتخاب کنید
  - بعد دستور متنی ساده مثل `pwd` بفرستید

- نکته برای سشن codex:
  - اگر اشتباهی `codex` را اجرا کردید، برای خروج از دکمه‌های `Ctrl+D`، `Stop` یا `Kill` داخل بات استفاده کنید

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
MAX_RUNNING_SESSIONS_PER_USER=3
MAX_SESSION_HISTORY_PER_USER=20
SESSIONS_PAGE_SIZE=5
DETACHED_SESSION_TTL_SECONDS=3600
DETACHED_SWEEP_INTERVAL_SECONDS=30
TIME_OFFSET=+03:30
SESSION_DB_PATH=./session_store.sqlite3
LOG_LEVEL=INFO
```

### توضیح `TIME_OFFSET`

- مقدار `TIME_OFFSET` روی زمان‌های نمایشی بات و زمان‌های ذخیره‌شده در SQLite اعمال می‌شود.
- فرمت آن باید `+HH:MM` یا `-HH:MM` باشد.
- مثال برای تهران: `TIME_OFFSET=+03:30`.
- اگر تایم‌زون سرور با تایم‌زون مدنظر شما فرق دارد، این مقدار را حتماً دستی تنظیم کنید.
- مقدار اشتباه باعث می‌شود `started_at`، `ended_at` و حتی `runtime` ناسازگار دیده شوند.

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
