import logging
import sys

import uvicorn
from pydantic import ValidationError

from medllmbot.admin.app import create_app
from medllmbot.config import AdminSettings, describe_settings_error
from medllmbot.db.migrate import upgrade_db

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        settings = AdminSettings.from_env()
    except ValidationError as error:
        sys.exit(describe_settings_error(error))
    upgrade_db(settings.db_path)
    uvicorn.run(create_app(settings), host=settings.admin_host, port=settings.admin_port)
