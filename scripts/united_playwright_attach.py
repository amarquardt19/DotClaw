#!/usr/bin/env python3
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_CDP_URL = "http://127.0.0.1:18801"
DEFAULT_URL = "https://www.united.com/en/us"


def log(message):
    print(f"[united-attach] {message}", flush=True)


def safe_page_info(page):
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    return {"url": url, "title": title}


def united_tab_score(page):
    info = safe_page_info(page)
    url = info["url"].lower()
    title = info["title"].lower()
    hay = f"{title} {url}"
    if "united.com" in url and "book" in hay:
        return 120
    if "united.com/en/us/fsr" in url:
        return 110
    if "united.com" in url:
        return 90
    if "united" in hay:
        return 20
    return -1


def pick_united_page(browser):
    pages = []
    for context in browser.contexts:
        pages.extend(context.pages)
    if not pages:
        return None
    live_pages = []
    for page in pages:
        try:
            info = safe_page_info(page)
            if not info["url"] and not info["title"]:
                continue
            live_pages.append(page)
        except Exception:
            continue
    if not live_pages:
        return None
    return max(live_pages, key=united_tab_score)


def visible_state(page, label):
    state = page.evaluate(
        """
({ label }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const inputs = [...document.querySelectorAll('input, textarea')]
    .filter(visible)
    .map((el) => ({
      tag: el.tagName,
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      name: el.getAttribute('name') || '',
      testId: el.getAttribute('data-testid') || '',
      value: el.value || ''
    }));
  const buttons = [...document.querySelectorAll('button, [role="button"], a')]
    .filter(visible)
    .map((el) => ({
      text: clean(el.innerText || el.textContent || ''),
      ariaLabel: el.getAttribute('aria-label') || '',
      testId: el.getAttribute('data-testid') || ''
    }))
    .filter((row) => row.text || row.ariaLabel || row.testId);
  const body = clean(document.body ? (document.body.innerText || document.body.textContent || '') : '');
  return {
    label,
    title: document.title,
    url: location.href,
    inputs: inputs.slice(0, 20),
    buttons: buttons.slice(0, 20),
    bodyPreview: body.slice(0, 1200)
  };
}
""",
        {"label": label},
    )
    log(json.dumps(state, indent=2))
    return state


def pause(page, seconds, label):
    log(f"pause {seconds:.1f}s - {label}")
    page.wait_for_timeout(int(seconds * 1000))


def dismiss_common_popups(page):
    selectors = [
        'button:has-text("Accept")',
        'button:has-text("I agree")',
        'button:has-text("Got it")',
        'button:has-text("No thanks")',
        'button[aria-label="Close"]',
        '[role="button"][aria-label="Close"]',
    ]
    clicked = []
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=800)
            text = (locator.inner_text(timeout=500) or "").strip()
            locator.click(timeout=1200)
            clicked.append({"selector": selector, "text": text})
            page.wait_for_timeout(150)
        except Exception:
            continue
    return clicked


def dismiss_booking_overlay(page):
    clicked = click_first(
        page,
        [
            'button[aria-label="Close"]',
            '[role="button"][aria-label="Close"]',
            'button:has-text("Close")',
            '[role="button"]:has-text("Close")',
        ],
        "dismiss-booking-overlay",
        timeout_ms=1000,
    )
    if clicked:
        page.wait_for_timeout(250)
    return clicked


def accept_cookies(page):
    clicked = click_first(
        page,
        [
            'button:has-text("Accept cookies")',
            '[role="button"]:has-text("Accept cookies")',
            'button:has-text("Accept")',
            '[role="button"]:has-text("Accept")',
        ],
        "accept-cookies",
        timeout_ms=1200,
    )
    if clicked:
        page.wait_for_timeout(250)
    return clicked


def click_first(page, selectors, label, timeout_ms=1500):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=timeout_ms)
            text = (locator.inner_text(timeout=500) or "").strip()
            log(f"{label}: clicking selector={selector!r} text={text!r}")
            locator.click(timeout=timeout_ms)
            return {"selector": selector, "text": text}
        except Exception:
            continue
    return None


def open_booking_form(page):
    clicked = click_first(
        page,
        [
            'button:has-text("Book")',
            '[role="button"]:has-text("Book")',
            'a:has-text("Book")',
        ],
        "open-booking-form",
        timeout_ms=1200,
    )
    if clicked:
        page.wait_for_timeout(300)
    return clicked


def booking_form_visible(page):
    selectors = [
        'input[aria-label*="Origin"]',
        'input[placeholder*="Origin"]',
        'input[aria-label*="From"]',
        'input[placeholder*="From"]',
        'button:has-text("Find flights")',
        '[role="button"]:has-text("Find flights")',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=400)
            return True
        except Exception:
            continue
    return False


def fill_field(page, selectors, value, label):
    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=2000)
            locator.click(timeout=1500)
            page.wait_for_timeout(100)
            try:
                locator.fill("")
                locator.fill(value)
            except Exception:
                locator.press("Meta+A")
                locator.press("Backspace")
                locator.type(value, delay=25)
            log(f"{label}: filled selector={selector!r} value={value!r}")
            return {"selector": selector, "value": value}
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError(f"{label}: no matching field")


def fill_airport_field(page, selectors, value, label):
    last_error = None
    airport_aliases = {
        "RIC": ["richmond", "richmond ric"],
        "CDG": ["paris", "paris fr", "charles de gaulle"],
    }
    target_tokens = [value.lower(), *airport_aliases.get(value.upper(), [])]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=2000)
            locator.click(timeout=1500)
            page.wait_for_timeout(80)
            try:
                locator.fill("")
                locator.type(value, delay=20)
            except Exception:
                locator.press("Meta+A")
                locator.press("Backspace")
                locator.type(value, delay=20)
            picked = maybe_pick_suggestion(page, label, target_tokens, timeout_ms=2200)
            if not picked:
                page.wait_for_timeout(180)
                locator.press("ArrowDown")
                page.wait_for_timeout(120)
                locator.press("Enter")
                picked = {"mode": "keyboard-autocomplete"}
            page.wait_for_timeout(180)
            log(f"{label}: typed selector={selector!r} value={value!r} mode={picked.get('mode')!r}")
            return {"selector": selector, "value": value, **picked}
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError(f"{label}: no matching field")


def maybe_pick_suggestion(page, label, target_tokens, timeout_ms=3500):
    end = time.time() + timeout_ms / 1000
    target_tokens = [t.lower() for t in target_tokens if t]
    airport_codes = [token for token in target_tokens if re.fullmatch(r"[a-z]{3}", token)]
    banned_tokens = ["use our map", "not sure", "explore", "interactive map"]
    while time.time() < end:
        try:
            rows = page.evaluate(
                """
({ targetTokens, bannedTokens }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll('[role="option"], li, [data-testid*="option"], [class*="suggest"], [class*="autoComplete"] li, [class*="autocomplete"] li, button, [role="button"], div')]
    .filter(visible)
    .map((el, idx) => {
      const text = clean(el.innerText || el.textContent || '');
      const hay = text.toLowerCase();
      const codeMatch = hay.match(/\(([a-z]{3})\)/i);
      const airportCode = codeMatch ? codeMatch[1].toLowerCase() : '';
      const score = targetTokens.filter((token) => hay.includes(token)).length;
      const banned = bannedTokens.some((token) => hay.includes(token));
      return {
        idx,
        text,
        score,
        banned,
        airportCode,
        role: el.getAttribute('role') || '',
        top: el.getBoundingClientRect().top,
        selector: el.tagName.toLowerCase(),
      };
    })
    .filter((row) => row.text && !row.banned)
    .sort((a, b) => {
      const aCodeBoost = a.airportCode && targetTokens.includes(a.airportCode) ? 1 : 0;
      const bCodeBoost = b.airportCode && targetTokens.includes(b.airportCode) ? 1 : 0;
      return (bCodeBoost - aCodeBoost) || (b.score - a.score) || (a.top - b.top);
    })
    .slice(0, 10);
}
""",
                {"targetTokens": target_tokens, "bannedTokens": banned_tokens},
            )
        except Exception:
            rows = []
        if rows:
            top_row = rows[0]
            top_text = (top_row.get("text") or "").strip()
            top_code = (top_row.get("airportCode") or "").strip().lower()
            if top_row.get("score", 0) >= 1 and (not airport_codes or top_code in airport_codes):
                try:
                    clicked = page.evaluate(
                        """
({ targetText }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const candidates = [...document.querySelectorAll('[role="option"], li, [data-testid*="option"], [class*="suggest"], [class*="autoComplete"] li, [class*="autocomplete"] li')]
    .filter(visible);
  const match = candidates.find((el) => clean(el.innerText || el.textContent || '') === targetText)
    || candidates.find((el) => clean(el.innerText || el.textContent || '').startsWith(targetText));
  if (!match) {
    return false;
  }
  match.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, cancelable: true, composed: true }));
  match.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, composed: true }));
  match.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, composed: true }));
  match.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
  return true;
}
""",
                        {"targetText": top_text},
                    )
                    if clicked:
                        log(f"{label}: selected suggestion text={top_text!r} mode='dom-top-row'")
                        return {"text": top_text, "mode": "dom-top-row"}
                except Exception:
                    pass
        for row in rows:
            if row.get("score", 0) < 1:
                continue
            text = row.get("text", "")
            try:
                locator = page.get_by_text(text[:120], exact=False).first
                locator.click(timeout=1200)
                log(f"{label}: selected suggestion text={text!r}")
                return {"text": text, "mode": "text-match"}
            except Exception:
                continue
        page.wait_for_timeout(100)
    return None


def ensure_roundtrip(page):
    wrapper_selectors = [
        'li.atm-c-radio-field__item:has(label.atm-c-radio-field__item-label:has-text("Roundtrip"))',
        'li.atm-c-radio-field__item:has(label:has-text("Roundtrip"))',
    ]
    for selector in wrapper_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=1200, force=True)
            page.wait_for_timeout(150)
            checked = page.evaluate(
                """
(el) => {
  const radio = el?.querySelector?.('input[type="radio"]');
  return !!radio?.checked;
}
""",
                locator.element_handle(timeout=1200),
            )
            if checked:
                return {"mode": "wrapper-roundtrip", "selector": selector, "checked": checked}
        except Exception:
            continue
    result = page.evaluate(
        """
(targetLabel) => {
  const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const clickTarget = (target) => {
    if (!target) return;
    try {
      target.scrollIntoView({ block: 'center', inline: 'center' });
    } catch (error) {}
    try {
      target.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
    } catch (error) {}
    try {
      if (typeof target.click === 'function') {
        target.click();
      }
    } catch (error) {}
  };
  const tabCandidates = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], a, li, div')).filter((node) => {
    const text = normalize(node.textContent || '');
    return visible(node) && (text === 'one way' || text === 'one-way');
  });
  if (tabCandidates.length) {
    const target = tabCandidates[0];
    clickTarget(target);
    return {
      ok: true,
      checked: true,
      activeValue: normalize(target.textContent || ''),
      text: normalize(target.textContent || ''),
      bubbleTag: target.tagName || null,
      mode: 'tab-oneway',
    };
  }
  const labels = Array.from(document.querySelectorAll('label.atm-c-radio-field__item-label, label'));
  const labelNode = labels.find((node) => normalize(node.textContent) === targetLabel);
  if (!labelNode) {
    return { ok: false, reason: 'label-not-found' };
  }
  const container = labelNode.parentElement;
  const bubble = labelNode.previousElementSibling || container?.querySelector('span, button, div, i');
  const radio = container?.querySelector('input[type="radio"]')
    || labelNode.control
    || document.querySelector('input[type="radio"][value*="round" i], input[type="radio"][aria-label*="Roundtrip" i]');
  const targets = [bubble, labelNode, container, radio].filter(Boolean);
  if (!targets.length) {
    return { ok: false, reason: 'radio-not-found' };
  }
  for (const target of targets) {
    clickTarget(target);
  }
  if (radio && !radio.checked) {
    radio.checked = true;
    radio.dispatchEvent(new Event('input', { bubbles: true }));
    radio.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const checkedRadio = Array.from(document.querySelectorAll('input[type="radio"]')).find((node) => node.checked);
  return {
    ok: true,
    checked: !!radio?.checked,
    activeValue: checkedRadio?.value || checkedRadio?.getAttribute('aria-label') || null,
    text: normalize(labelNode.textContent),
    bubbleTag: bubble?.tagName || null,
  };
}
""",
        "roundtrip",
    )
    if result and result.get("ok") and result.get("checked"):
        page.wait_for_timeout(150)
        return {"mode": "dom-roundtrip", **result}
    raise RuntimeError(f"trip-type-roundtrip: unable to select Roundtrip ({result})")


def ensure_oneway(page):
    wrapper_selectors = [
        'li.atm-c-radio-field__item:has(label.atm-c-radio-field__item-label:has-text("One-way"))',
        'li.atm-c-radio-field__item:has(label:has-text("One-way"))',
    ]
    for selector in wrapper_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=1200, force=True)
            page.wait_for_timeout(150)
            checked = page.evaluate(
                """
(el) => {
  const radio = el?.querySelector?.('input[type="radio"]');
  return !!radio?.checked;
}
""",
                locator.element_handle(timeout=1200),
            )
            if checked:
                return {"mode": "wrapper-oneway", "selector": selector, "checked": checked}
        except Exception:
            continue
    result = page.evaluate(
        """
(targetLabel) => {
  const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const clickTarget = (target) => {
    if (!target) return;
    try {
      target.scrollIntoView({ block: 'center', inline: 'center' });
    } catch (error) {}
    try {
      target.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, composed: true }));
      target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
    } catch (error) {}
    try {
      if (typeof target.click === 'function') {
        target.click();
      }
    } catch (error) {}
  };
  const tabCandidates = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], a, li, div')).filter((node) => {
    const text = normalize(node.textContent || '');
    return visible(node) && (text === 'one way' || text === 'one-way');
  });
  if (tabCandidates.length) {
    const target = tabCandidates[0];
    clickTarget(target);
    return {
      ok: true,
      checked: true,
      activeValue: normalize(target.textContent || ''),
      text: normalize(target.textContent || ''),
      bubbleTag: target.tagName || null,
      mode: 'tab-oneway',
    };
  }
  const selects = Array.from(document.querySelectorAll('select')).filter(visible);
  for (const select of selects) {
    const options = Array.from(select.options || []);
    const option = options.find((node) => {
      const text = normalize(node.textContent || node.label || '');
      const value = normalize(node.value || '');
      return text === 'one way' || text === 'one-way' || value === 'one way' || value === 'one-way' || value === 'oneway';
    });
    if (!option) {
      continue;
    }
    select.value = option.value;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
    return {
      ok: true,
      checked: true,
      activeValue: normalize(option.value || option.textContent || ''),
      text: normalize(option.textContent || ''),
      bubbleTag: select.tagName || null,
      mode: 'select-oneway',
    };
  }
  const labels = Array.from(document.querySelectorAll('label.atm-c-radio-field__item-label, label'));
  const labelNode = labels.find((node) => normalize(node.textContent) === targetLabel);
  if (!labelNode) {
    return { ok: false, reason: 'label-not-found' };
  }
  const container = labelNode.parentElement;
  const bubble = labelNode.previousElementSibling || container?.querySelector('span, button, div, i');
  const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
  const radio = container?.querySelector('input[type="radio"]')
    || labelNode.control
    || radios.find((node) => /one/i.test(node.value || '') || /one-way/i.test(node.getAttribute('aria-label') || ''));
  const targets = [bubble, labelNode, container, radio].filter(Boolean);
  if (!targets.length) {
    return { ok: false, reason: 'radio-not-found' };
  }
  for (const target of targets) {
    clickTarget(target);
  }
  if (radio && !radio.checked) {
    radio.checked = true;
    radio.dispatchEvent(new Event('input', { bubbles: true }));
    radio.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const checkedRadio = radios.find((node) => node.checked);
  return {
    ok: true,
    checked: !!radio?.checked,
    activeValue: checkedRadio?.value || checkedRadio?.getAttribute('aria-label') || null,
    text: normalize(labelNode.textContent),
    bubbleTag: bubble?.tagName || null,
  };
}
""",
        "one-way",
    )
    if result and result.get("ok") and result.get("checked"):
        page.wait_for_timeout(150)
        return {"mode": "dom-oneway", **result}
    raise RuntimeError(f"trip-type-oneway: unable to select One-way ({result})")


def synthetic_click(page, selectors, label, timeout_ms=1500):
    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=timeout_ms)
            handle = locator.element_handle(timeout=timeout_ms)
            if handle is None:
                continue
            page.evaluate(
                """
(element) => {
  element.scrollIntoView({ block: 'center', inline: 'center' });
  const rect = element.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const target = document.elementFromPoint(x, y) || element;
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    target.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: x,
      clientY: y,
    }));
  }
  if (typeof element.focus === 'function') {
    element.focus();
  }
}
""",
                handle,
            )
            log(f"{label}: synthetic click selector={selector!r}")
            return {"selector": selector, "mode": "synthetic-click"}
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError(f"{label}: no matching field")


def dismiss_cart_panel(page):
    try:
        panel = page.locator('section:has-text("Your cart is empty"), aside:has-text("Your cart is empty"), div:has-text("Your cart is empty")').first
        if panel.count() > 0:
            panel.wait_for(state="visible", timeout=800)
            close_button = panel.locator('button[aria-label="Close"], [role="button"][aria-label="Close"], button[aria-label*="close" i], [role="button"][aria-label*="close" i]').first
            if close_button.count() > 0:
                close_button.click(timeout=1000)
                page.wait_for_timeout(250)
                return {"mode": "drawer-close-button"}
            page.evaluate(
                """
() => {
  const heading = Array.from(document.querySelectorAll('*')).find((node) =>
    (node.textContent || '').trim() === 'Your cart is empty'
  );
  if (!heading) {
    return false;
  }
  const drawer = heading.closest('section, aside, div');
  if (!drawer) {
    return false;
  }
  const candidate = drawer.querySelector('button[aria-label="Close"], [role="button"][aria-label="Close"], button[aria-label*="close" i], [role="button"][aria-label*="close" i]');
  if (!candidate) {
    return false;
  }
  candidate.click();
  return true;
}
"""
            )
            page.wait_for_timeout(250)
            return {"mode": "drawer-dom-click"}
    except Exception:
        pass
    clicked = click_first(
        page,
        [
            'section:has-text("Your cart is empty") button[aria-label="Close"]',
            'aside:has-text("Your cart is empty") button[aria-label="Close"]',
            'div:has-text("Your cart is empty") button[aria-label="Close"]',
        ],
        "dismiss-cart-panel",
        timeout_ms=1200,
    )
    if clicked:
        page.wait_for_timeout(250)
    return clicked


def open_date_picker(page):
    return click_first(
        page,
        [
            'button:has-text("Dates")',
            '[role="button"]:has-text("Dates")',
            'button:has-text("Date")',
            '[role="button"]:has-text("Date")',
            'input[aria-label*="Depart"]',
            'input[placeholder*="Depart"]',
            'input[aria-label*="Date"]',
            'input[placeholder*="Date"]',
            'button[aria-label*="Depart"]',
            'button[aria-label*="Date"]',
            '[role="button"]:has-text("Depart")',
            '[data-testid*="date"]',
            '[class*="date"] button',
        ],
        "open-date-picker",
        timeout_ms=2000,
    )


def _focusable_date_locator(page):
    selectors = [
        'input[placeholder*="Dates"]',
        'input[aria-label*="Dates"]',
        'input[aria-label*="Date"]',
        'input[placeholder*="Date"]',
        'input[aria-label*="Depart"]',
        'input[placeholder*="Depart"]',
        'input[value*="/"]',
        'button[aria-label*="Date"]',
        'button[aria-label*="Depart"]',
        '[role="button"]:has-text("Dates")',
        '[role="button"]:has-text("Date")',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=1200)
            return locator, selector
        except Exception:
            continue
    raise RuntimeError("date-field: no matching input")


def type_date_field(page, target_date, label):
    value = target_date.strftime("%m/%d")
    try:
        synthetic_click(
            page,
            [
                'button:has-text("Dates")',
                '[role="button"]:has-text("Dates")',
                'button:has-text("Date")',
                '[role="button"]:has-text("Date")',
                'input[placeholder*="Dates"]',
                'input[aria-label*="Dates"]',
                'input[aria-label*="Date"]',
                'input[placeholder*="Date"]',
                'input[aria-label*="Depart"]',
                'input[placeholder*="Depart"]',
                'label:has-text("Date")',
                'label:has-text("Dates")',
            ],
            f"{label}-focus",
            timeout_ms=2000,
        )
    except Exception:
        open_date_picker(page)
    page.wait_for_timeout(150)
    locator, selector = _focusable_date_locator(page)
    locator.click(force=True)
    try:
        locator.fill("")
        locator.type(value, delay=40)
    except Exception:
        locator.press("Meta+A")
        locator.press("Backspace")
        locator.type(value, delay=40)
    log(f"{label}: typed date value={value!r} selector={selector!r}")
    return {"value": value, "mode": "typed-date", "selector": selector}


def click_calendar_day(page, target_date):
    labels = [
        target_date.strftime("%A, %B %-d, %Y"),
        target_date.strftime("%A, %B %d, %Y").replace(" 0", " "),
        target_date.strftime("%B %-d, %Y"),
        target_date.strftime("%B %d, %Y").replace(" 0", " "),
        target_date.strftime("%b %-d, %Y"),
        target_date.strftime("%b %d, %Y").replace(" 0", " "),
    ]
    for _ in range(12):
        for label in labels:
            selectors = [
                f'button[aria-label*="{label}"]',
                f'[role="button"][aria-label*="{label}"]',
                f'td[aria-label*="{label}"]',
            ]
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    locator.wait_for(state="visible", timeout=600)
                    locator.click(timeout=1000)
                    log(f"calendar: clicked {label!r} via {selector!r}")
                    return {"label": label, "selector": selector}
                except Exception:
                    continue
        next_click = click_first(
            page,
            [
                'button[aria-label*="Next month"]',
                'button[aria-label*="Next Month"]',
                '[role="button"][aria-label*="Next month"]',
                'button:has-text("Next")',
            ],
            "calendar-next-month",
            timeout_ms=800,
        )
        if not next_click:
            break
        page.wait_for_timeout(200)
    raise RuntimeError(f"Could not click calendar date {target_date.date().isoformat()}")


def confirm_dates(page):
    clicked = click_first(
        page,
        [
            'button:has-text("Done")',
            'button:has-text("Apply")',
            '[role="button"]:has-text("Done")',
            '[role="button"]:has-text("Apply")',
        ],
        "confirm-dates",
        timeout_ms=1000,
    )
    if clicked:
        page.wait_for_timeout(200)
    return clicked


def search_flights(page):
    clicked = click_first(
        page,
        [
            'button[class*="BookFlightForm-bookFlightForm__findFlightBtn"]',
            'button.atm-c-btn[class*="BookFlightForm"]',
            'button[class*="findFlightBtn"]',
            'button:has-text("Search")',
            'button:has-text("Find flights")',
            '[role="button"]:has-text("Search")',
            '[role="button"]:has-text("Find flights")',
        ],
        "search-flights",
        timeout_ms=2000,
    )
    if clicked:
        return clicked
    try:
        page.keyboard.press("Enter")
        log("search-flights: pressed Enter")
        return {"mode": "keyboard-enter"}
    except Exception:
        return None


def _parse_duration_minutes(text):
    value = (text or "").strip().lower()
    hours_match = re.search(r"(\d+)\s*h", value)
    minutes_match = re.search(r"(\d+)\s*m", value)
    if not hours_match and not minutes_match:
        return None
    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0
    return hours * 60 + minutes


def _parse_clock_minutes(text):
    value = (text or "").strip().upper()
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2))
    if match.group(3) == "PM":
        hour += 12
    return hour * 60 + minute


def choose_matching_result(page, selected_summary):
    cards = page.evaluate(
        """
() => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 40 && rect.height > 40;
  };
  const fareHeaders = Array.from(document.querySelectorAll('div[id^="priceCardContainer"], div[class*="priceCardContainer"], div[class*="priceCard"]')).filter(visible);
  const resultRows = Array.from(document.querySelectorAll('div')).filter((el) => {
    if (!visible(el)) return false;
    const text = clean(el.innerText || el.textContent || '');
    return /arrives/i.test(text) && /(CDG|RIC|stop)/i.test(text) && /(\d{1,2}:\d{2}\s*[AP]M)/i.test(text);
  });
  return resultRows.slice(0, 12).map((row, idx) => {
    const text = clean(row.innerText || row.textContent || '');
    const times = text.match(/\d{1,2}:\d{2}\s*[AP]M/g) || [];
    const durationMatch = text.match(/\d+H,?\s*\d+M|\d+H|\d+M/i);
    const stopMatch = text.match(/NONSTOP|\d+\s*STOP[S]?/i);
    const fareColumn = fareHeaders[idx] || fareHeaders[0] || null;
    const fareText = clean(fareColumn ? (fareColumn.innerText || fareColumn.textContent || '') : '');
    return {
      index: idx,
      text,
      departTime: times[0] || '',
      arriveTime: times[1] || '',
      duration: durationMatch ? durationMatch[0] : '',
      stops: stopMatch ? stopMatch[0] : '',
      fareText,
      fareType: fareText.split(' From')[0] || fareText.split(' | ')[0] || '',
    };
  });
}
"""
    )
    if not cards:
        return None
    valid_cards = []
    durations = []
    target_duration = _parse_duration_minutes(selected_summary.get("duration"))
    target_depart = _parse_clock_minutes(selected_summary.get("time_summary", "").split("→")[0].strip()) if selected_summary.get("time_summary") else None
    for card in cards:
        duration_minutes = _parse_duration_minutes(card.get("duration"))
        if duration_minutes is not None:
            durations.append(duration_minutes)
        valid_cards.append({**card, "durationMinutes": duration_minutes})
    if durations:
        max_duration = min(durations) + 360
        valid_cards = [card for card in valid_cards if card.get("durationMinutes") is None or card.get("durationMinutes") <= max_duration]
    if not valid_cards:
        return None

    fallback_depart = None if target_depart is not None else _parse_clock_minutes("6:30 PM")

    def score(card):
        fare_type = (card.get("fareType") or "").lower()
        penalty = 0
        if "economy" not in fare_type and "main cabin" not in fare_type:
            penalty += 500
        if "plus" in fare_type or "premium" in fare_type or "polaris" in fare_type or "business" in fare_type:
            penalty += 250
        duration_minutes = card.get("durationMinutes")
        if duration_minutes is not None:
            penalty += duration_minutes
            if target_duration is not None:
                penalty += abs(duration_minutes - target_duration) * 2
        depart_minutes = _parse_clock_minutes(card.get("departTime"))
        if depart_minutes is not None and target_depart is not None:
            penalty += abs(depart_minutes - target_depart)
        elif depart_minutes is not None and fallback_depart is not None:
            penalty += abs(depart_minutes - fallback_depart) * 3
        stops_text = (card.get("stops") or "").lower()
        if "nonstop" in stops_text:
            penalty += 20
        elif "1 stop" in stops_text:
            penalty += 10
        return penalty

    best = min(valid_cards, key=score)
    clicked = page.evaluate(
        """
({ targetText, targetFareText }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 40 && rect.height > 40;
  };
  const row = Array.from(document.querySelectorAll('div')).find((el) => visible(el) && clean(el.innerText || el.textContent || '') === targetText);
  if (!row) {
    return false;
  }
  const rowRect = row.getBoundingClientRect();
  const buttons = Array.from(document.querySelectorAll('button, [role="button"], div[id^="priceCardContainer"], div[class*="priceCard"], div[role="button"]')).filter(visible);
  const candidates = buttons
    .map((el) => {
      const text = clean(el.innerText || el.textContent || '');
      const rect = el.getBoundingClientRect();
      const verticalDistance = Math.abs((rect.top + rect.height / 2) - (rowRect.top + rowRect.height / 2));
      const horizontalDistance = rect.left >= rowRect.right - 40 ? (rect.left - rowRect.right) : 9999;
      let penalty = verticalDistance + horizontalDistance;
      const lower = text.toLowerCase();
      if (targetFareText && text === targetFareText) penalty -= 400;
      if (targetFareText && text.includes(targetFareText.slice(0, 16))) penalty -= 150;
      if (lower.includes('economy')) penalty -= 250;
      if (lower.includes('main cabin')) penalty -= 220;
      if (lower.includes('basic economy')) penalty -= 120;
      if (lower.includes('premium') || lower.includes('polaris') || lower.includes('business')) penalty += 250;
      if (!/from\s*\$|\$\d/.test(lower)) penalty += 120;
      return { el, penalty, text };
    })
    .filter((item) => item.penalty < 10000)
    .sort((a, b) => a.penalty - b.penalty);
  const target = (candidates[0] && candidates[0].el) || row;
  target.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, cancelable: true, composed: true }));
  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, composed: true }));
  target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, composed: true }));
  target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
  if (typeof target.click === 'function') {
    target.click();
  }
  return true;
}
""",
        {"targetText": best.get("text") or "", "targetFareText": best.get("fareText") or ""},
    )
    return {**best, "clicked": clicked}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--trip-type", choices=["roundtrip", "oneway"], default="roundtrip")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--depart", default="2026-07-17")
    parser.add_argument("--return-date", default="2026-07-20")
    parser.add_argument("--selected-airline", default="")
    parser.add_argument("--selected-time-summary", default="")
    parser.add_argument("--selected-duration", default="")
    parser.add_argument("--selected-stops", default="")
    parser.add_argument("--selected-price", default="")
    parser.add_argument("--stay-open", action="store_true")
    parser.add_argument("--step-seconds", type=float, default=0.8)
    parser.add_argument("--debug-dumps", action="store_true")
    args = parser.parse_args()

    depart_date = datetime.strptime(args.depart, "%Y-%m-%d")
    return_date = datetime.strptime(args.return_date, "%Y-%m-%d") if args.trip_type == "roundtrip" else None

    root = Path(__file__).resolve().parents[1]
    output_dir = root / ".playwright" / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / f"united-attach-{time.strftime('%Y%m%d-%H%M%S')}.png"

    with sync_playwright() as p:
        log(f"connecting over CDP to {args.cdp_url}")
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        page = pick_united_page(browser)
        if not page:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
        info = safe_page_info(page)
        log(f"attached to page title={info['title']!r} url={info['url']!r}")

        if "united.com" not in (info["url"] or ""):
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            pause(page, args.step_seconds, "navigated to united home")

        current_url = (safe_page_info(page).get("url") or "").lower()
        on_results_page = "/fsr/choose-flights" in current_url

        dismissed = dismiss_common_popups(page)
        overlay_dismissed = dismiss_booking_overlay(page)
        cookies_accepted = accept_cookies(page)
        cart_panel_dismissed = dismiss_cart_panel(page)
        booking_opened = None
        if not on_results_page and not booking_form_visible(page):
            booking_opened = open_booking_form(page)
            overlay_dismissed = overlay_dismissed or dismiss_booking_overlay(page)
        trip_type_click = None
        if not on_results_page:
            trip_type_click = ensure_roundtrip(page) if args.trip_type == "roundtrip" else ensure_oneway(page)
            pause(page, args.step_seconds, "booking form settle")

        initial_state = visible_state(page, "initial") if args.debug_dumps else None

        origin_fill = None
        destination_fill = None
        date_picker = None
        depart_pick = None
        return_pick = None
        search_click = None
        if not on_results_page:
            origin_fill = fill_airport_field(
                page,
                [
                    'input[aria-label*="Origin"]',
                    'input[placeholder*="Origin"]',
                    'input[name*="origin"]',
                    'input[id*="origin"]',
                ],
                args.origin,
                "origin-fill",
            )
            pause(page, args.step_seconds, "after origin")

            destination_fill = fill_airport_field(
                page,
                [
                    'input[aria-label*="Destination"]',
                    'input[placeholder*="Destination"]',
                    'input[name*="destination"]',
                    'input[id*="destination"]',
                ],
                args.destination,
                "destination-fill",
            )
            pause(page, args.step_seconds, "after destination")

            if args.trip_type == "oneway":
                trip_type_click = ensure_oneway(page)
                pause(page, 0.2, "re-assert one-way after airports")
            else:
                trip_type_click = ensure_roundtrip(page)
                pause(page, 0.2, "re-assert roundtrip after airports")

            if args.trip_type == "oneway":
                depart_pick = type_date_field(page, depart_date, "depart-date")
                page.wait_for_timeout(250)
                try:
                    page.keyboard.press("Tab")
                except Exception:
                    pass
            else:
                date_picker = open_date_picker(page)
                pause(page, args.step_seconds, "after date picker open")
                depart_pick = click_calendar_day(page, depart_date)
                if return_date is not None:
                    pause(page, 0.2, "after depart click")
                    return_pick = click_calendar_day(page, return_date)
                confirm_dates(page)
            pause(page, args.step_seconds, "after date selection")

            search_click = search_flights(page)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            pause(page, 3.0, "search results settle")

        matched_result = choose_matching_result(
            page,
            {
                "airline": args.selected_airline,
                "time_summary": args.selected_time_summary,
                "duration": args.selected_duration,
                "stops": args.selected_stops,
                "price": args.selected_price,
            },
        )

        final_state = visible_state(page, "final")
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(json.dumps({
            "cdpUrl": args.cdp_url,
            "attachedPage": safe_page_info(page),
            "dismissed": dismissed,
            "bookingOpened": booking_opened,
            "overlayDismissed": overlay_dismissed,
            "cookiesAccepted": cookies_accepted,
            "cartPanelDismissed": cart_panel_dismissed,
            "tripTypeClick": trip_type_click,
            "originFill": origin_fill,
            "destinationFill": destination_fill,
            "datePicker": date_picker,
            "departPick": depart_pick,
            "returnPick": return_pick,
            "searchClick": search_click,
            "matchedResult": matched_result,
            "initialState": initial_state,
            "finalState": final_state,
            "screenshot": str(screenshot_path),
        }, indent=2))

        if args.stay_open:
            print("Attached mode active. Press Ctrl+C to disconnect.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        browser.close()


if __name__ == "__main__":
    main()
