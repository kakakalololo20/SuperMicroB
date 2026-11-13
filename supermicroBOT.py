import json
import os
import re
import time
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright


def load_env_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env_file()

# البحث في الموقع عن RTX 5 Series
URL = "https://store.supermicro.com/us_en/catalogsearch/result/?q=rtx%205"

BOT_ENV = os.environ.get("BOT_ENV", "debian").strip().lower()

# فصل ملفات الحالة بين Debian وGitHub Actions
STATE_DIR = "github_state" if BOT_ENV == "github" else "."

if STATE_DIR != ".":
    os.makedirs(STATE_DIR, exist_ok=True)

SEEN_FILE = os.path.join(STATE_DIR, "SuperMicroSeen.json")
STATE_FILE = os.path.join(STATE_DIR, "SuperMicroState.json")
CONFIG_FILE = "monitor_config.json"
LAST_SUCCESS_FILE = os.path.join(STATE_DIR, "last_success.json")


def load_config():
    defaults = {
        "max_scan_gap_minutes": 90,
        "watchdog_interval_minutes": 10,
        "scan_interval_minutes": 60,
        "retry_attempts": 3,
        "retry_delay_seconds": 30,
    }

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    for key, value in defaults.items():
        config.setdefault(key, value)

    return config


CONFIG = load_config()

# أي اسم منتج يحتوي على RTX 5 سيتم تضمينه
MUST_CONTAIN = "RTX 5"

HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Telegram متوقف مؤقتاً
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

LABELS = {
    "in_stock": "متوفر للشراء",
    "out_of_stock": "غير متوفر",
    "preorder": "طلب مسبق",
    "unknown": "غير معروف",
}


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
            "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
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
        print("فشل إرسال Telegram:", type(exc).__name__)


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    print("الملف", path, "تالف، سيتم نقله.")

    try:
        os.replace(path, path + ".bad")
    except Exception:
        pass

    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize(seen):
    out = {}

    for url, data in seen.items():
        if not isinstance(data, dict):
            continue

        out[url] = {
            "name": data.get("name", ""),
            "price": data.get("price", "N/A"),
            "stock": data.get("stock", "unknown"),
            "source": data.get("source", ""),
        }

    return out


def decide(atc, stock_text, page_text=""):
    """تحديد حالة المنتج من نص صفحة المنتج، دون الاعتماد على زر الشراء وحده."""
    stock = re.sub(r"\s+", " ", stock_text.lower()).strip()
    body = re.sub(r"\s+", " ", page_text.lower()).strip()

    if "out of stock" in stock:
        return "out_of_stock", "stock-element"

    if "pre-order" in stock or "preorder" in stock:
        return "preorder", "stock-element"

    if "in stock" in stock:
        return "in_stock", "stock-element"

    # حسب معيارنا الحالي: المنتج الذي لا يتيح الشراء المباشر يعتبر غير متاح.
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

    # قراءة زر الشراء للتشخيص فقط.
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

    with open("debug_stock.txt", "w", encoding="utf-8") as file:
        file.write("PRODUCT: " + name + "\n")
        file.write("URL: " + url + "\n\n")
        file.write("PAGE TEXT:\n")
        file.write(body[:5000])


def skip_heavy(route):
    if route.request.resource_type in ("image", "font", "media"):
        route.abort()
    else:
        route.continue_()


def fetch_products():
    items = {}

    with sync_playwright() as playwright:
        proxy_server = os.environ.get("PROXY_SERVER", "").strip()
        launch_options = {
            "headless": HEADLESS,
        }

        # Debian يستخدم Chromium المثبت بالنظام،
        # وGitHub Actions يستخدم Chromium الذي يديره Playwright.
        system_chromium = "/usr/bin/chromium"
        if os.environ.get("BOT_ENV", "debian").lower() == "debian":
            if os.path.exists(system_chromium):
                launch_options["executable_path"] = system_chromium

        if proxy_server:
            launch_options["proxy"] = {"server": proxy_server}
            print("Using tested public proxy for Supermicro requests.")

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
                raise RuntimeError("HTTP " + str(status) + " عند فتح صفحة البحث")

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
                    name = element.locator("h3.result-title").inner_text().strip()
                except Exception:
                    continue

                if MUST_CONTAIN.lower() not in name.lower():
                    continue

                href = element.locator("a.result").get_attribute("href")
                if not href:
                    continue

                href = urllib.parse.urljoin(URL, href)

                match = re.search(r"\\$[\\d,]+\\.\\d{2}", element.inner_text())
                price = match.group(0) if match else "N/A"

                items[href] = {
                    "name": name,
                    "price": price,
                    "stock": "unknown",
                    "source": "",
                }

            print("Products discovered:", len(items))

            if not items:
                raise RuntimeError("لم يتم العثور على أي منتجات مطابقة لـ RTX 5")

            saved_debug = False

            for href, data in items.items():
                stock, source = check_stock(page, href)

                if source.startswith("http ") or source == "error":
                    raise RuntimeError(
                        "فشل فحص صفحة المنتج: " + data["name"] + " (" + source + ")"
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

    return items, False


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
    seen = normalize(load_json(SEEN_FILE, {}))
    state = load_json(STATE_FILE, {})

    fails = int(state.get("fails", 0))
    alerted = bool(state.get("alerted", False))

    try:
        current, blocked = fetch_products_with_retry()

        if blocked or not current:
            raise RuntimeError("الفحص لم يُرجع نتائج صالحة.")

    except Exception as exc:
        fails += 1
        print("فشل الفحص:", fails, "مرات متتالية.")
        print("السبب:", exc)

        if not alerted:
            notify(
                "🚨 تعذر فحص منتجات Supermicro بعد 3 محاولات"
                + "\\nالسبب: "
                + str(exc)[:800]
            )
            alerted = True

        save_json(STATE_FILE, {"fails": fails, "alerted": alerted})
        return

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

    if seen:
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
    else:
        print("أول تشغيل: تم حفظ القائمة بدون إشعارات.")

    # دمج النتائج الحالية مع السابقة والإبقاء على المنتجات التي اختفت من البحث.
    merged = dict(seen)
    merged.update(current)
    save_json(SEEN_FILE, merged)

    print()
    print("تم حفظ البيانات بنجاح.")

    success_data = {
        "last_success": time.time()
    }
    save_json(LAST_SUCCESS_FILE, success_data)
    print("تم تحديث وقت آخر فحص ناجح.")


if __name__ == "__main__":
    main()
