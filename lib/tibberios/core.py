import logging
from dataclasses import dataclass
from collections import Counter

from .tibber import *
from aiohttp import ClientSession

from os import remove


@dataclass
class Config:
    filepath: str  # Path to configuration file

    def __post_init__(self) -> None:
        from json import load

        # TODO: Allow for fetching from environment variables
        with open(self.filepath, "r") as f:
            self.config = load(f)
        self.tibber_api_key = self._try_fetch_config_key("TIBBER_API_KEY")
        self.database_path = self._try_fetch_config_key("DATABASE_PATH")

    def _try_fetch_config_key(self, key_name: str) -> str:
        try:
            return self.config[key_name]
        except:
            return None


@dataclass
class PriceData:
    historical_data: list[dict]  # Historical tibber API data
    current_data: dict[str, float]  # Today, current and tomorrow tibber API price data

    def __post_init__(self) -> None:
        self.price_table = self._convert_values()

    def _convert_values(self) -> list[tuple]:
        # assuming that historical values have the following schema:
        # (start_time, unit_price, total_cost, cost, consumption)
        # we model current values to be (start_time, unit_price, None, None, None)
        converted_current_data = []
        for start_time, unit_price in self.current_data.items():
            converted_current_data.append(
                (
                    start_time,
                    unit_price,
                    None,
                    None,
                    None,
                )
            )

        converted_historical_data = [
            tuple(row.values()) for row in self.historical_data
        ]
        converted_data = converted_current_data + converted_historical_data
        return self._keep_richest_duplicates(converted_data)

    def _keep_richest_duplicates(self, data: list[tuple]) -> list[tuple]:
        # extract start_times
        start_times = [row[0] for row in data]
        # get a count of occurrences to identify duplicates
        count_occurrences = dict(Counter(start_times))
        # filter to keep only duplicates
        duplicates = {
            start_time: {"missing_data": -1, "row": None}
            for start_time, occurrences in count_occurrences.items()
            if occurrences > 1
        }

        # find the row indices with duplicates with the fewest missing data points
        duplicates_tracker = {}
        row_num = 0
        for row in data:
            if row[0] in duplicates:
                missing_data = len([True for value in row if not value is None])
                if row[0] in duplicates_tracker.keys():
                    if duplicates_tracker[row[0]]["missing_data"] > missing_data:
                        duplicates_tracker[row[0]]["missing_data"] = missing_data
                        duplicates_tracker[row[0]]["row"] = row_num
                else:
                    duplicates_tracker[row[0]] = {
                        "missing_data": missing_data,
                        "row": row_num,
                    }

            row_num += 1

        # collect the richest rows from the duplicate data
        rich_data = []
        for _, info in duplicates_tracker.items():
            rich_data.append(data[info["row"]])

        # consolidate the rest of the data that were not duplicates
        non_duplicates = set(start_times) - set(duplicates.keys())
        for row in data:
            if row[0] in non_duplicates:
                rich_data.append(row)
        return rich_data


class TibberConnector:
    def __init__(self, tibber_api_key: str) -> None:
        self._access_token = tibber_api_key

    async def get_price_data(self, resolution: str, records: int) -> PriceData:
        # TODO: Allow for multiple homes
        async with ClientSession() as session:
            tibber_connection = Tibber(self._access_token, websession=session)
            await tibber_connection.update_info()
            home = tibber_connection.get_homes()[0]

            self.history = await home.get_historic_data(
                n_data=records,
                resolution=resolution,
            )

            await home.update_price_info()
            self.current_prices = home.price_total
        logging.debug(
            f"Tibber Graph Generator: Got {len(self.history)} past records and {len(self.current_prices)} records of current prices with resolution: {resolution}"
        )
        self.price_data = PriceData(
            historical_data=self.history, current_data=self.current_prices
        )
        return self.price_data


class Database:
    """For smoother SQLite3 database operations."""

    def __init__(self, filename: str) -> None:
        """An interface for performing common database operations on a SQLite3 database.

        Args
        ----
            filename: The path to the SQLite3 database file. Will get created if it doesn't exist.
        """
        from sqlite3 import connect

        self._database_path = filename
        self.connection = connect(database=self._database_path)

    def __del__(self) -> None:
        if hasattr(self, "connection") and self.connection:
            self.close()

    def delete_database(self) -> bool:
        """DESTROY THE DATABASE.

        Returns
        -------
            bool: True if the database was deleted, raises exception otherwise.
        """
        remove(self._database_path)
        return True

    def create_table(self, name: str, cols_n_types: dict) -> None:
        """Create a table in the database.

        Args
        ----
            name: The table name
            cols_n_types: A dictionary with keys as the column names and values as SQLite data types
        """
        query = f"""
            CREATE TABLE IF NOT EXISTS {name} (
                {','.join([c + " " + t for c, t in cols_n_types.items()])}
            );
        """
        cursor = self.connection
        cursor = cursor.execute(query)
        self.connection.commit()

    def get_latest_data(self, name: str, order: str, limit: int = 10) -> list[tuple]:
        """Get the latest values from a table.

        Args
        ----
            name: The table name
            order: The column name by which to order the results
            limit: The number of results to return

        Returns
        -------
            list: The latest data as queried from the database table,
                as a list of tuples where each row is represented in a tuple.
        """
        query = f"""
            SELECT *
            FROM {name}
            ORDER BY {order} DESC
            LIMIT {limit};
        """
        cursor = self.connection
        cursor = cursor.execute(query)
        return cursor.fetchall()

    def insert_table(self, name: str, columns: list[str], values: list[tuple]) -> None:
        """Insert values to a table

        Args
        ----
            name: The table name
            columns: The names of the columns in the table
            values: The values to insert in the table
        """
        query = f"""
            INSERT INTO {name} (
                {','.join(columns)}
            )
            VALUES ({','.join('?'*len(columns))})
        """
        cursor = self.connection
        cursor.executemany(query, values)
        self.connection.commit()

    def upsert_table(
        self, name: str, columns: list[str], values: list[tuple], pk: str
    ) -> None:
        """Upsert a table aka insert values and overwrite if pk already has values.

        Args
        ----
            name: The table name
            columns: The names of the columns in the table
            values: The values to insert in the table
            pk: The primary key of the table
        """
        n_cols = len(columns)
        for i, v in enumerate(values):
            assert n_cols == len(
                v
            ), f"Row {i} in received values contains {len(v)} values, expected {n_cols}"

        cols_to_update = set(columns) - set([pk])
        query = f"""
            INSERT INTO {name} (
                {','.join(columns)}
            )
            VALUES ({','.join('?'*len(columns))})
            ON CONFLICT ({pk}) DO UPDATE SET
                {','.join([f'{c} = excluded.{c}' for c in cols_to_update])}
            ;
        """
        cursor = self.connection
        cursor.executemany(query, values)
        self.connection.commit()

    def delete_null_rows(self, name: str, pk: str) -> None:
        """Delete rows where the pk

        Args
        ----
            name: The table name
            pk: The primary key of the table
        """
        query = f"""
            DELETE
            FROM {name}
            WHERE {pk} IS NULL
                OR trim({pk}) = '';
        """
        cursor = self.connection
        cursor.execute(query)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
