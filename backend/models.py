from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
import uuid
from db import Base

class AgentRun(Base):
    __tablename__ = "agent_runs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    goal = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="idle")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class AgentStep(Base):
    __tablename__ = "agent_steps"
    
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False)
    step_number = Column(Integer, nullable=False)
    step_type = Column(String(50), nullable=False)  # plan, think, action, error, done
    log_message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ExtractedResult(Base):
    __tablename__ = "extracted_results"
    
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False)
    data = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
