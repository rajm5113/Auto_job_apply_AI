import json
import sqlite3
import asyncio
from datetime import datetime
from rich.console import Console
from browser.human_delay import delay
from utils.llm_client import LLMClient
from graph.state import AgentState
import config

console = Console()


class ApplierAgent:
    def __init__(self, browser_manager, logger=None):
        self.bm = browser_manager
        self.logger = logger
        self.llm = LLMClient(role="form_filler", logger=logger)
        self.llm_writer = LLMClient(role="writer", logger=logger)

    async def apply_all(self, profile: dict) -> dict:
        """
        Fetch all scored jobs, attempt to apply to each.
        Returns dict with applied and manual_review counts.
        """
        jobs = self._fetch_scored_jobs()

        if not jobs:
            if self.logger:
                self.logger.warn("applier", "No scored jobs found to apply to.")
            return {"applied": 0, "manual_review": 0}

        if self.logger:
            self.logger.info("applier", f"Starting applications for {len(jobs)} jobs...")

        applied = 0
        manual_review = 0

        for job in jobs:
            console.print(
                f"\n[cyan]Applying to:[/cyan] {job['job_title']} "
                f"@ {job['company']} [dim](score: {job['score']:.2f})[/dim]"
            )

            success = await self._apply_one(job, profile)

            if success:
                applied += 1
                console.print(f"[green]  Applied successfully[/green]")
                # Rate limit protection — wait 8 to 20 seconds between applications
                await delay(8000, 20000)
            else:
                manual_review += 1
                console.print(f"[yellow]  Sent to manual review[/yellow]")
                await delay(3000, 6000)

        if self.logger:
            self.logger.info(
                "applier",
                f"Done. Applied: {applied} | Manual review: {manual_review}"
            )

        return {"applied": applied, "manual_review": manual_review}

    async def _apply_one(self, job: dict, profile: dict) -> bool:
        """
        Attempt to apply to one job. Returns True on success, False on any failure.
        Updates job status in SQLite either way.
        """
        page = self.bm.page

        try:
            # Step 1 — navigate and let React settle
            await page.goto(job["job_url"], wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                await asyncio.sleep(3)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(1)

            # Check if job is closed
            try:
                closed = page.locator(
                    "text='No longer accepting applications'"
                )
                closed2 = page.locator("text='No longer accepting'")
                if await closed.count() > 0 or await closed2.count() > 0:
                    console.print("[yellow]  Job is no longer accepting applications — skipping[/yellow]")
                    self._mark_manual_review(job["job_url"], "closed_job")
                    return False
            except Exception:
                pass

            # Step 2 — click Easy Apply in one atomic call (page.click has built-in retry)
            clicked = False
            for sel in [
                "button.jobs-apply-button",
                "button:has-text('Easy Apply')",
                "button[aria-label*='Easy Apply']",
                "//button[contains(., 'Easy Apply')]",
                "//button[contains(., 'Apply')]",
                "text=Easy Apply",
                "text='Easy Apply'",
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    easy_btn = page.get_by_role("button", name="Easy Apply", exact=False)
                    if await easy_btn.count() > 0:
                        await easy_btn.first.click(timeout=3000)
                        clicked = True
                except Exception:
                    pass

            if not clicked:
                # ── DEBUG: print every button text so we know exactly what
                #    the bot sees when it gives up ───────────────────────────
                all_buttons = await page.query_selector_all("button")
                debug_lines = []
                for btn in all_buttons:
                    try:
                        t = (await btn.inner_text()).strip().replace("\n", " ")
                        v = await btn.is_visible()
                        debug_lines.append(f"  {'✓' if v else '✗'} {t!r}")
                    except Exception:
                        debug_lines.append("  (error reading button)")
                console.print("[red bold]── DEBUG: buttons found on page ──[/red bold]")
                for line in debug_lines[:30]:
                    console.print(f"[dim]{line}[/dim]")

                import os as _os, datetime as _dt
                _os.makedirs("data/screenshots", exist_ok=True)
                _ts = _dt.datetime.now().strftime("%H%M%S")
                await page.screenshot(
                    path=f"data/screenshots/{_ts}_no_apply_btn.png",
                    full_page=False
                )
                console.print(f"[dim]Screenshot saved: data/screenshots/{_ts}_no_apply_btn.png[/dim]")
                self._mark_manual_review(job["job_url"], "easy_apply_button_not_found")
                return False

            await delay(2000, 3500)

            # Step 3 — verify EASY APPLY modal opened.
            # CRITICAL: Don't use div[role='dialog'] — that matches LinkedIn's
            # Messaging popup. Instead wait for the actual Next/Submit button
            # that only exists inside the Easy Apply modal.
            modal_ready = False
            for wait_sel in [
                "button:has-text('Next')",
                "button:has-text('Submit application')",
                "button:has-text('Review')",
                "button:has-text('Continue')",
            ]:
                try:
                    await page.locator(wait_sel).first.wait_for(
                        state="visible", timeout=4000
                    )
                    modal_ready = True
                    break
                except Exception:
                    continue

            if not modal_ready:
                # Take debug screenshot
                import os as _os2, datetime as _dt2
                _os2.makedirs("data/screenshots", exist_ok=True)
                _ts2 = _dt2.datetime.now().strftime("%H%M%S")
                await page.screenshot(
                    path=f"data/screenshots/{_ts2}_modal_not_ready.png",
                    full_page=False
                )
                console.print(f"[dim]Screenshot: data/screenshots/{_ts2}_modal_not_ready.png[/dim]")
                self._mark_manual_review(job["job_url"], "modal_did_not_open")
                return False

            # Step 4 — loop through modal steps
            success = await self._fill_modal(page, job, profile)

            if success:
                self._mark_applied(job["job_url"])
                return True
            else:
                return False

        except Exception as e:
            reason = f"unhandled_exception: {str(e)[:200]}"
            if self.logger:
                self.logger.error("applier", f"Exception on {job['job_title']}: {e}")
            self._mark_manual_review(job["job_url"], reason)

            # Close any open modal before moving on
            try:
                dismiss = await page.query_selector(
                    "button[aria-label='Dismiss'], button[data-test-modal-close-btn]"
                )
                if dismiss:
                    await dismiss.click()
                    await delay(800, 1500)
            except Exception:
                pass

            return False

    async def _fill_modal(self, page, job: dict, profile: dict) -> bool:
        """
        Loop through all modal steps, filling fields on each.
        Returns True if application submitted successfully.
        """
        max_steps = 15
        step_count = 0
        consecutive_failures = 0  # Track repeated failures on same step
        previous_errors = []      # Pass errors to the next loop iteration

        while step_count < max_steps:
            step_count += 1
            await delay(1500, 2500)

            console.print(f"[dim]  Step {step_count}...[/dim]")

            # Hard abort — check for blockers first
            if await self._is_blocked(page):
                self._mark_manual_review(job["job_url"], "blocked_captcha_or_redirect")
                return False

            if await self._has_external_redirect(page):
                self._mark_manual_review(job["job_url"], "external_redirect_in_modal")
                return False

            # ── Fill all fields on the current step FIRST ────────────────────
            await self._fill_current_step(page, job, profile, error_context=previous_errors)
            previous_errors = []  # Reset after passing to the LLM
            await delay(800, 1500)

            # ── Click button using Playwright's native API ───────────────────
            # get_by_role() pierces shadow DOM and uses ARIA semantics.
            # Priority: Next > Continue > Review > Submit
            btn_result = "no_button"
            for btn_name, result_type in [
                ("Next", "next"),
                ("Continue", "next"),
                ("Review your application", "next"),
                ("Review", "next"),
                ("Submit application", "submitted"),
            ]:
                try:
                    btn = page.get_by_role("button", name=btn_name, exact=True)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click(timeout=3000)
                        btn_result = result_type
                        break
                except Exception:
                    continue

            # Fallback: find any visible primary button
            if btn_result == "no_button":
                try:
                    primary = page.locator(
                        "button.artdeco-button--primary:visible"
                    )
                    if await primary.count() > 0:
                        txt = (await primary.first.inner_text()).strip().lower()
                        await primary.first.click(timeout=3000)
                        btn_result = "submitted" if "submit" in txt else "next"
                except Exception:
                    pass

            console.print(f"[dim]  Button result: {btn_result}[/dim]")

            if btn_result == "submitted":
                return await self._submit_verify(page, job)

            if btn_result == "no_button":
                # Debug screenshot
                import os as _os3, datetime as _dt3
                _os3.makedirs("data/screenshots", exist_ok=True)
                _ts3 = _dt3.datetime.now().strftime("%H%M%S")
                await page.screenshot(
                    path=f"data/screenshots/{_ts3}_no_button.png",
                    full_page=False
                )
                console.print(f"[dim]  Screenshot: data/screenshots/{_ts3}_no_button.png[/dim]")
                self._mark_manual_review(
                    job["job_url"], f"no_button_found_step_{step_count}"
                )
                return False

            # btn_result == 'next' — we advanced
            await delay(1000, 2000)

            # ── Validation: see if errors prevented us from moving forward ────
            try:
                error_loc = page.locator(
                    ".artdeco-inline-feedback--error, "
                    ".fb-form-element-error-text, "
                    ".jobs-easy-apply-form-element__error, "
                    ".artdeco-inline-feedback__message"
                )
                err_count = await error_loc.count()
                if err_count > 0:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        console.print("[dim]  ⚠ Validation errors persist. Bailing out.[/dim]")
                        self._mark_manual_review(job["job_url"], "stuck_on_validation_errors")
                        return False
                        
                    err_texts = []
                    for e_idx in range(err_count):
                        try:
                            t = (await error_loc.nth(e_idx).inner_text()).strip()
                            if t: err_texts.append(t)
                        except Exception:
                            pass
                    
                    previous_errors = list(set(err_texts))
                    console.print(f"[dim]  ⚠ Validation errors detected: {previous_errors}. Retrying loop...[/dim]")
                    continue  # Loop will instantly re-fill using the previous_errors context
                else:
                    consecutive_failures = 0
            except Exception:
                pass

        # Exceeded max steps
        self._mark_manual_review(job["job_url"], "exceeded_max_steps_15")
        return False


    # ─────────────────────────────────────────────────────────────────────────
    # UNIVERSAL FORM FILLER — JavaScript-first DOM scanning
    # ─────────────────────────────────────────────────────────────────────────

    # JS injected into the page to scan ALL visible fillable elements.
    # Does NOT require a specific modal container class — tries multiple
    # selectors and falls back to scanning visible elements page-wide.
    _JS_SCAN_FIELDS = """() => {
        // Try to find the modal container with various selectors
        const modalSelectors = [
            'div.jobs-easy-apply-modal',
            'div.artdeco-modal',
            'div.artdeco-modal-overlay .artdeco-modal',
            'div[role="dialog"]',
            'div.jobs-easy-apply-content',
            'div[data-test-modal]',
        ];
        let root = null;
        for (const sel of modalSelectors) {
            root = document.querySelector(sel);
            if (root) break;
        }
        // Fall back to entire document — visibility check will filter
        if (!root) root = document.body;

        function isVisible(el) {
            if (!el.offsetParent && el.style.position !== 'fixed') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }

        function getLabel(el) {
            // 1. aria-label
            if (el.getAttribute('aria-label'))
                return el.getAttribute('aria-label');
            // 2. <label for="id">
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) return lbl.innerText.trim();
            }
            // 3. Walk up to find nearest label-like text
            let node = el.parentElement;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                const lbl = node.querySelector(
                    'label, .fb-form-element-label, ' +
                    '.artdeco-text-input--label, span.t-14, ' +
                    '.jobs-easy-apply-form-element__label'
                );
                if (lbl && lbl !== el) {
                    const t = lbl.innerText.trim();
                    if (t.length > 1 && t.length < 120) return t;
                }
                // Direct text node children
                const directText = Array.from(node.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join(' ').trim();
                if (directText.length > 2 && directText.length < 100) return directText;
            }
            // 4. placeholder
            if (el.placeholder) return el.placeholder;
            return '';
        }

        const results = [];
        let counter = 0;

        // Inputs
        root.querySelectorAll(
            'input:not([type="hidden"]):not([type="file"]):not([type="radio"])' +
            ':not([type="checkbox"]):not([type="submit"]):not([readonly]):not([disabled])'
        ).forEach(el => {
            if (!isVisible(el)) return;
            const id = 'af_' + counter++;
            el.setAttribute('data-autofill-id', id);
            results.push({
                id: id,
                label: getLabel(el),
                tagName: 'input',
                type: el.type || 'text',
                value: el.value || '',
            });
        });

        // Selects
        root.querySelectorAll('select:not([disabled])').forEach(el => {
            if (!isVisible(el)) return;
            const id = 'af_' + counter++;
            el.setAttribute('data-autofill-id', id);
            const opts = Array.from(el.options).map(o => ({
                value: o.value, text: o.text.trim()
            }));
            results.push({
                id: id,
                label: getLabel(el),
                tagName: 'select',
                type: 'select',
                value: el.value || '',
                options: opts,
            });
        });

        // Textareas
        root.querySelectorAll('textarea:not([readonly]):not([disabled])').forEach(el => {
            if (!isVisible(el)) return;
            const id = 'af_' + counter++;
            el.setAttribute('data-autofill-id', id);
            results.push({
                id: id,
                label: getLabel(el),
                tagName: 'textarea',
                type: 'textarea',
                value: el.value || '',
            });
        });

        return results;
    }"""


    async def _fill_current_step(self, page, job: dict, profile: dict, error_context: list = None):
        """
        JavaScript-first universal form filler.

        1. Injects JS to scan all form fields in the modal, tags them with
           data-autofill-id, and returns JSON metadata.
        2. Uses deterministic matching first, then LLM batch call for unknowns.
        3. Fills each field by targeting [data-autofill-id=X] — immune to stale
           handles and React re-renders.
        """
        import json as _json, re as _re

        name_parts = profile.get("name", "Raj Mishra").split()
        first_name = name_parts[0]              if name_parts      else "Raj"
        last_name  = name_parts[-1]             if len(name_parts) > 1 else "Mishra"
        city       = profile.get("city", "Bengaluru") or "Bengaluru"
        email      = config.LINKEDIN_EMAIL or profile.get("email", "")
        phone      = profile.get("phone", "")

        # ── File upload ────────────────────────────────────────────────────────
        try:
            file_loc = page.locator("input[type='file']:visible")
            if await file_loc.count() > 0:
                await file_loc.first.set_input_files(self._get_resume_path())
                await delay(1000, 2000)
        except Exception:
            pass

        # ── Radio / Yes-No — handle mechanically ─────────────────────────────
        try:
            fieldsets = page.locator("fieldset:visible")
            fc = await fieldsets.count()
            for i in range(fc):
                try:
                    fs = fieldsets.nth(i)
                    # Get the fieldset element handle for _fill_radio_group
                    fs_handle = await fs.element_handle()
                    if fs_handle:
                        await self._fill_radio_group(fs_handle, profile)
                except Exception:
                    pass
        except Exception:
            pass

        # ── STEP A: Scan fields using Playwright locators (pierces shadow DOM) ──
        _GET_LABEL_JS = """el => {
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) return lbl.innerText.trim();
            }
            let node = el.parentElement;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                const lbl = node.querySelector(
                    'label, .fb-form-element-label, ' +
                    '.artdeco-text-input--label, span.t-14, ' +
                    '.jobs-easy-apply-form-element__label'
                );
                if (lbl && lbl !== el) {
                    const t = lbl.innerText.trim();
                    if (t.length > 1 && t.length < 120) return t;
                }
            }
            if (el.placeholder) return el.placeholder;
            return '';
        }"""

        fields = []
        counter = 0

        # Inputs (visible only — Playwright's :visible pierces shadow DOM)
        input_loc = page.locator(
            "input:visible:not([type='hidden']):not([type='file'])"
            ":not([type='radio']):not([type='checkbox'])"
            ":not([type='submit']):not([readonly]):not([disabled])"
        )
        ic = await input_loc.count()
        for i in range(ic):
            try:
                el = input_loc.nth(i)
                label = await el.evaluate(_GET_LABEL_JS)
                # Skip page-level elements (search bar, language picker, etc.)
                if label.lower() in ("search", "select language", ""):
                    continue
                fid = f"af_{counter}"
                counter += 1
                await el.evaluate("(el, id) => el.setAttribute('data-autofill-id', id)", fid)
                fields.append({
                    "id": fid,
                    "label": label,
                    "tagName": "input",
                    "type": await el.get_attribute("type") or "text",
                    "value": await el.input_value(),
                })
            except Exception:
                pass

        # Selects
        sel_loc = page.locator("select:visible:not([disabled])")
        sc = await sel_loc.count()
        for i in range(sc):
            try:
                el = sel_loc.nth(i)
                label = await el.evaluate(_GET_LABEL_JS)
                if label.lower() in ("search", "select language", ""):
                    continue
                fid = f"af_{counter}"
                counter += 1
                await el.evaluate("(el, id) => el.setAttribute('data-autofill-id', id)", fid)
                opts = await el.evaluate("""el =>
                    Array.from(el.options).map(o => ({value: o.value, text: o.text.trim()}))
                """)
                fields.append({
                    "id": fid,
                    "label": label,
                    "tagName": "select",
                    "type": "select",
                    "value": await el.input_value(),
                    "options": opts,
                })
            except Exception:
                pass

        # Textareas
        ta_loc = page.locator("textarea:visible:not([readonly]):not([disabled])")
        tc = await ta_loc.count()
        for i in range(tc):
            try:
                el = ta_loc.nth(i)
                label = await el.evaluate(_GET_LABEL_JS)
                fid = f"af_{counter}"
                counter += 1
                await el.evaluate("(el, id) => el.setAttribute('data-autofill-id', id)", fid)
                fields.append({
                    "id": fid,
                    "label": label,
                    "tagName": "textarea",
                    "type": "textarea",
                    "value": await el.input_value(),
                })
            except Exception:
                pass

        if not fields:
            console.print("[dim]  No fillable fields found on this step[/dim]")
            return

        console.print(f"[dim]  Found {len(fields)} fields: "
                       f"{[f.get('label','?')[:30] for f in fields]}[/dim]")


        # ── STEP B: Determine values ──────────────────────────────────────────
        unfilled = []
        for field in fields:
            label = field.get("label", "")
            ftype = field.get("type", "text")

            # Deterministic fast-path
            value = self._match_text_field(
                label, profile,
                first_name=first_name, last_name=last_name, city=city
            )

            # Special handling for email selects
            if value is None and "email" in label.lower():
                value = email

            # Special handling for phone
            if value is None and any(w in label.lower() for w in ["phone", "mobile"]):
                value = phone

            if value is not None:
                field["_answer"] = str(value)
            else:
                unfilled.append(field)

        # ── LLM batch call for everything we couldn't match ───────────────────
        if unfilled:
            # Include 'options' so LLM knows what selects to pick
            fields_for_llm = [
                {k: f[k] for k in ("id", "label", "type", "value", "options") if k in f}
                for f in unfilled
            ]

            prompt = f"""You are filling a LinkedIn job application form.

Job: {job.get('job_title')} at {job.get('company')}

Candidate:
  Full Name   : {profile.get('name', '')}
  First Name  : {first_name}
  Last Name   : {last_name}
  Email       : {email}
  Phone       : {phone}
  City        : {city}
  Skills      : {', '.join(profile.get('skills', [])[:12])}
  Experience  : {profile.get('experience_years', 0)} years total
  Education   : {_json.dumps(profile.get('education', []))}

Fields needing values:
{_json.dumps(fields_for_llm, indent=2)}

RULES:
- "number" type → integer 0-99. Skill-specific years = 0 unless candidate has that skill. Numeric fields MUST contain numbers only!
- "select" type → you MUST pick the exact option 'value' or 'text' from the provided 'options' list. Do NOT invent options.
- "textarea" → 2-3 professional sentences.
- For Salary/CTC → Always return a numeric value like "500000" if requested in INR, or "1000000" if standard, never "As per industry".
- NEVER return blank. Always give a reasonable value.
- Return JSON object: {{"af_0": "value", "af_1": "value", ...}} using the id field as keys.
No markdown, no extra text."""

            if error_context:
                prompt += "\n\nCRITICAL FIX REQUIRED! Your previous attempt failed with these validation errors on the page:\n"
                for err in error_context:
                    prompt += f"- {err}\n"
                prompt += "\nAdjust your answers to fix these errors! For example, if it says 'Enter a whole number', evaluate the field and output an integer instead of text."

            try:
                # Use the smarter writer model for error recovery
                active_llm = self.llm_writer if error_context else self.llm
                raw = active_llm.complete(prompt)
                cleaned = raw.strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                elif cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                
                start = cleaned.find('{')
                if start != -1:
                    bc = 0
                    end = -1
                    for i in range(start, len(cleaned)):
                        if cleaned[i] == '{': bc += 1
                        elif cleaned[i] == '}':
                            bc -= 1
                            if bc == 0:
                                end = i
                                break
                    if end != -1:
                        cleaned = cleaned[start:end+1]
                
                llm_map = _json.loads(cleaned)
            except Exception as e:
                console.print(f"[dim]  LLM fill failed: {e}[/dim]")
                llm_map = {}

            for field in unfilled:
                fid = field["id"]
                val = llm_map.get(fid, "")
                if not val:
                    label_lower = field.get("label", "").lower()
                    # Absolute fallback — context-aware
                    if field["type"] == "number":
                        if any(w in label_lower for w in ["salary", "ctc", "compensation", "package", "pay"]):
                            val = "500000"
                        elif any(w in label_lower for w in ["experience", "year"]):
                            val = str(profile.get("experience_years", 1))
                        else:
                            val = "0"
                    elif field["type"] == "select" and field.get("options"):
                        # Filter out placeholder options using substring matching
                        def _is_placeholder(opt_text):
                            t = opt_text.strip().lower()
                            if not t or t == "--":
                                return True
                            placeholder_phrases = [
                                "select", "choose", "please", "option",
                                "-- ", "—"
                            ]
                            return any(p in t for p in placeholder_phrases)

                        real_opts = [
                            o for o in field["options"]
                            if not _is_placeholder(o.get("text", ""))
                            and o.get("value", "") != ""
                        ]

                        if not real_opts:
                            real_opts = [o for o in field["options"] if o.get("value", "") != ""]

                        # Smart selection based on field label
                        if real_opts:
                            if any(w in label_lower for w in ["year", "experience"]):
                                # Pick option closest to user's experience
                                exp = profile.get("experience_years", 1)
                                best = real_opts[0]
                                best_diff = 999
                                for o in real_opts:
                                    try:
                                        nums = [int(x) for x in o.get("text", "").split() if x.isdigit()]
                                        if nums and abs(nums[0] - exp) < best_diff:
                                            best_diff = abs(nums[0] - exp)
                                            best = o
                                    except Exception:
                                        pass
                                val = best.get("value", best.get("text", ""))
                            elif "month" in label_lower:
                                # Months of additional experience — default to 0 or 1
                                for o in real_opts:
                                    if o.get("text", "").strip() in ("0", "1"):
                                        val = o.get("value", o.get("text", ""))
                                        break
                                if not val:
                                    val = real_opts[0].get("value", real_opts[0].get("text", ""))
                            else:
                                val = real_opts[0].get("value", real_opts[0].get("text", ""))
                        else:
                            val = field["options"][-1].get("text", "")
                    elif field["type"] == "textarea":
                        val = (
                            f"I am excited to apply for the {job.get('job_title')} role at {job.get('company')}. "
                            f"With my experience in {', '.join(profile.get('skills', ['data analysis'])[:3])}, "
                            f"I am confident I can make a meaningful contribution to your team."
                        )
                    elif "title" in label_lower or "role" in label_lower or "designation" in label_lower:
                        val = profile.get("current_title", "Data Analyst")
                    elif "company" in label_lower or "employer" in label_lower or "organization" in label_lower:
                        val = profile.get("current_company", "Self Employed")
                    elif "city" in label_lower or "location" in label_lower:
                        val = city
                    elif "description" in label_lower:
                        val = (
                            f"Worked on data analysis and visualization projects using "
                            f"{', '.join(profile.get('skills', ['SQL', 'Python'])[:3])}."
                        )
                    else:
                        val = "N/A"
                field["_answer"] = str(val)

        # ── STEP C: Fill every field using data-autofill-id ───────────────────
        for field in fields:
            fid    = field["id"]
            answer = field.get("_answer", "")
            tag    = field.get("tagName", "input")
            ftype  = field.get("type", "text")
            label  = field.get("label", "")

            if not answer:
                continue

            selector = f"[data-autofill-id='{fid}']"

            try:
                el = await page.query_selector(selector)
                if not el:
                    continue

                if tag == "select":
                    # Try multiple strategies to select the right option
                    selected = False
                    # 1. Try by value (most reliable — our fallback returns values)
                    if not selected:
                        try:
                            await el.select_option(value=answer)
                            selected = True
                        except Exception:
                            pass
                    # 2. Try by visible text (label)
                    if not selected:
                        try:
                            await el.select_option(label=answer)
                            selected = True
                        except Exception:
                            pass
                    # 3. Try partial text match via JS
                    if not selected:
                        try:
                            await el.evaluate("""(el, answer) => {
                                const ansLower = answer.toLowerCase().trim();
                                for (const opt of el.options) {
                                    if (opt.text.toLowerCase().trim() === ansLower ||
                                        opt.value === answer) {
                                        el.value = opt.value;
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        return;
                                    }
                                }
                                // Last resort: pick first non-empty option
                                for (const opt of el.options) {
                                    if (opt.value && opt.value !== '' &&
                                        !opt.text.toLowerCase().includes('select')) {
                                        el.value = opt.value;
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        return;
                                    }
                                }
                            }""", answer)
                            selected = True
                        except Exception:
                            pass
                else:
                    # input or textarea — fill() clears + types atomically
                    await el.fill(answer)

                    # ── Autocomplete handling for location/city fields ────────
                    # LinkedIn uses a typeahead for city fields — after typing,
                    # a dropdown appears and you MUST click a suggestion.
                    label_lower = label.lower()
                    is_autocomplete = any(
                        kw in label_lower
                        for kw in ["location", "city", "hometown"]
                    )
                    if is_autocomplete:
                        await asyncio.sleep(0.8)  # wait for dropdown
                        # Try to click the first suggestion
                        suggestion_clicked = False
                        for sug_sel in [
                            "div[role='listbox'] div[role='option']",
                            ".basic-typeahead__selectable",
                            ".jobs-easy-apply-typeahead-results li",
                            "ul[role='listbox'] li",
                        ]:
                            try:
                                sug = page.locator(sug_sel).first
                                if await sug.count() > 0 and await sug.is_visible():
                                    await sug.click(timeout=2000)
                                    suggestion_clicked = True
                                    console.print(
                                        f"[dim]    ↳ Selected autocomplete suggestion[/dim]"
                                    )
                                    break
                            except Exception:
                                continue

                        if not suggestion_clicked:
                            # Fallback: press ArrowDown + Enter to accept first suggestion
                            try:
                                await el.press("ArrowDown")
                                await asyncio.sleep(0.3)
                                await el.press("Enter")
                                console.print(
                                    f"[dim]    ↳ Accepted suggestion via keyboard[/dim]"
                                )
                            except Exception:
                                pass

                await asyncio.sleep(0.2)
                console.print(f"[dim]    ✓ '{label[:40]}' → '{answer[:30]}'[/dim]")

            except Exception as e:
                console.print(f"[dim]    ✗ '{label[:40]}' failed: {e}[/dim]")



    async def _safe_fill(self, page, element, value: str):
        """Fill a form field. fill() atomically clears then sets the value."""
        try:
            await element.fill(str(value))
        except Exception:
            try:
                # Fallback: click, clear via JS, type character by character
                await element.click()
                await asyncio.sleep(0.1)
                await element.evaluate("node => { node.value = ''; }")
                await element.type(str(value), delay=40)
            except Exception:
                pass
        await delay(200, 500)

    async def _fill_experience_dropdown(self, select_el, experience_years: int):
        """Select the option closest to the user's actual experience."""
        import re
        options = await select_el.query_selector_all("option")
        best_option = None
        best_diff = float("inf")

        for option in options:
            text = (await option.inner_text()).strip()
            # Extract first number from option text e.g. "3 years" -> 3
            nums = re.findall(r"\d+", text)
            if nums:
                opt_val = int(nums[0])
                diff = abs(opt_val - experience_years)
                if diff < best_diff:
                    best_diff = diff
                    best_option = await option.get_attribute("value")

        if best_option:
            await select_el.select_option(value=best_option)
            await delay(300, 600)

    async def _fill_radio_group(self, group_el, profile: dict):
        """
        For yes/no or boolean radio groups, default to 'Yes' for
        common work authorisation questions, 'No' for sponsorship questions.
        """
        group_text = (await group_el.inner_text()).lower()

        select_yes = any(w in group_text for w in [
            "authorized", "authorised", "eligible", "right to work",
            "legally", "willing to relocate", "background check"
        ])
        select_no = any(w in group_text for w in [
            "sponsorship", "visa sponsor", "require sponsorship"
        ])

        if select_yes:
            yes_radio = await group_el.query_selector(
                "input[type='radio'][value*='Yes'], "
                "input[type='radio'][value*='yes'], "
                "label:has-text('Yes') input[type='radio']"
            )
            if yes_radio:
                await yes_radio.check()
                await delay(300, 600)

        elif select_no:
            no_radio = await group_el.query_selector(
                "input[type='radio'][value*='No'], "
                "input[type='radio'][value*='no'], "
                "label:has-text('No') input[type='radio']"
            )
            if no_radio:
                await no_radio.check()
                await delay(300, 600)

    async def _get_field_label(self, page, element) -> str:
        """
        Try to find the label text associated with a form element.
        Checks aria-label, associated <label>, and parent text.
        """
        try:
            aria = await element.get_attribute("aria-label")
            if aria:
                return aria

            el_id = await element.get_attribute("id")
            if el_id:
                label_el = await page.query_selector(f"label[for='{el_id}']")
                if label_el:
                    return (await label_el.inner_text()).strip()

            parent = await element.evaluate_handle(
                "el => el.closest('div.fb-form-element, fieldset, "
                "div.jobs-easy-apply-form-section')"
            )
            if parent:
                text = await parent.evaluate("el => el.innerText")
                return text[:120].strip()
        except Exception:
            pass
        return ""

    def _match_text_field(self, label: str, profile: dict,
                           first_name: str = "", last_name: str = "",
                           city: str = "Bengaluru") -> str | None:
        """
        Map a field label to a concrete value.
        Returns None only if the field is truly unknown — caller will use LLM.
        """
        ll = label.lower()

        # Name variants
        if any(w in ll for w in ["first name", "firstname", "given name"]):
            return first_name
        if any(w in ll for w in ["last name", "lastname", "surname", "family name"]):
            return last_name
        if "full name" in ll and "first" not in ll and "last" not in ll:
            return f"{first_name} {last_name}".strip()
        # Generic "name" — prefer full name unless contextually first-name-only
        if "name" in ll and not any(x in ll for x in ["company", "school", "college"]):
            return f"{first_name} {last_name}".strip()

        if any(w in ll for w in ["city", "location", "where", "town"]):
            return city or "Bengaluru"
        if any(w in ll for w in ["linkedin", "profile url"]):
            return ""
        if any(w in ll for w in ["github", "portfolio", "website"]):
            return ""
        if any(w in ll for w in ["salary", "ctc", "compensation", "package"]):
            return "500000"
        if any(w in ll for w in ["notice", "joining", "available", "start date"]):
            return "Immediately"
        if any(w in ll for w in ["phone", "mobile", "contact"]):
            return profile.get("phone", "")
        if any(w in ll for w in ["email", "e-mail"]):
            return profile.get("email", "")
        if any(w in ll for w in ["year", "experience", "years of exp"]):
            # For skill-specific questions ("years with Python") use 0 for fresher
            # For total experience questions use actual years
            if any(w in ll for w in ["total", "overall", "general", "work exp"]):
                return str(profile.get("experience_years", 1))
            return "0"   # specific skill — fresher level
        if any(w in ll for w in ["state", "province"]):
            return "Karnataka"
        if any(w in ll for w in ["country"]):
            return "India"
        if any(w in ll for w in ["zip", "postal", "pin code", "pincode"]):
            return "560001"
        if any(w in ll for w in ["degree", "qualification", "education"]):
            return profile.get("education", "Bachelor of Technology")
        if any(w in ll for w in ["college", "university", "institution", "school"]):
            return profile.get("college", "")
        if any(w in ll for w in ["major", "specialization", "branch", "stream"]):
            return profile.get("major", "Computer Science")

        return None  # truly unknown — caller will use LLM

    async def _llm_fill_field(self, label: str, job: dict,
                               profile: dict, existing_value: str = "") -> str:
        """
        Ask the LLM to determine an appropriate answer for an unknown form field.
        Always returns a non-empty string — uses safe defaults if LLM fails.
        """
        prompt = f"""You are helping fill out a LinkedIn job application form.

Job being applied to:
  Title: {job.get('job_title', 'Unknown')}
  Company: {job.get('company', 'Unknown')}

Candidate profile summary:
  Name: {profile.get('name', '')}
  Skills: {', '.join(profile.get('skills', [])[:10])}
  Experience: {profile.get('experience_years', 0)} years
  Education: {profile.get('education', '')}
  City: {profile.get('city', 'Bengaluru')}
  Current value in field: "{existing_value}"

The form field label is: "{label}"

Instructions:
- If the field is asking for personal info in the profile (name, email, phone, location etc.), use the profile data.
- If it's a yes/no or numeric question, give the most appropriate answer for a fresher/intern.
- If it's a legal/work auth question in India, answer truthfully (authorized=Yes, sponsorship=No).
- Keep the answer concise and form-appropriate (no long paragraphs unless it's a text area).
- NEVER leave blank. Always provide an appropriate value.
- Return ONLY the field value, nothing else."""

        try:
            return self.llm.complete(prompt).strip()
        except Exception:
            # Safe generic defaults by label
            ll = label.lower()
            if "experience" in ll or "year" in ll:
                return "0"
            if "salary" in ll or "ctc" in ll:
                return "500000"
            if "notice" in ll or "joining" in ll:
                return "Immediately"
            if "city" in ll or "location" in ll:
                return profile.get("city", "Bengaluru")
            return "Yes"

    async def _generate_cover_letter(self, job: dict, profile: dict) -> str:
        """Generate a short 3-sentence cover letter via LLM."""
        prompt = f"""Write a concise 3-sentence cover letter for the following job application.
Use a professional but enthusiastic tone. Do not use placeholder text.
Address the letter to the hiring team.

Candidate name: {profile.get('name', 'the candidate')}
Candidate skills: {', '.join(profile.get('skills', [])[:8])}
Years of experience: {profile.get('experience_years', 0)}

Job title: {job['job_title']}
Company: {job['company']}

Return only the cover letter text. No subject line. No header. No signature."""

        try:
            return self.llm_writer.complete(prompt).strip()
        except Exception:
            return (
                f"I am excited to apply for the {job['job_title']} role at {job['company']}. "
                f"My background in {', '.join(profile.get('skills', [])[:3])} "
                f"makes me a strong fit for this position. "
                f"I look forward to discussing how I can contribute to your team."
            )

    async def _ask_user_for_field(self, label: str, job: dict) -> str:
        """
        Print a CLI prompt for an unknown field.
        Waits up to 5 minutes for user input.
        Times out and returns empty string — job continues, field left blank.
        """
        console.print(
            f"\n[yellow bold][MANUAL INPUT NEEDED][/yellow bold]\n"
            f"Job: [cyan]{job['job_title']} @ {job['company']}[/cyan]\n"
            f"Field: [white]{label}[/white]\n"
            f"Type a value and press Enter (or press Enter to skip, "
            f"times out in 5 minutes):"
        )
        try:
            value = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, input, "> "),
                timeout=300
            )
            return value.strip()
        except asyncio.TimeoutError:
            if self.logger:
                self.logger.warn(
                    "applier",
                    f"User input timeout for field '{label}' — leaving blank"
                )
            return ""

    async def _submit_verify(self, page, job: dict) -> bool:
        """Verify submission after JS already clicked Submit."""
        console.print("[dim]  Verifying submission...[/dim]")
        await delay(3000, 5000)

        # Look for confirmation signal
        try:
            confirmation = await page.wait_for_selector(
                "h3:has-text('Application submitted'), "
                "div:has-text('Your application was sent'), "
                "div.artdeco-inline-feedback--success",
                timeout=6000
            )
            if confirmation:
                return True
        except Exception:
            pass

        # Secondary — modal might have closed
        modal_still_open = await page.query_selector(
            "div.jobs-easy-apply-modal, div.artdeco-modal"
        )
        if not modal_still_open:
            return True

        self._mark_manual_review(job["job_url"], "submit_confirmation_not_found")
        return False

    async def _submit(self, page, job: dict) -> bool:
        """Click the Submit button and confirm success."""
        submit_btn = await page.query_selector(
            "button[aria-label='Submit application'], "
            "button[data-easy-apply-next-button-source='submitApplication']"
        )
        if not submit_btn:
            self._mark_manual_review(job["job_url"], "submit_button_disappeared")
            return False

        await delay(1500, 3000)
        await submit_btn.click()
        await delay(3000, 5000)

        # Look for confirmation signal
        try:
            confirmation = await page.wait_for_selector(
                "h3:has-text('Application submitted'), "
                "div:has-text('Your application was sent'), "
                "div.artdeco-inline-feedback--success",
                timeout=6000
            )
            if confirmation:
                return True
        except Exception:
            pass

        # Secondary check — modal should be gone or show a success state
        modal_still_open = await page.query_selector(
            "div.jobs-easy-apply-modal"
        )
        if not modal_still_open:
            # Modal closed — treat as success
            return True

        self._mark_manual_review(job["job_url"], "submit_confirmation_not_found")
        return False


    async def _is_blocked(self, page) -> bool:
        """Check for CAPTCHA or checkpoint pages."""
        url = page.url
        if "captcha" in url.lower() or "checkpoint" in url:
            return True
        captcha_frame = await page.query_selector(
            "iframe[src*='challenge'], iframe[src*='captcha']"
        )
        return captcha_frame is not None

    async def _has_external_redirect(self, page) -> bool:
        """
        Check if the modal contains a link that redirects to an external site.
        LinkedIn sometimes embeds "Apply on company website" inside the Easy Apply flow.
        """
        external_link = await page.query_selector(
            "a[href*='apply']:not([href*='linkedin.com']), "
            "button[aria-label*='company website']"
        )
        return external_link is not None

    def _get_resume_path(self) -> str:
        import os
        return os.path.abspath(config.RESUME_PATH)

    def _fetch_scored_jobs(self) -> list:
        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute("""
            SELECT id, job_title, company, location, job_url, description, score
            FROM jobs
            WHERE status = 'scored'
            ORDER BY score DESC
        """).fetchall()
        conn.close()
        return [
            {
                "id": r[0], "job_title": r[1], "company": r[2],
                "location": r[3], "job_url": r[4],
                "description": r[5], "score": r[6]
            }
            for r in rows
        ]

    def _mark_applied(self, job_url: str):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            UPDATE jobs
            SET status = 'applied', applied_at = ?
            WHERE job_url = ?
        """, (datetime.now().isoformat(), job_url))
        conn.commit()
        conn.close()

    def _mark_manual_review(self, job_url: str, reason: str):
        if self.logger:
            self.logger.warn("applier", f"Manual review: {reason} — {job_url}")
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            UPDATE jobs
            SET status = 'manual_review', fail_reason = ?
            WHERE job_url = ?
        """, (reason, job_url))
        conn.commit()
        conn.close()


# LangGraph node function
async def applier_node(state: AgentState) -> dict:
    from browser.browser_manager import BrowserManager
    from utils.logger import Logger

    logger = Logger(config.DB_PATH, state["run_id"])
    bm = BrowserManager()

    try:
        await bm.start()
        await bm.login(logger)

        agent = ApplierAgent(bm, logger)
        result = await agent.apply_all(profile=state["user_profile"])

        return {
            "applied_count": result["applied"],
            "manual_review_count": result["manual_review"],
            "current_phase": "applied"
        }

    except Exception as e:
        logger.error("applier", f"Applier node failed: {e}")
        return {"error": str(e)}

    finally:
        try:
            await bm.stop()
        except Exception:
            pass  # Browser already disconnected — safe to ignore
