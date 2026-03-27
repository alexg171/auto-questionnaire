import argparse
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader


TEMPLATE_PATH = Path(__file__).with_name("answers.template.json")
ANSWER_TEMPLATE = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))

INT_KEYS = {
    "question_3_international",
    "question_5_undergrad_year",
    "question_9_military_affiliation",
    "question_10_employed_during_mba",
    "question_11_changed_jobs",
    "question_14_industry",
    "question_16_occupation",
    "question_17_employment_status",
    "question_18_current_salary",
    "question_19_years_with_employer",
    "question_20_remain_with_employer",
    "question_21_expect_salary_change",
    "question_23_new_annual_salary",
    "question_24_already_have_new_job",
    "question_32_currently_seeking_employment",
    "question_36_satisfaction_level",
    "question_40_recommend_to_colleague",
    "question_42_curriculum_relevance",
}

LIST_INT_KEYS = {
    "question_4_ethnicity",
    "question_44_course_evaluation_matrix",
    "question_45_ranking_order",
}

DATE_KEYS = {
    "question_1_survey_date": ("mm", "dd", "yyyy"),
    "question_6_start_date": ("mm", "yyyy"),
    "question_7_completion_date": ("mm", "yyyy"),
}

TEXT_ANCHORS = {
    "question_2_name": ["name"],
    "question_12_current_employer": ["current employer", "employer"],
    "question_13_employer_location": ["employer location", "location"],
    "question_15_job_title": ["job title", "title"],
    "question_22_new_position_title": ["new position title", "position title"],
    "question_25_new_employer_name": ["new employer name", "employer name"],
    "question_26_new_employer_location": ["new employer location"],
    "question_27_new_employer_industry": ["new employer industry"],
    "question_28_new_job_title": ["new job title"],
    "question_31_job_acceptance_timing": ["job acceptance timing"],
    "question_33_seeking_industry": ["seeking industry"],
    "question_34_duration_seeking": ["duration seeking", "how long seeking"],
    "question_37_comments_suggestions": ["comments", "suggestions"],
    "question_38_admin_improvements": ["administrative improvements", "admin improvements"],
    "question_39_areas_worked_well": ["worked well", "areas worked well"],
    "question_41_why_recommend": ["why recommend"],
    "question_43_curriculum_comments": ["curriculum comments", "comments on curriculum"],
}

DATE_ANCHORS = {
    "question_1_survey_date": ["date of survey", "survey date", "date"],
    "question_6_start_date": ["start date", "mba start date"],
    "question_7_completion_date": ["completion date", "mba completion date"],
}


def _question_sort_key(key):
    match = re.match(r"^question_(\d+)_", key)
    if match:
        return int(match.group(1))
    return 1_000_000_000


def _normalize_key(key):
    if not key:
        return ""
    return re.sub(r"^page_", "question_", str(key).strip())


def _first_integer(text):
    if text is None:
        return ""
    match = re.search(r"-?\d+", str(text))
    if not match:
        return ""
    try:
        return int(match.group(0))
    except ValueError:
        return ""


def _normalize_int_list(value):
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.findall(r"\d+", str(value))

    normalized = []
    for item in items:
        number = _first_integer(item)
        if number != "":
            normalized.append(number)
    return normalized


def _normalize_date(value, fields):
    result = {field: "" for field in fields}
    if value in (None, ""):
        return result

    if isinstance(value, dict):
        for field in fields:
            if field in value and value[field] is not None:
                raw = str(value[field]).strip()
                result[field] = raw.zfill(2) if field in ("mm", "dd") and raw else raw
        return result

    parts = re.findall(r"\d+", str(value))
    if fields == ("mm", "dd", "yyyy") and len(parts) >= 3:
        result["mm"] = parts[0].zfill(2)
        result["dd"] = parts[1].zfill(2)
        result["yyyy"] = parts[2]
    elif fields == ("mm", "yyyy") and len(parts) >= 2:
        result["mm"] = parts[0].zfill(2)
        result["yyyy"] = parts[1]
    return result


def _normalize_scalar(value):
    if value is None:
        return ""
    return str(value).strip()


def _normalize_value(key, value):
    if key in DATE_KEYS:
        return _normalize_date(value, DATE_KEYS[key])
    if key in LIST_INT_KEYS:
        return _normalize_int_list(value)
    if key in INT_KEYS:
        return _first_integer(value)
    return _normalize_scalar(value)


def _empty_value_for(key):
    if key in DATE_KEYS:
        return {field: "" for field in DATE_KEYS[key]}
    if key in LIST_INT_KEYS:
        return []
    return ""


def merge_answers(extracted_batches):
    answers = json.loads(json.dumps(ANSWER_TEMPLATE))
    for batch in extracted_batches:
        for raw_key, raw_value in batch.items():
            key = _normalize_key(raw_key)
            if key not in answers:
                continue
            normalized = _normalize_value(key, raw_value)
            if normalized == _empty_value_for(key):
                continue
            answers[key] = normalized
    return answers


def render_pdf_to_images(pdf_path, output_dir):
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(pdf_path))
    images = []
    for index, page in enumerate(reader.pages, start=1):
        page_images = list(page.images)
        if not page_images:
            raise RuntimeError(
                f"Page {index} does not contain an embedded scan image. "
                "This local OCR script currently expects scanned-image PDFs."
            )

        best = max(page_images, key=lambda img: img.image.size[0] * img.image.size[1])
        image = best.image
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")

        output_path = output_dir / f"page-{index:03d}.png"
        image.save(output_path, format="PNG")
        images.append(output_path)

    if not images:
        raise RuntimeError("No page images were created from the PDF.")
    return images


def preprocess_image(image_path):
    image = Image.open(image_path)
    try:
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image)
        image = image.filter(ImageFilter.MedianFilter(size=3))

        max_dim = 2200
        largest = max(image.width, image.height)
        if largest > max_dim:
            scale = max_dim / float(largest)
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            )

        # Light thresholding helps handwriting contrast without requiring OpenCV.
        image = image.point(lambda px: 255 if px > 190 else 0)
        processed_path = image_path.with_name(f"{image_path.stem}.prep.png")
        image.save(processed_path, format="PNG")
        return processed_path
    finally:
        image.close()


def create_easyocr_reader(model_dir, gpu=False):
    try:
        import easyocr
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "EasyOCR is not installed. Install it with `pip install easyocr` and run again."
        ) from exc

    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    return easyocr.Reader(
        ["en"],
        gpu=gpu,
        model_storage_directory=str(model_dir),
        user_network_directory=str(model_dir),
        verbose=False,
    )


def extract_page_ocr(reader, image_path):
    results = reader.readtext(str(image_path), detail=1, paragraph=False)
    entries = []
    for item in results:
        if len(item) != 3:
            continue
        bbox, text, confidence = item
        if not text or not str(text).strip():
            continue
        xs = [pt[0] for pt in bbox]
        ys = [pt[1] for pt in bbox]
        entries.append(
            {
                "text": str(text).strip(),
                "confidence": float(confidence),
                "left": float(min(xs)),
                "top": float(min(ys)),
                "right": float(max(xs)),
                "bottom": float(max(ys)),
            }
        )
    entries.sort(key=lambda item: (round(item["top"] / 25), item["left"]))
    return entries


def group_entries_into_lines(entries, y_tolerance=28):
    lines = []
    for entry in entries:
        if not lines:
            lines.append([entry])
            continue
        last_line = lines[-1]
        avg_top = sum(item["top"] for item in last_line) / len(last_line)
        if abs(entry["top"] - avg_top) <= y_tolerance:
            last_line.append(entry)
        else:
            lines.append([entry])

    packed = []
    for items in lines:
        items.sort(key=lambda item: item["left"])
        packed.append(
            {
                "text": " ".join(item["text"] for item in items).strip(),
                "entries": items,
                "top": min(item["top"] for item in items),
                "bottom": max(item["bottom"] for item in items),
            }
        )
    return packed


def clean_text(text):
    text = str(text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def normalized_text(text):
    return re.sub(r"[^a-z0-9]+", " ", clean_text(text).lower()).strip()


def looks_like_prompt_value(text):
    if not text:
        return False
    low = normalized_text(text)
    if not low:
        return False
    stop_phrases = (
        "please",
        "select",
        "check all",
        "circle",
        "rank",
        "comments",
        "suggestions",
        "question",
        "page",
    )
    if any(phrase in low for phrase in stop_phrases):
        return False
    return True


def extract_text_after_anchor(line_text, anchor):
    pattern = re.compile(rf"\b{re.escape(anchor)}\b\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    match = pattern.search(line_text)
    if not match:
        return ""
    return clean_text(match.group(1))


def find_text_value(lines, anchors):
    normalized_lines = [normalized_text(line["text"]) for line in lines]
    for idx, line in enumerate(lines):
        line_norm = normalized_lines[idx]
        for anchor in anchors:
            anchor_norm = normalized_text(anchor)
            if anchor_norm and anchor_norm in line_norm:
                inline = extract_text_after_anchor(line["text"], anchor)
                if inline and looks_like_prompt_value(inline):
                    return inline
                if idx + 1 < len(lines):
                    candidate = clean_text(lines[idx + 1]["text"])
                    if looks_like_prompt_value(candidate):
                        return candidate
    return ""


def find_date_value(lines, anchors, fields):
    for idx, line in enumerate(lines):
        line_norm = normalized_text(line["text"])
        for anchor in anchors:
            if normalized_text(anchor) not in line_norm:
                continue
            inline = extract_text_after_anchor(line["text"], anchor)
            if inline:
                normalized = _normalize_date(inline, fields)
                if normalized != {field: "" for field in fields}:
                    return normalized
            if idx + 1 < len(lines):
                candidate = clean_text(lines[idx + 1]["text"])
                normalized = _normalize_date(candidate, fields)
                if normalized != {field: "" for field in fields}:
                    return normalized
    return {field: "" for field in fields}


def find_name_fallback(lines):
    for line in lines:
        text = clean_text(line["text"])
        match = re.search(r"\bname\b\s*[:\-]?\s*(.+)$", text, re.IGNORECASE)
        if match:
            candidate = clean_text(match.group(1))
            if looks_like_prompt_value(candidate):
                return candidate
    return ""


def find_year_fallback(lines):
    for line in lines:
        text = clean_text(line["text"])
        match = re.search(r"\b(19|20)\d{2}\b", text)
        if match and "undergrad" in normalized_text(text):
            return match.group(0)
    return ""


def parse_ranking(lines):
    ranking = []
    for line in lines:
        text = clean_text(line["text"])
        for match in re.findall(r"\((\d)\)|\b(\d)\b", text):
            digit = next((part for part in match if part), "")
            if digit:
                ranking.append(int(digit))
    if len(ranking) >= 3:
        return ranking
    return []


def parse_matrix(lines):
    ratings = []
    for line in lines:
        numbers = [int(val) for val in re.findall(r"\b([1-5])\b", line["text"])]
        if len(numbers) == 1:
            ratings.append(numbers[0])
    return ratings


def parse_page(lines, page_number):
    data = {}

    for key, anchors in DATE_ANCHORS.items():
        value = find_date_value(lines, anchors, DATE_KEYS[key])
        if value != _empty_value_for(key):
            data[key] = value

    for key, anchors in TEXT_ANCHORS.items():
        value = find_text_value(lines, anchors)
        if value:
            data[key] = value

    if "question_2_name" not in data:
        fallback_name = find_name_fallback(lines)
        if fallback_name:
            data["question_2_name"] = fallback_name

    if "question_5_undergrad_year" not in data:
        year = find_year_fallback(lines)
        if year:
            data["question_5_undergrad_year"] = year

    if page_number >= 7:
        ranking = parse_ranking(lines)
        if ranking:
            data["question_45_ranking_order"] = ranking

    if page_number >= 6:
        matrix = parse_matrix(lines)
        if len(matrix) >= 3:
            data["question_44_course_evaluation_matrix"] = matrix

    return data


def ocr_pdf_locally(pdf_path, temp_dir, model_dir, gpu=False):
    reader = create_easyocr_reader(model_dir, gpu=gpu)
    page_images = render_pdf_to_images(pdf_path, temp_dir / "pages")
    extracted_batches = []
    ocr_debug = []

    for page_number, image_path in enumerate(page_images, start=1):
        print(f"OCR page {page_number}/{len(page_images)}: {image_path.name}")
        processed_path = preprocess_image(image_path)
        entries = extract_page_ocr(reader, processed_path)
        lines = group_entries_into_lines(entries)
        extracted = parse_page(lines, page_number)
        extracted_batches.append(extracted)
        ocr_debug.append(
            {
                "page": page_number,
                "image": image_path.name,
                "processed_image": processed_path.name,
                "lines": [line["text"] for line in lines],
                "extracted": extracted,
            }
        )

    return extracted_batches, ocr_debug


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read a scanned survey PDF with EasyOCR and generate answers.json locally."
    )
    parser.add_argument("pdf", help="Path to the scanned PDF")
    parser.add_argument(
        "-o",
        "--output",
        default="answers.json",
        help="Where to write the extracted answers JSON",
    )
    parser.add_argument(
        "--debug-json",
        default="ocr_debug.json",
        help="Where to write page-by-page OCR debug data",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Enable GPU mode in EasyOCR if available",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    temp_root = Path.cwd() / ".survey_tmp"
    temp_root.mkdir(exist_ok=True)
    temp_dir = temp_root / f"survey_pdf_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        extracted_batches, ocr_debug = ocr_pdf_locally(
            pdf_path=pdf_path,
            temp_dir=temp_dir,
            model_dir=Path.cwd() / ".easyocr_models",
            gpu=args.gpu,
        )
        answers = merge_answers(extracted_batches)

        output_path = Path(args.output)
        output_path.write_text(json.dumps(answers, indent=2), encoding="utf-8")

        debug_path = Path(args.debug_json)
        debug_path.write_text(json.dumps(ocr_debug, indent=2), encoding="utf-8")

        print(f"Wrote {output_path}")
        print(f"Wrote {debug_path}")
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
