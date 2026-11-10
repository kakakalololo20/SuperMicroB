"""Find a public HTTP proxy that can reach the public Supermicro RTX 5 search page.

Public proxies are untrusted and unreliable. This script sends only a public GET
request to the product search page and never sends credentials or Telegram tokens.
"""
import concurrent.futures
import os
import sys
import urllib.error
import urllib.request

PROXY_LIST_URL = (
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/"
    "protocols/http.txt"
)
TARGET_URL = "https://store.supermicro.com/us_en/catalogsearch/result/?q=rtx%205"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MAX_CANDIDATES = 100
TIMEOUT_SECONDS = 7


def fetch_candidates():
    request = urllib.request.Request(
        PROXY_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8", errors="replace")

    candidates = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#") or ":" not in value:
            continue
        host, _, port = value.rpartition(":")
        if host and port.isdigit():
            candidates.append(f"http://{host}:{port}")
    return candidates[:MAX_CANDIDATES]


def test_proxy(proxy):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(
        TARGET_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
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
    print("جاري تحميل قائمة البروكسيات العامة...")
    candidates = fetch_candidates()
    print(f"تم تحميل {len(candidates)} عنواناً. يجري اختبارها على صفحة Supermicro...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(test_proxy, proxy) for proxy in candidates]
        for future in concurrent.futures.as_completed(futures):
            proxy = future.result()
            if proxy:
                print("تم العثور على Proxy وصل إلى صفحة البحث بنجاح.")
                github_env = os.environ.get("GITHUB_ENV")
                if not github_env:
                    print("GITHUB_ENV غير موجود؛ العنوان المختبر:", proxy)
                    return
                with open(github_env, "a", encoding="utf-8") as env_file:
                    env_file.write(f"PROXY_SERVER={proxy}\n")
                return

    print("لم نعثر على Proxy مجاني اجتاز اختبار صفحة Supermicro.")
    print("لن يتم تشغيل البوت باتصال مباشر حتى لا نسجل فشل 403 كأنه نفاد منتجات.")
    sys.exit(1)


if __name__ == "__main__":
    main()
