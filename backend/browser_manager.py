from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import logging
import base64
from urllib.parse import urlparse
import ipaddress
import socket

logger = logging.getLogger("minerva_backend.browser")

# List of common selectors for cookie banners to dismiss
COOKIE_SELECTORS = [
    "button[id*=cookie]", "button[class*=cookie]", "a[class*=cookie]",
    "[id*=consent]", "[class*=consent]", "button:has-text('Accept')",
    "button:has-text('Agree')", "button:has-text('Allow')",
    "button:has-text('Accept All')"
]

class SafeBrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None

    async def start(self):
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--disable-web-security", "--no-sandbox"]
            )
            # Emulate standard user viewport and user agent
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            self.page = await self.context.new_page()
            logger.info("Playwright Browser launched successfully.")

    async def cleanup(self):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.playwright = None
        logger.info("Playwright Browser stopped and cleaned up.")

    def is_url_safe(self, url: str) -> bool:
        """
        Enforce safety checks on URLs to prevent private network requests.
        """
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False
            
            # Resolve domain to IP
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)
            
            if ip_obj.is_private or ip_obj.is_loopback:
                logger.warning(f"Blocked private network access to hostname: {hostname} ({ip})")
                return False
            return True
        except Exception as e:
            logger.error(f"Error validating URL safety for {url}: {e}")
            return False

    async def safe_goto(self, url: str):
        if not self.is_url_safe(url):
            raise ValueError(f"Navigation to URL {url} is blocked for safety (Private IP restriction).")
        
        await self.page.goto(url, wait_until="load", timeout=30000)
        await self.page.wait_for_timeout(2000)  # Wait for JS hydration
        await self.dismiss_cookie_banners()

    async def dismiss_cookie_banners(self):
        """
        Attempt to auto-click common cookie banners to clear page overlay issues.
        """
        for selector in COOKIE_SELECTORS:
            try:
                elements = await self.page.query_selector_all(selector)
                for element in elements:
                    if await element.is_visible():
                        await element.click()
                        logger.info(f"Auto-dismissed cookie banner using selector: {selector}")
                        await self.page.wait_for_timeout(500)
            except Exception:
                continue

    async def capture_screenshot(self) -> str:
        """
        Captures a viewport screenshot and returns it as a base64 encoded string.
        """
        screenshot_bytes = await self.page.screenshot(type="jpeg", quality=75)
        return base64.b64encode(screenshot_bytes).decode("utf-8")

    async def get_interactive_dom(self) -> str:
        """
        Injects a JS script to extract interactive elements with stable visual indices
        and formats them into a simplified DOM structure for the LLM.
        """
        js_script = """
        () => {
            const interactiveElements = [];
            const tags = ['button', 'a', 'input', 'select', 'textarea'];
            
            let idx = 0;
            
            // Clear previous highlighting
            document.querySelectorAll('[data-minerva-id]').forEach(el => {
                el.removeAttribute('data-minerva-id');
            });

            // Find all potential interactive nodes
            const allElements = document.querySelectorAll('*');
            allElements.forEach(el => {
                const tagName = el.tagName.toLowerCase();
                const isInteractiveTag = tags.includes(tagName);
                const hasCursorPointer = window.getComputedStyle(el).cursor === 'pointer';
                const hasClickAttr = el.hasAttribute('onclick') || el.getAttribute('role') === 'button';
                
                // Element viability check
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
        elements = await self.page.evaluate(js_script)
        
        # Format elements as a simplified structural string
        simplified_dom = []
        for el in elements:
            text_part = f" text='{el['text']}'" if el['text'] else ""
            placeholder_part = f" placeholder='{el['placeholder']}'" if el['placeholder'] else ""
            role_part = f" role='{el['role']}'" if el['role'] else ""
            simplified_dom.append(
                f"<{el['tag']} id={el['id']}{text_part}{placeholder_part}{role_part} />"
            )
            
        return "\n".join(simplified_dom)
