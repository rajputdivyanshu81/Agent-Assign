from dotenv import load_dotenv
import os

# Load .env from project root (one level up from /backend)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import logging
import uuid

from db import engine, Base, async_session
from models import AgentRun, AgentStep, ExtractedResult
from agent_core import MinervaAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minerva_backend")

app = FastAPI(title="Minerva Agent Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """Validate env vars and initialize database tables on startup."""
    import os
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL environment variable is not set.")

    logger.info("Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully.")


@app.get("/")
def read_root():
    return {"status": "Minerva Agent Backend is running"}


# ---------------------------------------------------------------------------
# WebSocket Connection Manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        self.active_agents: dict[str, MinervaAgent] = {}

    async def connect(self, ws_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[ws_id] = websocket
        logger.info(f"Client connected: {ws_id}")

    def disconnect(self, ws_id: str):
        # Stop any running agent for this connection
        if ws_id in self.active_agents:
            self.active_agents[ws_id].stop()
            del self.active_agents[ws_id]
        if ws_id in self.active_connections:
            del self.active_connections[ws_id]
        logger.info(f"Client disconnected: {ws_id}")

    async def send_json(self, ws_id: str, data: dict):
        if ws_id in self.active_connections:
            try:
                await self.active_connections[ws_id].send_json(data)
            except Exception as e:
                logger.error(f"Failed to send to {ws_id}: {e}")


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Agent run orchestrator
# ---------------------------------------------------------------------------
async def run_agent(ws_id: str, goal: str, run_id: uuid.UUID):
    """Launch the real MinervaAgent and persist steps/results to DB."""

    async def ws_send_callback(data: dict):
        """Callback passed to the agent to stream events to this WS client."""
        await manager.send_json(ws_id, data)

        # Persist log steps to database (non-blocking — DB failure won't kill the agent)
        if data.get("type") == "log":
            try:
                async with async_session() as db:
                    step = AgentStep(
                        run_id=run_id,
                        step_number=data.get("step", 0),
                        step_type=data.get("step_type", "unknown"),
                        log_message=data.get("message", "")
                    )
                    db.add(step)
                    await db.commit()
            except Exception as e:
                logger.error(f"DB write failed for step (non-fatal): {e}")

    # Create the agent
    agent = MinervaAgent(ws_send_callback=ws_send_callback, run_id=str(run_id))
    manager.active_agents[ws_id] = agent

    # Save the run to DB
    try:
        async with async_session() as db:
            run = AgentRun(id=run_id, goal=goal, status="running")
            db.add(run)
            await db.commit()
    except Exception as e:
        logger.error(f"DB write failed for run creation (non-fatal): {e}")

    # Execute the agent
    await agent.run(goal)

    # Update final status in DB
    try:
        async with async_session() as db:
            run_db = await db.get(AgentRun, run_id)
            if run_db:
                run_db.status = agent.state.status

            # Save extracted results if any
            if agent.state.extracted_data:
                result = ExtractedResult(run_id=run_id, data=agent.state.extracted_data)
                db.add(result)

            await db.commit()
    except Exception as e:
        logger.error(f"DB write failed for final status (non-fatal): {e}")

    # Clean up agent reference
    if ws_id in manager.active_agents:
        del manager.active_agents[ws_id]


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_id = str(uuid.uuid4())
    await manager.connect(ws_id, websocket)

    current_task = None

    try:
        while True:
            raw_msg = await websocket.receive_text()
            logger.info(f"WS [{ws_id}]: {raw_msg}")

            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await manager.send_json(ws_id, {
                    "type": "error",
                    "message": "Invalid JSON format."
                })
                continue

            msg_type = msg.get("type")

            # --- START ---
            if msg_type == "start":
                goal = msg.get("goal", "").strip()
                if not goal or len(goal) < 10:
                    await manager.send_json(ws_id, {
                        "type": "error",
                        "message": "Goal must be at least 10 characters long."
                    })
                    continue

                if ws_id in manager.active_agents:
                    await manager.send_json(ws_id, {
                        "type": "error",
                        "message": "An agent run is already in progress. Stop it first."
                    })
                    continue

                run_id = uuid.uuid4()
                current_task = asyncio.create_task(run_agent(ws_id, goal, run_id))
                await manager.send_json(ws_id, {
                    "type": "status",
                    "status": "running",
                    "run_id": str(run_id)
                })

            # --- STOP ---
            elif msg_type == "stop":
                if ws_id in manager.active_agents:
                    manager.active_agents[ws_id].stop()
                    await manager.send_json(ws_id, {
                        "type": "status",
                        "status": "stopped"
                    })
                else:
                    await manager.send_json(ws_id, {
                        "type": "error",
                        "message": "No active agent to stop."
                    })

            # --- PAUSE ---
            elif msg_type == "pause":
                if ws_id in manager.active_agents:
                    manager.active_agents[ws_id].pause()

            # --- RESUME ---
            elif msg_type == "resume":
                if ws_id in manager.active_agents:
                    manager.active_agents[ws_id].resume()

            # --- APPROVE STEP ---
            elif msg_type == "approve":
                if ws_id in manager.active_agents:
                    manager.active_agents[ws_id].approve_step()

            # --- TOGGLE APPROVAL MODE ---
            elif msg_type == "set_approval_mode":
                if ws_id in manager.active_agents:
                    enabled = msg.get("enabled", False)
                    manager.active_agents[ws_id].set_approval_mode(enabled)

    except WebSocketDisconnect:
        manager.disconnect(ws_id)
        if current_task and not current_task.done():
            current_task.cancel()
