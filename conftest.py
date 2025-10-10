import sys
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "lib"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if LIB_DIR.exists() and str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

if "async_timeout" not in sys.modules:
    async_timeout_stub = types.ModuleType("async_timeout")

    class _AsyncTimeout:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def timeout(*args, **kwargs):
        return _AsyncTimeout()

    async_timeout_stub.timeout = timeout
    sys.modules["async_timeout"] = async_timeout_stub
