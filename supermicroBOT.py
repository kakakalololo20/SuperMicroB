import json
import os
import re
import urllib.parse
from playwright.sync_api import sync_playwright

# البحث في الموقع عن RTX 5 Series
URL = "https://store.supermicro.com/us_en/catalogsearch/result/?q=rtx%205"

SEEN_FILE = "SuperMicroSeen.json"
STATE_FILE = "SuperMicroState.json"

# أي اسم منتج يحتوي على RTX 5 سيتم تضمينه
MUST_CONTAIN = "RTX 5"

HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Telegram متوقف مؤقتاً
TELEGRAM_ENABLED = False

LABELS = {
    "in_stock": "متوفر للشراء",
    "out_of_stock": "غير متوفر",
    "preorder": "طلب مسبق",
    "unknown": "غير معروف",
}


def notify(message):
    # حالياً نطبع التنبيهات في سجل GitHub Actions فقط.
    # يمكن إضافة Telegram لاحقاً.
    print(message)
    print()


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
    blocked = False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)

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

            if status in (403, 429):
                print("الموقع رفض الطلب.")
                return items, True

            try:
                page.wait_for_selector(
                    "div.result-wrapper",
                    state="attached",
                    timeout=45000,
                )
            except Exception:
                print("ما ظهرت المنتجات.")
                print("title:", page.title())
                return items, False

            page.wait_for_timeout(2000)

            # استخراج كل المنتجات التي يحتوي اسمها على RTX 5.
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

                match = re.search(r"\$[\d,]+\.\d{2}", element.inner_text())
                price = match.group(0) if match else "N/A"

                items[href] = {
                    "name": name,
                    "price": price,
                    "stock": "unknown",
                    "source": "",
                }

            print("Products discovered:", len(items))

            saved_debug = False

            # فحص المخزون لكل منتج.
            for href, data in items.items():
                stock, source = check_stock(page, href)
                data["stock"] = stock
                data["source"] = source

                if source in ("http 403", "http 429"):
                    print("الموقع رفض صفحة المنتج.")
                    blocked = True
                    break

                if stock == "unknown" and not saved_debug:
                    dump_debug(data["name"], href, page)
                    print("تعذر تحديد المخزون:", data["name"])
                    print("تم حفظ debug_stock.txt")
                    saved_debug = True

                page.wait_for_timeout(2000)

        finally:
            browser.close()

    return items, blocked


def main():
    seen = normalize(load_json(SEEN_FILE, {}))
    state = load_json(STATE_FILE, {})

    fails = int(state.get("fails", 0))
    alerted = bool(state.get("alerted", False))

    try:
        current, blocked = fetch_products()
    except Exception as exc:
        print("خطأ أثناء الجلب:", exc)
        current = {}
        blocked = False

    # لا نحدّث قائمة المنتجات عند فشل الجلب حتى لا نمسح الحالة السابقة.
    if blocked or not current:
        fails += 1
        print("فشل الجلب:", fails, "مرات متتالية.")
        save_json(STATE_FILE, {"fails": fails, "alerted": alerted})
        return

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

            if old.get("price") != data["price"]:
                notify(
                    "💲 تغيّر السعر\n"
                    + data["name"]
                    + "\n"
                    + str(old.get("price"))
                    + " ← "
                    + data["price"]
                    + "\n"
                    + url
                )
                changes += 1

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


if __name__ == "__main__":
    main()
