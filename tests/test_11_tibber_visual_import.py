import importlib


def test_embedded_tibber_client_imports():
    importlib.import_module("lib.tibberios.tibber")
