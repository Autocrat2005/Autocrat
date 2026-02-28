"""
Autocrat — Comet Web Agent Plugin
Deep web automation using Playwright.
"""

import os
import re
import json
import hashlib
import httpx
from urllib.parse import urlparse
from typing import Any, Dict, Optional

from nexus.core.plugin import NexusPlugin
from nexus.core.config import Config


class CometWebAgentPlugin(NexusPlugin):
    name = "comet_web_agent"
    icon = "☄️"
    description = "Deep web automation with Playwright"
    version = "1.0.0"

    def setup(self):
        self.config = Config()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

        self.register_command(
            "web_navigate",
            self.web_navigate,
            "Open URL in Comet browser context",
            "web navigate <url>",
        )
        self.register_command(
            "web_click",
            self.web_click,
            "Click a CSS selector",
            "web click <selector>",
        )
        self.register_command(
            "web_type",
            self.web_type,
            "Type into a CSS selector",
            "web type <selector> <text>",
        )
        self.register_command(
            "web_extract_text",
            self.web_extract_text,
            "Extract text from a CSS selector",
            "web extract <selector>",
        )
        self.register_command(
            "codeforces_latest_div2_download",
            self.codeforces_latest_div2_download,
            "Find latest Codeforces Div 2 contest and download statements",
            "download latest codeforces div2 to <workspace>",
        )
        self.register_command(
            "react_plan",
            self.react_plan,
            "Run ReAct loop: DOM -> LLM next step -> execute/mock -> observe",
            "react plan <task> [start_url]",
        )

    def _ensure_browser(self, headless: bool = True):
        if self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            raise RuntimeError(
                "Playwright not installed. Install with: pip install playwright && playwright install"
            )

        self._playwright = sync_playwright().start()
        launch_error = None
        
        # Add stealth args to bypass Cloudflare
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--window-size=1920,1080"
        ]
        
        try:
            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=browser_args
            )
        except Exception as exc:
            launch_error = exc
            # Fallback for environments where playwright-managed browser binaries
            # cannot be downloaded: use the locally installed Edge channel.
            self._browser = self._playwright.chromium.launch(
                channel="msedge",
                headless=headless,
                args=browser_args
            )

        self._context = self._browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        # Add stealth scripts to context
        self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        self._page = self._context.new_page()
        if launch_error:
            self.log.warning(f"Playwright default Chromium unavailable, using Edge channel: {launch_error}")

    def _safe_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith(("http://", "https://")):
            return url
        return f"https://{url}"

    def _allowed_domains(self):
        domains = self.config.get("safety", "web", "allowlist_domains")
        if isinstance(domains, list) and domains:
            return [str(d).lower().strip() for d in domains if str(d).strip()]
        return ["codeforces.com", "github.com", "localhost"]

    def _destructive_terms(self):
        terms = self.config.get("safety", "web", "blocked_selector_terms")
        if isinstance(terms, list) and terms:
            return [str(t).lower().strip() for t in terms if str(t).strip()]
        return ["delete", "remove", "drop", "erase", "destroy", "terminate", "submit"]

    def _is_domain_allowed(self, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return False
        for domain in self._allowed_domains():
            if host == domain or host.endswith(f".{domain}"):
                return True
        return False

    def _is_safe_mode(self) -> bool:
        return bool(self.config.get("system", "safe_mode", default=False))

    # ── Auto-Bouncer: common popup / cookie-consent selectors ──────────────
    _BOUNCER_SELECTORS = [
        # Cookie consent buttons
        "#onetrust-accept-btn-handler",
        "#accept-cookie",
        ".cookie-consent-button",
        "[data-testid='cookie-policy-manage-dialog-accept-button']",
        "button[id*='cookie'][id*='accept']",
        "button[class*='cookie'][class*='accept']",
        "button[class*='consent'][class*='accept']",
        "a[id*='cookie'][id*='accept']",
        ".cc-accept",
        ".cc-dismiss",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#didomi-notice-agree-button",
        "[aria-label='Accept cookies']",
        "[aria-label='Accept all cookies']",
        "[aria-label='Close']",
        # Generic "close" modal buttons
        "[aria-label='Close modal']",
        "[aria-label='Dismiss']",
        "button[class*='modal-close']",
        "button[class*='dialog-close']",
        ".modal .close",
        "button[data-dismiss='modal']",
        # Newsletter / subscribe popups
        "[aria-label='Close newsletter signup']",
        "[aria-label='No thanks']",
        # GDPR banners
        ".gdpr-accept",
        "#gdpr-accept",
    ]

    def _dismiss_popups(self) -> list:
        """Auto-Bouncer: algorithmically dismiss cookie banners and modals.

        Runs *before* the LLM sees the DOM so overlays never confuse the planner.
        Returns list of selectors that were clicked/removed.
        """
        if not self._page:
            return []
        dismissed = []
        for sel in self._BOUNCER_SELECTORS:
            try:
                btn = self._page.query_selector(sel)
                if not btn:
                    continue
                # For overlays that intercept pointer events, remove them from DOM
                tag = btn.evaluate("el => el.tagName.toLowerCase()")
                is_overlay = btn.evaluate(
                    "el => el.hasAttribute('data-modal-dialog-overlay') || "
                    "el.classList.contains('modal-overlay') || "
                    "window.getComputedStyle(el).pointerEvents === 'auto' && el.offsetWidth > window.innerWidth * 0.8"
                )
                if is_overlay and tag == "div":
                    btn.evaluate("el => el.remove()")
                    dismissed.append(f"{sel} (removed overlay)")
                    try:
                        self._page.wait_for_timeout(200)
                    except Exception:
                        pass
                    continue
                # Normal click-to-dismiss for buttons
                if btn.is_visible():
                    btn.click(timeout=3000)
                    dismissed.append(sel)
                    try:
                        self._page.wait_for_timeout(400)
                    except Exception:
                        pass
            except Exception:
                pass
        return dismissed

    def _dom_snapshot(self, max_elements: int = 60) -> Dict[str, Any]:
        """Inject nexus-id tags into the live page and return a simplified element map.

        Phase 0: Auto-Bouncer — dismiss cookie / modal overlays before snapshotting.
        Phase 1: Tag every *truly visible* interactive element with nexus-id="N".
                 Uses getComputedStyle + getBoundingClientRect + isConnected to
                 filter out display:none, visibility:hidden, zero-size, and
                 off-screen elements.  This is the **Spatial Filter**.
        Phase 2: Read back only elements that carry meaningful text or href,
                 skipping pure-chrome (empty nav spans, decorative icons).
        """
        self._ensure_browser()

        # ── Phase 0 — Auto-Bouncer ──────────────────────────────────────────
        self._dismiss_popups()

        # ── Phase 1 — Spatial-filtered nexus-id injection ───────────────────
        inject_script = """
        () => {
          document.querySelectorAll('[nexus-id]').forEach(el => el.removeAttribute('nexus-id'));
          const selectors = 'a,button,input,textarea,select,[role="button"],[type="submit"],h1,h2,h3,h4,p,article,td,th,tr,li,label,span[role]';
          const nodes = Array.from(document.querySelectorAll(selectors));
          const vw = window.innerWidth  || document.documentElement.clientWidth;
          const vh = window.innerHeight || document.documentElement.clientHeight;
          let nid = 1;
          for (const el of nodes) {
            // --- Spatial Filter ---
            if (!el.isConnected) continue;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width <= 1 || rect.height <= 1) continue;
            // Off-screen: completely above/below/left/right of viewport
            if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) continue;

            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            const href = el.getAttribute('href') || '';
            const tag  = el.tagName;
            // Skip elements with no text and no useful href (decorative / icon-only nav)
            if (!text && !['INPUT','TEXTAREA','SELECT'].includes(tag)) continue;
            // Skip tiny nav/breadcrumb links (text < 2 chars, not an input)
            if (text.length < 2 && !['INPUT','TEXTAREA','SELECT'].includes(tag)) continue;
            el.setAttribute('nexus-id', String(nid));
            nid++;
          }
          return nid - 1;
        }
        """
        self._page.evaluate(inject_script)

        # ── Phase 2 — read back tagged elements as an indexed map ───────────
        read_script = """
        (maxN) => {
          const nodes = Array.from(document.querySelectorAll('[nexus-id]')).slice(0, maxN);
          const out = [];
          for (const el of nodes) {
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            out.push({
              nid: parseInt(el.getAttribute('nexus-id')),
              tag: (el.tagName || '').toLowerCase(),
              text: text.slice(0, 140),
              href: el.getAttribute('href') || '',
              type: el.getAttribute('type') || '',
              placeholder: el.getAttribute('placeholder') || '',
            });
          }
          return {
            title: document.title,
            url: location.href,
            total_tagged: nodes.length,
            elements: out,
          };
        }
        """
        return self._page.evaluate(read_script, max_elements)

    def _resolve_nexus_id(self, nexus_id) -> Any:
        """Resolve a nexus-id to a Playwright ElementHandle."""
        if not self._page or not nexus_id:
            return None
        return self._page.query_selector(f'[nexus-id="{nexus_id}"]')

    def _dom_hash(self, dom: Dict[str, Any]) -> str:
        stable = {
            "title": dom.get("title", ""),
            "url": dom.get("url", ""),
            "elements": dom.get("elements", []),
        }
        raw = json.dumps(stable, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    def _action_signature(self, step: Dict[str, Any]) -> str:
        action = str(step.get("action", "")).strip().lower()
        nexus_id = str(step.get("nexus_id", "")).strip()
        selector = str(step.get("selector", "")).strip()
        url = str(step.get("url", "")).strip()
        text = str(step.get("text", "")).strip()
        submit = bool(step.get("submit", False))
        wait_for_navigation = bool(step.get("wait_for_navigation", False))
        return json.dumps(
            {
                "action": action,
                "nexus_id": nexus_id,
                "selector": selector,
                "url": url,
                "text": text,
                "submit": submit,
                "wait_for_navigation": wait_for_navigation,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _summary_payload(
        self,
        task: str,
        status: str,
        trace: list,
        safe_mode: bool,
        result_data: str = "",
        success: bool = True,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        final_url = self._page.url if self._page else ""
        summary = {
            "task": task,
            "status": status,
            "steps_taken": len(trace),
            "final_url": final_url,
            "result_data": result_data,
            "safe_mode": safe_mode,
        }
        payload = {
            "success": success,
            "task": task,
            "status": status,
            "steps_taken": len(trace),
            "final_url": final_url,
            "result_data": result_data,
            "safe_mode": safe_mode,
            "steps": trace,
            "summary": summary,
        }
        if error:
            payload["error"] = error
        payload["result"] = result_data or status
        return payload


    def _planner_prompt(
        self,
        task: str,
        dom: Dict[str, Any],
        trace: list,
        step_idx: int,
        max_steps: int,
        feedback: str = "",
    ) -> str:
        trace_text = "\n".join(f"- {t}" for t in trace[-6:]) if trace else "- (none)"
        correction = feedback.strip() or "(none)"
        return f"""You are Comet planner for browser automation.
ORIGINAL GOAL: "{task}"
PREVIOUS STEPS TAKEN:
{trace_text}
CURRENT URL: {dom.get('url','')}
Step: {step_idx}/{max_steps}

Current page:
- title: {dom.get('title','')}
- url: {dom.get('url','')}
- total tagged elements: {dom.get('total_tagged', 0)}

Loop correction feedback:
{correction}

Tagged interactive elements (each has a unique nid):
{json.dumps(dom.get('elements', []), ensure_ascii=False)}

Allowed actions (choose exactly one):
1) {{"action":"navigate","url":"https://...","reasoning":"..."}}
2) {{"action":"click","nexus_id":"<nid>","wait_for_navigation":true,"reasoning":"..."}}
3) {{"action":"type","nexus_id":"<nid>","text":"...","submit":false,"wait_for_navigation":false,"reasoning":"..."}}
4) {{"action":"fill_form","fields":{{"<nid>":"value","<nid>":"value"}},"submit_id":"<nid or empty>","reasoning":"..."}}
5) {{"action":"extract","nexus_id":"<nid>","reasoning":"..."}}
6) {{"action":"extract_and_finish","status":"success","data":"<the answer>","reasoning":"..."}}
7) {{"action":"finish","status":"Task complete","result":"..."}}

Rules:
- Output STRICT JSON only (no markdown).
- ALWAYS use a nexus_id from the elements list above for click/type/extract/fill_form. NEVER invent CSS selectors.
- Prefer safest minimal next step.
- If the answer is already visible in an element's text field above, call extract_and_finish immediately with the data.
- Prefer extract_and_finish as soon as the answer is visible in any element text.
- If you need to fill multiple fields (e.g. login form), use fill_form to bundle them in one step.
- When typing into a search box, set "submit":true to press Enter and navigate.
- If previous action repeated with no page change, pick a different nexus_id or call finish.
"""

    def _planner_step(self, prompt: str) -> Dict[str, Any]:
        base_url = (self.config.get("ai", "local_base_url") or "http://127.0.0.1:11434").rstrip("/")
        model = self.config.get("ai", "local_model") or "qwen2.5-coder:3b"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 320},
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{base_url}/api/generate", json=payload)
        resp.raise_for_status()
        raw = (resp.json().get("response") or "").strip()

        clean = raw
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            parsed = json.loads(clean)
        except Exception:
            m = re.search(r'(\{(?:[^{}]|\{[^{}]*\})*\})', clean, re.DOTALL)
            if not m:
                return {"action": "finish", "status": "failed", "result": f"Planner output not JSON: {raw[:200]}"}
            try:
                parsed = json.loads(m.group(1))
            except Exception:
                return {"action": "finish", "status": "failed", "result": f"Planner output invalid JSON: {raw[:200]}"}
        if not isinstance(parsed, dict):
            return {"action": "finish", "status": "failed", "result": "Planner output must be object"}
        return parsed


    def react_plan(self, task: str = "", start_url: str = "", max_steps: int = 8, headless: bool = True, **kwargs):
        """Run ReAct planning loop with domain guardrails and safe-mode mocks."""
        if not task:
            return {"success": False, "error": "Missing task"}

        self._ensure_browser(headless=headless)

        target = self._safe_url(start_url) if start_url else "https://github.com"
        if not self._is_domain_allowed(target):
            return {
                "success": False,
                "error": f"Safety block: start_url not in allowlist ({target})",
                "allowed_domains": self._allowed_domains(),
            }

        try:
            self._page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return {
                "success": False,
                "error": f"Navigation failed for start_url: {target}",
                "details": str(e),
                "safe_mode": self._is_safe_mode(),
            }

        safe_mode = self._is_safe_mode()
        max_steps = max(1, min(int(max_steps or 8), 20))
        simulate_safe_mode_navigation = bool(
            kwargs.get(
                "simulate_safe_mode_navigation",
                self.config.get("safety", "web", "react_mock_navigation_in_safe_mode", default=False),
            )
        )
        trace = []
        previous_action_signature = ""
        previous_dom_hash = ""
        llm_feedback = ""
        consecutive_loop_count = 0
        last_extracted_data = ""

        for step_idx in range(1, max_steps + 1):
            dom = self._dom_snapshot(max_elements=60)
            current_dom_hash = self._dom_hash(dom)
            prompt = self._planner_prompt(task, dom, trace, step_idx, max_steps, llm_feedback)
            step = self._planner_step(prompt)

            action = str(step.get("action", "")).strip().lower()
            reasoning = step.get("reasoning", "")
            action_signature = self._action_signature(step)
            same_action = action_signature == previous_action_signature
            same_dom = current_dom_hash == previous_dom_hash

            if same_action and same_dom and action != "finish":
                consecutive_loop_count += 1
                trace.append(f"step {step_idx}: loop-intercept ({action})")

                # Auto-promote: if repeated extract with unchanged DOM and we have data → finish
                if action == "extract" and last_extracted_data:
                    trace.append(f"step {step_idx}: auto-promoted extract→extract_and_finish")
                    return self._summary_payload(
                        task=task,
                        status="success",
                        trace=trace,
                        safe_mode=safe_mode,
                        result_data=last_extracted_data,
                        success=True,
                    )

                # Auto-promote: if repeated type into same field → auto-submit by pressing Enter
                if action == "type" and consecutive_loop_count >= 1:
                    nid = str(step.get("nexus_id", "")).strip()
                    el = self._resolve_nexus_id(nid) if nid else None
                    if el and not safe_mode:
                        try:
                            self._page.keyboard.press("Enter")
                            self._page.wait_for_timeout(1500)
                            try:
                                self._page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            trace.append(f"step {step_idx}: auto-submit (Enter) after repeated type into nid={nid}")
                            previous_action_signature = ""
                            previous_dom_hash = ""
                            consecutive_loop_count = 0
                            llm_feedback = ""
                            continue
                        except Exception:
                            pass

                if consecutive_loop_count >= 3:
                    return self._summary_payload(
                        task=task,
                        status="failed_stuck_in_loop",
                        trace=trace,
                        safe_mode=safe_mode,
                        result_data=last_extracted_data or "Planner aborted after 3 consecutive unchanged-loop interceptions",
                        success=False,
                        error="Planner stuck in repeated action loop with unchanged DOM",
                    )
                msg = (
                    "Warning: You just tried that action and the page did not change. "
                    "Try a different nexus_id, call extract_and_finish with data you already see, or call finish."
                )
                llm_feedback = msg
                continue

            llm_feedback = ""
            consecutive_loop_count = 0

            if action == "extract_and_finish":
                status = step.get("status", "success")
                data = str(step.get("data", "") or step.get("result", "") or "").strip()
                result_data = data or "Extracted result"
                trace.append(f"step {step_idx}: extract_and_finish -> {result_data[:180]}")
                return self._summary_payload(
                    task=task,
                    status=status,
                    trace=trace,
                    safe_mode=safe_mode,
                    result_data=result_data,
                    success=True,
                )

            if action == "finish":
                status = step.get("status", "complete")
                result_data = step.get("result") or status or "Task complete"
                return self._summary_payload(
                    task=task,
                    status=status,
                    trace=trace,
                    safe_mode=safe_mode,
                    result_data=result_data,
                    success=True,
                )

            if action == "navigate":
                url = self._safe_url(str(step.get("url", "")).strip())
                if not url:
                    trace.append(f"step {step_idx}: navigate missing url")
                    llm_feedback = "Missing URL for navigate action. Provide a valid allowlisted URL or finish."
                    continue
                if not self._is_domain_allowed(url):
                    return {
                        "success": False,
                        "error": f"Safety block: planner tried non-allowlisted domain ({url})",
                        "allowed_domains": self._allowed_domains(),
                        "steps": trace,
                    }
                try:
                    wait_for_navigation = bool(step.get("wait_for_navigation", False))
                    wait_until = "networkidle" if wait_for_navigation else "domcontentloaded"
                    self._page.goto(url, wait_until=wait_until, timeout=60000)
                    trace.append(f"step {step_idx}: navigate -> {url} ({reasoning})")
                    previous_action_signature = action_signature
                    previous_dom_hash = current_dom_hash
                except Exception as e:
                    trace.append(f"step {step_idx}: navigate failed -> {url}: {e}")
                    return self._summary_payload(
                        task=task,
                        status="failed",
                        trace=trace,
                        safe_mode=safe_mode,
                        result_data="",
                        success=False,
                        error=f"Navigation failed during step {step_idx}: {e}",
                    )
                continue

            if action == "click":
                nexus_id = str(step.get("nexus_id", "")).strip()
                selector = str(step.get("selector", "")).strip()
                wait_for_navigation = bool(step.get("wait_for_navigation", False))
                # Resolve element: prefer nexus_id, fall back to raw selector
                element = self._resolve_nexus_id(nexus_id) if nexus_id else None
                click_label = f"nid={nexus_id}" if nexus_id else selector
                if not element and selector:
                    element = self._page.query_selector(selector)
                    click_label = selector
                if not element:
                    trace.append(f"step {step_idx}: click target not found (nid={nexus_id} sel={selector})")
                    llm_feedback = f"Element nexus_id={nexus_id} not found on page. Pick a different nexus_id from the list or call finish."
                    continue
                el_tag = element.evaluate("el => el.tagName.toLowerCase()")
                el_text = (element.inner_text() or "")[:80]
                blocked = self._guard_mutation("react_click", selector=f"{click_label} {el_text}")
                if blocked:
                    trace.append(f"step {step_idx}: blocked click {click_label}: {blocked.get('error')}")
                    return {"success": False, "error": blocked.get("error"), "steps": trace, "safe_mode": safe_mode}
                if safe_mode:
                    trace.append(
                        f"step {step_idx}: [SAFE MODE] would click {click_label} wait_nav={wait_for_navigation} ({reasoning})"
                    )
                    if simulate_safe_mode_navigation:
                        self._page.set_content(
                            (
                                "<html><head><title>SAFE MODE SIMULATION</title></head>"
                                f"<body><h1>Safe Mode Simulated Transition</h1><p>step: {step_idx}</p>"
                                f"<p>action: click</p><p>nexus_id: {nexus_id}</p></body></html>"
                            )
                        )
                else:
                    try:
                        element.click(timeout=15000)
                    except Exception as click_err:
                        # Overlay intercept fallback: use force click
                        if "subtree intercepts pointer events" in str(click_err):
                            self._dismiss_popups()
                            try:
                                element.click(timeout=10000, force=True)
                            except Exception as force_err:
                                trace.append(f"step {step_idx}: click failed even with force: {force_err}")
                                llm_feedback = f"Click on {click_label} failed (overlay). Try a different element or call finish."
                                continue
                        else:
                            trace.append(f"step {step_idx}: click failed: {click_err}")
                            llm_feedback = f"Click on {click_label} failed: {str(click_err)[:100]}. Try a different element."
                            continue
                    if wait_for_navigation:
                        try:
                            self._page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                    trace.append(f"step {step_idx}: clicked {click_label} [{el_tag}: {el_text[:40]}] ({reasoning})")
                previous_action_signature = action_signature
                previous_dom_hash = current_dom_hash
                continue

            if action == "type":
                nexus_id = str(step.get("nexus_id", "")).strip()
                selector = str(step.get("selector", "")).strip()
                text = str(step.get("text", ""))
                submit = bool(step.get("submit", False))
                wait_for_navigation = bool(step.get("wait_for_navigation", False))
                element = self._resolve_nexus_id(nexus_id) if nexus_id else None
                type_label = f"nid={nexus_id}" if nexus_id else selector
                if not element and selector:
                    element = self._page.query_selector(selector)
                    type_label = selector
                if not element:
                    trace.append(f"step {step_idx}: type target not found (nid={nexus_id} sel={selector})")
                    llm_feedback = f"Element nexus_id={nexus_id} not found on page. Pick a different nexus_id or call finish."
                    continue
                blocked = self._guard_mutation("react_type", selector=type_label, text=text)
                if blocked:
                    trace.append(f"step {step_idx}: blocked type {type_label}: {blocked.get('error')}")
                    return {"success": False, "error": blocked.get("error"), "steps": trace, "safe_mode": safe_mode}
                if safe_mode:
                    trace.append(
                        f"step {step_idx}: [SAFE MODE] would type '{text[:60]}' into {type_label} submit={submit} wait_nav={wait_for_navigation} ({reasoning})"
                    )
                    if simulate_safe_mode_navigation:
                        self._page.set_content(
                            (
                                "<html><head><title>SAFE MODE SIMULATION</title></head>"
                                f"<body><h1>Safe Mode Simulated Transition</h1><p>step: {step_idx}</p>"
                                f"<p>action: type</p><p>nexus_id: {nexus_id}</p></body></html>"
                            )
                        )
                else:
                    # Determine if target is an actual input/textarea or a trigger button
                    el_tag_t = element.evaluate("el => el.tagName.toLowerCase()")
                    el_type_t = element.evaluate("el => (el.type || '').toLowerCase()")
                    is_real_input = el_tag_t in ("input", "textarea", "select") and el_type_t != "submit"

                    if is_real_input:
                        # Standard fill for genuine input elements
                        try:
                            element.fill(text)
                        except Exception as fill_err:
                            if "subtree intercepts pointer events" in str(fill_err) or "Element is not an" in str(fill_err):
                                self._dismiss_popups()
                                try:
                                    element.evaluate("(el, t) => { el.focus(); el.value = t; el.dispatchEvent(new Event('input', {bubbles:true})); }", text)
                                except Exception as js_err:
                                    trace.append(f"step {step_idx}: type failed (overlay): {js_err}")
                                    llm_feedback = f"Could not type into {type_label}. Try a different element."
                                    continue
                            else:
                                trace.append(f"step {step_idx}: type failed: {fill_err}")
                                llm_feedback = f"Type into {type_label} failed: {str(fill_err)[:100]}."
                                continue
                    else:
                        # For buttons/links that trigger a search dialog (e.g. GitHub),
                        # click to open the dialog, wait, then keyboard-type
                        try:
                            element.click(timeout=5000)
                        except Exception:
                            try:
                                element.click(timeout=5000, force=True)
                            except Exception:
                                pass
                        self._page.wait_for_timeout(600)
                        # Do NOT call _dismiss_popups here — the dialog IS the search UI
                        self._page.keyboard.type(text, delay=30)

                    if submit:
                        self._page.keyboard.press("Enter")
                        # Give SPA time to navigate before checking URL
                        self._page.wait_for_timeout(1500)
                        try:
                            self._page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                    elif wait_for_navigation:
                        try:
                            self._page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                    trace.append(f"step {step_idx}: typed into {type_label} submit={submit} ({reasoning})")
                previous_action_signature = action_signature
                previous_dom_hash = current_dom_hash
                continue

            if action == "fill_form":
                # ── Semantic Action Bundling ─────────────────────────────────
                fields = step.get("fields", {})
                submit_id = str(step.get("submit_id", "")).strip()
                if not isinstance(fields, dict) or not fields:
                    trace.append(f"step {step_idx}: fill_form missing/empty fields dict")
                    llm_feedback = "fill_form requires a 'fields' dict mapping nexus_id->value. Try again or use type."
                    continue
                filled = []
                fill_failed = []
                for fld_nid, fld_val in fields.items():
                    el = self._resolve_nexus_id(str(fld_nid).strip())
                    if not el:
                        fill_failed.append(str(fld_nid))
                        continue
                    blocked = self._guard_mutation("react_type", selector=f"nid={fld_nid}", text=str(fld_val))
                    if blocked:
                        trace.append(f"step {step_idx}: fill_form blocked field nid={fld_nid}: {blocked.get('error')}")
                        return {"success": False, "error": blocked.get("error"), "steps": trace, "safe_mode": safe_mode}
                    if safe_mode:
                        trace.append(f"step {step_idx}: [SAFE MODE] would fill nid={fld_nid} with '{str(fld_val)[:40]}'")
                    else:
                        el.fill(str(fld_val))
                        filled.append(str(fld_nid))
                # Submit button
                if submit_id and not safe_mode:
                    sbtn = self._resolve_nexus_id(submit_id)
                    if sbtn:
                        blocked = self._guard_mutation("react_click", selector=f"nid={submit_id}")
                        if blocked:
                            trace.append(f"step {step_idx}: fill_form submit blocked: {blocked.get('error')}")
                            return {"success": False, "error": blocked.get("error"), "steps": trace, "safe_mode": safe_mode}
                        sbtn.click(timeout=15000)
                        try:
                            self._page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        filled.append(f"submit={submit_id}")
                    else:
                        fill_failed.append(f"submit={submit_id}")
                summary = f"filled [{','.join(filled)}]"
                if fill_failed:
                    summary += f" missing [{','.join(fill_failed)}]"
                trace.append(f"step {step_idx}: fill_form {summary} ({reasoning})")
                if fill_failed and not filled:
                    llm_feedback = f"fill_form: none of the nexus_ids were found ({','.join(fill_failed)}). Check the element list."
                    continue
                previous_action_signature = action_signature
                previous_dom_hash = current_dom_hash
                continue

            if action == "extract":
                nexus_id = str(step.get("nexus_id", "")).strip()
                selector = str(step.get("selector", "")).strip()
                node = None
                extract_label = "body"
                if nexus_id:
                    node = self._resolve_nexus_id(nexus_id)
                    extract_label = f"nid={nexus_id}"
                if not node and selector:
                    node = self._page.query_selector(selector)
                    extract_label = selector
                if not node:
                    node = self._page.query_selector("body")
                    extract_label = "body (fallback)"
                extracted = (node.inner_text() if node else "") or ""
                extracted = extracted.strip()[:1200]
                if extracted:
                    last_extracted_data = extracted
                trace.append(f"step {step_idx}: extracted from {extract_label}: {extracted[:120]} ({reasoning})")
                previous_action_signature = action_signature
                previous_dom_hash = current_dom_hash
                continue

            trace.append(f"step {step_idx}: unknown action '{action}'")
            llm_feedback = f"Unknown action '{action}'. Choose navigate/click/type/fill_form/extract/finish only."

        return self._summary_payload(
            task=task,
            status="max_steps",
            trace=trace,
            safe_mode=safe_mode,
            result_data="Planner reached max steps",
            success=True,
        )

    def _guard_mutation(self, action: str, selector: str = "", text: str = ""):
        if not self._page:
            return {"success": False, "error": "Browser page not initialized"}

        current_url = self._page.url or ""
        if not self._is_domain_allowed(current_url):
            return {
                "success": False,
                "error": f"Safety block: '{action}' disabled outside allowlisted domains",
                "url": current_url,
                "allowed_domains": self._allowed_domains(),
            }

        haystack = f"{selector} {text}".lower()
        for term in self._destructive_terms():
            if term and term in haystack:
                return {
                    "success": False,
                    "error": f"Safety block: destructive term '{term}' detected in action payload",
                    "url": current_url,
                }
        return None

    def web_navigate(self, url: str = "", headless: bool = True, **kwargs):
        if not url:
            return {"success": False, "error": "Missing url"}
        self._ensure_browser(headless=headless)
        target = self._safe_url(url)
        self._page.goto(target, wait_until="domcontentloaded", timeout=60000)
        title = self._page.title()
        return {"success": True, "result": f"Opened {target}", "title": title, "url": self._page.url}

    def web_click(self, selector: str = "", **kwargs):
        if not selector:
            return {"success": False, "error": "Missing selector"}
        self._ensure_browser()
        blocked = self._guard_mutation("web_click", selector=selector)
        if blocked:
            return blocked
        self._page.click(selector, timeout=15000)
        return {"success": True, "result": f"Clicked {selector}", "url": self._page.url}

    def web_type(self, selector: str = "", text: str = "", submit: bool = False, **kwargs):
        if not selector:
            return {"success": False, "error": "Missing selector"}
        self._ensure_browser()
        blocked = self._guard_mutation("web_type", selector=selector, text=text)
        if blocked:
            return blocked
        self._page.fill(selector, text or "")
        if submit:
            self._page.press(selector, "Enter")
        return {"success": True, "result": f"Typed into {selector}", "url": self._page.url}

    def web_extract_text(self, selector: str = "", max_chars: int = 2000, **kwargs):
        if not selector:
            return {"success": False, "error": "Missing selector"}
        self._ensure_browser()
        node = self._page.query_selector(selector)
        if not node:
            return {"success": False, "error": f"Selector not found: {selector}"}
        text = (node.inner_text() or "").strip()
        return {"success": True, "result": text[:max_chars], "url": self._page.url}

    def codeforces_latest_div2_download(self, workspace: str = "", headless: bool = True, **kwargs):
        """
        Finds latest (most recent past) Codeforces Div.2 contest and saves problem statements.
        Uses tr[data-contestid] selector which is what Codeforces actually uses in their HTML.
        """
        self._ensure_browser(headless=headless)

        out_root = workspace.strip() or os.getcwd()
        out_root = os.path.abspath(out_root)

        self._page.goto("https://codeforces.com/contests", wait_until="domcontentloaded", timeout=60000)

        # Use JS evaluate to find the first past Div. 2 contest row
        # (skip upcoming contests which have "before start" / "register" text)
        contest_js = self._page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tr[data-contestid]'));
            const div2rows = rows.filter(r => {
                const txt = (r.innerText || '').toLowerCase();
                return txt.includes('div. 2') || txt.includes('div 2');
            });
            // Prefer past contests (no upcoming register/before-start text)
            const past = div2rows.filter(r => {
                const txt = (r.innerText || '').toLowerCase();
                return !txt.includes('before start') && !txt.includes('register \\u00bb');
            });
            const row = past.length ? past[0] : div2rows[0];
            if (!row) return null;
            const cid = row.getAttribute('data-contestid');
            const cells = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim());
            return { contestId: cid, name: cells[0] || '' };
        }""")

        if not contest_js or not contest_js.get("contestId"):
            return {"success": False, "error": "Could not find a Div. 2 contest on the page"}

        contest_id = str(contest_js["contestId"])
        contest_name = contest_js.get("name", "").replace("\n", " ").strip()[:180]

        contest_url = f"https://codeforces.com/contest/{contest_id}"
        self._page.goto(contest_url, wait_until="domcontentloaded", timeout=60000)

        links = self._page.query_selector_all(f"a[href*='/contest/{contest_id}/problem/']")
        problem_urls = []
        seen = set()
        for node in links:
            href = node.get_attribute("href") or ""
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://codeforces.com{href}"
            if full_url in seen:
                continue
            seen.add(full_url)
            problem_urls.append(full_url)

        if not problem_urls:
            return {
                "success": False,
                "error": f"Contest found ({contest_id}) but no problem links were detected",
                "contest_url": contest_url,
            }

        contest_dir = os.path.join(out_root, "codeforces", str(contest_id))
        os.makedirs(contest_dir, exist_ok=True)

        saved = []
        for problem_url in problem_urls[:8]:
            self._page.goto(problem_url, wait_until="domcontentloaded", timeout=60000)
            statement = self._page.query_selector(".problem-statement")
            text = (statement.inner_text() if statement else self._page.inner_text("body")) or ""
            text = text.strip()

            suffix = problem_url.rstrip("/").split("/")[-1]
            suffix = re.sub(r"[^A-Za-z0-9_-]", "_", suffix)
            file_path = os.path.join(contest_dir, f"{suffix}.txt")
            with open(file_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(text)
            saved.append(file_path)

        return {
            "success": True,
            "result": f"Downloaded {len(saved)} problem statements",
            "contest_id": contest_id,
            "contest_name": contest_name,
            "contest_url": contest_url,
            "output_dir": contest_dir,
            "files": saved,
        }
