"""Generate stage2/solution.ipynb.

The notebook is generated rather than hand-edited: this script is the source,
the .ipynb is the artifact, and a reviewer can read the whole shape of the
answer in one file.  It is executed afterwards with nbconvert so the delivered
notebook carries real outputs (Delivery Guide 2.1.3).

    python scripts/build_stage2_notebook.py

Cell sources are written with raw triple-single-quote delimiters so the code
cells can use triple-double-quotes for their SQL without escaping.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("stage2/solution.ipynb")


def md(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {},
            "source": source.strip("\n").splitlines(True)}


def code(source: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": source.strip("\n").splitlines(True)}


CELLS: list[dict] = []
add = CELLS.append

# ============================================================ title
add(md(r'''
# مرحله ۲ — «پشت میز DBA»

**PostgreSQL · ایندکس · Execution Plan · بهینه‌سازی پرس‌وجو · View · Function و Procedure · پل Python و SQL**

داده‌ی خام Olist برزیل (۹ فایل CSV) به یک پایگاه‌داده‌ی طراحی‌شده تبدیل می‌شود، شش پرس‌وجوی
داشبورد نوشته می‌شوند، و هر کدام با **شاهد پلن** به بودجه‌ی ۲۰۰ میلی‌ثانیه می‌رسند.

## قاعده‌ای که در کل نوت‌بوک رعایت شده

هیچ عددی در این نوت‌بوک بازنویسی نشده است. هر عدد **همین حالا** اندازه‌گیری می‌شود و هر پلن
**همین حالا** از موتور گرفته می‌شود. اگر دوباره اجرا کنید اعداد چند درصد جابه‌جا می‌شوند، چون
ماشین و وضعیت cache عوض شده است؛ اما روش اندازه‌گیری یکی است: **میانه‌ی چند اجرا بعد از
گرم‌کردن، همراه با p95** — نه یک‌بار اجرا و خواندن یک عدد.

منطق سنگین در `src/data_factory/db/` است؛ این نوت‌بوک صداگذاری می‌کند و روایت می‌کند.
'''))

# ============================================================ setup
add(md(r'''
## ۰. آماده‌سازی

سه چیز لازم است و هر سه این‌جا بررسی می‌شوند، چون اگر یکی نباشد بقیه‌ی نوت‌بوک بی‌معناست:
یک PostgreSQL در دسترس، نه فایل CSV اُلیست با شمارش سطر تأییدشده، و خروجی مرحله ۱.
'''))

add(code(r'''
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import NUMERIC, SMALLINT, TIMESTAMP, VARCHAR, text

from data_factory.db.bootstrap import bootstrap
from data_factory.db.connection import engine_scope, redact_url
from data_factory.db.evidence import (
    index_experiments,
    insert_cost_experiment,
    query_evidence,
    result_frame,
    view_vs_snapshot,
)
from data_factory.db.loader import COLUMN_TYPES, write_frame
from data_factory.db.queries import BY_KEY
from data_factory.db.timing import explain

# seed ثابت، یک‌جا (AGENTS 2.1.5). این مرحله تصادفی‌سازی آماری ندارد، ولی
# قرارداد رعایت می‌شود تا دو اجرای متوالی همان اعداد را بدهند.
SEED = 20261028

pd.set_option("display.width", 170)
pd.set_option("display.max_columns", 40)
pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

# مسیرها نسبی‌اند (AGENTS 2.1.4). این نوت‌بوک هم در ریشه‌ی تحویل و هم در
# stage2/ اجرا می‌شود، و nbconvert پوشه‌ی خود نوت‌بوک را cwd می‌کند؛ پس
# ریشه‌ی پروژه پیدا و به آن تغییر مسیر داده می‌شود.
ROOT = Path.cwd().resolve()
while not (ROOT / "pyproject.toml").is_file() and ROOT.parent != ROOT:
    ROOT = ROOT.parent
os.chdir(ROOT)
print("project root:", ROOT)

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit(
        "DATABASE_URL تنظیم نشده است. مثال:\n"
        "  export DATABASE_URL=postgresql+psycopg://user:secret@localhost:5432/olist"
    )

SOURCE_DIR = Path("data/raw/olist")
CURATED = Path("data/curated/cases.csv")
if not CURATED.exists() and Path("data/curated/cases.csv.gz").exists():
    CURATED = Path("data/curated/cases.csv.gz")

print("SEED        :", SEED)
print("DATABASE_URL:", redact_url(DB_URL))
print("source dir  :", SOURCE_DIR, "| exists:", SOURCE_DIR.is_dir())
print("curated     :", CURATED, "| exists:", CURATED.exists())
'''))

add(code(r'''
EXPECTED_ROWS = {
    "olist_orders_dataset.csv": 99_441,
    "olist_order_items_dataset.csv": 112_650,
    "olist_order_payments_dataset.csv": 103_886,
    "olist_order_reviews_dataset.csv": 99_224,
    "olist_customers_dataset.csv": 99_441,
    "olist_sellers_dataset.csv": 3_095,
    "olist_products_dataset.csv": 32_951,
    "olist_geolocation_dataset.csv": 1_000_163,
    "product_category_name_translation.csv": 71,
}

rows = []
for name, expected in EXPECTED_ROWS.items():
    path = SOURCE_DIR / name
    actual = None
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            actual = sum(1 for _ in reader)
    rows.append({"file": name, "rows": actual, "expected": expected,
                 "ok": actual == expected})
source_check = pd.DataFrame(rows)
print("همه‌ی ۹ فایل با شمارش منتشرشده برابرند:", bool(source_check["ok"].all()))

with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar_one()
        settings = conn.execute(text(
            "SELECT current_setting('shared_buffers') AS shared_buffers,"
            "       current_setting('work_mem') AS work_mem,"
            "       current_setting('max_parallel_workers_per_gather') AS parallel"
        )).mappings().one()
print(version.split(",")[0])
print(dict(settings))
source_check
'''))

# ============================================================ bootstrap
add(md(r'''
## ۱. یک فرمان: از PostgreSQL خالی تا پایگاه‌داده‌ی کامل

معیار پذیرش اول: «با یک فرمان، از PostgreSQL خالی تا Database کامل و پرشده بالا بیاید.»

این فرمان چهار کار می‌کند: ساختن دیتابیس اگر نباشد، اجرای همه‌ی `sql/*.sql`، بارگذاری در
ترتیب وابستگی کلید خارجی، و ساخت پنل‌های از پیش محاسبه‌شده. بارگذاری در **یک تراکنش** انجام
می‌شود، پس شکست در سطر ۹۰٬۰۰۰ جدول `order_item` پایگاه‌داده را همان‌قدر خالی می‌گذارد که بود.

بارگذاری، کد یک‌بارمصرف نیست: مقصد دیتابیس به همان بسته‌ی مرحله ۱ اضافه شده است
(`src/data_factory/db/`) و با همان قرارداد خطا و `exit code` کار می‌کند.
'''))

add(code(r'''
started = time.perf_counter()
report = bootstrap(DB_URL, source_dir=SOURCE_DIR, curated_path=CURATED)
elapsed = time.perf_counter() - started

print(f"database created : {report.created_database}")
print(f"DDL files applied: {[item['file'] for item in report.ddl]}")
print(f"wall clock       : {elapsed:.1f} s")
print()
for name, count in sorted(report.tables.items()):
    print(f"  {name:28} {count:>10,} rows")
'''))

add(md(r'''
### ۱.۱ چه رکوردی بارگذاری نشد، و چرا

قاعده‌ی سخت: داده‌ای که محدودیت را نقض می‌کند **قبل از بارگذاری** تعیین تکلیف می‌شود، نه با
حذف محدودیت. هر دلیل با شمارش و با شناسه‌ی نمونه ثبت شده تا قابل ردیابی باشد.

دو سیاست متفاوت، و تفاوتشان مهم است:

- **رد (rejection)**: رکورد نمی‌تواند محدودیت را برآورده کند، پس بارگذاری نمی‌شود.
- **اصلاح (repair)**: رکورد با تبدیل یک مقدار غیرممکن به «نامعلوم» بارگذاری‌شدنی می‌شود.
  هیچ محدودیتی برای عبور دادن رکورد حذف نشده است.
'''))

add(code(r'''
rejections = pd.DataFrame(report.rejections)
by_reason = (rejections.groupby("reason", as_index=False)["row_count"].sum()
             .sort_values("row_count", ascending=False))
by_reason["share_of_raw_pct"] = (100 * by_reason["row_count"] / 1_124_000).round(4)
print(f"کل رکوردهای ردشده: {int(by_reason['row_count'].sum()):,}")
by_reason
'''))

add(code(r'''
repairs = pd.DataFrame(report.repairs)[["source_file", "reason", "row_count", "detail"]]
print("مقادیر اصلاح‌شده (نه حذف‌شده) — هر کدام به «نامعلوم» تبدیل شده، نه به صفر:")
repairs[["source_file", "reason", "row_count"]]
'''))

add(md(r'''
### ۱.۲ شاهد اینکه محدودیت‌ها واقعاً کار می‌کنند

محدودیتی که هرگز چیزی را رد نکرده، شاهد نیست. چهار محدودیت نمونه با درج عمداً نامعتبر داخل
savepoint آزمایش می‌شوند و هر چهار مورد باید رد شوند.
'''))

add(code(r'''
probes = pd.DataFrame(
    [{"probe": name, "insert_refused": refused}
     for name, refused in report.checks["constraints_refused_probe"].items()]
)
print("هر چهار کاوش رد شد:", report.checks["constraints_all_enforced"])
print("یتیم کلید خارجی، همه صفر:", report.checks["fk_all_zero"])
probes
'''))

# ============================================================ schema
add(md(r'''
## ۲. طراحی schema

نوع ستون‌ها را خودم انتخاب کرده‌ام، نه از روی CSV. تصمیم‌های غیربدیهی، با دلیل، در
`sql/01_schema.sql` نوشته شده‌اند:

- **`timestamp` بدون منطقه‌ی زمانی** — منبع هیچ اطلاعات منطقه‌ی زمانی ندارد؛ `timestamptz`
  یعنی اختراع یک منطقه برای داده‌ای که ثبتش نکرده است. هزینه‌اش: برای پنجره‌ی DST سال‌های
  ۲۰۱۷–۲۰۱۸ یک تحلیل ساعتی می‌تواند یک ساعت خطا داشته باشد؛ هیچ کوئری این مرحله بر حسب ساعت
  گروه‌بندی نمی‌کند، پس پذیرفته شد.
- **پول `numeric(10,2)`** — `price` و `payment_value` مبلغ دقیق‌اند و در ۱۱۲هزار سطر جمع
  می‌شوند؛ `float` خطای نمایش را در رقم‌های درآمد انبار می‌کند.
- **`char(5)` برای پیش‌شماره‌ی ZIP** — شناسه است نه کمیت؛ `01037` و `1037` به‌عنوان عدد
  یکسان‌اند و به‌عنوان شناسه نه.
- **`customer_id` کلید اصلی است، نه `customer_unique_id`** — ۹۹٬۴۴۱ سفارش برای ۹۶٬۰۹۶ نفر؛
  نگه‌داشتن هر دو ستون باعث می‌شود دوباره‌شماری در تحلیل «به ازای هر نفر» دیده شود.
- **جغرافیا کلید خارجی اجباری نیست** — ۱۵۷ کد پستی مشتری و ۷ کد فروشنده مختصات ندارند؛
  کلید خارجی سخت، ۲۸۵ سفارش سالم را به‌خاطر یک غنی‌سازی غایب رد می‌کرد.
- **کلید طبیعی بازدید `(review_id, order_id)`** — `review_id` یکتا نیست: ۸۱۴ شناسه بین
  سفارش‌های مختلف مشترک‌اند.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        columns = pd.DataFrame(conn.execute(text("""
            SELECT table_schema || '.' || table_name                AS table_name,
                   count(*)                                         AS columns,
                   count(*) FILTER (WHERE is_nullable = 'NO')        AS not_null
            FROM information_schema.columns
            WHERE table_schema IN ('olist', 'factory')
            GROUP BY 1 ORDER BY 1
        """)).mappings().all())
        constraint_count = conn.execute(text("""
            SELECT count(*) FROM information_schema.table_constraints
            WHERE table_schema IN ('olist', 'factory')
        """)).scalar_one()
print(f"محدودیت‌ها: {constraint_count}")
columns
'''))

add(code(r'''
# توضیح‌های ثبت‌شده بخشی از طراحی‌اند: دلیلی که نوشته نشود، بعداً از دست می‌رود.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        notes = pd.DataFrame(conn.execute(text("""
            SELECT n.nspname || '.' || c.relname AS object, d.description
            FROM pg_description d
            JOIN pg_class c ON c.oid = d.objoid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('olist', 'factory')
            ORDER BY 1
        """)).mappings().all())
notes
'''))

# ============================================================ correctness
add(md(r'''
## ۳. درستی، پیش از سرعت

قاعده‌ی ۴.۵: «درستی نتیجه اول، سرعت بعد: قبل از بهینه‌سازی، نتیجه‌ی هر کوئری روی داده‌ی
کوچک تأیید شود.»

کوئری‌ای که سریع شود ولی غلط باشد، بهینه نشده است. پس اول روی یک مجموعه‌ی کوچک و کاملاً
کنترل‌شده بررسی می‌شود: سه سفارش با تأخیرهای دستی ۱-، ۵ و ۱۵ روز. تست‌ها داخل تراکنشی
اجرا می‌شوند که `rollback` می‌شود، پس داده‌ی اصلی دست نمی‌خورد.
'''))

add(code(r'''
env = dict(os.environ, DATABASE_URL=DB_URL)
# sys.executable, not "python": the kernel's interpreter is the venv one, and
# a bare "python" resolved to a different interpreter without sqlalchemy.
completed = subprocess.run(
    [sys.executable, "-m", "unittest", "tests.test_db", "-v"],
    capture_output=True, text=True, env=env, timeout=900,
)
tail = [line for line in completed.stderr.splitlines()
        if line.startswith("test_") or line.startswith("Ran ") or line.strip() == "OK"]
print("\n".join(tail[-8:]))
print("exit code:", completed.returncode)
'''))

add(md(r'''
### ۳.۱ چه چیزی تأیید شد

- مرزهای `delay_bucket` در دو طرف هر مرز (`0/1/3/4/7/8/14/15`)
- NULL در ورودی، NULL در خروجی — تأخیر نامعلوم نباید «به‌موقع» شود
- سفارشی که در روز وعده تحویل شده «دیر» نیست (مقایسه در سطح تاریخ، نه لحظه)
- سفارش لغوشده فروش نیست
- واحد تحلیل پرداخت، ردیف پرداخت است نه سفارش
- `Procedure` با اجرای دوباره داده‌ی تکراری نمی‌سازد
- خطای میانه‌ی کار، `DELETE` را هم با خودش برمی‌گرداند

هر تست با تغییر دادن SQL و دیدن شکستن تست اعتبارسنجی شده است (مرز `3` به `2`، و حذف
`STRICT`)؛ تستی که هرگز شکست نخورده فقط یک کامنت است.
'''))

# ============================================================ queries
add(md(r'''
## ۴. پنج پرس‌وجوی تیم محصول

صورت سؤال، طرح کوئری نیست. برای هر پرس‌وجو باید تصمیم می‌گرفتم: قاعده‌ی پیوند، فیلتر، سطح
تجمیع، و رفتار با NULL. این تصمیم‌ها در `src/data_factory/db/queries.py` کنار خود کوئری
نوشته شده‌اند تا از کد جدا نشوند، و این‌جا خروجی‌شان دیده می‌شود.
'''))

QUERY_BLURBS = {
    "q1": r'''
**Q1 — سبد و درآمد.** درآمد فقط از `order_item` قابل نسبت‌دادن به دسته است؛ پرداخت در سطح
سفارش ثبت شده و به دسته تقسیم نمی‌شود. پس درآمد = `price + freight` در سطح آیتم، و سفارش
لغوشده یا ناموجود فروش نیست (از `v_valid_order` می‌آید).
''',
    "q2": r'''
**Q2 — تحویل.** تأخیر در سطح تاریخ مقایسه می‌شود، چون وعده یک تاریخ است نه یک لحظه: سفارشی که
ساعت ۱۸ روز وعده رسیده «دیر» نیست. فقط سفارش تحویل‌شده تاریخ تحویل دارد؛ بقیه «نامعلوم»اند نه
«به‌موقع».
''',
    "q3": r'''
**Q3 — رضایت.** تأخیر = تحویل منهای وعده، به روز؛ منفی یعنی زودتر. نظر به ازای هر ردیف نظر
شمرده می‌شود، نه هر سفارش: ۵۴۷ سفارش بیش از یک نظر دارند و هر نظر یک عقیده است.
''',
    "q4": r'''
**Q4 — فروشنده.** زمان آماده‌سازی = تحویل به حامل منهای خرید، و روی جفت‌های **یکتای
(فروشنده، سفارش)** میانگین گرفته می‌شود تا فروشنده به تعداد آیتم‌های یک سفارش وزن نگیرد.
همین یکتاسازی، هزینه‌ی اصلی این کوئری است — بخش ۷.
''',
    "q5": r'''
**Q5 — پرداخت.** واحد تحلیل ردیف پرداخت است، نه سفارش: یک سفارش می‌تواند با دو واچر و یک کارت
پرداخت شود. `not_defined` دسته‌ی جدای خودش می‌ماند؛ اختراع یک روش پرداخت بدتر از
توضیح‌ندادن سه ردیف است.
''',
}

for _key in ("q1", "q2", "q3", "q4", "q5"):
    add(md(QUERY_BLURBS[_key]))
    add(code(f'''
# {_key.upper()} — خروجی واقعی روی داده‌ی کامل.
# اتصال با engine_scope باز و بسته می‌شود؛ خواندن هم دسته‌ای است (AGENTS 4.3.8).
with engine_scope(DB_URL) as engine:
    {_key}_frame = result_frame(engine, BY_KEY["{_key}"].served_sql, limit=40)

{_key}_query = BY_KEY["{_key}"]
print("{_key.upper()} از:", {_key}_query.served_from,
      "| سطرها:", len({_key}_frame))
print("تصمیم:", {_key}_query.decision)
{_key}_frame
'''))

# ============================================================ timing
add(md(r'''
## ۵. چرخه‌ی بهینه‌سازی با شاهد

برای هر پرس‌وجو: نسخه‌ی اولیه و زمانش، خواندن پلن، تغییر، زمان و پلن بعد، و یک جمله که چرا
کار کرد.

**روش اندازه‌گیری:** چند اجرا بعد از گرم‌کردن، و گزارش میانه و p95. یک‌بار اجرا اندازه‌گیری
نیست. وضعیت cache: بعد از اجراهای گرم‌کردن، `shared_buffers` و page cache سیستم گرم‌اند؛ این
همان حالتی است که بودجه‌ی ۲۰۰ms درباره‌ی آن حرف می‌زند — داشبوردی که در حال استفاده است، نه
اولین بارگذاری سرد.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    evidence = query_evidence(engine, runs=15, warmup=3, live_runs=5)

summary = pd.DataFrame([{
    "query": item.key,
    "served_from": item.extra["served_from"],
    "live_median_ms": item.extra["live_median_ms"],
    "served_median_ms": item.timing.median_ms,
    "p95_ms": item.timing.p95_ms,
    "max_ms": item.timing.max_ms,
    "rows": item.timing.rows,
    "within_200ms": item.timing.within_budget,
} for item in evidence])
# نسبت سرعت فقط جایی معنا دارد که نسخه‌ی سروشده با نسخه‌ی زنده فرق کند؛
# وگرنه عددِ ۱٫۰ فقط نوفه‌ی اندازه‌گیری است و آن را NaN می‌گذاریم.
summary["speedup"] = [
    round(live / served, 1) if served_from == "snapshot" else float("nan")
    for live, served, served_from in zip(
        summary["live_median_ms"], summary["served_median_ms"], summary["served_from"])
]
print("بودجه‌ی ۲۰۰ms — همه رعایت شد:", bool(summary["within_200ms"].all()))
summary
'''))

add(md(r'''
### ۵.۱ پلن قبل و بعد، برای سنگین‌ترین مورد

Q1 دو شکل دارد: نمایش زنده (View که هر بار محاسبه می‌کند) و جدول از پیش محاسبه‌شده. این دو
پلن تفاوت را نشان می‌دهند — یکی ۱۱۲هزار سطر آیتم را می‌خواند، دیگری ۱٬۲۸۲ سطر آماده.

پلن با `EXPLAIN (ANALYZE, BUFFERS)` گرفته می‌شود تا سطر تخمینی در برابر واقعی و میزان خواندن
بافر دیده شود.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        live_plan = explain(conn, BY_KEY["q1"].live_sql, key="q1-live")
        served_plan = explain(conn, BY_KEY["q1"].served_sql, key="q1-served")

print("=== قبل: View که هر بار محاسبه می‌کند ===")
print(live_plan.text_plan[:1400])
print()
print(f"زمان اجرا: {live_plan.execution_ms:.1f} ms")
print("Seq Scan روی:", ", ".join(live_plan.seq_scans) or "-")
'''))

add(code(r'''
print("=== بعد: خواندن جدول از پیش محاسبه‌شده ===")
print(served_plan.text_plan[:900])
print()
print(f"زمان اجرا: {served_plan.execution_ms:.1f} ms")
print("Seq Scan روی:", ", ".join(served_plan.seq_scans) or "-")
'''))

add(md(r'''
### ۵.۲ خواندن پلن: چه چیزی گران بود؟

سه چیز در پلن‌های بالا قابل خواندن است:

1. **`Seq Scan` روی `order_item`** با ۱۱۲٬۴۵۵ سطر — طبیعی است، چون کوئری به تقریباً همه‌ی
   سطرها نیاز دارد.
2. **`Sort ... external merge Disk`** — هزینه‌ی واقعی همان جایی است که به چشم نمی‌آید:
   مرتب‌سازی روی دیسک سرریز می‌کند، نه اسکن.
3. **سطر تخمینی در برابر واقعی** — اگر این دو خیلی فاصله بگیرند، پلن گمراه شده است؛ در این
   پرس‌وجوها فاصله‌ی معناداری دیده نشد، پس مشکل «آمار بد» نبود.

نتیجه‌ی مهم: **مسئله، اسکن نبود.** ۱۱۲هزار سطر در حافظه در حدود ده میلی‌ثانیه خوانده می‌شود.
مسئله محاسبه‌ی مجدد و مرتب‌سازی روی ۱۱۲هزار سطر بود — و برای همین Index راه‌حل نبود. بخش بعد
این را با اندازه‌گیری نشان می‌دهد، نه با ادعا.
'''))

# ============================================================ index cost
add(md(r'''
## ۶. هزینه‌ی پنهان Index

Index مجانی نیست: فضا می‌گیرد و سرعت درج را کم می‌کند. این‌جا هر کاندید **ساخته، اندازه‌گیری و
حذف** می‌شود. قاعده‌ی سخت: هر Index باید با پلن توجیه شود؛ Index بدون شاهد نمرهٔ منفی دارد.

نکته‌ی روش: خط مبنا برای هر کاندید **بلافاصله قبل از آن** و روی همان اتصال دوباره اندازه‌گیری
می‌شود. این جزئیات مهم است — در نسخه‌ی اول این آزمایش، یک Index جزئی «۴۳ میلی‌ثانیه بهبود» نشان
داد که کل آن مربوط به `ANALYZE` تازه بود، نه به Index. با اجرای کنترل (بدون Index، با Index،
بعد از حذف Index) معلوم شد تفاوت واقعی صفر است. `bootstrap` به همین دلیل بعد از بارگذاری
`ANALYZE` اجرا می‌کند.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    candidates = index_experiments(engine, runs=5)

rows = []
for item in candidates:
    for query_key, effect in item["effects"].items():
        rows.append({
            "index": item["index"],
            "size": item["size"],
            "query": query_key,
            "baseline_ms": effect["baseline_ms"],
            "with_index_ms": effect["with_index_ms"],
            "delta_ms": effect["delta_ms"],
        })
index_table = pd.DataFrame(rows)
print("بهترین بهبود در کل کاندیدها:", f"{index_table['delta_ms'].min():+.0f} ms")
print("بدترین اثر:", f"{index_table['delta_ms'].max():+.0f} ms")
index_table
'''))

add(code(r'''
# هزینه‌ی درج: همان ۵۰٬۰۰۰ سطر، با ۰ و ۱ و ۲ و ۴ Index.
with engine_scope(DB_URL) as engine:
    insert_cost = insert_cost_experiment(engine)

insert_table = pd.DataFrame(insert_cost)
baseline_rate = insert_table.loc[0, "rows_per_second"]
insert_table["vs_no_index_pct"] = (
    100 * (insert_table["rows_per_second"] - baseline_rate) / baseline_rate).round(1)
insert_table
'''))

add(md(r'''
### ۶.۱ نتیجه: هیچ Indexی در این دیتاست سود نداد

همه‌ی کاندیدها ساخته و اندازه‌گیری شدند و **همه حذف شدند**. شاهدش فقط «کوچک بودن عدد»
نیست: این آزمایش دو بار در دو وضعیت cache مختلف اجرا شد و **علامت اختلاف‌ها بین دو اجرا
عوض شد** (مثلاً `order_item(product_id)` در یک اجرا ۲۸ میلی‌ثانیه بهتر شد و در اجرای دیگر
۲۷ میلی‌ثانیه بدتر). چیزی که علامتش با هر بار اجرا عوض می‌شود، اثر نیست؛ نوفه است. فایل `sql/02_indexes.sql` به همین
دلیل `CREATE INDEX` ندارد و به‌جایش جدول اندازه‌گیری‌ها را نگه می‌دارد.

**چرا؟** `order_item` با ۱۱۲٬۴۵۵ سطر در ۲٬۱۶۳ صفحه (حدود ۱۷ مگابایت) جا می‌شود و در
`shared_buffers` می‌ماند. اسکن ترتیبی روی داده‌ی کش‌شده ارزان است، و هر شش پرس‌وجو کسر بزرگی از
سطرهایی را که لمس می‌کنند برمی‌گردانند (Q1: ۱٬۲۸۲ سطر خروجی از ۱۱۲٬۴۵۵ سطر ورودی). برای چنین
گزینش‌پذیری پایینی، Index Scan یک خواندن ترتیبی کش‌شده را با پیمایش تصادفی در ساختاری بزرگ‌تر
عوض می‌کند — و پلن‌ساز آن را رد می‌کند و **درست می‌کند**.

حتی حالت کتابی هم امتحان شد: یک Index پوشا روی
`order_item (seller_id, order_id) INCLUDE (price, freight_value)` (۱۱ مگابایت) برای حذف
مرتب‌سازی ۵.۹ مگابایتی Q4. با `VACUUM ANALYZE` تأیید شد که Index-Only Scan واقعاً ممکن است
(۲۱۶۳ از ۲۱۶۳ صفحه all-visible)؛ پلن‌ساز باز هم اسکن ترتیبی موازی را ترجیح داد، چون Index از
جدولی که جایگزین اسکنش می‌شود بزرگ‌تر است.

**هزینه‌ی درج** (جدول بالا): با چهار Index سرعت درج به‌طور محسوس کم می‌شود و حجم جدول از ۱۰ به
۱۷ مگابایت می‌رسد. این هزینه‌ای است که هر Index تحمیل می‌کند، حتی وقتی سودی ندارد.
'''))

# ============================================================ hard query
add(md(r'''
## ۷. پرس‌وجویی که Index نجاتش نمی‌دهد

پنج سؤال بالا طبیعتاً به این حالت نمی‌رسیدند، پس **سؤال ششم** طراحی شد؛ و در واقع حالت
سخت‌ترش Q4 خودمان است. سه دلیل که Index این‌جا بی‌اثر است:

1. **یکتاسازی اجباری.** میانگین زمان آماده‌سازی باید روی جفت‌های یکتای (فروشنده، سفارش)
   گرفته شود. این یعنی حدود ۹۹هزار جفت باید ساخته و مرتب شوند، صرف‌نظر از اینکه از کدام ستون
   می‌خوانیم. هیچ Indexای این یکتاسازی را حذف نمی‌کند.
2. **تجمیع کامل قبل از `LIMIT`.** برای دانستن ۲۰ فروشنده‌ی برتر باید درآمد **همه‌ی** فروشنده‌ها
   حساب شود. `LIMIT 20` بعد از تجمیع می‌آید، پس نمی‌تواند دامنه را کوچک کند.
3. **سه متریک از سه جهت مختلف.** درآمد از `order_item`، زمان آماده‌سازی از `orders`، و امتیاز
   از `order_review`. هر Index فقط می‌تواند یکی از این سه مسیر را سرویس کند.

سه بازنویسی و شش Index امتحان شد و بهترین نتیجه‌ی زنده بالای بودجه ماند. راه‌حل، Index نبود:
**محاسبه‌ی از پیش** (بخش بعد) — و آنچه در عوضش پرداختیم، تازگی داده است.
'''))

add(code(r'''
# چرا Index بی‌اثر بود: پلن زنده‌ی Q4 را بخوانید.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        q4_plan = explain(conn, BY_KEY["q4"].live_sql, key="q4-live")

interesting = [line for line in q4_plan.text_plan.splitlines()
               if any(token in line for token in
                      ("Sort Method", "external merge", "Seq Scan", "HashAggregate",
                       "GroupAggregate", "Limit"))]
print("\n".join(interesting[:12]))
print()
print(f"زمان زنده: {q4_plan.execution_ms:.1f} ms")
print("Seq Scan روی:", ", ".join(sorted(set(q4_plan.seq_scans))))
print()
print("سرریز مرتب‌سازی روی دیسک، همان هزینه‌ای است که Index باید حذف می‌کرد و نکرد.")
'''))

# ============================================================ views
add(md(r'''
## ۸. View: یک تعریف برای هر تصمیم تکرارشده

سه View ساخته شد، هر کدام با نقش متفاوت:

| View | نوع | کاری که می‌کند |
|---|---|---|
| `v_valid_order` | ساده، یک جدول | پنهان‌کردن فیلتر «فروش است»؛ قبلاً در سه کوئری تکرار می‌شد |
| `v_delivered_order` | چندجدولی | نگه‌داشتن پیوند سفارش↔مشتری و محاسبه‌ی تأخیر به روز، یک‌جا |
| `v_revenue_by_month_category` | چندجدولی، سنگین | سنگین‌ترین تحلیل، به‌صورت **زنده** — نیمه‌ی «هر بار محاسبه کن» |

Q2 و Q3 روی View نوشته شده‌اند، نه جدول خام.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        view_rows = pd.DataFrame(conn.execute(text("""
            SELECT c.relname                                     AS view_name,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS size
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'olist' AND c.relkind = 'v'
            ORDER BY 1
        """)).mappings().all())
view_rows
'''))

add(md(r'''
### ۸.۱ تصمیم View یا جدول snapshot — با عدد

برای سنگین‌ترین تحلیل‌ها هر دو راه ساخته شد: View که هر بار محاسبه می‌کند، و جدول از پیش
محاسبه‌شده. هر دو زمان گرفته می‌شوند، و هزینه‌ی نگه‌داری snapshot هم اندازه‌گیری می‌شود.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    comparison = view_vs_snapshot(engine, runs=7)

comparison_table = pd.DataFrame([
    {
        "panel": key.upper(),
        "live_object": comparison[key]["live_object"],
        "live_median_ms": comparison[key]["live_median_ms"],
        "snapshot_median_ms": comparison[key]["snapshot_median_ms"],
        "speedup": comparison[key]["speedup"],
        "snapshot_size": comparison[key]["snapshot_size"],
        "snapshot_rows": comparison[key]["snapshot_rows"],
        "last_computed": comparison[key]["last_computed"],
    }
    for key in ("q1", "q4")
])
print(f"هزینه‌ی نگه‌داری — بازسازی کامل: {comparison['refresh_full_seconds']} s"
      f" | یک ماه: {comparison['refresh_one_month_seconds']} s")
comparison_table
'''))

add(md(r'''
**تصمیم، و آنچه از دست دادیم.** snapshot انتخاب شد، چون تفاوت سرعت یک مرتبه‌ی بزرگی است و
هزینه‌ی نگه‌داری‌اش کسری از ثانیه. اما چیزی که از دست دادیم **تازگی** است: تا وقتی
`refresh_dashboard_snapshot` اجرا نشود، ماه جدید در پنل دیده نمی‌شود. برای اینکه این نقص پنهان
نشود، هر جدول snapshot ستون `computed_at` دارد و داشبورد باید «as of» را نشان دهد.

Q2، Q3، Q5 و Q6 snapshot نگرفتند، چون زنده هم زیر بودجه‌اند؛ اضافه‌کردن snapshot برای آن‌ها فقط
کهنگی می‌خرید و چیزی بهتر نمی‌شد.
'''))

# ============================================================ function/procedure
add(md(r'''
## ۹. Function و Procedure

**تفاوت را با ارجاع به کد خودمان می‌گوییم، نه از کتاب:**

- `olist.delay_bucket()` یک **Function** است: داخل `SELECT` صدا زده می‌شود، یک مقدار برمی‌گرداند
  و داخل تراکنش فراخوان می‌ماند. برای همین می‌شود در `GROUP BY` گذاشتش — کاری که با Procedure
  نمی‌شود کرد.
- `olist.refresh_dashboard_snapshot()` یک **Procedure** است: چند دستور را پشت سر هم اجرا می‌کند،
  پارامتر دارد، و کنترل تراکنش را در دست دارد. این چیزی است که یک Function نمی‌تواند: یک
  Function نمی‌تواند `DELETE` و `INSERT` چندمرحله‌ای را با ضمانت «همه یا هیچ» مدیریت کند، و
  نمی‌شود آن را با `CALL` به‌عنوان یک عمل مستقل اجرا کرد.

پس: Function برای «محاسبه‌ی یک مقدار»، Procedure برای «انجام یک کار».
'''))

add(code(r'''
# Function داخل SELECT — همان چیزی که Procedure نمی‌تواند.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        demo = pd.DataFrame(conn.execute(text("""
            SELECT olist.delay_bucket(v.delay_days::numeric) AS bucket,
                   count(*)                                    AS orders,
                   round(avg(v.delay_days), 2)                 AS avg_delay_days
            FROM olist.v_delivered_order v
            GROUP BY 1
            ORDER BY min(v.delay_days)
        """)).mappings().all())
        null_case = conn.execute(
            text("SELECT olist.delay_bucket(NULL::numeric) AS null_in_null_out")
        ).scalar_one()

print("NULL در ورودی، NULL در خروجی:", null_case)
demo
'''))

add(code(r'''
# Procedure: دوبار اجرا برای یک ماه — داده‌ی تکراری نباید بسازد.
MONTH = "2017-05-01"


def month_count(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM olist.revenue_snapshot WHERE purchase_month = :m"),
            {"m": MONTH},
        ).scalar_one()


with engine_scope(DB_URL) as engine:
    before = month_count(engine)
    with engine.begin() as conn:
        conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"), {"m": MONTH})
    after_first = month_count(engine)
    with engine.begin() as conn:
        conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"), {"m": MONTH})
    after_second = month_count(engine)

print(f"قبل: {before} | بعد از اجرای اول: {after_first} | بعد از اجرای دوم: {after_second}")
print("idempotent:", before == after_first == after_second)
'''))

add(md(r'''
### ۹.۱ رفتار در برابر خطای میانه

قرارداد: Procedure هرگز داخل خودش `COMMIT` نمی‌کند. پس اگر در میانه خطا بدهد، تراکنش فراخوان
abort می‌شود و `DELETE` هم با آن برمی‌گردد — داده نیمه‌کاره نمی‌ماند.

این رفتار با یک Trigger که درج را رد می‌کند شبیه‌سازی می‌شود: `DELETE` انجام می‌شود، `INSERT`
می‌شکند، و ماه باید دست‌نخورده بماند. همین تست در `tests/test_db.py` هم هست.
'''))

add(code(r'''
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        rows_before = conn.execute(text(
            "SELECT category, revenue FROM olist.revenue_snapshot "
            "WHERE purchase_month = :m ORDER BY category"), {"m": MONTH}).fetchall()

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION olist._fail_on_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'simulated mid-refresh failure'; END
            $$;
            CREATE TRIGGER _test_fail BEFORE INSERT ON olist.revenue_snapshot
                FOR EACH ROW EXECUTE FUNCTION olist._fail_on_insert();
        """))

    failed_as_expected = False
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"),
                             {"m": MONTH})
            except Exception:
                failed_as_expected = True
                transaction.rollback()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS _test_fail ON olist.revenue_snapshot"))
            conn.execute(text("DROP FUNCTION IF EXISTS olist._fail_on_insert()"))

    with engine.connect() as conn:
        rows_after = conn.execute(text(
            "SELECT category, revenue FROM olist.revenue_snapshot "
            "WHERE purchase_month = :m ORDER BY category"), {"m": MONTH}).fetchall()

print("خطای میانه رخ داد:", failed_as_expected)
print("ماه پس از شکست دست‌نخورده است:", rows_before == rows_after)
print(f"تعداد ردیف‌های ماه: {len(rows_before)} -> {len(rows_after)}")
'''))

# ============================================================ bridge
add(md(r'''
## ۱۰. پل Python و SQL

این بخش سخت‌گیرانه بررسی می‌شود. چهار چیز باید نشان داده شود:

1. **مدیریت صریح Connection** — هیچ Connection بازمانده‌ای بعد از اجرا نماند.
2. **هیچ کوئری با الحاق رشته** — پارامتر باید پارامتر باشد.
3. **خواندن بزرگ به DataFrame به‌صورت دسته‌ای** — نه یک‌جا.
4. **نوشتن DataFrame با نگاشت نوع ستون صریح** — نه نوع پیش‌فرض خودکار.

هر چهار مورد در `src/data_factory/db/` پیاده شده‌اند و این‌جا نشان داده می‌شوند.
'''))

add(code(r'''
# ۱) Connection بازمانده: بعد از این‌همه کار، جز اتصال خودِ این بررسی چیزی نباید بماند.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        leftover = conn.execute(text("""
            SELECT coalesce(state, 'unknown') AS state, count(*) AS connections
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'   -- autovacuum is not ours
            GROUP BY 1 ORDER BY 1
        """)).mappings().all()

print("اتصال‌های بازمانده از این برنامه:", leftover if leftover else "هیچ")
'''))

add(code(r'''
# ۲) هیچ الحاق رشته‌ای: بازرسی AST روی خود بسته.
# قانون: هیچ مقداری نباید با رشته‌سازی داخل SQL برود. شناسه‌ها (نام جدول، ستون،
# Index) در PostgreSQL نمی‌توانند پارامتر باشند، پس فقط یک فایل اجازه‌ی
# رشته‌سازی دارد و آن هم هر شناسه را قبل از استفاده اعتبارسنجی می‌کند.
# این سلول هم آن استثنا را بررسی می‌کند، نه اینکه فرضش بگیرد.
import ast
import re

CHOKEPOINT = Path("src/data_factory/db/sqltext.py")
violations = []
inspected = 0
for path in sorted(Path("src").rglob("*.py")):
    inspected += 1
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "text" or not node.args:
            continue
        argument = node.args[0]
        built = (isinstance(argument, ast.JoinedStr)
                 or (isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add))
                 or (isinstance(argument, ast.Call)
                     and getattr(argument.func, "attr", "") == "format"))
        if built and path != CHOKEPOINT:
            violations.append(f"{path}:{node.lineno}")

guard = re.search(r"_IDENTIFIER = re\.compile\(r\"\^\[a-z_\]\[a-z0-9_\]",
                  CHOKEPOINT.read_text(encoding="utf-8"))
print(f"فایل‌های بازرسی‌شده: {inspected}")
print("رشته‌سازی SQL بیرون از chokepoint:", violations if violations else "هیچ")
print("اعتبارسنجی شناسه در chokepoint موجود است:", bool(guard))
'''))

add(code(r'''
# ۳) خواندن دسته‌ای: نتیجه‌ی کامل Q2 در دسته‌های ۲۰۰۰تایی خوانده می‌شود.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        batches = []
        with conn.execution_options(stream_results=True).execute(
                text(BY_KEY["q2"].served_sql)) as result:
            while True:
                batch = result.fetchmany(2000)
                if not batch:
                    break
                batches.append(len(batch))

print("تعداد دسته‌ها:", len(batches))
print("اندازه‌ی دسته‌ها:", batches)
print("مجموع سطرها:", sum(batches), "| کل نتیجه هرگز یک‌جا در حافظه نبود")
'''))

add(code(r'''
# ۴) نوشتن با نگاشت نوع ستون صریح: نوع‌ها اعلام می‌شوند، حدس زده نمی‌شوند.
sample = pd.DataFrame({
    "batch_id": [1, 2],
    "label": ["alpha", "beta"],
    "measured_at": pd.to_datetime(["2024-01-01 10:00", "2024-01-02 11:00"]),
    "amount": [10.50, 20.25],
})

demo_types = {
    "batch_id": SMALLINT,
    "label": VARCHAR(20),
    "measured_at": TIMESTAMP,
    "amount": NUMERIC(10, 2),
}

with engine_scope(DB_URL) as engine:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS olist.write_demo"))
        conn.execute(text("""
            CREATE TABLE olist.write_demo (
                batch_id    smallint      NOT NULL,
                label       varchar(20)   NOT NULL,
                measured_at timestamp     NOT NULL,
                amount      numeric(10,2) NOT NULL
            )
        """))
        written = write_frame(conn, sample, "write_demo", column_types=demo_types)
    with engine.connect() as conn:
        actual_types = pd.DataFrame(conn.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'olist' AND table_name = 'write_demo'
            ORDER BY ordinal_position
        """)).mappings().all())
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE olist.write_demo"))

print("سطرهای نوشته‌شده:", written)
declared = pd.DataFrame([{"column_name": name, "declared": str(kind)}
                         for name, kind in demo_types.items()])
declared.merge(actual_types, on="column_name")
'''))

add(code(r'''
# و خواندن از View — یکی از الزامات صریح بخش ۸ صورت مسئله.
with engine_scope(DB_URL) as engine:
    with engine.connect() as conn:
        from_view = pd.DataFrame(conn.execute(text("""
            SELECT state,
                   count(*)                                    AS delivered,
                   round(avg(delay_days), 2)                    AS avg_delay_days,
                   round(100.0 * count(*) FILTER (WHERE delay_days > 0)
                         / count(*), 2)                         AS late_pct
            FROM olist.v_delivered_order
            GROUP BY 1
            ORDER BY delivered DESC
            LIMIT 8
        """)).mappings().all())
from_view
'''))

# ============================================================ conclusion
add(md(r'''
## ۱۱. جمع‌بندی: چه چیزی ساخته شد و چه چیزی اندازه‌گیری شد

**ساخته شد:** یک schema با ۱۲ جدول و محدودیت‌های واقعی، یک مقصد دیتابیس داخل بسته‌ی مرحله ۱
(نه کد یک‌بارمصرف)، سه View، دو Function، یک Procedure idempotent و اتمی، دو جدول snapshot،
۱۳ تست لایه‌ی داده، و شش پرس‌وجوی داشبورد.

**اندازه‌گیری شد و نتیجه‌اش خلاف انتظار بود:** هیچ Indexی در این دیتاست سود نداد. شش کاندید
ساخته و حذف شدند. این «بهینه‌سازی نکردن» نیست؛ نتیجه‌ی خواندن پلن است: جدولی که در حافظه جا
می‌شود و کوئری‌ای که کسر بزرگی از سطرهایش را برمی‌گرداند، از Index سودی نمی‌برد و پلن‌ساز
درست تصمیم می‌گیرد.

**آنچه واقعاً کار کرد:** تعریف یک‌باره‌ی منطق در View، و محاسبه‌ی از پیش برای دو پنلی که زنده
زیر بودجه نمی‌آمدند — با پذیرش صریح هزینه‌اش (کهنگی، که با `computed_at` اعلام می‌شود).

**و یک درس روش‌شناختی:** یک Index جزئی «۴۳ میلی‌ثانیه بهبود» نشان داد که کل آن مربوط به
`ANALYZE` تازه بود، نه به Index. اگر اجرای کنترل (بدون Index و بعد از حذف Index) نبود، این
عدد به گزارش می‌رفت و غلط بود. تفاوت «عدد گرفتم» و «اندازه‌گیری کردم» همین است.
'''))


def main() -> int:
    notebook = {
        "cells": CELLS,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf-8")
    code_cells = sum(1 for cell in CELLS if cell["cell_type"] == "code")
    print(f"wrote {OUT} — {len(CELLS)} cells ({code_cells} code, "
          f"{len(CELLS) - code_cells} markdown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
