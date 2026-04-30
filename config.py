import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'qex-dev-secret-2024')
    UPLOAD_FOLDER = str(BASE_DIR / 'uploads')
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{BASE_DIR / "questionnaires.db"}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Google Vision REST API key (existing key from repo)
    GOOGLE_VISION_API_KEY = os.environ.get(
        'GOOGLE_VISION_API_KEY',
        'AIzaSyDzGSBIG3dX6oyKVgsUmAzH0s597EWPAQg'
    )
    # Tesseract path — adjust if installed elsewhere
    TESSERACT_CMD = os.environ.get(
        'TESSERACT_CMD',
        r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    )
