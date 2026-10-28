# مرحله ۲ — پشت میز DBA (Olist + کارخانه مرحله ۱)

پایگاه‌داده‌ی PostgreSQL، شش پرس‌وجوی داشبورد زیر ۲۰۰ میلی‌ثانیه با شاهد پلن، View، Function،
Procedure و پل SQLAlchemy.

## پیش‌نیازها

1. PostgreSQL ۱۴ یا بالاتر، در حال اجرا.
2. ۹ فایل CSV اُلیست در `data/raw/olist/`:

       python scripts/fetch_olist.py --from-zip ~/Downloads/archive.zip   # از Kaggle
       python scripts/fetch_olist.py --mirror                              # یا آینه‌ی عمومی
       python scripts/fetch_olist.py --verify-only                         # فقط بررسی

   اسکریپت **همه‌ی ۹ شمارش سطر منتشرشده را بررسی می‌کند** و اگر یکی نخواند، فایل‌ها را قبول
   نمی‌کند. ۱۲۶ مگابایت داده‌ی خام در Git نیست.

3. خروجی مرحله ۱ در `data/curated/cases.csv` (یا `cases.csv.gz`).

## اجرا

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt && pip install -e .
    export DATABASE_URL=postgresql+psycopg://user:secret@localhost:5432/olist

    data-factory db bootstrap          # یک فرمان: از PostgreSQL خالی تا پایگاه‌داده‌ی کامل
    python -m unittest discover -s tests          # تست‌ها (بدون دیتابیس، تست‌های DB skip می‌شوند)
    python -m unittest tests.test_db -v           # ۱۳ تست لایه‌ی داده (نیازمند DATABASE_URL)

نوت‌بوک پاسخ: `solution.ipynb` — با `Restart Kernel & Run All` اجرا می‌شود و همه‌ی اعداد و
پلن‌ها را همین‌جا اندازه‌گیری می‌کند (حدود ۲ دقیقه، سقف ۱۰ دقیقه).

## ساختار

    sql/         همه‌ی DDL نسخه‌دار، به ترتیب شماره
    src/         بسته (مرحله ۱ + مقصد دیتابیس در src/data_factory/db/)
    scripts/     اندازه‌گیری‌های قابل تکرار + ساخت پوشه‌ی تحویل + دریافت داده
    tests/       تست‌های مرحله ۱ و ۱۳ تست لایه‌ی داده
    data/db/     شاهد خام: گزارش بارگذاری و نتیجه‌ی آزمایش‌ها (JSON)

`sql/02_indexes.sql` عمداً `CREATE INDEX` ندارد؛ جدول اندازه‌گیری شش کاندید و دلیل حذفشان
در همان فایل نوشته شده است.
