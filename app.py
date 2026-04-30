"""
Questionnaire Intelligence System — Flask application.

User flow:
  1. Upload  → file saved, status='pending'
  2. Run     → OCR fires in background, status='processing' → 'review_pending'
  3. Review  → user sees extracted Q&A, can edit each answer
  4. Accept  → edits saved, status='completed' (visible in Results)
"""

import csv
import io
import json
import os
import threading
import uuid
from werkzeug.utils import secure_filename

from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, send_file, url_for,
)
from flask_cors import CORS

from config import Config
from models import db, Questionnaire, QuestionResponse, QuestionnaireSchema
from questionnaire_processor import process_questionnaire


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    CORS(app)          # allow Android app to call the API
    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Add current_page column to existing DBs (safe to run repeatedly)
        try:
            from sqlalchemy import text
            with db.engine.connect() as conn:
                conn.execute(text(
                    'ALTER TABLE questionnaires ADD COLUMN current_page INTEGER DEFAULT 0'
                ))
                conn.commit()
        except Exception:
            pass  # column already exists

    _register_routes(app)
    return app


def _allowed_file(filename: str, allowed: set) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: Flask):

    # ── Index / Upload ──────────────────────────────────────────────────────

    @app.route('/', methods=['GET'])
    def index():
        schema = QuestionnaireSchema.query.order_by(QuestionnaireSchema.id.asc()).first()
        total = Questionnaire.query.count()
        completed = Questionnaire.query.filter_by(status='completed').count()
        return render_template('index.html', schema=schema, total=total, completed=completed)

    @app.route('/upload', methods=['POST'])
    def upload():
        if 'file' not in request.files:
            flash('No file selected.', 'danger')
            return redirect(url_for('index'))

        file = request.files['file']
        if not file or file.filename == '':
            flash('No file selected.', 'danger')
            return redirect(url_for('index'))

        allowed = app.config['ALLOWED_EXTENSIONS']
        if not _allowed_file(file.filename, allowed):
            flash(f'Unsupported file type. Allowed: {", ".join(sorted(allowed))}.', 'danger')
            return redirect(url_for('index'))

        original_name = secure_filename(file.filename)
        if not original_name:
            original_name = file.filename
        unique_name = f'{uuid.uuid4().hex}_{original_name}'
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
        file.save(save_path)

        q = Questionnaire(
            filename=unique_name,
            original_filename=original_name,
            status='processing',      # auto-start immediately
        )
        db.session.add(q)
        db.session.commit()

        # Auto-start OCR in background thread
        t = threading.Thread(
            target=process_questionnaire,
            args=(q.id, save_path, app),
            daemon=True,
        )
        t.start()

        # Send user to a live status page for this questionnaire
        return redirect(url_for('processing_status', qid=q.id))

    # ── Live processing status page ─────────────────────────────────────────

    @app.route('/processing/<int:qid>')
    def processing_status(qid):
        q = Questionnaire.query.get_or_404(qid)
        return render_template('processing.html', q=q)

    # ── Retry / re-run extraction ────────────────────────────────────────────

    @app.route('/run/<int:qid>', methods=['POST'])
    def run_extraction(qid):
        q = Questionnaire.query.get_or_404(qid)
        if q.status not in ('pending', 'failed'):
            flash('Can only retry failed or pending questionnaires.', 'warning')
            return redirect(url_for('results'))

        file_path = os.path.join(app.config['UPLOAD_FOLDER'], q.filename)
        if not os.path.exists(file_path):
            flash('Uploaded file not found on disk. Please re-upload.', 'danger')
            return redirect(url_for('results'))

        QuestionResponse.query.filter_by(questionnaire_id=qid).delete()
        q.status = 'processing'
        q.error_message = None
        db.session.commit()

        t = threading.Thread(
            target=process_questionnaire,
            args=(qid, file_path, app),
            daemon=True,
        )
        t.start()

        return redirect(url_for('processing_status', qid=qid))

    # ── Results dashboard ───────────────────────────────────────────────────

    @app.route('/results')
    def results():
        schema = QuestionnaireSchema.query.order_by(QuestionnaireSchema.id.asc()).first()
        questionnaires = Questionnaire.query.order_by(Questionnaire.uploaded_at.desc()).all()
        columns = schema.field_keys_sorted() if schema else []

        response_map = {}
        for q in questionnaires:
            response_map[q.id] = {r.field_name: r for r in q.responses}

        return render_template(
            'results.html',
            questionnaires=questionnaires,
            columns=columns,
            response_map=response_map,
            schema=schema,
        )

    # ── Status polling (AJAX) ───────────────────────────────────────────────

    @app.route('/status/<int:qid>')
    def status(qid):
        q = Questionnaire.query.get_or_404(qid)
        pct = 0
        if q.page_count and q.page_count > 0:
            pct = round((q.current_page / q.page_count) * 100)
        return jsonify({
            'id': q.id,
            'status': q.status,
            'page_count': q.page_count,
            'current_page': q.current_page,
            'progress_pct': pct,
            'overall_accuracy': q.overall_accuracy,
            'error_message': q.error_message,
        })

    # ── Review extracted answers ────────────────────────────────────────────

    @app.route('/results/<int:qid>/review')
    def questionnaire_review(qid):
        q = Questionnaire.query.get_or_404(qid)
        if q.status not in ('review_pending', 'completed'):
            flash('Extraction must complete before reviewing.', 'warning')
            return redirect(url_for('results'))

        responses = (
            QuestionResponse.query
            .filter_by(questionnaire_id=qid)
            .order_by(QuestionResponse.sort_order)
            .all()
        )
        schema = QuestionnaireSchema.query.order_by(QuestionnaireSchema.id.asc()).first()
        schema_fields = schema.get_fields() if schema else {}

        return render_template(
            'review.html',
            q=q,
            responses=responses,
            schema_fields=schema_fields,
        )

    # ── Accept (save to DB as completed) ───────────────────────────────────

    @app.route('/results/<int:qid>/accept', methods=['POST'])
    def questionnaire_accept(qid):
        q = Questionnaire.query.get_or_404(qid)

        # Persist any user edits from the review form
        for resp in q.responses:
            edited = request.form.get(f'answer_{resp.id}')
            if edited is not None:
                resp.normalized_answer = edited.strip()

        q.status = 'completed'
        db.session.commit()

        flash(f'"{q.original_filename}" accepted and saved to the database.', 'success')
        return redirect(url_for('questionnaire_detail', qid=qid))

    # ── Questionnaire detail (completed only) ───────────────────────────────

    @app.route('/results/<int:qid>')
    def questionnaire_detail(qid):
        q = Questionnaire.query.get_or_404(qid)
        responses = (
            QuestionResponse.query
            .filter_by(questionnaire_id=qid)
            .order_by(QuestionResponse.sort_order)
            .all()
        )
        return render_template('questionnaire_detail.html', q=q, responses=responses)

    # ── Schema management ───────────────────────────────────────────────────

    @app.route('/schema')
    def schema_view():
        schema = QuestionnaireSchema.query.order_by(QuestionnaireSchema.id.asc()).first()
        return render_template('schema.html', schema=schema)

    @app.route('/schema/reset', methods=['POST'])
    def schema_reset():
        QuestionnaireSchema.query.delete()
        db.session.commit()
        flash('Schema cleared. The next processed questionnaire will generate a new one.', 'info')
        return redirect(url_for('schema_view'))

    # ── Export CSV ──────────────────────────────────────────────────────────

    @app.route('/export/csv')
    def export_csv():
        schema = QuestionnaireSchema.query.order_by(QuestionnaireSchema.id.asc()).first()
        questionnaires = Questionnaire.query.filter_by(status='completed').all()
        columns = schema.field_keys_sorted() if schema else []

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ['ID', 'Filename', 'Uploaded At', 'OCR Method', 'Overall Accuracy (%)'] + columns
        )
        for q in questionnaires:
            resp_map = {r.field_name: r.normalized_answer for r in q.responses}
            writer.writerow([
                q.id,
                q.original_filename,
                q.uploaded_at.strftime('%Y-%m-%d %H:%M'),
                q.ocr_method or '',
                q.accuracy_pct() or '',
            ] + [resp_map.get(col, '') for col in columns])

        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            mimetype='text/csv',
            as_attachment=True,
            download_name='questionnaire_results.csv',
        )

    # ── Export JSON ─────────────────────────────────────────────────────────

    @app.route('/export/json')
    def export_json():
        questionnaires = Questionnaire.query.filter_by(status='completed').all()
        out = []
        for q in questionnaires:
            out.append({
                'id': q.id,
                'filename': q.original_filename,
                'uploaded_at': q.uploaded_at.isoformat(),
                'ocr_method': q.ocr_method,
                'overall_accuracy': q.overall_accuracy,
                'responses': {
                    r.field_name: {
                        'answer': r.normalized_answer,
                        'type': r.answer_type,
                        'confidence': r.confidence,
                    }
                    for r in q.responses
                },
            })
        buf = io.BytesIO(json.dumps(out, indent=2, ensure_ascii=False).encode('utf-8'))
        return send_file(
            buf, mimetype='application/json',
            as_attachment=True, download_name='questionnaire_results.json',
        )

    # ── Delete questionnaire ────────────────────────────────────────────────

    @app.route('/delete/<int:qid>', methods=['POST'])
    def delete_questionnaire(qid):
        q = Questionnaire.query.get_or_404(qid)
        try:
            fp = os.path.join(app.config['UPLOAD_FOLDER'], q.filename)
            if os.path.exists(fp):
                os.remove(fp)
        except OSError:
            pass
        db.session.delete(q)
        db.session.commit()
        flash(f'Deleted "{q.original_filename}".', 'info')
        return redirect(url_for('results'))

    # ── Raw OCR text ────────────────────────────────────────────────────────

    @app.route('/results/<int:qid>/raw')
    def questionnaire_raw(qid):
        q = Questionnaire.query.get_or_404(qid)
        return render_template('raw_ocr.html', q=q)

    # =========================================================================
    # Mobile API  (called by Android app)
    # =========================================================================

    @app.route('/api/scan-upload', methods=['POST'])
    def api_scan_upload():
        """
        Receive one or more page images from the Android scanner.
        Each page is a field named 'page' (repeatable).
        Returns JSON {id, status, pages}.
        """
        pages = request.files.getlist('page')
        if not pages:
            return jsonify({'error': 'No pages received'}), 400

        try:
            from PIL import Image as PILImage

            images = []
            for f in pages:
                img = PILImage.open(f.stream).convert('RGB')
                images.append(img)

            # Stack all pages vertically into one tall image
            max_w = max(im.width for im in images)
            total_h = sum(im.height for im in images)
            combined = PILImage.new('RGB', (max_w, total_h), (255, 255, 255))
            y_off = 0
            for im in images:
                combined.paste(im, (0, y_off))
                y_off += im.height

            unique_name = f'{uuid.uuid4().hex}_mobile_scan.jpg'
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
            combined.save(save_path, 'JPEG', quality=90)

            q = Questionnaire(
                filename=unique_name,
                original_filename=f'Mobile Scan ({len(images)} page(s))',
                status='processing',
            )
            db.session.add(q)
            db.session.commit()

            t = threading.Thread(
                target=process_questionnaire,
                args=(q.id, save_path, app),
                daemon=True,
            )
            t.start()

            return jsonify({'id': q.id, 'status': 'processing', 'pages': len(images)})

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/status/<int:qid>')
    def api_status(qid):
        """JSON status — used by Android polling."""
        q = Questionnaire.query.get_or_404(qid)
        pct = 0
        if q.page_count and q.page_count > 0:
            pct = round((q.current_page / q.page_count) * 100)
        return jsonify({
            'id': q.id,
            'status': q.status,
            'progress_pct': pct,
            'overall_accuracy': q.overall_accuracy,
            'error_message': q.error_message,
        })

    @app.route('/api/results/<int:qid>')
    def api_results(qid):
        """Full JSON results — used by Android to show extracted answers."""
        q = Questionnaire.query.get_or_404(qid)
        return jsonify({
            'id': q.id,
            'status': q.status,
            'overall_accuracy': q.overall_accuracy,
            'responses': [
                {
                    'field': r.field_name,
                    'question': r.question_text,
                    'answer': r.normalized_answer,
                    'type': r.answer_type,
                    'confidence': r.confidence,
                }
                for r in sorted(q.responses, key=lambda x: x.sort_order)
            ],
        })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

app = create_app()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
