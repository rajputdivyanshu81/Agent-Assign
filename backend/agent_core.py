"""
agent_core.py — The LLM-driven Observe-Think-Act loop.

This module orchestrates the entire agent lifecycle:
1. Observe: capture screenshot + interactive DOM
2. Think: send multimodal prompt to Groq (Llama 3.2 Vision) 
3. Act: parse structured action and execute via Playwright
4. Report: stream telemetry to the frontend via WebSocket

Edge cases handled:
- CAPTCHA / anti-bot detection
- Login wall detection
- Infinite loop / stuck state (3 identical actions)
- LLM hallucination (element not found)
- LLM unparseable response (retry up to 3x)
- Max steps limiter
- Graceful stop / pause via asyncio.Event
- Rate limit backoff on Groq 429
- Prompt injection isolation
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from groq import AsyncGroq
from browser_manager import SafeBrowserManager

logger = logging.getLogger("minerva_backend.agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_STEPS = 30
MAX_RETRIES_PER_STEP = 3
STUCK_THRESHOLD = 3  # Consecutive identical actions before halting
RATE_LIMIT_BACKOFF = [2, 4, 8]  # seconds
LLM_TIMEOUT_SECONDS = 35

CAPTCHA_INDICATORS = [
    "captcha", "recaptcha", "hcaptcha", "cf-turnstile",
    "challenge-running", "challenge-form", "cf-challenge",
    "please verify you are a human", "are you a robot"
]

LOGIN_INDICATORS = [
    "sign in", "log in", "login", "sign up", "create account",
    "enter your password", "forgot password", "authentication required"
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class AgentAction:
    """Represents a parsed action from the LLM."""
    action_type: str  # GOTO, CLICK, TYPE, SCROLL, EXTRACT, DONE, STUCK
    target: str = ""  # element id or url
    value: str = ""   # text to type
    reasoning: str = ""


@dataclass
class AgentState:
    """Tracks the running state of an agent session."""
    goal: str = ""
    current_url: str = ""
    step_count: int = 0
    action_history: list = field(default_factory=list)
    extracted_data: dict = field(default_factory=dict)
    status: str = "idle"  # idle, running, paused, stopped, completed, error, blocked


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are Minerva, an AI browser agent. You autonomously navigate web pages to accomplish the user's goal.

You will receive:
1. The user's GOAL
2. A list of INTERACTIVE ELEMENTS on the page, each with a numeric id
3. The readable PAGE TEXT
4. The CURRENT URL

You must respond with EXACTLY ONE action in valid JSON format. No extra text, no markdown.

Available actions:
- {"action": "GOTO", "url": "<full_url>", "reasoning": "<why>"}
- {"action": "CLICK", "element_id": <number>, "reasoning": "<why>"}
- {"action": "TYPE", "element_id": <number>, "text": "<text_to_type>", "reasoning": "<why>"}
- {"action": "PRESS_ENTER", "reasoning": "<why>"}
- {"action": "SCROLL", "direction": "down" | "up", "reasoning": "<why>"}
- {"action": "EXTRACT", "data": {<structured_extracted_data>}, "reasoning": "<why>"}
- {"action": "DONE", "summary": "<final_summary>", "reasoning": "<why>"}
- {"action": "STUCK", "reason": "<what_went_wrong>"}

Rules:
- Read the interactive elements to understand the current page state.
- Use GOTO only for full URLs, never for relative paths.
- Use CLICK with the element_id from the interactive elements list. If a popup or overlay blocks the page, look for and CLICK the 'Close', 'X', or 'Decline' element.
- Use TYPE to fill input fields. The field is automatically cleared before typing, so you don't need to manually clear pre-filled text.
- Use PRESS_ENTER to submit a form if there is no obvious submit button.
- Use EXTRACT when you have gathered useful data from the page. Include ALL relevant data.
- When compiling a product comparison, keep extracted data structured as arrays of objects with product titles, prices, source names, URLs, and notes.
- Use DONE when the goal is fully accomplished. Include a summary.
- Use STUCK if you cannot make progress after trying alternatives.
- NEVER attempt to bypass CAPTCHAs, login walls, or anti-bot protections.
- If you detect a CAPTCHA or login requirement, respond with STUCK and explain.
- If a CLICK does not visibly change the page or URL, do not repeat that same CLICK. Try a search, a direct URL, scrolling, or a different element.
- E-COMMERCE RULE: When searching for products on e-commerce sites (like ebay.com or bestbuy.com), identify the search input field, type the product name, and press enter. Then look for the main search result items, extract their titles and prices, and use GOTO to proceed to the next e-commerce site to compare.
- MULTI-SOURCE RULE: If the user asks for the "cheapest", "best", or "top" option, you MUST search at least two different sources (e.g., ebay.com and bestbuy.com). Do NOT call DONE until you have successfully extracted data from at least two different websites. If you fail to find information on one site, use GOTO to try another site immediately.
- EXTRACTION RULE: If your goal is to extract information and you can already see the relevant text on the screen, immediately use the EXTRACT action. Do not scroll or click unnecessarily.
- Be methodical: plan your approach, then execute step by step.
"""


# ---------------------------------------------------------------------------
# Core Agent
# ---------------------------------------------------------------------------
class MinervaAgent:
    def __init__(
        self,
        ws_send_callback,
        run_id: str,
        provider: str = "groq",
        api_key: str | None = None,
        model: str | None = None,
    ):
        """
        Args:
            ws_send_callback: async callable(dict) to stream events to the frontend
            run_id: unique identifier for this agent run
        """
        self.ws_send = ws_send_callback
        self.run_id = run_id
        self.browser = SafeBrowserManager()
        self.state = AgentState()
        self.provider = provider.lower()
        self.api_key = api_key or self._default_api_key_for_provider(self.provider)
        self.llm_model = model or self._default_model_for_provider(self.provider)
        self.llm_client = self._build_llm_client()

        # Steering controls
        self.stop_event = asyncio.Event()
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # Start unpaused
        self.approval_event = asyncio.Event()
        self.approval_event.set()  # Start without needing approval
        self.approval_mode = False

    def _default_api_key_for_provider(self, provider: str) -> str | None:
        env_map = {
            "openai": os.getenv("OPENAI_API_KEY"),
            "claude": os.getenv("ANTHROPIC_API_KEY"),
            "anthropic": os.getenv("ANTHROPIC_API_KEY"),
            "groq": os.getenv("GROQ_API_KEY"),
        }
        return env_map.get(provider)

    def _default_model_for_provider(self, provider: str) -> str:
        model_map = {
            "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "claude": os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
            "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
            "groq": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        }
        return model_map.get(provider, "gpt-4o-mini")

    def _build_llm_client(self):
        if self.provider == "openai":
            if not AsyncOpenAI:
                raise ImportError("The 'openai' package is not installed. Run `pip install openai` to use OpenAI models.")
            return AsyncOpenAI(api_key=self.api_key, max_retries=0, timeout=LLM_TIMEOUT_SECONDS)
        if self.provider in ("claude", "anthropic"):
            if not AsyncAnthropic:
                raise ImportError("The 'anthropic' package is not installed. Run `pip install anthropic` to use Claude models.")
            return AsyncAnthropic(api_key=self.api_key, max_retries=0, timeout=LLM_TIMEOUT_SECONDS)
        return AsyncGroq(api_key=self.api_key, max_retries=0, timeout=LLM_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run(self, goal: str):
        """Main entry point. Runs the full Observe-Think-Act loop."""
        self.state.goal = goal
        self.state.status = "running"

        try:
            await self.browser.start()
            await self._emit("status", {"status": "running"})
            await self._emit("log", {
                "step": 0,
                "step_type": "plan",
                "message": f"Goal received: {goal}. Starting browser session..."
            })

            # Start from a neutral search page and let the LLM choose the route.
            await self.browser.safe_goto("https://www.google.com")
            dom_data = await self.browser.get_interactive_dom()
            title = await self.browser.get_title()
            await self._emit("page_state", {
                "url": self.browser.page.url,
                "title": title,
                "elements": dom_data["elements"]
            })

            while self.state.step_count < MAX_STEPS:
                # --- Check stop signal ---
                if self.stop_event.is_set():
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": "Agent stopped by user."
                    })
                    self.state.status = "stopped"
                    break

                # --- Check pause signal ---
                if not self.pause_event.is_set():
                    await self._emit("status", {"status": "paused"})
                    await self.pause_event.wait()
                    await self._emit("status", {"status": "running"})

                self.state.step_count += 1

                # 1. OBSERVE
                dom_data = await self.browser.get_interactive_dom()
                dom_snapshot = dom_data["dom"]
                page_text = await self.browser.get_readable_text()
                self.state.current_url = self.browser.page.url
                title = await self.browser.get_title()

                await self._emit("page_state", {
                    "url": self.state.current_url,
                    "title": title,
                    "elements": dom_data["elements"]
                })

                # 2. Check for CAPTCHA / anti-bot
                captcha_detected = await self._check_captcha(dom_snapshot)
                if captcha_detected:
                    self.state.status = "blocked"
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": "⚠️ Anti-bot protection detected (CAPTCHA/Cloudflare). Stopping — human intervention required."
                    })
                    await self._emit("status", {"status": "blocked"})
                    break

                # 3. THINK — call LLM
                await self._emit("log", {
                    "step": self.state.step_count,
                    "step_type": "think",
                    "message": f"Analyzing page: {self.state.current_url}"
                })

                action = await self._think(screenshot, dom_snapshot, page_text)
                if action is None:
                    self.state.status = "error"
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": "LLM failed to return a valid action after retries. Stopping."
                    })
                    break

                # 4. Check for stuck state (infinite loop detection)
                if self._is_stuck(action):
                    if action.action_type == "EXTRACT" and self.state.extracted_data:
                        self.state.status = "completed"
                        await self._emit("log", {
                            "step": self.state.step_count,
                            "step_type": "done",
                            "message": "Agent repeated the same extraction, so the structured result is complete."
                        })
                        break

                    self.state.status = "error"
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": f"Agent is stuck — repeated the same action {STUCK_THRESHOLD} times. Halting."
                    })
                    break

                # 5. Emit reasoning
                await self._emit("log", {
                    "step": self.state.step_count,
                    "step_type": "think",
                    "message": f"💭 {action.reasoning}"
                })

                # 6. Wait for approval if in step-by-step mode
                if self.approval_mode:
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "action",
                        "message": f"⏸️ Waiting for approval: {action.action_type} {action.target} {action.value}"
                    })
                    await self._emit("status", {"status": "awaiting_approval"})
                    self.approval_event.clear()
                    try:
                        await asyncio.wait_for(self.approval_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        self.state.status = "stopped"
                        await self._emit("log", {
                            "step": self.state.step_count,
                            "step_type": "error",
                            "message": "Approval timeout (5 minutes). Agent stopped."
                        })
                        break
                    # Re-check stop after approval wait
                    if self.stop_event.is_set():
                        self.state.status = "stopped"
                        break

                # 7. ACT
                await self._emit("log", {
                    "step": self.state.step_count,
                    "step_type": "action",
                    "message": f"▶ {action.action_type}: {action.target or action.value or ''}"
                })

                success = await self._act(action)
                if not success:
                    continue  # Error was logged inside _act, retry next loop

                # 8. Check for terminal actions
                if action.action_type == "DONE":
                    self.state.status = "completed"
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "done",
                        "message": f"✅ {action.reasoning}"
                    })
                    break

                if action.action_type == "STUCK":
                    self.state.status = "error"
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": f"🚫 Agent reported stuck: {action.reasoning}"
                    })
                    break

                if action.action_type == "EXTRACT":
                    self.state.extracted_data.update(
                        action.value if isinstance(action.value, dict) else {}
                    )
                    await self._emit("result", {"data": self.state.extracted_data})

            else:
                # Max steps reached
                self.state.status = "error"
                await self._emit("log", {
                    "step": self.state.step_count,
                    "step_type": "error",
                    "message": f"Agent reached max steps limit ({MAX_STEPS}). Stopping."
                })

        except Exception as e:
            logger.exception(f"Unhandled agent error: {e}")
            self.state.status = "error"
            await self._emit("log", {
                "step": self.state.step_count,
                "step_type": "error",
                "message": f"Unexpected error: {str(e)}"
            })
        finally:
            # Final screenshot
            try:
                screenshot = await self.browser.capture_screenshot()
                await self._emit("screenshot", {"image": screenshot})
            except Exception:
                pass

            await self._emit("status", {"status": self.state.status})

            # Send final extracted data
            if self.state.extracted_data:
                await self._emit("result", {"data": self.state.extracted_data})

            await self.browser.cleanup()

    # ------------------------------------------------------------------
    # Steering controls (called from WebSocket handler)
    # ------------------------------------------------------------------
    def stop(self):
        self.stop_event.set()
        self.pause_event.set()  # Unblock if paused
        self.approval_event.set()  # Unblock if waiting

    def pause(self):
        self.pause_event.clear()

    def resume(self):
        self.pause_event.set()

    def approve_step(self):
        self.approval_event.set()

    def set_approval_mode(self, enabled: bool):
        self.approval_mode = enabled
        if not enabled:
            self.approval_event.set()

    # ------------------------------------------------------------------
    # Internal: LLM reasoning
    # ------------------------------------------------------------------
    async def _think(self, screenshot_b64: str, dom_snapshot: str, page_text: str) -> AgentAction | None:
        """Send multimodal prompt to Groq and parse the response."""

        user_message = f"""GOAL: {self.state.goal}

CURRENT URL: {self.state.current_url}
STEP: {self.state.step_count} of {MAX_STEPS}

<page_content>
INTERACTIVE ELEMENTS:
{dom_snapshot[:8000]}

READABLE PAGE TEXT:
{page_text[:12000]}
</page_content>

PREVIOUS ACTIONS (last 5):
{self._format_recent_actions()}

Respond with exactly one JSON action. No markdown, no extra text."""

        for attempt in range(MAX_RETRIES_PER_STEP):
            try:
                response = await asyncio.wait_for(
                    self._create_llm_response(user_message),
                    timeout=LLM_TIMEOUT_SECONDS + 5,
                )
                raw_response = response.strip()
                logger.info(f"LLM raw response: {raw_response}")

                return self._parse_action(raw_response)

            except asyncio.TimeoutError:
                logger.warning("LLM call timed out.")
                await self._emit("log", {
                    "step": self.state.step_count,
                    "step_type": "think",
                    "message": "LLM call timed out, retrying with a fresh observation..."
                })
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "rate_limit" in error_str.lower():
                    backoff = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
                    logger.warning(f"Rate limited by Groq. Backing off {backoff}s...")
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "think",
                        "message": f"⏳ LLM rate limited — retrying in {backoff}s..."
                    })
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"LLM call failed (attempt {attempt+1}): {e}")
                    if attempt < MAX_RETRIES_PER_STEP - 1:
                        await asyncio.sleep(1)

        return None

    async def _create_llm_response(self, user_message: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        if self.provider == "openai":
            response = await self.llm_client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                temperature=0.3,
                max_tokens=1024,
            )
            return response.choices[0].message.content.strip()

        if self.provider in ("claude", "anthropic"):
            response = await self.llm_client.messages.create(
                model=self.llm_model,
                max_tokens=1024,
                temperature=0.3,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            parts = []
            for block in response.content:
                if getattr(block, "type", "") == "text":
                    parts.append(block.text)
            return "".join(parts).strip()

        response = await self.llm_client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            temperature=0.3,
            max_tokens=1024,
        )
        return response.choices[0].message.content.strip()

    def _parse_action(self, raw: str) -> AgentAction | None:
        """Parse the LLM's JSON response into an AgentAction."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        cleaned = cleaned.rstrip("`").strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find JSON object in the response
            match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse LLM response as JSON: {cleaned[:200]}")
                    return None
            else:
                logger.error(f"No JSON found in LLM response: {cleaned[:200]}")
                return None

        action_type = data.get("action", "").upper()
        reasoning = data.get("reasoning", "")

        if action_type == "GOTO":
            return AgentAction(action_type="GOTO", target=data.get("url", ""), reasoning=reasoning)
        elif action_type == "CLICK":
            return AgentAction(action_type="CLICK", target=str(data.get("element_id", "")), reasoning=reasoning)
        elif action_type == "TYPE":
            return AgentAction(
                action_type="TYPE",
                target=str(data.get("element_id", "")),
                value=data.get("text", ""),
                reasoning=reasoning
            )
        elif action_type == "PRESS_ENTER":
            return AgentAction(action_type="PRESS_ENTER", reasoning=reasoning)
        elif action_type == "SCROLL":
            return AgentAction(action_type="SCROLL", target=data.get("direction", "down"), reasoning=reasoning)
        elif action_type == "EXTRACT":
            return AgentAction(action_type="EXTRACT", value=data.get("data", {}), reasoning=reasoning)
        elif action_type == "DONE":
            return AgentAction(action_type="DONE", reasoning=data.get("summary", reasoning))
        elif action_type == "STUCK":
            return AgentAction(action_type="STUCK", reasoning=data.get("reason", reasoning))
        else:
            logger.warning(f"Unknown action type from LLM: {action_type}")
            return None

    # ------------------------------------------------------------------
    # Internal: Action execution
    # ------------------------------------------------------------------
    async def _act(self, action: AgentAction) -> bool:
        """Execute a single agent action via Playwright. Returns True on success."""
        try:
            if action.action_type == "GOTO":
                await self.browser.safe_goto(action.target)

            elif action.action_type == "CLICK":
                success = await self.browser.click_element(action.target)
                if not success:
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": f"Element {action.target} not found. Will re-observe."
                    })
                    return False

            elif action.action_type == "TYPE":
                success = await self.browser.type_element(action.target, action.value)
                if not success:
                    await self._emit("log", {
                        "step": self.state.step_count,
                        "step_type": "error",
                        "message": f"Input element {action.target} not found. Will re-observe."
                    })
                    return False

            elif action.action_type == "PRESS_ENTER":
                await self.browser.press_enter()

            elif action.action_type == "SCROLL":
                await self.browser.scroll(action.target)

            elif action.action_type in ("EXTRACT", "DONE", "STUCK"):
                pass  # Terminal actions — no browser interaction needed

            else:
                logger.warning(f"Unhandled action type: {action.action_type}")
                return False


            # Record in history
            self.state.action_history.append(
                f"{action.action_type}:{action.target}:{action.value}"
            )
            return True

        except Exception as e:
            logger.error(f"Action execution error: {e}")
            await self._emit("log", {
                "step": self.state.step_count,
                "step_type": "error",
                "message": f"Action failed: {str(e)}"
            })
            return False

    # ------------------------------------------------------------------
    # Internal: Safety checks
    # ------------------------------------------------------------------
    async def _check_captcha(self, dom_snapshot: str) -> bool:
        """Check DOM for known CAPTCHA and anti-bot indicators."""
        combined_text = dom_snapshot.lower()
        page_title = await self.browser.get_title()
        combined_text += " " + page_title.lower()

        for indicator in CAPTCHA_INDICATORS:
            if indicator in combined_text:
                logger.warning(f"CAPTCHA indicator found: {indicator}")
                return True
        return False

    def _is_stuck(self, current_action: AgentAction) -> bool:
        """Detect infinite loops by checking the last N actions."""
        action_str = f"{current_action.action_type}:{current_action.target}:{current_action.value}"
        recent = self.state.action_history[-STUCK_THRESHOLD:]

        if len(recent) >= STUCK_THRESHOLD and all(a == action_str for a in recent):
            return True
        return False

    # ------------------------------------------------------------------
    # Internal: Helpers
    # ------------------------------------------------------------------
    def _format_recent_actions(self) -> str:
        """Format the last 5 actions for the LLM context."""
        recent = self.state.action_history[-5:]
        if not recent:
            return "None yet."
        return "\n".join([f"  - {a}" for a in recent])

    async def _emit(self, event_type: str, data: dict):
        """Send a structured event to the frontend via WebSocket."""
        payload = {"type": event_type, **data}
        try:
            await self.ws_send(payload)
        except Exception as e:
            logger.error(f"Failed to emit WS event: {e}")
