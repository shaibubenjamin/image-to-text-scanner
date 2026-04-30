import json
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class QuestionnaireSchema(db.Model):
    """Stores the auto-generated schema from the first processed questionnaire."""
    __tablename__ = 'questionnaire_schemas'

    id = db.Column(db.Integer, primary_key=True)
    # JSON: {"Q1": {"question_text": "...", "answer_type": "text|checkbox|multiple_choice"}, ...}
    fields_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    source_questionnaire_id = db.Column(db.Integer, nullable=True)
    field_count = db.Column(db.Integer, default=0)

    def get_fields(self):
        try:
            return json.loads(self.fields_json)
        except Exception:
            return {}

    def set_fields(self, fields_dict):
        self.fields_json = json.dumps(fields_dict, ensure_ascii=False)
        self.field_count = len(fields_dict)

    def field_keys_sorted(self):
        fields = self.get_fields()
        def sort_key(k):
            import re
            m = re.search(r'\d+', k)
            return int(m.group()) if m else 9999
        return sorted(fields.keys(), key=sort_key)


class Questionnaire(db.Model):
    """One uploaded questionnaire file."""
    __tablename__ = 'questionnaires'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), default='pending')  # pending|processing|completed|failed
    page_count = db.Column(db.Integer, default=0)
    current_page = db.Column(db.Integer, default=0)   # pages processed so far
    overall_accuracy = db.Column(db.Float, nullable=True)
    typed_accuracy = db.Column(db.Float, nullable=True)
    handwritten_accuracy = db.Column(db.Float, nullable=True)
    checkbox_accuracy = db.Column(db.Float, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    raw_ocr_text = db.Column(db.Text, nullable=True)
    ocr_method = db.Column(db.String(50), nullable=True)  # google_vision|tesseract|hybrid

    responses = db.relationship(
        'QuestionResponse',
        backref='questionnaire',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='QuestionResponse.sort_order'
    )

    def accuracy_pct(self):
        if self.overall_accuracy is None:
            return None
        return round(self.overall_accuracy * 100, 1)

    def accuracy_color(self):
        a = self.overall_accuracy or 0
        if a >= 0.90:
            return 'success'
        if a >= 0.75:
            return 'warning'
        return 'danger'


class QuestionResponse(db.Model):
    """One Q&A pair extracted from a questionnaire."""
    __tablename__ = 'question_responses'

    id = db.Column(db.Integer, primary_key=True)
    questionnaire_id = db.Column(
        db.Integer, db.ForeignKey('questionnaires.id'), nullable=False
    )
    field_name = db.Column(db.String(50))   # e.g. "Q1"
    question_text = db.Column(db.Text)
    raw_answer = db.Column(db.Text)          # straight OCR output
    normalized_answer = db.Column(db.Text)   # cleaned/post-processed
    answer_type = db.Column(db.String(50))   # text|handwritten|checkbox|multiple_choice
    confidence = db.Column(db.Float)         # 0.0 – 1.0
    page_number = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)

    def confidence_pct(self):
        if self.confidence is None:
            return None
        return round(self.confidence * 100, 1)

    def confidence_color(self):
        c = self.confidence or 0
        if c >= 0.90:
            return 'success'
        if c >= 0.75:
            return 'warning'
        return 'danger'

    def answer_type_badge(self):
        badges = {
            'text': 'primary',
            'handwritten': 'info',
            'checkbox': 'secondary',
            'multiple_choice': 'dark',
        }
        return badges.get(self.answer_type, 'light')
