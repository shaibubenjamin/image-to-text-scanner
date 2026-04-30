"""
Full questionnaire processing pipeline.

Handles:
  - PDF → image conversion (via PyMuPDF)
  - OCR per page
  - Question/answer parsing (Q1, Q2 … patterns)
  - Checkbox/tick detection (text + image)
  - Schema auto-generation from the first questionnaire
  - Accuracy metrics computation
  - Persisting results to the database
"""

import logging
import os
import re
import tempfile
from pathlib import Path

from ocr_pipeline import extract_text_with_confidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches: Q1, Q1., Q1:, Q1), Q 1, Question 1, Question1
QUESTION_RE = re.compile(
    r'(?:^|\n)\s*'
    r'(?:Q(?:uestion)?\s*(\d{1,3}))'
    r'[\s.:)]*',
    re.IGNORECASE | re.MULTILINE,
)

# Fallback: plain numbered items "1. " or "1) " at start of line
NUMBERED_RE = re.compile(
    r'(?:^|\n)\s*(\d{1,3})[.)]\s+(?=[A-Z])',
    re.MULTILINE,
)

# Checked checkbox — broad: handles handwritten ticks OCR'd as /, v, V, \, |, 1, i, !, 7
CHECKED_TEXT_RE = re.compile(
    r'\[[\s]*[Xx✓✗√×☑/\\vV1iI!|7]\s*\]'   # [X] [x] [/] [v] [✓] etc.
    r'|☑|☒|✓|✗|✘'                           # Unicode tick/cross
    r'|\([\s]*[Xx✓/vV1]\s*\)'               # (X) (x) (/) (v)
    r'|■|▪|●|◼',                             # filled shapes (scanned ticks)
    re.IGNORECASE,
)
# Unchecked checkbox
UNCHECKED_TEXT_RE = re.compile(
    r'\[[\s]{0,3}\]'                         # [] [ ]
    r'|☐|□|◻|○|◯'                            # empty shapes
    r'|\([\s]{0,3}\)',                        # () ( )
    re.IGNORECASE,
)
# All checkbox markers combined (for splitting)
ALL_CB_RE = re.compile(
    r'\[[\s]*[Xx✓✗√×☑/\\vV1iI!|7]\s*\]'
    r'|☑|☒|✓|✗|✘'
    r'|\([\s]*[Xx✓/vV1]\s*\)'
    r'|■|▪|●|◼'
    r'|\[[\s]{0,3}\]'
    r'|☐|□|◻|○|◯'
    r'|\([\s]{0,3}\)',
    re.IGNORECASE,
)
# Words that are themselves checkbox symbols — skip when extracting option labels
_CB_WORD_RE = re.compile(r'^[\[\]XxOo□☐☑☒✓✗■◻◼()Vv/\\|!1i✘●▪◼]+$')

# Multiple-choice option markers: "A)", "(A)", "A.", etc.
MC_OPTION_RE = re.compile(
    r'(?:^|\n)\s*(?:\()?([A-Ea-e])[.)]\s+\S',
    re.MULTILINE,
)

# Blank answer lines
BLANK_LINE_RE = re.compile(r'_{3,}|\.{3,}|-{3,}')

# ---------------------------------------------------------------------------
# PDF → images
# ---------------------------------------------------------------------------

def extract_text_from_pdf_direct(pdf_path: str) -> tuple[str, int]:
    """
    Try to pull embedded text straight from a digital PDF (no OCR needed).
    Returns (full_text, page_count).  Returns ('', 0) if the PDF is image-only.
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        parts = []
        for page in doc:
            parts.append(page.get_text())
        doc.close()
        full = '\n\n--- PAGE BREAK ---\n\n'.join(parts)
        return full, len(parts)
    except Exception as exc:
        logger.error('Direct PDF text extraction failed: %s', exc)
        return '', 0


def pdf_to_images(pdf_path: str, dpi: int = None) -> list[str]:
    """
    Render every PDF page to a JPEG file (smaller than PNG = faster API transfer).

    Windows fix: close NamedTemporaryFile handle before PyMuPDF writes to it.
    """
    from ocr_pipeline import RENDER_DPI, JPEG_QUALITY
    if dpi is None:
        dpi = RENDER_DPI

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        images = []
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        upload_dir = os.path.dirname(pdf_path)

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # JPEG instead of PNG — ~5× smaller, much faster to upload to Vision API
            tmp = tempfile.NamedTemporaryFile(
                suffix=f'_p{page_num + 1}.jpg',
                dir=upload_dir,
                delete=False,
            )
            tmp_name = tmp.name
            tmp.close()  # close before PyMuPDF writes (Windows requirement)

            pix.save(tmp_name, output='jpeg', jpg_quality=JPEG_QUALITY)
            images.append(tmp_name)

        doc.close()
        return images
    except Exception as exc:
        logger.error('PDF→images failed: %s', exc)
        return []


# ---------------------------------------------------------------------------
# Question / answer parsing
# ---------------------------------------------------------------------------

def _infer_answer_type(answer_text: str) -> str:
    """Guess whether the answer is checkbox, multiple_choice, or text."""
    if CHECKED_TEXT_RE.search(answer_text) or UNCHECKED_TEXT_RE.search(answer_text):
        options = MC_OPTION_RE.findall(answer_text)
        if len(options) >= 2:
            return 'multiple_choice'
        return 'checkbox'
    if MC_OPTION_RE.findall(answer_text):
        return 'multiple_choice'
    if BLANK_LINE_RE.search(answer_text):
        return 'handwritten'
    clean = answer_text.strip()
    if len(clean) < 60 and '\n' not in clean:
        return 'handwritten'
    return 'text'


# ---------------------------------------------------------------------------
# Checkbox option extraction
# ---------------------------------------------------------------------------

def _extract_options_text_based(text: str) -> list:
    """
    Split on ALL checkbox markers and, for each checked marker,
    extract the option label that sits immediately before OR after it.

    Handles both:
      [X] Option A     (marker before label)
      Option A [X]     (label before marker)
      Option A [X]  Option B [ ]  Option C [ ]   (all on one line)
    """
    parts = ALL_CB_RE.split(text)
    markers = ALL_CB_RE.findall(text)

    if not markers:
        return []

    selected = []
    for i, marker in enumerate(markers):
        if not CHECKED_TEXT_RE.fullmatch(marker.strip()):
            continue  # unchecked

        before_raw = parts[i] if i < len(parts) else ''
        after_raw  = parts[i + 1] if i + 1 < len(parts) else ''

        # Last line of text before the marker (same line as [X])
        before_line = ALL_CB_RE.sub('', before_raw.split('\n')[-1]).strip()
        before_line = re.sub(r'\s{2,}', ' ', before_line).strip('.,;:- ')

        # First line of text after the marker
        after_line = ALL_CB_RE.sub('', after_raw.split('\n')[0]).strip()
        after_line = re.sub(r'\s{2,}', ' ', after_line).strip('.,;:- ')

        # Layout detection:
        #   "Option [X] ..."  — marker follows the label on the same line → use before_line
        #   "[X]\nOption"     — marker is on its own line, label is below → use after_line
        #   "[X] Option"      — marker and label on same line, label comes after → use after_line
        before_has_newline = '\n' in before_raw

        if not before_has_newline and before_line:
            # Same-line "Option [X]" layout
            option = before_line
        else:
            # New-line "[X]\nOption" or same-line "[X] Option" layout
            option = after_line if after_line else before_line

        if option and 0 < len(option) <= 80:
            selected.append(option)

    return selected


def _extract_options_spatial(checkboxes: list, words: list) -> list:
    """
    Use Vision API word bounding boxes and image-detected checkbox positions
    to find which option labels are physically next to ticked boxes.

    `words`      — list of {text, cx, cy, confidence} from Vision API
    `checkboxes` — list of {x, y, w, h, checked} from OpenCV detector
    """
    if not checkboxes or not words:
        return []

    selected = []
    for cb in checkboxes:
        if not cb.get('checked'):
            continue

        cb_cx = cb['x'] + cb['w'] / 2
        cb_cy = cb['y'] + cb['h'] / 2
        line_tolerance = cb['h'] * 2.0  # same line = within 2 box-heights vertically

        candidates = []
        for w in words:
            if _CB_WORD_RE.match(w['text']):
                continue  # skip words that are themselves checkbox symbols
            wcx = w.get('cx', 0)
            wcy = w.get('cy', 0)
            if abs(wcy - cb_cy) > line_tolerance:
                continue  # different line
            dx = wcx - cb_cx
            dy = wcy - cb_cy
            dist = (dx ** 2 + dy ** 2) ** 0.5
            candidates.append((dist, dx, w['text']))

        if not candidates:
            continue

        candidates.sort()
        # Prefer words to the right of the checkbox (standard [X] Label layout);
        # fall back to words on the left (Label [X] layout).
        right = [(d, dx, t) for d, dx, t in candidates if dx >= -cb['w']]
        chosen = right[0][2] if right else candidates[0][2]
        chosen = chosen.strip('.,;:- ')
        if chosen and chosen not in selected:
            selected.append(chosen)

    return selected


def _normalize_answer(raw: str, answer_type: str,
                       all_words: list = None, all_checkboxes: list = None) -> str:
    """
    Clean and normalise an extracted answer.

    For checkbox/MC questions uses a two-stage approach:
      1. Text-based token splitting (handles well-OCR'd marks)
      2. Spatial matching (handles poorly-OCR'd / handwritten ticks)
    """
    text = raw.strip()

    if answer_type in ('checkbox', 'multiple_choice'):
        selected = []

        # Stage 1 — text-based
        if CHECKED_TEXT_RE.search(text) or UNCHECKED_TEXT_RE.search(text):
            selected = _extract_options_text_based(text)

        # Stage 2 — spatial (runs when text-based found nothing or no markers at all)
        if not selected and all_words and all_checkboxes:
            selected = _extract_options_spatial(all_checkboxes, all_words)

        if selected:
            return '; '.join(selected)

        # Last resort: return any non-empty text (the answer region may contain the response)
        clean = ALL_CB_RE.sub('', text).strip()
        clean = re.sub(r'\s{2,}', ' ', clean)
        if clean and len(clean) <= 120:
            return clean
        return 'Not selected / blank'

    # Plain text / handwritten — collapse whitespace
    text = re.sub(r'\n{2,}', '\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = BLANK_LINE_RE.sub('', text).strip()
    return text


def parse_questions_from_text(full_text: str,
                              all_words: list = None,
                              all_checkboxes: list = None) -> list[dict]:
    """
    Split full OCR text into a list of question blocks.

    Returns:
        [{"label": "Q1", "question_text": "...", "raw_answer": "..."}, ...]
    """
    # Try Q-prefix pattern first
    spans = [(m.start(), m.group(1)) for m in QUESTION_RE.finditer(full_text)]

    if len(spans) < 2:
        # Fall back to numbered items
        spans = [(m.start(), m.group(1)) for m in NUMBERED_RE.finditer(full_text)]

    if not spans:
        return []

    blocks = []
    for i, (start, num) in enumerate(spans):
        end = spans[i + 1][0] if i + 1 < len(spans) else len(full_text)
        chunk = full_text[start:end].strip()

        # Split chunk: first line(s) = question, rest = answer
        lines = chunk.splitlines()
        q_lines = []
        a_lines = []
        in_answer = False
        for line in lines:
            if not in_answer and (BLANK_LINE_RE.search(line) or
                                   CHECKED_TEXT_RE.search(line) or
                                   UNCHECKED_TEXT_RE.search(line) or
                                   MC_OPTION_RE.search(line)):
                in_answer = True
            if in_answer:
                a_lines.append(line)
            else:
                q_lines.append(line)

        # If no split found, first line is question, rest is answer
        if not a_lines and len(lines) > 1:
            q_lines = [lines[0]]
            a_lines = lines[1:]

        question_text = ' '.join(q_lines).strip()
        # Strip the leading "Q1." or "1." prefix from question text
        question_text = re.sub(
            r'^(?:Q(?:uestion)?\s*\d+\s*[.:)]*\s*|\d+[.)]\s*)',
            '', question_text, flags=re.IGNORECASE
        ).strip()

        raw_answer = '\n'.join(a_lines).strip()
        answer_type = _infer_answer_type(raw_answer)

        blocks.append({
            'label': f'Q{int(num)}',
            'sort_order': int(num),
            'question_text': question_text,
            'raw_answer': raw_answer,
            'normalized_answer': _normalize_answer(
                raw_answer, answer_type, all_words, all_checkboxes
            ),
            'answer_type': answer_type,
        })

    return blocks


# ---------------------------------------------------------------------------
# Confidence estimation per question
# ---------------------------------------------------------------------------

def _compute_question_confidence(raw_answer: str, words: list[dict], answer_type: str) -> float:
    """
    Estimate confidence for a single question's answer using word-level data.
    Falls back to heuristics when word data is unavailable.
    """
    if not words:
        # Heuristic fallback
        if answer_type == 'text':
            return 0.88
        if answer_type == 'handwritten':
            return 0.75
        if answer_type in ('checkbox', 'multiple_choice'):
            return 0.90
        return 0.80

    # Match words from the answer against the word-level confidence list
    answer_tokens = set(re.findall(r'\w+', raw_answer.lower()))
    relevant = [
        w['confidence'] for w in words
        if w['text'].lower() in answer_tokens
    ]
    if relevant:
        return round(sum(relevant) / len(relevant), 4)

    # If no token match, use global average
    return round(sum(w['confidence'] for w in words) / len(words), 4)


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

def generate_schema(question_blocks: list[dict]) -> dict:
    """
    Build the schema dict from the first questionnaire's parsed questions.

    Schema format:
        {
          "Q1": {"question_text": "...", "answer_type": "text|checkbox|..."},
          ...
        }
    """
    schema = {}
    for block in question_blocks:
        schema[block['label']] = {
            'question_text': block['question_text'],
            'answer_type': block['answer_type'],
        }
    return schema


# ---------------------------------------------------------------------------
# Main processing function (called in background thread)
# ---------------------------------------------------------------------------

def process_questionnaire(questionnaire_id: int, file_path: str, app):
    """
    Full pipeline for one uploaded file.  Must be called inside an app context.
    """
    from models import db, Questionnaire, QuestionResponse, QuestionnaireSchema

    with app.app_context():
        q = Questionnaire.query.get(questionnaire_id)
        if not q:
            return

        q.status = 'processing'
        db.session.commit()

        api_key = app.config['GOOGLE_VISION_API_KEY']
        tesseract_cmd = app.config.get('TESSERACT_CMD')

        try:
            ext = Path(file_path).suffix.lower()
            image_paths = []
            own_images = False

            full_text = ''
            all_words = []        # [{text, confidence, cx, cy}, ...]  — for spatial matching
            all_checkboxes = []  # [{x, y, w, h, checked}, ...]       — from OpenCV
            method_used = 'google_vision'

            if ext == '.pdf':
                # ── Step 1: try extracting embedded text directly (fast path) ──
                direct_text, page_count = extract_text_from_pdf_direct(file_path)

                # A digital PDF typically has > 80 chars per page on average
                avg_chars = len(direct_text) / max(page_count, 1)
                if avg_chars > 80:
                    logger.info('Using direct PDF text extraction (%d pages, avg %.0f chars/page)',
                                page_count, avg_chars)
                    full_text = direct_text
                    all_words = []
                    method_used = 'pdf_direct'
                    q.page_count = page_count
                    q.current_page = page_count   # instant — mark all done
                    q.raw_ocr_text = full_text
                    q.ocr_method = method_used
                    db.session.commit()
                else:
                    # ── Step 2: scanned PDF → render each page → OCR ──
                    logger.info('PDF appears image-based (avg %.0f chars/page); rendering to images', avg_chars)
                    image_paths = pdf_to_images(file_path)
                    own_images = True

                    if not image_paths:
                        raise RuntimeError(
                            'Could not render PDF pages to images. '
                            'Ensure PyMuPDF is installed correctly (pip install PyMuPDF).'
                        )

                    q.page_count = len(image_paths)
                    q.current_page = 0
                    db.session.commit()

                    text_parts = []
                    method_used = 'google_vision'

                    # ── Batch OCR: send BATCH_SIZE pages per API call ──
                    from ocr_pipeline import (
                        run_google_vision_ocr_batch, run_tesseract_ocr,
                        detect_checkboxes_in_image, BATCH_SIZE,
                    )

                    for batch_start in range(0, len(image_paths), BATCH_SIZE):
                        batch = image_paths[batch_start:batch_start + BATCH_SIZE]
                        batch_results = run_google_vision_ocr_batch(batch, api_key)

                        for i, result in enumerate(batch_results):
                            global_idx = batch_start + i

                            if not result['success'] or not result.get('text', '').strip():
                                logger.info('Page %d: Vision empty, trying Tesseract', global_idx + 1)
                                result = run_tesseract_ocr(batch[i], tesseract_cmd)
                                result['method'] = 'tesseract'
                            else:
                                method_used = 'google_vision'

                            if result.get('text'):
                                text_parts.append(result['text'])
                            all_words.extend(result.get('words', []))
                            # Collect checkbox positions from every page
                            all_checkboxes.extend(detect_checkboxes_in_image(batch[i]))

                            q.current_page = global_idx + 1
                            db.session.commit()

                    full_text = '\n\n--- PAGE BREAK ---\n\n'.join(text_parts)
                    q.raw_ocr_text = full_text
                    q.ocr_method = method_used

            else:
                # ── Image file: single OCR call ──
                q.page_count = 1
                q.current_page = 0
                db.session.commit()

                ocr_result = extract_text_with_confidence(file_path, api_key, tesseract_cmd)
                full_text = ocr_result.get('text', '')
                all_words = ocr_result.get('words', [])
                all_checkboxes = ocr_result.get('checkboxes', [])
                method_used = ocr_result.get('method', 'google_vision')
                q.raw_ocr_text = full_text
                q.ocr_method = method_used
                q.current_page = 1
                db.session.commit()

            # Parse questions — pass spatial data so checkbox normaliser can use it
            question_blocks = parse_questions_from_text(full_text, all_words, all_checkboxes)

            if not question_blocks:
                q.status = 'review_pending'
                q.overall_accuracy = None
                q.error_message = 'No question structure (Q1/Q2…) detected in document.'
                db.session.commit()
                return

            # --- Schema: generate or load ---
            existing_schema = QuestionnaireSchema.query.order_by(
                QuestionnaireSchema.id.asc()
            ).first()

            if existing_schema is None:
                schema_fields = generate_schema(question_blocks)
                new_schema = QuestionnaireSchema()
                new_schema.set_fields(schema_fields)
                new_schema.source_questionnaire_id = questionnaire_id
                db.session.add(new_schema)
                db.session.flush()  # get the id

            # --- Persist question responses ---
            confidences = []
            typed_conf = []
            hw_conf = []
            cb_conf = []

            for block in question_blocks:
                conf = _compute_question_confidence(
                    block['raw_answer'], all_words, block['answer_type']
                )
                confidences.append(conf)
                if block['answer_type'] == 'text':
                    typed_conf.append(conf)
                elif block['answer_type'] == 'handwritten':
                    hw_conf.append(conf)
                elif block['answer_type'] in ('checkbox', 'multiple_choice'):
                    cb_conf.append(conf)

                resp = QuestionResponse(
                    questionnaire_id=questionnaire_id,
                    field_name=block['label'],
                    question_text=block['question_text'],
                    raw_answer=block['raw_answer'],
                    normalized_answer=block['normalized_answer'],
                    answer_type=block['answer_type'],
                    confidence=conf,
                    sort_order=block['sort_order'],
                )
                db.session.add(resp)

            # Accuracy aggregates
            q.overall_accuracy = round(sum(confidences) / len(confidences), 4) if confidences else None
            q.typed_accuracy = round(sum(typed_conf) / len(typed_conf), 4) if typed_conf else None
            q.handwritten_accuracy = round(sum(hw_conf) / len(hw_conf), 4) if hw_conf else None
            q.checkbox_accuracy = round(sum(cb_conf) / len(cb_conf), 4) if cb_conf else None
            q.status = 'review_pending'

            db.session.commit()

        except Exception as exc:
            logger.error('Processing failed for questionnaire %s: %s', questionnaire_id, exc, exc_info=True)
            try:
                q.status = 'failed'
                q.error_message = str(exc)
                db.session.commit()
            except Exception:
                db.session.rollback()

        finally:
            if own_images:
                for p in image_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
