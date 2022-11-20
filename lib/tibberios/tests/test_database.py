import unittest
from os import path
from sqlite3 import OperationalError

from tibberios.core import Database


class TestDatabaseMethods(unittest.TestCase):
    def setUp(self):
        self.db = Database("test_tibberios.db")
        self.name = "test"
        self.cols_n_types = {
            "col1": "REAL PRIMARY KEY",
            "col2": "REAL",
            "col3": "REAL",
        }
        self.columns = self.cols_n_types.keys()
        self.values = [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
        self.new_values = [(1, 3, 4), (4, 6, 7), (7, 9, 9)]
        self.pk = "col1"

    def test_database_connection(self):
        # make sure the database connection works
        with self.assertRaises(OperationalError):
            _ = self.db.get_latest_data(name="nonexistingtable", order="bad_column")

    def test_database_file_created(self):
        self.assertTrue(
            path.exists(self.db._database_path),
        )

    def test_database_operations(self):
        # table creation
        self.db.create_table(name=self.name, cols_n_types=self.cols_n_types)
        results = self.db.get_latest_data(name=self.name, order=self.pk)
        # table should be empty
        self.assertEqual(len(results), 0)

        # add data to table
        self.db.insert_table(name=self.name, columns=self.columns, values=self.values)
        results = self.db.get_latest_data(name=self.name, order=self.pk)
        self.assertEqual(len(self.values), len(results))

        # upsert table
        self.db.upsert_table(
            name=self.name, columns=self.columns, values=self.values, pk=self.pk
        )
        results = self.db.get_latest_data(name=self.name, order=self.pk)
        # table should have values
        self.assertEqual(len(results), len(self.values))

    def tearDown(self):
        self.db.delete_database()


if __name__ == "__main__":
    unittest.main()
