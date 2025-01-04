import requests

from lib.constants import logging, PushOverConfig


def pushover_notification(topic: str, msg: str) -> bool:
    _id = PushOverConfig.get("id")
    _key = PushOverConfig.get("key")

    msg = f"{topic}: {msg}"

    payload = {"message": msg, "user": _id, "token": _key}
    try:
        _req = requests.post('https://api.pushover.net/1/messages.json', data=payload, headers={'User-Agent': 'CerbomoticzGx'})
    except Exception as e:
        logging.info(f"lib.notifications error: {e}")


def pushover_notification_critical(topic: str, msg: str) -> bool:
    """
    Sends a critical priority Pushover notification with static parameters.

    Parameters:
        topic (str): The topic of the notification.
        msg (str): The message content.

    Returns:
        bool: True if the notification was successfully sent, False otherwise.
    """
    _id = PushOverConfig.get("id")
    _key = PushOverConfig.get("key")

    payload = {
        "message": f"{topic}: {msg}",
        "user": _id,
        "token": _key,
        "priority": 2,  # Critical priority
        "title": "Critical Energy Alert",  # Static title
        "url": "http://192.168.1.163/app",  # Static URL
        "url_title": "Go to Dashboard",  # Static URL title
        "sound": "my_siren",  # TODO: Does not work for some reason
        "retry": 30,  # Retry interval in seconds
        "expire": 3600,  # Expires after 1 hour
    }

    try:
        response = requests.post(
            'https://api.pushover.net/1/messages.json',
            json=payload,  # Send as JSON
            headers={'User-Agent': 'CerbomoticzGx', 'Content-Type': 'application/json'}
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logging.error(f"lib.notifications error: {e}")
        return False

