import logging
import os

from waitress import serve
from print_center.app import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(create_app(), host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "18080")), threads=6,
          max_request_body_size=(int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024 + 65536))
