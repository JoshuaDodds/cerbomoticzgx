import threading
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import dotenv_values
from lib.config_paths import env_path as runtime_env_path
from lib.helpers import publish_message
from lib.constants import logging


RESTART_REQUIRED_ENV_KEYS = {
    "ACTIVE_MODULES",
    "WINTER_MODE",
    "APPLIANCE_OPTIMIZATION_ENABLED",
}
FIRST_WRITE_HANDLER_KEYS = {
    "WINTER_MODE",
    "APPLIANCE_OPTIMIZATION_ENABLED",
    "VICTRON_HARDWARE_MIN_SOC",
}


def handle_env_change(env_variable):
    """
    handle changes in .env variables
    """
    if env_variable in RESTART_REQUIRED_ENV_KEYS:
        logging.info(f"config_change_handler: This change requires a restart...")
        publish_message("Cerbomoticzgx/system/shutdown", message="True", retain=True)

    if env_variable == "SWITCH_TO_GRID_PRICE_THRESHOLD":
        from lib.energy_broker import manage_grid_usage_based_on_current_price

        logging.info(f"config_change_handler: Updating EnergyBroker...")
        manage_grid_usage_based_on_current_price()

    if env_variable == "VICTRON_HARDWARE_MIN_SOC":
        from lib.victron_integration import set_minimum_ess_soc

        logging.info("config_change_handler: Applying Victron hardware minimum SoC...")
        try:
            set_minimum_ess_soc(force=True)
        except Exception as error:
            # The watcher runs from a timer thread. A transient MQTT failure must
            # not terminate that thread; startup and each optimizer cycle also
            # reconcile this idempotent setting.
            logging.error(
                "config_change_handler: Failed to apply Victron hardware minimum "
                "SoC; startup/optimizer reconciliation will retry: %s",
                error,
            )


class ConfigWatcher(FileSystemEventHandler):
    """
    Watches the .env file for changes and triggers handlers for modified variables.
    """

    def __init__(self, env_file=None, handler=None, debounce_time=0.5):
        logging.info("ConfigWatcher: Initializing...")
        self.env_file = env_file or runtime_env_path()
        self.env_path = Path(self.env_file).expanduser().resolve()
        self.handler = handler
        self._cache = dotenv_values(str(self.env_path))  # Cache initial values
        self.observer = None
        self.thread = None
        self.debounce_time = debounce_time
        self.last_modified_time = 0

    def _matches_env_file(self, event) -> bool:
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        return any(p and Path(p).expanduser().resolve() == self.env_path for p in paths)

    def _debounced_check(self):
        now = time.time()
        if now - self.last_modified_time > self.debounce_time:
            self.last_modified_time = now
            threading.Timer(self.debounce_time, self.check_changes).start()

    def on_modified(self, event):
        if self._matches_env_file(event):
            self._debounced_check()

    def on_created(self, event):
        if self._matches_env_file(event):
            self._debounced_check()

    def on_moved(self, event):
        if self._matches_env_file(event):
            self._debounced_check()

    def check_changes(self):
        current_values = dotenv_values(str(self.env_path))
        for key, original_value in self._cache.items():
            current_value = current_values.get(key)

            # Skip transient `None` states unless it persists
            if current_value is None:
                continue

            if original_value != current_value:
                logging.info(f"Change detected for {key}: {original_value} -> {current_value}")
                if self.handler:
                    self.handler(key)
                self._cache[key] = current_value  # Update the cache

        # Add new keys if introduced
        for key in current_values.keys() - self._cache.keys():
            # These settings do not exist in older deployed .env files. Their
            # first dashboard write must still run the corresponding handler;
            # silently caching them would defer the intended restart/write.
            if key in FIRST_WRITE_HANDLER_KEYS and self.handler:
                self.handler(key)
            self._cache[key] = current_values[key]

    def start(self):
        """Start the observer in its own thread."""
        self.observer = Observer()
        watch_dir = str(self.env_path.parent)
        self.observer.schedule(self, watch_dir, recursive=False)

        # Start observer in a separate thread
        self.thread = threading.Thread(target=self._run_observer, daemon=True)
        self.thread.start()
        logging.info("ConfigWatcher: Started.")

    def _run_observer(self):
        """Internal method to run the observer."""
        self.observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.observer.stop()
        self.observer.join()

    def stop(self):
        """Stop the observer and thread."""
        logging.info("ConfigWatcher: Stopping...")
        if self.observer:
            self.observer.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join()
