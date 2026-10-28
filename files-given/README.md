# کارخانه داده 311 نیویورک

یک package پایتونی که شکایت‌های 311 نیویورک (NYPD، شش نوع noise، ژانویه 2025 تا ژوئن 2026) را دریافت، اعتبارسنجی، پاک‌سازی و با جمعیت هر محله ترکیب می‌کند.

## اجرا

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    pip install -e .
    python -m unittest discover -s tests      # تست‌ها، بدون اینترنت
    python -m data_factory run                # کل کارخانه با یک فرمان

نیازمند Python 3.11 یا بالاتر است. برای اجرای نوت‌بوک بدون `requirements.txt` می‌شود از `pip install -e ".[analysis]"` استفاده کرد.

اجرای کامل از پوشه‌ی خالی شامل دانلود حدود 1.08 میلیون رکورد است. اجرای دوباره روی همان بازه، بدون درخواست جدید (`requests=0`) و با hash یکسان خروجی‌ها انجام می‌شود. اجرای `--since-last-run` فقط داده‌ی بعد از آخرین اجرا را می‌گیرد و خروجی‌اش در `data/curated/rolling/` جدا ذخیره می‌شود. توکن Socrata اختیاری است: متغیر `SOCRATA_APP_TOKEN`.

## خروجی‌ها (data/)

- `curated/cases.csv`: dataset سطح پرونده با مدت تا بسته‌شدن
- `curated/complaints_by_area_month.csv`: جدول wide منطقه × نوع شکایت × ماه (تعداد و نرخ به ازای 10,000 نفر)
- `curated/data_quality_report.json`: گزارش کیفیت هر ستون
- `quarantine/quarantine.jsonl`: رکوردهای ردشده با کد دلیل
- `run_manifest.json`: شمارش‌ها، بودجه‌ی درخواست و hash هر خروجی

## فرمان‌های دیگر

    python -m data_factory fetch | fetch-reference | validate | clean
    python -m data_factory run --since-last-run   # فقط داده‌ی بعد از آخرین اجرا

تنظیمات در `config/default.toml`. نوت‌بوک: `solution.ipynb` (Restart & Run All).