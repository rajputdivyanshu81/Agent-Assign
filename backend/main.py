from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import asyncio
import json
import logging
from db import engine, Base, async_session
from models import AgentRun, AgentStep, ExtractedResult
import uuid

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minerva_backend")

app = FastAPI(title="Minerva Agent Backend")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully.")

@app.get("/")
def read_root():
    return {"status": "Backend is running"}

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, ws_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[ws_id] = websocket
        logger.info(f"WebSocket client connected: {ws_id}")

    def disconnect(self, ws_id: str):
        if ws_id in self.active_connections:
            del self.active_connections[ws_id]
            logger.info(f"WebSocket client disconnected: {ws_id}")

    async def send_json(self, ws_id: str, data: dict):
        if ws_id in self.active_connections:
            await self.active_connections[ws_id].send_json(data)

manager = ConnectionManager()

async def simulate_agent_run(ws_id: str, goal: str, run_id: uuid.UUID):
    """
    Simulates the agent execution to verify WebSocket flow and steering controls.
    """
    logger.info(f"Starting simulated agent run {run_id} for goal: {goal}")
    steps = [
        {"type": "plan", "msg": "Parsed goal. Research path: 1. Search Google, 2. Visit Slack pricing page, 3. Visit Notion pricing page."},
        {"type": "think", "msg": "Searching Google for 'Slack pricing plans'"},
        {"type": "action", "msg": "Navigating to: https://www.google.com"},
        {"type": "think", "msg": "Typing 'Slack pricing plans' in search box"},
        {"type": "action", "msg": "Clicking first result: https://slack.com/pricing"},
        {"type": "think", "msg": "Extracting pricing cards from Slack page"},
        {"type": "done", "msg": "Agent task finished successfully."}
    ]
    
    # Save the run to DB
    async with async_session() as db:
        run = AgentRun(id=run_id, goal=goal, status="running")
        db.add(run)
        await db.commit()

    try:
        for idx, step in enumerate(steps):
            # Check pause/stop states here when we wire them up in Phase 3
            await asyncio.sleep(2.0)
            
            logger.info(f"Simulating step {idx}: {step['type']} - {step['msg']}")
            
            # Send live updates to frontend
            await manager.send_json(ws_id, {
                "type": "log",
                "step": idx + 1,
                "step_type": step["type"],
                "message": step["msg"]
            })
            
            # Save step to DB
            async with async_session() as db:
                db_step = AgentStep(
                    run_id=run_id,
                    step_number=idx + 1,
                    step_type=step["type"],
                    log_message=step["msg"]
                )
                db.add(db_step)
                await db.commit()

        # Save mock results to DB
        mock_result = {
            "slack": {"Free": "$0", "Pro": "$7.25/mo", "Business+": "$12.50/mo"},
            "notion": {"Free": "$0", "Plus": "$8/mo", "Business": "$15/mo"}
        }
        async with async_session() as db:
            result = ExtractedResult(run_id=run_id, data=mock_result)
            db.add(result)
            # Update status
            run_db = await db.get(AgentRun, run_id)
            if run_db:
                run_db.status = "completed"
            await db.commit()

        await manager.send_json(ws_id, {
            "type": "result",
            "data": mock_result
        })
        
    except Exception as e:
        logger.error(f"Error in simulated agent run: {e}")
        async with async_session() as db:
            run_db = await db.get(AgentRun, run_id)
            if run_db:
                run_db.status = "error"
            await db.commit()
        await manager.send_json(ws_id, {
            "type": "status",
            "status": "error",
            "message": str(e)
        })

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_id = str(uuid.uuid4())
    await manager.connect(ws_id, websocket)
    
    current_run_id = None
    
    try:
        while True:
            raw_msg = await websocket.receive_text()
            logger.info(f"Received raw WebSocket message from {ws_id}: {raw_msg}")
            
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON format"})
                continue
                
            msg_type = msg.get("type")
            if msg_type == "start":
                goal = msg.get("goal")
                if not goal:
                    await websocket.send_json({"type": "error", "message": "Goal is required"})
                    continue
                
                # Check for active conflict
                if current_run_id is not None:
                    await websocket.send_json({"type": "error", "message": "An agent run is already in progress for this session"})
                    continue
                
                current_run_id = uuid.uuid4()
                # Start simulated background run
                asyncio.create_task(simulate_agent_run(ws_id, goal, current_run_id))
                await websocket.send_json({"type": "status", "status": "running", "run_id": str(current_run_id)})
                
            elif msg_type == "stop":
                if current_run_id is None:
                    await websocket.send_json({"type": "error", "message": "No active agent run to stop"})
                    continue
                
                # Signal stop
                logger.info(f"Stopping run {current_run_id}")
                async with async_session() as db:
                    run_db = await db.get(AgentRun, current_run_id)
                    if run_db:
                        run_db.status = "stopped"
                    await db.commit()
                
                await websocket.send_json({"type": "status", "status": "stopped"})
                current_run_id = None
                
    except WebSocketDisconnect:
        manager.disconnect(ws_id)
