"""
logging_setup.py
Ботын log тохиргоо.

Өмнө нь бүх мэдээлэл `print`-ээр гардаг байсан тул:
  - цаг хугацааны тэмдэглэгээгүй (алдаа хэзээ болсныг мэдэх боломжгүй)
  - түвшин ялгаагүй (алдаа болон энгийн мэдээлэл ижил харагдана)
  - зөвхөн stdout руу — Railway лог эргүүлэн харах хугацаа хязгаартай

Одоо консол дээр өмнөх шигээ emoji-той мөр гарна, гэхдээ цаг, түвшин нэмэгдэнэ.
STATE_DIR persistent volume дээр байвал log файл руу ч бичнэ (эргэлдэх файлаар),
тиймээс redeploy хийсний дараа ч өмнөх түүх үлдэнэ.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FILE_NAME = "bot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
LOG_BACKUP_COUNT = 3


def _level_from_env():
    name = os.environ.get("LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def setup_logging(state_dir=None, persistent=False):
    """Root logger-ийг тохируулж, ашигласан log файлын замыг буцаана.

    state_dir нь persistent volume биш бол файл руу бичихгүй — түр зуурын
    дискэн дээр log хуримтлуулах нь зөвхөн зай эзэлнэ.
    """
    root = logging.getLogger()
    root.setLevel(_level_from_env())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console_fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(console_fmt)
    root.addHandler(console)

    if not (state_dir and persistent):
        return None

    try:
        path = os.path.join(state_dir, LOG_FILE_NAME)
        file_handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(file_handler)
        return path
    except Exception as e:
        root.warning(f"⚠️ Log файл нээгдсэнгүй ({state_dir}): {e}")
        return None


def get_logger(name="bot"):
    return logging.getLogger(name)
