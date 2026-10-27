import json
import logging

from app.logging import configure_logging


def test_logs_are_json_lines(capsys):
    configure_logging()
    logging.getLogger("app").info("hello", extra={"memory_id": "m1"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO" and payload["memory_id"] == "m1"
