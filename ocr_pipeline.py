"""
Hybrid OCR pipeline: Google Cloud Vision (primary) → Tesseract (fallback).

Speed optimisations:
  - Batch API: up to BATCH_SIZE pages per Vision API request (fewer round trips)
  - JPEG output from PDF renderer (5× smaller payload vs PNG)
  - Configurable DPI (default 150 — good OCR quality, smaller images)
"""

import base64
import logging
import os

import requests

logger = logging.getLogger(__name__)

VISION_URL = 'https://vision.googleapis.com/v1/images:annotate?key={api_key}'
BATCH_SIZE = 8          # pages per Vision API call (max 16 per API limits)
RENDER_DPI = 150        # PDF→image resolution; 150 is sufficient for OCR
JPEG_QUALITY = 85       # JPEG quality for rendered pages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _image_to_b64(image_path: str) -> str:
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def _parse_vision_response(response: dict) -> dict:
    """Turn one Vision API response object into our standard result dict."""
    if 'error' in response:
        logger.warning('Vision API error: %s', response['error'])
        return {'success': False, 'text': '', 'confidence': 0.0, 'words': [], 'method': 'google_vision'}

    full_text_ann = response.get('fullTextAnnotation', {})
    full_text = full_text_ann.get('text', '')

    words = []
    for page in full_text_ann.get('pages', []):
        for block in page.get('blocks', []):
            for para in block.get('paragraphs', []):
                for word in para.get('words', []):
                    word_text = ''.join(
                        sym.get('text', '') for sym in word.get('symbols', [])
                    )
                    conf = word.get('confidence', 0.0)
                    if not word_text.strip():
                        continue
                    # Bounding box center — used for spatial checkbox matching
                    verts = word.get('boundingBox', {}).get('vertices', [])
                    if verts:
                        cx = sum(v.get('x', 0) for v in verts) / len(verts)
                        cy = sum(v.get('y', 0) for v in verts) / len(verts)
                    else:
                        cx = cy = 0
                    words.append({'text': word_text, 'confidence': conf, 'cx': cx, 'cy': cy})

    avg_conf = sum(w['confidence'] for w in words) / len(words) if words else 0.0
    return {
        'success': True,
        'text': full_text,
        'confidence': avg_conf,
        'words': words,
        'method': 'google_vision',
    }


# ---------------------------------------------------------------------------
# Google Vision — single image (kept for fallback / image uploads)
# ---------------------------------------------------------------------------

def run_google_vision_ocr(image_path: str, api_key: str) -> dict:
    try:
        payload = {
            'requests': [{
                'image': {'content': _image_to_b64(image_path)},
                'features': [{'type': 'DOCUMENT_TEXT_DETECTION', 'maxResults': 1}],
            }]
        }
        resp = requests.post(VISION_URL.format(api_key=api_key), json=payload, timeout=60)
        resp.raise_for_status()
        return _parse_vision_response(resp.json().get('responses', [{}])[0])
    except Exception as exc:
        logger.error('Vision OCR failed: %s', exc)
        return {'success': False, 'text': '', 'confidence': 0.0, 'words': [], 'method': 'google_vision'}


# ---------------------------------------------------------------------------
# Google Vision — BATCH (multiple pages in one API call)
# ---------------------------------------------------------------------------

def run_google_vision_ocr_batch(image_paths: list, api_key: str) -> list:
    """
    Send up to BATCH_SIZE images to Vision API in a single HTTP request.
    Returns one result dict per input image.
    """
    if not image_paths:
        return []

    try:
        payload = {
            'requests': [
                {
                    'image': {'content': _image_to_b64(p)},
                    'features': [{'type': 'DOCUMENT_TEXT_DETECTION', 'maxResults': 1}],
                }
                for p in image_paths
            ]
        }
        resp = requests.post(
            VISION_URL.format(api_key=api_key),
            json=payload,
            timeout=120,   # longer timeout for batch
        )
        resp.raise_for_status()
        responses = resp.json().get('responses', [])
        results = [_parse_vision_response(r) for r in responses]

        # Pad with empty results if API returned fewer than expected
        while len(results) < len(image_paths):
            results.append({'success': False, 'text': '', 'confidence': 0.0, 'words': [], 'method': 'google_vision'})

        return results

    except Exception as exc:
        logger.error('Vision batch OCR failed: %s', exc)
        # Return empty results for all pages in the batch
        return [{'success': False, 'text': '', 'confidence': 0.0, 'words': [], 'method': 'google_vision'}
                for _ in image_paths]


# ---------------------------------------------------------------------------
# Tesseract (fallback — used only if Vision fails for a page)
# ---------------------------------------------------------------------------

def run_tesseract_ocr(image_path: str, tesseract_cmd: str | None = None) -> dict:
    try:
        import pytesseract
        from PIL import Image
        import cv2
        import numpy as np

        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

        img = cv2.imread(image_path)
        if img is None:
            img_pil = Image.open(image_path).convert('RGB')
            img = np.array(img_pil)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        processed = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )

        data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT, config='--psm 6')
        full_text = pytesseract.image_to_string(processed, config='--psm 6')

        words = []
        for i, word_text in enumerate(data['text']):
            if word_text.strip():
                conf_raw = data['conf'][i]
                conf = max(0.0, float(conf_raw) / 100.0) if conf_raw != -1 else 0.5
                words.append({'text': word_text, 'confidence': conf})

        avg_conf = sum(w['confidence'] for w in words) / len(words) if words else 0.0
        return {'success': True, 'text': full_text, 'confidence': avg_conf, 'words': words, 'method': 'tesseract'}

    except Exception as exc:
        logger.error('Tesseract OCR failed: %s', exc)
        return {'success': False, 'text': '', 'confidence': 0.0, 'words': [], 'method': 'tesseract'}


# ---------------------------------------------------------------------------
# Checkbox detection (image-based)
# ---------------------------------------------------------------------------

def detect_checkboxes_in_image(image_path: str) -> list:
    try:
        import cv2
        import numpy as np

        img = cv2.imread(image_path)
        if img is None:
            return []

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(
            cv2.GaussianBlur(gray, (5, 5), 0), 0, 255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        checkboxes = []
        img_h, img_w = gray.shape[:2]
        min_sz = max(10, int(min(img_h, img_w) * 0.01))
        max_sz = int(min(img_h, img_w) * 0.08)

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if not (min_sz < w < max_sz and min_sz < h < max_sz):
                continue
            if not (0.6 < w / h < 1.4):
                continue
            roi = thresh[y:y+h, x:x+w]
            filled = np.count_nonzero(roi) / (w * h) if w * h > 0 else 0
            checked = filled > 0.25
            checkboxes.append({
                'x': x, 'y': y, 'w': w, 'h': h,
                'checked': checked,
                'confidence': round(min(1.0, filled * 2) if checked else min(1.0, (1 - filled) * 2), 3),
            })
        return checkboxes
    except Exception as exc:
        logger.error('Checkbox detection failed: %s', exc)
        return []


# ---------------------------------------------------------------------------
# Single-image entry point (used for non-PDF uploads)
# ---------------------------------------------------------------------------

def extract_text_with_confidence(image_path: str, api_key: str, tesseract_cmd: str | None = None) -> dict:
    result = run_google_vision_ocr(image_path, api_key)

    if not result['success'] or not result['text'].strip():
        logger.info('Falling back to Tesseract for %s', image_path)
        result = run_tesseract_ocr(image_path, tesseract_cmd)

    result['checkboxes'] = detect_checkboxes_in_image(image_path)
    return result
