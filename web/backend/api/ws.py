import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from services import jobs

logger = logging.getLogger(__name__)

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, analysis_id: str):
        await websocket.accept()
        self.active_connections.setdefault(analysis_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, analysis_id: str):
        conns = self.active_connections.get(analysis_id)
        if conns is not None:
            conns.discard(websocket)
            if not conns:
                del self.active_connections[analysis_id]

    async def broadcast(self, message: dict, analysis_id: str):
        conns = self.active_connections.get(analysis_id)
        if not conns:
            return
        for websocket in list(conns):
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error sending ws message to {analysis_id}: {e}")
                self.disconnect(websocket, analysis_id)


manager = ConnectionManager()


@router.websocket("/progress/{analysis_id}")
async def websocket_endpoint(websocket: WebSocket, analysis_id: str):
    await manager.connect(websocket, analysis_id)
    try:
        for event in await jobs.get_events(analysis_id):
            await websocket.send_json(event)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, analysis_id)
