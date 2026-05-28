import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict

import logging

logger = logging.getLogger(__name__)

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, analysis_id: str):
        await websocket.accept()
        self.active_connections[analysis_id] = websocket

    def disconnect(self, analysis_id: str):
        if analysis_id in self.active_connections:
            del self.active_connections[analysis_id]

    async def send_personal_message(self, message: dict, analysis_id: str):
        if analysis_id in self.active_connections:
            websocket = self.active_connections[analysis_id]
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error sending ws message to {analysis_id}: {e}")
                self.disconnect(analysis_id)

manager = ConnectionManager()

@router.websocket("/progress/{analysis_id}")
async def websocket_endpoint(websocket: WebSocket, analysis_id: str):
    await manager.connect(websocket, analysis_id)
    try:
        while True:
            # Keep connection alive, wait for client messages if any
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(analysis_id)
