import sqlite3
import logging

from lib.helpers import publish_message, reduce_decimal

logging.basicConfig(
    format='%(asctime)s cerbomoticzGx: %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')


class SQLiteConnection:
    def __init__(self, path):
        self.path = path
        self.connection = None
        self.cursor = None

    def __enter__(self):
        self.connection = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        self.cursor = self.connection.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.cursor.close()
            self.connection.close()
            logging.debug("SQLiteConnection: Connection to database closed.")


class GlobalStateDatabase:
    def __init__(self):
        self.db_path = "/dev/shm/cerbo_state.db"
        self.init_database()

    def init_database(self):
        with SQLiteConnection(self.db_path) as cursor:
            cursor.execute("DROP TABLE IF EXISTS data")
            cursor.execute("CREATE TABLE IF NOT EXISTS data (key TEXT PRIMARY KEY, value TEXT)")
            cursor.connection.commit()
            logging.info("GlobalStateDatabase: database initialized.")

    def export_to_file(self, export_path):
        """Export the in-memory SQLite database to a file."""
        try:
            with SQLiteConnection(self.db_path) as cursor:
                with sqlite3.connect(export_path) as file_conn:
                    cursor.connection.backup(file_conn)
                    logging.info(f"GlobalStateDatabase: Exported to {export_path}.")
        except Exception as e:
            logging.error(f"GlobalStateDatabase: Failed to export database - {e}")

    def import_from_file(self, import_path):
        """Import a SQLite database file into the in-memory database."""
        try:
            with sqlite3.connect(import_path) as file_conn:
                with SQLiteConnection(self.db_path) as cursor:
                    file_conn.backup(cursor.connection)
                    logging.info(f"GlobalStateDatabase: Imported from {import_path}.")
        except Exception as e:
            logging.error(f"GlobalStateDatabase: Failed to import database - {e}")


class GlobalStateClient:
    @staticmethod
    def all():
        with SQLiteConnection("/dev/shm/cerbo_state.db") as cursor:
            cursor.execute("SELECT key,value FROM data")
            result = cursor.fetchall()
            return result if result else None

    @staticmethod
    def get(key):
        with SQLiteConnection("/dev/shm/cerbo_state.db") as cursor:
            cursor.execute("SELECT value FROM data WHERE key=?", (str(key),))
            result = cursor.fetchone()

            if result:
                result_value = result[0]
                try:
                    if '.' in result_value:
                        return float(result_value)
                    elif "True" in str(result_value):
                        return bool(True)
                    elif "False" in str(result_value):
                        return bool(False)
                    else:
                        return int(result_value)
                except Exception as e: # noqa
                    return str(result_value)
            else:
                return 0

    @staticmethod
    def set(key, value):
        _value = reduce_decimal(value)

        with SQLiteConnection("/dev/shm/cerbo_state.db") as cursor:
            cursor.execute("INSERT OR REPLACE INTO data VALUES (?, ?)", (key, _value))
            publish_message(f"Cerbomoticzgx/GlobalState/{key}", message=_value, retain=True)
            cursor.connection.commit()
