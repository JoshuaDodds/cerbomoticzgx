import sqlite3
from lib.constants import logging

class GlobalStateDatabase:
    def __init__(self):
        self.connection = sqlite3.connect("/dev/shm/cerbo_state.db", uri=True, check_same_thread=False)
        self.cursor = self.connection.cursor()
        self.cursor.execute("DROP TABLE IF EXISTS data")
        self.cursor.execute("CREATE TABLE IF NOT EXISTS data (key TEXT PRIMARY KEY, value TEXT)")
        self.connection.commit()
        logging.info("GlobalStateDatabase:  database initialized.")

    def __del__(self):
        self.stop()

    def stop(self):
        if self.connection:
            self.cursor.close()
            self.connection.close()
            logging.info("GlobalStateDatabase: Connection to Global state database closed.")


class GlobalStateClient:
    def __init__(self):
        self.connection = sqlite3.connect("/dev/shm/cerbo_state.db", uri=True, check_same_thread=False)
        self.cursor = self.connection.cursor()

    def __del__(self):
        if self.connection:
            self.cursor.close()
            self.connection.close()
            # logging.info("GlobalStateClient: Connection to Global state database closed.")

    def all(self):
        self.cursor.execute("SELECT key,value FROM data")
        result = self.cursor.fetchall()
        return result if result else None

    def get(self, key):
        self.cursor.execute("SELECT value FROM data WHERE key=?", (str(key),))
        result = self.cursor.fetchone()

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
            except Exception as e:
                return str(result_value)
        else:
            return 0

    def set(self, key, value):
        self.cursor.execute("INSERT OR REPLACE INTO data VALUES (?, ?)", (key, value))
        self.connection.commit()
