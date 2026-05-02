import sys
import types
from importlib.util import find_spec


if find_spec("fastapi") is None:
    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get(self, *args, **kwargs):
            return lambda func: func

        def websocket(self, *args, **kwargs):
            return lambda func: func

    class WebSocket:
        pass

    class WebSocketDisconnect(Exception):
        pass

    fastapi.FastAPI = FastAPI
    fastapi.WebSocket = WebSocket
    fastapi.WebSocketDisconnect = WebSocketDisconnect
    sys.modules["fastapi"] = fastapi

if find_spec("httpx") is None:
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))

if find_spec("websockets") is None:
    websockets = types.ModuleType("websockets")
    websocket_exceptions = types.ModuleType("websockets.exceptions")

    class ConnectionClosed(Exception):
        pass

    websocket_exceptions.ConnectionClosed = ConnectionClosed
    websockets.exceptions = websocket_exceptions
    sys.modules["websockets"] = websockets
    sys.modules["websockets.exceptions"] = websocket_exceptions
