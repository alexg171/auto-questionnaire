import json
import re
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURATION ---
STEP_DELAY = 1  # Seconds to wait between EVERY action

def slow_wait():
    """Standardized pause to let animations finish."""
    time.sleep(STEP_DELAY)

def _set_bootstrap_select_value(driver, element, value):
    """
    QuestionPro wraps date <select>s in bootstrap-select; the native control is
    hidden, so Selenium's Select() raises 'element not interactable'. Set value
    in JS and sync the plugin when jQuery/bootstrap-select is present.
    """
    val = str(value)
    driver.execute_script(
        """
        var el = arguments[0];
        var v = String(arguments[1]);
        el.value = v;
        if (window.jQuery && jQuery.fn.selectpicker) {
            var $el = jQuery(el);
            $el.selectpicker('val', v);
            $el.selectpicker('refresh');
            $el.trigger('changed.bs.select');
        } else if (window.jQuery) {
            jQuery(el).val(v).trigger('change');
        } else {
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """,
        element,
        val,
    )

def fill_questionpro_date(driver, wait, date_dict):
    """
    Date-of-survey fields from QuestionPro (see cover-wrapper.html): native
    <select> elements named dt_month_<id>, dt_day_<id>, dt_year_<id>.
    """
    mm = str(date_dict["mm"]).zfill(2)
    dd = str(date_dict["dd"]).zfill(2)
    yyyy = str(date_dict["yyyy"])

    month_el = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "select[name^='dt_month_']"))
    )
    _set_bootstrap_select_value(driver, month_el, mm)
    day_el = driver.find_element(By.CSS_SELECTOR, "select[name^='dt_day_']")
    _set_bootstrap_select_value(driver, day_el, dd)
    year_el = driver.find_element(By.CSS_SELECTOR, "select[name^='dt_year_']")
    _set_bootstrap_select_value(driver, year_el, yyyy)
    # QuestionPro may only enable Next after the UI plugin and validators sync.
    driver.execute_script(
        """
        document.querySelectorAll(
            "select[name^='dt_month_'], select[name^='dt_day_'], select[name^='dt_year_']"
        ).forEach(function (el) {
            if (window.jQuery && jQuery.fn.selectpicker) {
                var $el = jQuery(el);
                $el.selectpicker('refresh');
                $el.trigger('changed.bs.select');
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        });
        """
    )
    # Close any open bootstrap-select menus so they do not cover the Next control.
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(0.4)

def _first_visible_next_anchor(driver):
    """QuestionPro often has multiple .ok-btn Next links; only one is shown (others stay in display:none)."""
    xpath = (
        "//a[contains(@class, 'ok-btn') and normalize-space()='Next']"
        "|//button[contains(@class, 'ok-btn') and normalize-space()='Next']"
    )
    for el in driver.find_elements(By.XPATH, xpath):
        try:
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None

def click_next(driver, wait):
    """Clicks the visible UTEP / QuestionPro Next control (skips hidden template duplicates)."""
    print("  Waiting to click 'Next'...")
    slow_wait()  # Uses your STEP_DELAY

    def visible_next_ready(d):
        btn = _first_visible_next_anchor(d)
        return btn if btn is not None else False

    try:
        next_btn = wait.until(visible_next_ready)
        driver.execute_script("arguments[0].click();", next_btn)
        print("  Successfully clicked Next.")
    except Exception as e:
        print(f"  Warning: Visible 'Next' not found in time. Trying fallbacks... {e}")
        try:
            def any_visible_next(d):
                for el in d.find_elements(By.PARTIAL_LINK_TEXT, "Next"):
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
                for el in d.find_elements(By.XPATH, "//button[normalize-space()='Next']"):
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
                return False

            fallback_btn = wait.until(any_visible_next)
            driver.execute_script("arguments[0].click();", fallback_btn)
            print("  Successfully clicked Next (fallback).")
        except Exception:
            print("  Critical: Could not find a visible Next button. Please click it manually in the browser.")
            raise


def _visible_question_fingerprint(driver):
    """Stable fingerprint of visible questions (id + text) for next-page detection."""
    items = []
    for container in driver.find_elements(By.CSS_SELECTOR, ".question-container[id^='legend_']"):
        try:
            if not container.is_displayed():
                continue
            qid = (container.get_attribute("id") or "").strip()
            t = (container.text or "").strip()
            if qid or t:
                items.append((qid, t))
        except Exception:
            continue
    return tuple(items)


def _visible_inline_errors(driver):
    messages = []
    for el in driver.find_elements(By.CSS_SELECTOR, "[id^='errorSpan_'], .error"):
        try:
            if not el.is_displayed():
                continue
            cl = (el.get_attribute("class") or "").lower()
            if "hidden" in cl or "d-none" in cl or "vhidden" in cl:
                continue
            t = (el.text or "").strip()
            if t:
                messages.append(t)
        except Exception:
            continue
    return messages


def _visible_mc_choice_rows(driver):
    """
    Ordered visible choice rows for multiple-choice questions.
    We target rows/labels instead of input visibility because QuestionPro often
    hides the native input and renders a custom indicator.
    """
    rows = []
    containers = driver.find_elements(
        By.CSS_SELECTOR, "div.answer-container.multiple-choice-question"
    )
    for container in containers:
        try:
            if not container.is_displayed():
                continue
        except Exception:
            continue

        for row in container.find_elements(By.CSS_SELECTOR, "div.answer-options"):
            try:
                cl = (row.get_attribute("class") or "").lower()
                if "dynamic-explode" in cl or "hidden" in cl or "d-none" in cl:
                    continue
                if not row.is_displayed():
                    continue
                # Must contain a real choice input.
                inp = row.find_element(By.CSS_SELECTOR, "input.radio-check")
                t = (inp.get_attribute("type") or "").lower()
                if t not in ("radio", "checkbox"):
                    continue
            except Exception:
                continue
            rows.append(row)
    return rows


def click_next_and_advance(driver, wait):
    """
    Clicks Next and fails fast if the visible question(s) do not change
    (unanswered required field, validation, or wrong target).
    """
    before = _visible_question_fingerprint(driver)
    click_next(driver, wait)
    transition_wait = max(30, STEP_DELAY * 2)
    w = WebDriverWait(driver, transition_wait)
    try:
        w.until(lambda d: _visible_question_fingerprint(d) != before)
        print("  Confirmed: new question loaded.")
    except Exception as e:
        err = _visible_inline_errors(driver)
        hint = f" Visible validation: {err!r}" if err else ""
        raise RuntimeError(
            "Next did not advance — the on-screen question text did not change. "
            "Often the survey is still waiting for a radio/checkbox or a follow-up field."
            f"{hint} Fingerprint before click: {before!r}."
        ) from e


def fill_undergrad_year(driver, wait, year_value):
    """
    Selects the visible dt_year_* field and validates that it really changed.
    Some date-time pages include hidden day/month/year selects and bootstrap wrappers.
    """
    target = str(year_value)

    # Use the visible bootstrap-select control first (what a user clicks).
    btn_xpath = (
        "//button[contains(@class,'dropdown-toggle') and "
        "starts-with(@data-id,'dt_year_') and "
        "not(ancestor::*[contains(@class,'hidden') or contains(@class,'d-none')])]"
    )
    option_xpath = (
        "//div[contains(@class,'dropdown-menu') and contains(@class,'open')]"
        "//span[contains(@class,'text') and normalize-space()="
        + json.dumps(target)
        + "]"
    )

    def visible_year_button(d):
        return _first_visible(d, btn_xpath) or False

    btn = wait.until(visible_year_button)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
    driver.execute_script("arguments[0].click();", btn)

    option = wait.until(lambda d: _first_visible(d, option_xpath) or False)
    driver.execute_script("arguments[0].click();", option)
    time.sleep(0.2)

    # Verify visible UI text reflects the selected year.
    ui_text = (btn.get_attribute("title") or "").strip()
    if not ui_text:
        ui_text = (driver.execute_script(
            "var fo = arguments[0].querySelector('.filter-option'); return fo ? fo.textContent : '';",
            btn,
        ) or "").strip()

    # Sync native select as fallback and verify both surfaces.
    sel_name = (btn.get_attribute("data-id") or "").replace("ID", "")
    year_select = driver.find_element(By.CSS_SELECTOR, f"select[name='{sel_name}']")
    if ui_text != target:
        _set_bootstrap_select_value(driver, year_select, target)
        time.sleep(0.2)
        ui_text = (btn.get_attribute("title") or "").strip()
    selected_val = (year_select.get_attribute("value") or "").strip()

    # Close dropdown overlays to avoid next-button obstruction.
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass

    if ui_text != target or selected_val != target:
        raise RuntimeError(
            "Undergrad year did not stick in the visible dropdown. "
            f"Expected '{target}', got ui='{ui_text or '<empty>'}', select='{selected_val or '<empty>'}'."
        )


def _first_visible(driver, xpath):
    """Return first displayed element for xpath, or None (avoids hidden bootstrap search boxes, etc.)."""
    for el in driver.find_elements(By.XPATH, xpath):
        try:
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None

def handle_input(driver, wait, value, xpath_type="input"):
    """Waits, clears, and types slowly. Prefer xpath that resolves to a single visible field."""
    if value == "" or value == 0:
        return

    slow_wait()
    xpath = xpath_type if xpath_type.strip().startswith("//") else f"//{xpath_type}"

    def visible_ready(d):
        el = _first_visible(d, xpath)
        return el if el is not None else False

    element = wait.until(visible_ready)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    element = wait.until(visible_ready)
    try:
        element.clear()
    except Exception:
        driver.execute_script("arguments[0].value = '';", element)
    for char in str(value):
        element.send_keys(char)
        time.sleep(0.1)


def fill_name_question(driver, wait, value):
    """QuestionPro open-text 'Name' sits under multi-row-question; generic //input[@type='text'] hits hidden inputs first."""
    if value == "" or value == 0:
        return
    slow_wait()
    xpath = (
        "//span[contains(@class,'question-text-span')][normalize-space()='Name']"
        "/ancestor::div[contains(@class,'multi-row-question')][1]"
        "//input[@type='text']"
    )

    def name_input_ready(d):
        el = _first_visible(d, xpath)
        return el if el is not None else False

    element = wait.until(name_input_ready)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    element = wait.until(name_input_ready)
    try:
        element.clear()
    except Exception:
        driver.execute_script("arguments[0].value = '';", element)
    for char in str(value):
        element.send_keys(char)
        time.sleep(0.1)

def _click_choice_row(driver, row):
    """Click visible row with label/input fallbacks; verifies the choice toggled when possible."""
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
    time.sleep(0.2)

    inp = row.find_element(By.CSS_SELECTOR, "input.radio-check")
    is_checkbox = (inp.get_attribute("type") or "").lower() == "checkbox"
    try:
        if inp.is_selected() and is_checkbox:
            return
    except Exception:
        pass

    # Try label first (closest to real user interaction), then input.
    try:
        label = row.find_element(By.CSS_SELECTOR, "label.controls[for]")
        driver.execute_script("arguments[0].click();", label)
    except Exception:
        pass

    time.sleep(0.1)
    now_selected = False
    try:
        now_selected = inp.is_selected()
    except Exception:
        now_selected = False

    # Fallback to input click only if label click did not select it.
    if not now_selected:
        try:
            driver.execute_script("arguments[0].click();", inp)
        except Exception:
            pass
        time.sleep(0.2)
        try:
            now_selected = inp.is_selected()
        except Exception:
            now_selected = False

    if not now_selected:
        kind = "checkbox" if is_checkbox else "radio option"
        raise RuntimeError(f"Choice click did not select the {kind}.")


def select_by_index(driver, wait, choice):
    """
    Clicks the Nth visible option (1-based) among multiple-choice radios/checkboxes on screen.
    Ignores hidden template sections and dynamic-explode rows (follow-up text areas).
    """
    if choice == "" or choice == 0 or choice == "-1":
        return

    slow_wait()
    choices = choice if isinstance(choice, list) else [choice]
    for c in choices:
        idx = int(c) - 1

        def nth_choice_ready(d):
            rows = _visible_mc_choice_rows(d)
            if 0 <= idx < len(rows):
                return rows[idx]
            return False

        target = wait.until(nth_choice_ready)
        _click_choice_row(driver, target)


def _active_wrapper(driver):
    """Best-effort current question wrapper."""
    wrappers = driver.find_elements(By.CSS_SELECTOR, ".survey-question-wrapper")
    for w in wrappers:
        try:
            if w.is_displayed() and "active-question" in (w.get_attribute("class") or ""):
                return w
        except Exception:
            continue
    for w in wrappers:
        try:
            if w.is_displayed():
                return w
        except Exception:
            continue
    return None


def _visible_choice_rows_in_wrapper(wrapper):
    rows = []
    for row in wrapper.find_elements(By.CSS_SELECTOR, ".answer-container.multiple-choice-question .answer-options"):
        try:
            cl = (row.get_attribute("class") or "").lower()
            if "dynamic-explode" in cl or "hidden" in cl or "d-none" in cl:
                continue
            if not row.is_displayed():
                continue
            inp = row.find_element(By.CSS_SELECTOR, "input.radio-check")
            if (inp.get_attribute("type") or "").lower() not in ("radio", "checkbox"):
                continue
        except Exception:
            continue
        rows.append(row)
    return rows


def _select_choice_in_wrapper(driver, wrapper, choice):
    if choice in ("", 0, -1, "-1", None):
        return
    rows = _visible_choice_rows_in_wrapper(wrapper)
    if not rows:
        raise RuntimeError("No visible multiple-choice options found on current page.")

    value_str = str(choice).strip()
    target_row = None

    if value_str.isdigit():
        idx = int(value_str) - 1
        if 0 <= idx < len(rows):
            target_row = rows[idx]

    if target_row is None:
        low = value_str.lower()
        for row in rows:
            try:
                label = row.find_element(By.CSS_SELECTOR, "span.control-label")
                text = (label.text or "").strip().lower()
                if text == low or low in text:
                    target_row = row
                    break
            except Exception:
                continue

    if target_row is None:
        raise RuntimeError(f"Could not match option '{choice}' on current page.")

    _click_choice_row(driver, target_row)


def _fill_text_in_wrapper(driver, wait, wrapper, value):
    if value in ("", None):
        return
    text = str(value)
    candidates = wrapper.find_elements(By.CSS_SELECTOR, "input[type='text'], textarea")
    field = None
    for el in candidates:
        try:
            if el.is_displayed():
                field = el
                break
        except Exception:
            continue
    if field is None:
        raise RuntimeError("Could not find visible text field on current page.")

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", field)
    time.sleep(0.2)
    try:
        field.clear()
    except Exception:
        driver.execute_script("arguments[0].value='';", field)
    for ch in text:
        field.send_keys(ch)
        time.sleep(0.04)


def _fill_date_in_wrapper(driver, wrapper, date_dict):
    if not isinstance(date_dict, dict):
        raise RuntimeError("Expected date object for date-time question.")
    mm = str(date_dict.get("mm", "")).zfill(2) if "mm" in date_dict else None
    dd = str(date_dict.get("dd", "")).zfill(2) if "dd" in date_dict else None
    yyyy = str(date_dict.get("yyyy", "")) if "yyyy" in date_dict else None

    if mm:
        month = wrapper.find_element(By.CSS_SELECTOR, "select[name^='dt_month_']")
        _set_bootstrap_select_value(driver, month, mm)
    if dd:
        day = wrapper.find_element(By.CSS_SELECTOR, "select[name^='dt_day_']")
        _set_bootstrap_select_value(driver, day, dd)
    if yyyy:
        year = wrapper.find_element(By.CSS_SELECTOR, "select[name^='dt_year_']")
        _set_bootstrap_select_value(driver, year, yyyy)

    driver.execute_script(
        """
        var root = arguments[0];
        root.querySelectorAll("select[name^='dt_month_'],select[name^='dt_day_'],select[name^='dt_year_']").forEach(function (el) {
            if (window.jQuery) {
                var $el = jQuery(el);
                if (jQuery.fn.selectpicker) $el.selectpicker('refresh');
                $el.trigger('changed.bs.select').trigger('input').trigger('change');
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        });
        """,
        wrapper,
    )


def _normalize_money_text(text):
    return "".join((text or "").lower().split())


def _salary_code_to_label(value):
    """
    Compact salary input mapping:
    - 0   -> Under $25K
    - 25  -> $25K - $29K
    - 290 -> $290k - $299k
    """
    s = str(value).strip()
    if not s:
        return s
    try:
        n = int(float(s))
    except Exception:
        return s

    if n <= 0:
        return "Under $25K"
    if n == 25:
        return "$25K - $29K"
    if 30 <= n <= 290 and n % 10 == 0:
        return f"${n}K - ${n + 9}K"
    if n >= 300:
        return "$300k or more"
    return s


def _fill_dropdown_in_wrapper(driver, wrapper, key, value):
    if value in ("", None, -1, "-1"):
        return

    select_el = None
    for sel in wrapper.find_elements(By.CSS_SELECTOR, ".answer-container.dropdown-question select"):
        try:
            if sel.get_attribute("name"):
                select_el = sel
                break
        except Exception:
            continue
    if select_el is None:
        raise RuntimeError("Could not find dropdown <select> on current page.")

    desired = str(value).strip()
    if "salary" in key:
        desired = _salary_code_to_label(value)

    options = select_el.find_elements(By.TAG_NAME, "option")
    if not options:
        raise RuntimeError("Dropdown has no options.")

    target_val = None
    desired_norm = _normalize_money_text(desired)

    # 1) Exact text match (case-insensitive / spacing-insensitive)
    for opt in options:
        txt = (opt.text or "").strip()
        if _normalize_money_text(txt) == desired_norm:
            target_val = opt.get_attribute("value")
            break

    # 2) Index mapping for numeric non-salary values (1-based)
    if target_val is None and desired.isdigit() and "salary" not in key:
        idx = int(desired)
        if 0 < idx < len(options):
            target_val = options[idx].get_attribute("value")

    # 3) Contains match for minor text variations
    if target_val is None:
        for opt in options:
            txt = _normalize_money_text((opt.text or "").strip())
            if desired_norm and (desired_norm in txt or txt in desired_norm):
                target_val = opt.get_attribute("value")
                break

    if not target_val or target_val == "-1":
        raise RuntimeError(f"Could not map dropdown value '{value}' for {key}.")

    _set_bootstrap_select_value(driver, select_el, target_val)
    time.sleep(0.2)
    actual = (select_el.get_attribute("value") or "").strip()
    if actual != target_val:
        raise RuntimeError(
            f"Dropdown selection did not stick for {key}. Expected option value '{target_val}', got '{actual}'."
        )


def _wrapper_has_fill_target(driver, wrapper, key, value):
    if wrapper is None:
        return False

    try:
        if not wrapper.is_displayed():
            return False
    except Exception:
        return False

    if isinstance(value, dict):
        if "undergrad_year" in key:
            return _first_visible(
                driver,
                "//button[contains(@class,'dropdown-toggle') and starts-with(@data-id,'dt_year_')]",
            ) is not None
        return bool(
            wrapper.find_elements(
                By.CSS_SELECTOR,
                "select[name^='dt_month_'], select[name^='dt_day_'], select[name^='dt_year_']",
            )
        )

    if "undergrad_year" in key:
        return _first_visible(
            driver,
            "//button[contains(@class,'dropdown-toggle') and starts-with(@data-id,'dt_year_')]",
        ) is not None

    if key in ("question_44_course_evaluation_matrix", "page_44_course_evaluation_matrix"):
        rows = wrapper.find_elements(By.CSS_SELECTOR, "tr[id^='questionRow']")
        return any(row.is_displayed() for row in rows)

    if _visible_choice_rows_in_wrapper(wrapper):
        return True

    if wrapper.find_elements(By.CSS_SELECTOR, ".answer-container.dropdown-question select"):
        return True

    for el in wrapper.find_elements(By.CSS_SELECTOR, "input[type='text'], textarea"):
        try:
            if el.is_displayed():
                return True
        except Exception:
            continue

    return False


def _wait_for_fillable_page(driver, key, value, timeout_s=18):
    """
    Some QuestionPro transitions briefly show interstitial pages that auto-advance.
    Wait until the active page exposes controls that match the value we intend to fill.
    """
    end = time.time() + timeout_s
    last_fp = None
    last_change = time.time()

    while time.time() < end:
        wrapper = _active_wrapper(driver)
        if _wrapper_has_fill_target(driver, wrapper, key, value):
            return wrapper

        fp = _visible_question_fingerprint(driver)
        now = time.time()
        if fp != last_fp:
            last_fp = fp
            last_change = now
        elif now - last_change > 0.8:
            _wait_for_scroll_settle(driver, settle_ms=700, timeout_s=3)
            last_change = now

        time.sleep(0.2)

    wrapper = _active_wrapper(driver)
    raise RuntimeError(
        f"Timed out waiting for a fillable page for {key}. "
        f"Visible page fingerprint: {_visible_question_fingerprint(driver)!r}."
    )


def _fill_current_page_value(driver, wait, key, value):
    wrapper = _wait_for_fillable_page(driver, key, value)
    if wrapper is None:
        raise RuntimeError("Could not detect active survey page.")

    if isinstance(value, dict):
        if "undergrad_year" in key:
            yyyy = value.get("yyyy", "")
            if yyyy not in ("", None, 0):
                fill_undergrad_year(driver, wait, yyyy)
        else:
            _fill_date_in_wrapper(driver, wrapper, value)
        return

    if "undergrad_year" in key:
        if value not in ("", None, 0):
            fill_undergrad_year(driver, wait, value)
        return

    if wrapper.find_elements(By.CSS_SELECTOR, ".answer-container.dropdown-question select"):
        _fill_dropdown_in_wrapper(driver, wrapper, key, value)
        return

    if isinstance(value, list):
        for item in value:
            _select_choice_in_wrapper(driver, wrapper, item)
        return

    # Try choice questions first when options exist.
    if _visible_choice_rows_in_wrapper(wrapper):
        # Site ordering is reversed for military-affiliation page; flip index here.
        if key in ("question_9_military_affiliation", "page_9_military_affiliation"):
            rows = _visible_choice_rows_in_wrapper(wrapper)
            v = str(value).strip()
            if v.isdigit():
                idx = int(v)
                if 1 <= idx <= len(rows):
                    value = str(len(rows) + 1 - idx)
        _select_choice_in_wrapper(driver, wrapper, value)
        return

    _fill_text_in_wrapper(driver, wait, wrapper, value)


def _fill_course_evaluation_matrix(driver, wait, ratings):
    wrapper = _wait_for_fillable_page(driver, "question_44_course_evaluation_matrix", ratings)
    slow_wait()

    matrix_rows = []
    for row in wrapper.find_elements(By.CSS_SELECTOR, "tr[id^='questionRow']"):
        try:
            if row.is_displayed():
                matrix_rows.append(row)
        except Exception:
            continue

    if not matrix_rows:
        raise RuntimeError("Matrix page was expected from answers order, but matrix rows are not visible.")

    if len(ratings) > len(matrix_rows):
        raise RuntimeError(
            f"Matrix answer list has {len(ratings)} ratings, but only {len(matrix_rows)} visible rows were found."
        )

    for i, rating in enumerate(ratings):
        idx = int(rating) - 1
        options = matrix_rows[i].find_elements(By.CSS_SELECTOR, "input.radio-check")
        visible_options = []
        for opt in options:
            try:
                opt.is_displayed()
                visible_options.append(opt)
            except Exception:
                visible_options.append(opt)

        if idx < 0 or idx >= len(visible_options):
            raise RuntimeError(
                f"Matrix rating {rating} is out of range for row {i + 1}; found {len(visible_options)} options."
            )

        target = visible_options[idx]
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
        time.sleep(0.15)
        try:
            label = matrix_rows[i].find_element(By.CSS_SELECTOR, f"label[for='{target.get_attribute('id')}']")
            driver.execute_script("arguments[0].click();", label)
        except Exception:
            driver.execute_script("arguments[0].click();", target)
        time.sleep(0.15)


def _ordered_question_keys(data):
    pairs = []
    for k in data.keys():
        m = re.match(r"^(?:question|page)_(\d+)_", k)
        if m:
            pairs.append((int(m.group(1)), k))
    return [k for _, k in sorted(pairs, key=lambda x: x[0])]


def _wait_for_scroll_settle(driver, settle_ms=800, timeout_s=8):
    """
    Wait until window scroll position stops changing.
    Useful for QuestionPro pages that auto-scroll after Next.
    """
    end = time.time() + timeout_s
    last_y = None
    stable_since = None
    while time.time() < end:
        try:
            y = float(driver.execute_script("return window.pageYOffset || document.documentElement.scrollTop || 0;"))
        except Exception:
            return
        now = time.time()
        if last_y is None or abs(y - last_y) > 1:
            last_y = y
            stable_since = now
        elif stable_since is not None and (now - stable_since) * 1000 >= settle_ms:
            return
        time.sleep(0.12)

def auto_survey(data):
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    wait = WebDriverWait(driver, 15) # Increased wait time for slow UTEP servers
    
    try:
        driver.get("https://utep.questionpro.com/a/TakeSurvey?tt=bKOz34HbvOmMuk7Y1EUFpg%3D%3D&lcfpn=false")
        
        # --- START THE SURVEY ---
        print("Clicking Start...")
        start_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Start')] | //button[contains(text(), 'Start')]")))
        driver.execute_script("arguments[0].click();", start_btn)
        
        # Next
        print("Clicking next")
        click_next_and_advance(driver, wait)
            
        # Follow exact numeric order from answers.json/question keys.
        post_scroll_pages_remaining = 0
        for key in _ordered_question_keys(data):
            if post_scroll_pages_remaining > 0:
                print("  Waiting for auto-scroll to settle...")
                _wait_for_scroll_settle(driver, settle_ms=900, timeout_s=10)
                slow_wait()
                post_scroll_pages_remaining -= 1

            if key in ("question_45_ranking_order", "page_45_ranking_order"):
                print("\n--- MANUAL RANKING ORDER ---")
                print("Please drag items in this order:", data[key])
                input("Press ENTER here after you finish the Drag-and-Drop to close the script...")
                break

            if key in ("question_44_course_evaluation_matrix", "page_44_course_evaluation_matrix"):
                print("Filling Course Evaluation Matrix...")
                _fill_course_evaluation_matrix(driver, wait, data[key])
                click_next_and_advance(driver, wait)
                continue

            print(f"Filling {key}...")
            _fill_current_page_value(driver, wait, key, data[key])
            click_next_and_advance(driver, wait)
            if key in ("question_35_plans_next_18_months", "page_35_plans_next_18_months"):
                # This transition triggers extra auto-scroll behavior on the next two pages.
                print("  Extra wait after page 35 (auto-scroll pages).")
                _wait_for_scroll_settle(driver, settle_ms=1100, timeout_s=12)
                time.sleep(max(1.0, STEP_DELAY))
                post_scroll_pages_remaining = 2

    except Exception as e:
        print(f"\nStopped at an error: {e}")
        input("Press Enter to close the browser and check your code...")
    finally:
        driver.quit()
        
# Load answer JSON and Run
if __name__ == "__main__":
    import sys
    filename = sys.argv[1] if len(sys.argv) > 1 else 'answers.json'
    with open(filename, 'r') as f:
        data = json.load(f)
    auto_survey(data)
