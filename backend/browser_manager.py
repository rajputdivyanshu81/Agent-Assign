"""
browser_manager.py — Thread-safe Playwright browser orchestration.

Uses Playwright's SYNC API running on a dedicated thread to avoid
Windows asyncio event loop incompatibility with uvicorn.
All public methods are async wrappers that delegate to the sync thread.
"""

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
import asyncio
import logging
import base64
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import ipaddress
import socket

logger = logging.getLogger("minerva_backend.browser")

# Dedicated thread pool for Playwright sync operations
_playwright_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")

# List of common selectors for cookie banners to dismiss
COOKIE_SELECTORS = [
    "button[id*=cookie]", "button[class*=cookie]", "a[class*=cookie]",
    "[id*=consent]", "[class*=consent]", "button:has-text('Accept')",
    "button:has-text('Agree')", "button:has-text('Allow')",
    "button:has-text('Accept All')"
]


class SafeBrowserManager:
    def __init__(self):
        self._pw = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a sync function in the dedicated Playwright thread."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _playwright_executor,
            functools.partial(func, *args, **kwargs)
        )

    # ------------------------------------------------------------------
    # Sync internals (run on the Playwright thread)
    # ------------------------------------------------------------------
    def _start_sync(self):
        if not self._pw:
            self._pw = sync_playwright().start()
            
            # Stealth arguments to bypass automation flags
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security"
            ]
            
            # Use a persistent context to save cookies/sessions
            user_data_dir = os.getenv("MINERVA_BROWSER_USER_DATA_DIR", "./browser_user_data")
            headless = os.getenv("MINERVA_HEADLESS", "true").lower() not in ("0", "false", "no")
            
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                args=browser_args,
                ignore_default_args=["--enable-automation"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720}
            )
            
            # In persistent context, there might be pages already open. We grab the first or create one.
            pages = self.context.pages
            self.page = pages[0] if pages else self.context.new_page()
            
            # Modify navigator.webdriver via init script
            self.page.add_init_script("delete navigator.__proto__.webdriver;")
            
            logger.info("Playwright Browser launched successfully (stealth mode sync thread).")

    def _cleanup_sync(self):
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self._pw:
                self._pw.stop()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
        finally:
            self._pw = None
            self.browser = None
            self.context = None
            self.page = None
            logger.info("Playwright Browser stopped and cleaned up.")

    def _goto_sync(self, url: str):
        self.page.goto(url, wait_until="load", timeout=30000)
        self.page.wait_for_timeout(2000)  # Wait for JS hydration
        self._dismiss_cookie_banners_sync()

    def _dismiss_cookie_banners_sync(self):
        for selector in COOKIE_SELECTORS:
            try:
                elements = self.page.query_selector_all(selector)
                for element in elements:
                    if element.is_visible():
                        element.click()
                        logger.info(f"Auto-dismissed cookie banner: {selector}")
                        self.page.wait_for_timeout(500)
            except Exception:
                continue

    def _screenshot_sync(self) -> str:
        screenshot_bytes = self.page.screenshot(type="jpeg", quality=75)
        return base64.b64encode(screenshot_bytes).decode("utf-8")

    def _get_interactive_dom_sync(self) -> str:
        js_script = """
        () => {
            const interactiveElements = [];
            const tags = ['button', 'a', 'input', 'select', 'textarea'];
            let idx = 0;

            document.querySelectorAll('[data-minerva-id]').forEach(el => {
                el.removeAttribute('data-minerva-id');
            });

            const allElements = document.querySelectorAll('*');
            allElements.forEach(el => {
                const tagName = el.tagName.toLowerCase();
                const isInteractiveTag = tags.includes(tagName);
                const hasCursorPointer = window.getComputedStyle(el).cursor === 'pointer';
                const hasClickAttr = el.hasAttribute('onclick') || el.getAttribute('role') === 'button';

                const rect = el.getBoundingClientRect();
                const isVisible = rect.width > 0 && rect.height > 0 &&
                                  window.getComputedStyle(el).display !== 'none' &&
                                  window.getComputedStyle(el).visibility !== 'hidden';

                if (isVisible && (isInteractiveTag || hasCursorPointer || hasClickAttr)) {
                    el.setAttribute('data-minerva-id', idx);

                    let textContent = (el.innerText || el.textContent || '').trim().substring(0, 50);
                    if (tagName === 'input') {
                        textContent = el.getAttribute('placeholder') || el.getAttribute('value') || '';
                    }

                    interactiveElements.push({
                        id: idx,
                        tag: tagName,
                        text: textContent,
                        placeholder: el.getAttribute('placeholder') || '',
                        role: el.getAttribute('role') || ''
                    });
                    idx++;
                }
            });
            return interactiveElements;
        }
        """
        elements = self.page.evaluate(js_script)

        simplified_dom = []
        for el in elements:
            text_part = f" text='{el['text']}'" if el['text'] else ""
            placeholder_part = f" placeholder='{el['placeholder']}'" if el['placeholder'] else ""
            role_part = f" role='{el['role']}'" if el['role'] else ""
            simplified_dom.append(
                f"<{el['tag']} id={el['id']}{text_part}{placeholder_part}{role_part} />"
            )
        return "\n".join(simplified_dom)

    def _get_readable_text_sync(self) -> str:
        js_script = """
        () => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script, style, noscript, svg, canvas').forEach(el => el.remove());
            return clone.innerText
                .split('\\n')
                .map(line => line.trim())
                .filter(Boolean)
                .join('\\n')
                .slice(0, 12000);
        }
        """
        return self.page.evaluate(js_script) or ""

    def _click_element_sync(self, minerva_id: str) -> bool:
        element = self.page.query_selector(f"[data-minerva-id='{minerva_id}']")
        if not element:
            return False
        # Use force=True to bypass overlays/modals that intercept clicks
        element.click(force=True, timeout=5000)
        self.page.wait_for_timeout(1500)
        return True

    def _type_element_sync(self, minerva_id: str, text: str) -> bool:
        element = self.page.query_selector(f"[data-minerva-id='{minerva_id}']")
        if not element:
            return False
        # Use force=True to bypass overlays intercepting pointer events
        element.click(force=True, timeout=5000)
        element.fill("")
        element.type(text, delay=50)
        self.page.wait_for_timeout(500)
        return True

    def _press_enter_sync(self):
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)
        return True

    def _scroll_sync(self, direction: str):
        delta = 400 if direction == "down" else -400
        self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(1000)

    def _get_url_sync(self) -> str:
        return self.page.url

    def _get_title_sync(self) -> str:
        return self.page.title()

    # ------------------------------------------------------------------
    # Public async API (delegates to sync thread)
    # ------------------------------------------------------------------
    async def start(self):
        await self._run_in_thread(self._start_sync)

    async def cleanup(self):
        await self._run_in_thread(self._cleanup_sync)

    def is_url_safe(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_private or ip_obj.is_loopback:
                logger.warning(f"Blocked private network access: {hostname} ({ip})")
                return False
            return True
        except Exception as e:
            logger.error(f"URL safety check failed for {url}: {e}")
            return False

    async def safe_goto(self, url: str):
        if not self.is_url_safe(url):
            raise ValueError(f"Navigation to {url} blocked (private IP restriction).")
        await self._run_in_thread(self._goto_sync, url)

    async def capture_screenshot(self) -> str:
        return await self._run_in_thread(self._screenshot_sync)

    async def get_interactive_dom(self) -> str:
        return await self._run_in_thread(self._get_interactive_dom_sync)

    async def get_readable_text(self) -> str:
        return await self._run_in_thread(self._get_readable_text_sync)

    async def click_element(self, minerva_id: str) -> bool:
        return await self._run_in_thread(self._click_element_sync, minerva_id)

    async def type_element(self, minerva_id: str, text: str) -> bool:
        return await self._run_in_thread(self._type_element_sync, minerva_id, text)

    async def press_enter(self) -> bool:
        return await self._run_in_thread(self._press_enter_sync)

    async def scroll(self, direction: str):
        await self._run_in_thread(self._scroll_sync, direction)

    async def get_url(self) -> str:
        return await self._run_in_thread(self._get_url_sync)

    async def get_title(self) -> str:
        return await self._run_in_thread(self._get_title_sync)
