import json
import os
import re
import time
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
URL = "https://store.supermicro.com/us_en/catalogsearch/result/?q=rtx%205"
MUST_CONTAIN = "RTX 5"

BOT_ENV = os.environ.get("BOT_ENV", "debian").strip().lower()
if BOT_ENV not in ("debian", "github"):
    raise ValueError("BOT_ENV must be debian or github")

# قاعدة المنتجات مشتركة، أما ملفات الحالة فمستقلة لكل بيئة.
DB_FILE = ROOT / "SuperMicroSeen.json"
STATE_DIR = ROOT / "github_state" if BOT_ENV == "github" else ROOT
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STATE_DIR / "SuperMicroState.json"
LAST_SUCCESS_FILE = STATE_DIR / "last_success.json"
HISTORY_FILE = STATE_DIR / ("check_history_server.json" if BOT_ENV == "debian" else "check_history_github.json")
CONFIG_FILE = ROOT / "monitor_config.json"

HEADLESS = os.environ.get("HEADLESS", "1") != "0"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

LABELS = {
    "in_stock": "متوفر للشراء",
    "out_of_stock": "غير متوفر",
    "preorder": "طلب مسبق",
    "unknown": "غير معروف",
}


def load_env_file():
    path = ROOT / ".env"
    if not path.exists():
        return

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(
                    key.strip(), value.strip().strip('"').strip("'")
                )


load_env_file()

# إعادة قراءة Telegram بعد تحميل .env
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def load_config():
    defaults = {
        "max_scan_gap_minutes": 90,
        "watchdog_interval_minutes": 10,
        "scan_interval_minutes": 60,
        "retry_attempts": 3,
        "retry_delay_seconds": 30,
    }

    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    for key, value in defaults.items():
        config.setdefault(key, value)

    return config


CONFIG = load_config()


def notify(message):
    print(message)
    print()

    if not TELEGRAM_ENABLED:
        print("Telegram غير مفعّل أو بياناته ناقصة.")
        return

    try:
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }).encode("utf-8")

        request = urllib.request.Request(
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendMessage",
            data=data,
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("ok"):
            print("تم إرسال التنبيه إلى Telegram.")
        else:
            print("Telegram رفض إرسال الرسالة.")

    except Exception as exc:
        print("فشل إرسال Telegram:", type(exc).__name__, str(exc))


def load_json(path, default):
    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        print("تعذر قراءة الملف:", path)
        return default


def save_json(path, data):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def normalize(products):
    result = {}

    if not isinstance(products, dict):
        return result

    for url, data in products.items():
        if not isinstance(data, dict):
            continue

        result[url] = {
            "name": data.get("name", ""),
            "price": data.get("price", "N/A"),
            "stock": data.get("stock", "unknown"),
            "source": data.get("source", ""),
        }

        if data.get("checked_at"):
            result[url]["checked_at"] = data["checked_at"]

    return result


def run_git(args, check=True):
    result = subprocess.run(
        ["git"] + args,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            "Git command failed: git "
            + " ".join(args)
            + "\n"
            + (result.stderr or result.stdout).strip()
        )

    return result


def read_remote_database():
    result = run_git(
        ["show", "origin/main:SuperMicroSeen.json"],
        check=False,
    )

    if result.returncode != 0:
        return {}

    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("قاعدة المنتجات ليست JSON object")
        return normalize(data)
    except Exception as exc:
        raise RuntimeError("قاعدة المنتجات على GitHub تالفة") from exc


def load_central_database():
    # لا نعتمد على نسخة محلية قديمة؛ نقرأ آخر نسخة من main.
    run_git(["fetch", "origin", "main"])
    return read_remote_database()


def merge_databases(remote, current):
    merged = normalize(remote)

    for url, data in current.items():
        merged[url] = dict(data)

    return merged


def publish_database(current, checked_at):
    """دمج آمن مع main وإعادة المحاولة عند حدوث تعارض في الدفع."""
    for attempt in range(1, 6):
        print(f"نشر قاعدة المنتجات: المحاولة {attempt}/5")

        run_git(["fetch", "origin", "main"])
        remote = read_remote_database()
        merged = merge_databases(remote, current)

        save_json(DB_FILE, merged)

        run_git(["add", "--", "SuperMicroSeen.json"])

        staged = run_git(
            ["diff", "--cached", "--quiet", "--", "SuperMicroSeen.json"],
            check=False,
        )

        if staged.returncode == 0:
            print("قاعدة المنتجات المركزية محدثة بالفعل.")
            return merged

        run_git(["config", "user.name", "Supermicro Monitor"])
        run_git([
            "config", "user.email",
            "supermicro-monitor@users.noreply.github.com",
        ])

        commit = run_git(
            [
                "commit",
                "-m",
                f"Update Supermicro product database ({checked_at})",
                "--",
                "SuperMicroSeen.json",
            ],
            check=False,
        )

        if commit.returncode != 0:
            check_again = run_git(
                ["diff", "--cached", "--quiet", "--", "SuperMicroSeen.json"],
                check=False,
            )
            if check_again.returncode == 0:
                return merged
            raise RuntimeError("تعذر إنشاء commit لقاعدة المنتجات")

        push = run_git(
            ["push", "origin", "HEAD:main"],
            check=False,
        )

        if push.returncode == 0:
            print("تم رفع قاعدة المنتجات إلى GitHub بنجاح.")
            return merged

        print("تعذر الدفع، سيتم جلب آخر نسخة ومحاولة الدمج مجدداً.")
        print((push.stderr or push.stdout).strip()[-1000:])

        run_git(["fetch", "origin", "main"])
        run_git(["reset", "--mixed", "origin/main"])

        if attempt < 5:
            time.sleep(2)

    raise RuntimeError("فشل رفع قاعدة المنتجات بعد 5 محاولات")


def append_history(record):
    history = load_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    history.append(record)
    history = history[-500:]
    save_json(HISTORY_FILE, history)



def publish_server_history():
    """رفع سجل نجاحات Debian إلى main دون المساس ببقية الملفات."""
    if BOT_ENV != "debian":
        return

    for attempt in range(1, 6):
        run_git(["fetch", "origin", "main"])
        run_git(["add", "--", "check_history_server.json"])

        staged = run_git(
            ["diff", "--cached", "--quiet", "--", "check_history_server.json"],
            check=False,
        )

        if staged.returncode == 0:
            print("سجل السيرفر محدث بالفعل على GitHub.")
            return

        run_git(["config", "user.name", "Supermicro Monitor"])
        run_git([
            "config", "user.email",
            "supermicro-monitor@users.noreply.github.com",
        ])

        commit = run_git([
            "commit", "-m", "Update server success history",
            "--", "check_history_server.json",
        ], check=False)

        if commit.returncode != 0:
            raise RuntimeError("تعذر حفظ سجل السيرفر في Git")

        push = run_git(["push", "origin", "HEAD:main"], check=False)

        if push.returncode == 0:
            print("تم رفع سجل السيرفر إلى GitHub بنجاح.")
            return

        print(f"تعذر رفع سجل السيرفر، المحاولة {attempt}/5")
        run_git(["fetch", "origin", "main"])
        run_git(["reset", "--mixed", "origin/main"])

        if attempt < 5:
            time.sleep(2)

    raise RuntimeError("فشل رفع سجل السيرفر بعد 5 محاولات")

def decide(atc, stock_text, page_text=""):
    stock = re.sub(r"\s+", " ", stock_text.lower()).strip()
    body = re.sub(r"\s+", " ", page_text.lower()).strip()

    if "out of stock" in stock:
        return "out_of_stock", "stock-element"

    if "pre-order" in stock or "preorder" in stock:
        return "preorder", "stock-element"

    if "in stock" in stock:
        return "in_stock", "stock-element"

    if "contact us for more info" in body:
        return "out_of_stock", "contact-us-only"

    return "unknown", "no reliable stock signal"


def check_stock(page, url):
    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        if response is not None and response.status >= 400:
            return "unknown", "http " + str(response.status)

    except Exception as exc:
        print("Product page error:", exc)
        return "unknown", "error"

    try:
        page.wait_for_selector(
            "#product-addtocart-button, div.stock",
            state="attached",
            timeout=10000,
        )
    except Exception:
        pass

    page.wait_for_timeout(1500)

    try:
        stock_text = " ".join(page.locator("div.stock").all_inner_texts())
    except Exception:
        stock_text = ""

    try:
        page_text = page.locator("body").inner_text()
    except Exception:
        page_text = ""

    atc = None
    try:
        locator = page.locator("#product-addtocart-button")
        if locator.count() > 0:
            atc = {
                "visible": locator.first.is_visible(),
                "enabled": locator.first.is_enabled(),
                "text": locator.first.inner_text().strip(),
            }
    except Exception:
        pass

    return decide(atc, stock_text, page_text)


def dump_debug(name, url, page):
    try:
        body = re.sub(r"\s+", " ", page.inner_text("body"))
    except Exception:
        body = ""

    (ROOT / "debug_stock.txt").write_text(
        "PRODUCT: " + name + "\n"
        + "URL: " + url + "\n\n"
        + "PAGE TEXT:\n"
        + body[:5000],
        encoding="utf-8",
    )


def skip_heavy(route):
    if route.request.resource_type in ("image", "font", "media"):
        route.abort()
    else:
        route.continue_()


def fetch_products():
    items = {}

    with sync_playwright() as playwright:
        proxy_server = os.environ.get("PROXY_SERVER", "").strip()
        launch_options = {"headless": HEADLESS}

        system_chromium = "/usr/bin/chromium"
        if BOT_ENV == "debian" and os.path.exists(system_chromium):
            launch_options["executable_path"] = system_chromium

        if proxy_server:
            launch_options["proxy"] = {"server": proxy_server}
            print("Using configured proxy for Supermicro requests.")

        browser = playwright.chromium.launch(**launch_options)

        try:
            page = browser.new_page(
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page.route("**/*", skip_heavy)

            response = page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            status = response.status if response else 0
            print("http status:", status)

            if status >= 400 or status == 0:
                raise RuntimeError(
                    "HTTP " + str(status) + " عند فتح صفحة البحث"
                )

            try:
                page.wait_for_selector(
                    "div.result-wrapper",
                    state="attached",
                    timeout=45000,
                )
            except Exception as exc:
                raise RuntimeError(
                    "لم تظهر قائمة المنتجات. عنوان الصفحة: " + page.title()
                ) from exc

            page.wait_for_timeout(2000)

            for element in page.locator("div.result-wrapper").all():
                try:
                    name = element.locator(
                        "h3.result-title"
                    ).inner_text().strip()
                except Exception:
                    continue

                if MUST_CONTAIN.lower() not in name.lower():
                    continue

                href = element.locator("a.result").get_attribute("href")
                if not href:
                    continue

                href = urllib.parse.urljoin(URL, href)

                match = re.search(
                    r"\$[\d,]+\.\d{2}",
                    element.inner_text(),
                )
                price = match.group(0) if match else "N/A"

                items[href] = {
                    "name": name,
                    "price": price,
                    "stock": "unknown",
                    "source": "",
                }

            print("Products discovered:", len(items))

            if not items:
                raise RuntimeError(
                    "لم يتم العثور على أي منتجات مطابقة لـ RTX 5"
                )

            saved_debug = False

            for href, data in items.items():
                stock, source = check_stock(page, href)

                if source.startswith("http ") or source == "error":
                    raise RuntimeError(
                        "فشل فحص صفحة المنتج: "
                        + data["name"]
                        + " ("
                        + source
                        + ")"
                    )

                data["stock"] = stock
                data["source"] = source

                if stock == "unknown" and not saved_debug:
                    dump_debug(data["name"], href, page)
                    print("تعذر تحديد المخزون:", data["name"])
                    print("تم حفظ debug_stock.txt")
                    saved_debug = True

                page.wait_for_timeout(2000)

        finally:
            browser.close()

    return items


def fetch_products_with_retry():
    last_error = None
    attempts = int(CONFIG["retry_attempts"])
    delay = int(CONFIG["retry_delay_seconds"])

    for attempt in range(1, attempts + 1):
        try:
            print(f"محاولة الفحص {attempt}/{attempts}")
            return fetch_products()

        except Exception as exc:
            last_error = exc
            print("فشلت المحاولة:", type(exc).__name__, str(exc))

            if attempt < attempts:
                print(f"إعادة المحاولة بعد {delay} ثانية...")
                time.sleep(delay)

    raise RuntimeError(
        f"فشل الفحص بعد {attempts} محاولات. آخر خطأ: {last_error}"
    ) from last_error


def main():
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_json(STATE_FILE, {})
    fails = int(state.get("fails", 0))
    alerted = bool(state.get("alerted", False))

    try:
        seen = load_central_database()
    except Exception as exc:
        print("تعذر قراءة قاعدة المنتجات المركزية:", exc)
        fails += 1
        if not alerted:
            notify(
                "🚨 تعذر الاتصال بقاعدة Supermicro المركزية على GitHub\n"
                + str(exc)[:700]
            )
            alerted = True
        save_json(STATE_FILE, {"fails": fails, "alerted": alerted})
        raise

    try:
        current = fetch_products_with_retry()
        if not current:
            raise RuntimeError("الفحص لم يُرجع منتجات.")

    except Exception as exc:
        fails += 1
        print("فشل الفحص:", fails, "مرات متتالية.")
        print("السبب:", exc)

        if not alerted:
            notify(
                "🚨 تعذر فحص منتجات Supermicro بعد محاولات الإعادة\n"
                + "السبب: "
                + str(exc)[:800]
            )
            alerted = True

        save_json(STATE_FILE, {"fails": fails, "alerted": alerted})
        return

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # أول تعبئة للقاعدة لا ترسل تنبيهات منتجات جديدة.
    first_population = len(seen) == 0

    for data in current.values():
        data["checked_at"] = checked_at

    try:
        publish_database(current, checked_at)
    except Exception as exc:
        print("فشل نشر قاعدة المنتجات:", exc)
        save_json(STATE_FILE, {"fails": fails + 1, "alerted": alerted})
        raise

    if alerted:
        notify("✅ عاد بوت Supermicro للعمل بنجاح.")

    save_json(STATE_FILE, {"fails": 0, "alerted": False})

    print()
    print("found", len(current), "products")
    print()

    for url, data in current.items():
        print(
            "-",
            data["name"],
            "|",
            data["price"],
            "|",
            data["stock"],
            "(" + data["source"] + ")",
        )

    print()

    if first_population:
        print("أول تعبئة للقاعدة المركزية: تم الحفظ بدون تنبيهات تغيّر المخزون.")
    else:
        changes = 0

        for url, data in current.items():
            old = seen.get(url)

            if old is None:
                notify(
                    "🆕 منتج جديد\n"
                    + data["name"]
                    + "\nالسعر: "
                    + data["price"]
                    + "\nالحالة: "
                    + LABELS.get(data["stock"], data["stock"])
                    + "\n"
                    + url
                )
                changes += 1
                continue

            old_stock = old.get("stock", "unknown")

            if (
                data["stock"] in ("in_stock", "preorder")
                and old_stock == "out_of_stock"
            ):
                notify(
                    "🔄 رجع للمخزون\n"
                    + data["name"]
                    + "\n"
                    + data["price"]
                    + "\n"
                    + url
                )
                changes += 1

            elif (
                data["stock"] == "out_of_stock"
                and old_stock in ("in_stock", "preorder")
            ):
                notify("❌ نفد المخزون\n" + data["name"] + "\n" + url)
                changes += 1

        if changes == 0:
            print("ما فيه تغييرات.")

    save_json(LAST_SUCCESS_FILE, {"last_success": time.time()})

    append_history({
        "checked_at": checked_at,
        "channel": "server" if BOT_ENV == "debian" else "github",
        "result": "success",
        "products": len(current),
        "first_population": first_population,
    })

    publish_server_history()

    print()
    print("تم تحديث قاعدة المنتجات المركزية.")
    print("تم حفظ حالة البيئة:", BOT_ENV)
    print("تم تحديث سجل الفحوص.")


if __name__ == "__main__":
    main()
