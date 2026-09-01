# analyzer/schemas.py
from pydantic import BaseModel
from typing import List, Optional

class DocumentRequest(BaseModel):
    text: str

class ClauseAnalysis(BaseModel):
    clause_text: str
    category: str
    risk_score: int
    risk_level: str
    explanation: str
    simplified_explanation: Optional[str] = None
    suggested_revision: Optional[str] = None

class AnalysisResponse(BaseModel):
    overall_document_risk_score: float
    clauses: List[ClauseAnalysis]

class SimplifyRequest(BaseModel):
    clause_text: str

class SimplifyResponse(BaseModel):
    simplified_text: str

# Chat models (optional)
class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
