import os
import re
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import google.generativeai as genai

# ==============================================================================
# 1. LOGGING & CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("legal-analyzer")

# ==============================================================================
# 2. PYDANTIC SCHEMAS (Data Validation & OpenAPI Docs)
# ==============================================================================
class DocumentRequest(BaseModel):
    text: str = Field(..., min_length=10, description="The raw legal text to analyze")

class ClauseAnalysis(BaseModel):
    clause_text: str
    category: str
    analysis: str
    risk_score: float = Field(..., ge=0, le=100)
    suggested_revision: Optional[str] = None

class AnalysisResponse(BaseModel):
    overall_document_risk_score: float
    clauses: List[ClauseAnalysis]

class SimplifyRequest(BaseModel):
    clause_text: str = Field(..., min_length=5)

class SimplifyResponse(BaseModel):
    simplified_text: str
    explanation: str

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)

class ChatResponse(BaseModel):
    response: str

# ==============================================================================
# 3. NLP & UTILITY FUNCTIONS
# ==============================================================================
def segment_clauses(text: str) -> List[str]:
    """Splits contracts into logical provisions using structural delimiters."""
    pattern = r'(?=\n\s*(?:ARTICLE|SECTION|[0-9]+\.|\([a-z]\))\s+)'
    segments = re.split(pattern, text, flags=re.IGNORECASE)
    clauses = [c.strip() for c in segments if len(c.strip()) > 40]
    return clauses if clauses else [p.strip() for p in text.split("\n\n") if len(p.strip()) > 30]

def classify_clause(clause: str) -> str:
    """Heuristically categorizes the legal provision."""
    clause_lower = clause.lower()
    keywords = {
        "Liability & Indemnification": ["liability", "indemnify", "damages", "hold harmless"],
        "Termination": ["terminate", "termination", "cancel", "expiration"],
        "Payment": ["payment", "fee", "invoice", "compensation"],
        "Confidentiality": ["confidential", "nda", "proprietary"],
        "Governing Law": ["governing law", "jurisdiction", "arbitration"]
    }
    for category, terms in keywords.items():
        if any(term in clause_lower for term in terms):
            return category
    return "General Covenant"

def parse_llm_json(raw_text: str) -> Dict[str, Any]:
    """Safely extracts and parses JSON from LLM markdown responses."""
    raw_text = raw_text.strip()
    json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON Parsing failed: {e}")
        return {}

# ==============================================================================
# 4. CORE AI LOGIC 
# ==============================================================================
async def get_llm_analysis(clause: str, category: str, model: genai.GenerativeModel, sem: asyncio.Semaphore) -> Dict[str, Any]:
    prompt = f"""
    Analyze this legal clause ({category}). Provide a strict JSON response:
    {{
        "analysis": "Brief 2-sentence legal critique",
        "risk_score": <number 0-100>,
        "suggested_revision": "Pro-client revised clause"
    }}
    Clause: "{clause}"
    """
    async with sem:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            data = parse_llm_json(response.text)
            
            return {
                "analysis": data.get("analysis", "Analysis completed."),
                "risk_score": max(0.0, min(100.0, float(data.get("risk_score", 50.0)))),
                "suggested_revision": data.get("suggested_revision", None)
            }
        except Exception as e:
            logger.error(f"Analysis Error: {e}")
            return {"analysis": "Error analyzing clause.", "risk_score": 50.0, "suggested_revision": None}

async def get_simplification(clause: str, model: genai.GenerativeModel, sem: asyncio.Semaphore) -> Dict[str, Any]:
    prompt = f"""
    Simplify this legal clause into plain English. Output strictly in JSON:
    {{"simplified_text": "...", "explanation": "..."}}
    Clause: "{clause}"
    """
    async with sem:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            data = parse_llm_json(response.text)
            return {
                "simplified_text": data.get("simplified_text", clause),
                "explanation": data.get("explanation", "Simplified for readability.")
            }
        except Exception as e:
            logger.error(f"Simplification Error: {e}")
            raise HTTPException(status_code=500, detail="Failed to simplify clause.")

async def get_chat_response(message: str, model: genai.GenerativeModel, sem: asyncio.Semaphore) -> str:
    prompt = f"You are a specialized corporate legal assistant. User Question: {message}"
    async with sem:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"Chat Error: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate chat response.")

# ==============================================================================
# 5. FASTAPI LIFESPAN & DEPENDENCIES
# ==============================================================================
# Global state for dependency injection
app_state = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern startup/shutdown event handler for FastAPI."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        try:
            with open("config.json", "r") as f:
                cfg = json.load(f)
                api_key = cfg.get("GEMINI_API_KEY") or cfg.get("OPENAI_API_KEY")
        except FileNotFoundError:
            pass
            
    if not api_key:
        logger.error("CRITICAL: GEMINI_API_KEY is missing. AI endpoints will fail.")
    else:
        genai.configure(api_key=api_key)
        model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        app_state["model"] = genai.GenerativeModel(model_name)
        app_state["semaphore"] = asyncio.Semaphore(int(os.getenv("GEMINI_CONCURRENCY", "6")))
        logger.info(f"✅ Gemini initialized: {model_name}")
        
    yield
    # Cleanup logic (if any) goes here on shutdown
    app_state.clear()

def get_ai_model():
    if "model" not in app_state:
        raise HTTPException(status_code=503, detail="AI Model not initialized. Check API Key.")
    return app_state["model"]

def get_semaphore():
    return app_state.get("semaphore", asyncio.Semaphore(1))

# ==============================================================================
# 6. APP INITIALIZATION & ROUTES
# ==============================================================================
app = FastAPI(
    title="Gemini-Powered Legal Analyzer API",
    description="A professional, high-performance API for analyzing legal text.",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Restrict to specific domains in production (e.g., ["http://localhost:8501"])
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Health"])
def root():
    return {"status": "online", "message": "Legal Analyzer API is running"}

@app.post("/analyze/", response_model=AnalysisResponse, tags=["Analysis"])
async def analyze_document(
    request: DocumentRequest, 
    model: genai.GenerativeModel = Depends(get_ai_model),
    sem: asyncio.Semaphore = Depends(get_semaphore)
):
    try:
        clauses_text = segment_clauses(request.text)
        
        # Process concurrently
        tasks = [get_llm_analysis(text, classify_clause(text), model, sem) for text in clauses_text[:25]]
        llm_results = await asyncio.gather(*tasks)
        
        analysis_results = [
            ClauseAnalysis(
                clause_text=text, 
                category=classify_clause(text), 
                **result
            )
            for text, result in zip(clauses_text, llm_results)
        ]

        valid_scores = [res.risk_score for res in analysis_results if res.risk_score >= 0]
        overall_score = (sum(valid_scores) / len(valid_scores)) if valid_scores else 0.0

        return AnalysisResponse(
            overall_document_risk_score=round(overall_score, 2),
            clauses=analysis_results
        )
    except Exception as e:
        logger.exception("Analysis Error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/simplify/", response_model=SimplifyResponse, tags=["Simplification"])
async def simplify_clause_endpoint(
    request: SimplifyRequest,
    model: genai.GenerativeModel = Depends(get_ai_model),
    sem: asyncio.Semaphore = Depends(get_semaphore)
):
    return await get_simplification(request.clause_text, model, sem)

@app.post("/api/chat", response_model=ChatResponse, tags=["Chat"])
async def chat_endpoint(
    req: ChatRequest,
    model: genai.GenerativeModel = Depends(get_ai_model),
    sem: asyncio.Semaphore = Depends(get_semaphore)
):
    resp_text = await get_chat_response(req.message, model, sem)
    return ChatResponse(response=resp_text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)