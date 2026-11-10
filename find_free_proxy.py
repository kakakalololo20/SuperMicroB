"""Find a public HTTP proxy and verify it can load the public Supermicro search page.

Public proxies are untrusted and unreliable. Only public GET requests are sent;
no credentials, cookies, account data, or Telegram tokens are routed through them.
"""
import concurrent.futures
import os
import random
import re
import sys
import urllib.request

TARGET_URL = "https://store.supermicro.com/us_en/catalogsearch/result/?q=rtx%205"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Multiple independently maintained lists. These are HTTP proxy endpoints only;
# SOCKS entries are intentionally excluded because this workflow uses urllib
# and Playwright without extra SOCKS dependencies.
PROXY_SOURCES = [
    ("HProxy", "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt"),
    ("Proxy-Scraper", "https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/http.txt"),
    ("Monosans", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("Relayglass", "https://raw.githubusercontent.com/relayglass/free-proxy-list/main/protocol/http/http.txt"),
    ("Litport", "https://raw.githubusercontent.com/litportnet/free-proxy-list/live/proxies/http.txt"),
]

MAX_PER_SOURCE = 180
MAX_CANDIDATES = 400
MAX_TARGET_TESTS = 400
WORKERS = 25
TIMEOUT_SECONDS = 9


def download_text(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,*/*"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_http_proxies(text):
    found = []
    for line in text.splitlines():
        value = line.strip().strip("\"' ,")
        if not value or value.startswith("#"):
            continue

        # Accept only HTTP proxy endpoints. Do not silently reinterpret SOCKS.
        if value.startswith("http://"):
            endpoint = value[len("http://"):]
        elif "://" in value:
            continue
        else:
            endpoint = value

        endpoint = endpoint.split("/")[0]
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}", endpoint):
            host, port = endpoint.rsplit(":", 1)
            if all(0 <= int(part) <= 255 for part in host.split(".")) and 1 <= int(port) <= 65535:
                found.append("http://" + endpoint)
    return list(dict.fromkeys(found))


def fetch_candidates():
    by_source = []
    for name, url in PROXY_SOURCES:
        try:
            proxies = parse_http_proxies(download_text(url))[:MAX_PER_SOURCE]
            print(f"- {name}: تم تحميل {len(proxies)} عنوان HTTP")
            by_source.append(proxies)
        except Exception as exc:
            print(f"- {name}: تعذر تحميل القائمة ({type(exc).__name__})")
            by_source.append([])

    # Round-robin sources so one large list does not consume the whole sample.
    candidates = []
    seen = set()
    max_len = max((len(items) for items in by_source), default=0)
    for index in range(max_len):
        for items in by_source:
            if index < len(items) and items[index] not in seen:
                seen.add(items[index])
                candidates.append(items[index])
                if len(candidates) >= MAX_CANDIDATES:
                    break
        if len(candidates) >= MAX_CANDIDATES:
            break

    random.shuffle(candidates)
    return candidates


def test_proxy(proxy):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(
        TARGET_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(1_500_000).decode("utf-8", errors="ignore")
            if response.status == 200 and "result-wrapper" in body and "RTX 5" in body:
                return proxy
    except Exception:
        return None
    return None


def main():
    print("جاري تحميل قوائم البروكسيات العامة من عدة مصادر...")
    candidates = fetch_candidates()
    print(f"إجمالي العناوين الفريدة بعد إزالة التكرار: {len(candidates)}")
    print(f"يجري اختبار ما يصل إلى {min(len(candidates), MAX_TARGET_TESTS)} عنواناً على صفحة Supermicro...")

    if not candidates:
        print("لم يتم تحميل أي عناوين صالحة من المصادر.")
        sys.exit(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(test_proxy, proxy) for proxy in candidates[:MAX_TARGET_TESTS]]
        for future in concurrent.futures.as_completed(futures):
            proxy = future.result()
            if proxy:
                print("تم العثور على Proxy اجتاز اختبار صفحة البحث.")
                github_env = os.environ.get("GITHUB_ENV")
                if not github_env:
                    print("GITHUB_ENV غير موجود؛ العنوان المختبر:", proxy)
                    return
                with open(github_env, "a", encoding="utf-8") as env_file:
                    env_file.write(f"PROXY_SERVER={proxy}\n")
                return

    print("لم نعثر على Proxy مجاني اجتاز اختبار صفحة Supermicro من المصادر المتاحة.")
    print("تم إيقاف التشغيل بأمان؛ لن يتم الاتصال المباشر أو تحديث ملفات حالة المنتجات.")
    sys.exit(1)


if __name__ == "__main__":
    main()
