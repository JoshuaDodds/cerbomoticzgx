import requests

from lib.constants import PushOverConfig, logging


def pushover_notification(topic: str, msg: str) -> bool:
    _id = PushOverConfig.get("id")
    _key = PushOverConfig.get("key")

    msg = f"{topic}: {msg}"

    payload = {"message": msg, "user": _id, "token": _key}
    try:
        _req = requests.post('https://api.pushover.net/1/messages.json', data=payload, headers={'User-Agent': 'CerbomoticzGx'})
    except Exception as e:
        logging.info(f"lib.notifications error: {e}")
