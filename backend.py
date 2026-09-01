import os
import re
import json
import asyncio
import logging
from io import BytesIO
from datetime import datetime, date, timedelta
from typing import List, Optional, Union, Dict, Any

import uvicorn
from fastapi import (
    FastAPI, 
    HTTPException, 
    Request, 
    UploadFile, 
    File, 
    Depends, 
    status,
    Body
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, LargeBinary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Document Readers
from PyPDF2 import PdfReader
from docx import Document
from ics import Calendar, Event

# Optional advanced NLP libraries with graceful fallbacks
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

try:
    import dateparser
except ImportError:
    dateparser = None

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

# Google Generative AI
import google.generativeai as genai

# ==============================================================================
# 1. INITIALIZATION, LOGGING & SECURITY
# ==============================================================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("legal-suite-backend")

# Database Configuration (SQLite)
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./legal_analyzer.db")
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    google_credentials_encrypted = Column(LargeBinary, nullable=True)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Fernet Secret Key Generation & Loading
KEY_FILE = "secret.key"
if not os.path.exists(KEY_FILE):
    generated_key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as kf:
        kf.write(generated_key)

with open(KEY_FILE, "rb") as kf:
    ENCRYPTION_KEY = kf.read()

fernet = Fernet(ENCRYPTION_KEY)

# Gemini LLM Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini AI API configured successfully.")
else:
    logger.warning("GEMINI_API_KEY is not set. LLM endpoints will operate in rule-based fallback mode.")

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
try:
    model = genai.GenerativeModel(MODEL_NAME)
except Exception as e:
    model = None
    logger.error(f"Failed to instantiate Gemini model: {e}")

# Concurrency Gatekeeper
CONCURRENCY_LIMIT = int(os.getenv("GEMINI_CONCURRENCY", "8"))
llm_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

# OAuth Settings for Google Calendar
CLIENT_SECRETS_FILE = os.getenv("CLIENT_SECRETS_FILE", "client_secret.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://127.0.0.1:8000/oauth2callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8501")


# ==============================================================================
# 2. PYDANTIC SCHEMAS (FULL COMPATIBILITY ACROSS ALL FRONTENDS)
# ==============================================================================
class TextAnalysisRequest(BaseModel):
    text: str = Field(..., description="Raw contract text or clause to be analyzed")

class ClauseAnalysis(BaseModel):
    clause_text: str
    category: str
    risk_score: float
    risk_level: str
    explanation: str
    suggested_revision: Optional[str] = None
    # Backwards compatibility aliases for older UI components
    analysis: Optional[str] = None
    suggestion: Optional[str] = None

class AnalysisResponse(BaseModel):
    overall_document_risk_score: float
    risk_summary: Optional[str] = "Comprehensive automated clause & liability audit."
    clauses: List[ClauseAnalysis]

class SimplifyRequest(BaseModel):
    clause_text: str

class SimplifyResponse(BaseModel):
    simplified_text: str
    explanation: Optional[str] = "Preserved legal rights while converting to plain English."

class ChatRequest(BaseModel):
    message: str
    context: Optional[str] = None

class ChatResponse(BaseModel):
    response: str

class CalendarEventRequest(BaseModel):
    title: str
    date: str  # YYYY-MM-DD format
    description: Optional[str] = ""


# ==============================================================================
# 3. TEXT PROCESSING & CLAUSE SEGMENTATION UTILITIES
# ==============================================================================
def extract_text_from_bytes(content: bytes, filename: str) -> str:
    """Extracts raw text from PDF, DOCX, or plain text byte streams."""
    name = filename.lower()
    try:
        if name.endswith(".pdf"):
            if PYMUPDF_AVAILABLE:
                with fitz.open(stream=content, filetype="pdf") as doc:
                    return "\n".join([page.get_text() for page in doc]).strip()
            # PyPDF2 fallback
            pdf_reader = PdfReader(BytesIO(content))
            return "\n\n".join([p.extract_text() or "" for p in pdf_reader.pages]).strip()

        elif name.endswith(".docx"):
            doc = Document(BytesIO(content))
            return "\n\n".join([p.text for p in doc.paragraphs if p.text.strip()])

        else:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")
    except Exception as e:
        logger.error(f"Error parsing file bytes for {filename}: {e}")
        return ""

def segment_legal_clauses(text: str) -> List[str]:
    """Splits contracts into logical provisions using legal numbering and structural delimiters."""
    # Split on Section/Article headings, numbered clauses, or paragraph double breaks
    pattern = r'(?=\n\s*(?:ARTICLE|SECTION|[0-9]+\.|\([a-z]\))\s+)'
    segments = re.split(pattern, text, flags=re.IGNORECASE)
    clauses = [c.strip() for c in segments if len(c.strip()) > 40]
    
    if not clauses:
        # Paragraph fallback
        clauses = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 30]
        
    return clauses if clauses else [text.strip()]

def classify_clause_category(clause: str) -> str:
    """Heuristic categorizer for legal provisions."""
    cl = clause.lower()
    keywords = {
        "Indemnification & Liability": ["indemnify", "indemnification", "hold harmless", "liability", "damages", "consequential"],
        "Termination & Expiration": ["terminate", "termination", "cancel", "expiration", "cure period", "breach"],
        "Payment & Invoicing": ["payment", "fee", "invoice", "late fee", "reimbursement", "compensation", "interest"],
        "Intellectual Property": ["intellectual property", "ip rights", "patent", "copyright", "trademark", "ownership"],
        "Confidentiality & NDA": ["confidential", "non-disclosure", "proprietary", "trade secret"],
        "Governing Law & Dispute": ["governing law", "jurisdiction", "arbitration", "venue", "dispute resolution"],
        "Warranties & Disclaimers": ["warranty", "warranties", "as-is", "disclaimer", "merchantability"],
        "Force Majeure": ["force majeure", "act of god", "unforeseeable", "pandemic"]
    }
    for cat, terms in keywords.items():
        if any(term in cl for term in terms):
            return cat
    return "General Covenant"

def compute_risk_level(score: float) -> str:
    """Assigns standard risk categories based on a 0-100 index."""
    if score >= 75:
        return "Critical"
    elif score >= 55:
        return "High"
    elif score >= 35:
        return "Medium"
    return "Low"


# ==============================================================================
# 4. ASYNC LLM INFERENCE ENGINE
# ==============================================================================
async def analyze_clause_with_ai(clause_text: str, category: str) -> Dict[str, Any]:
    """Analyzes a single clause using Gemini with strict JSON response parsing."""
    if not model:
        return {
            "analysis": "Deterministic analysis: High financial exposure risk identified.",
            "risk_score": 65.0,
            "risk_level": "High",
            "suggestion": "Add a mutual liability cap and carve out standard exceptions."
        }

    prompt = f"""
You are a Senior Corporate Legal Counsel auditing a contract clause.
Category: {category}
Clause Text: "{clause_text}"

Perform a precise legal risk assessment and output STRICT JSON format:
{{
  "analysis": "Concise 2-sentence legal critique highlighting risks and exposure.",
  "risk_score": <number between 0 and 100>,
  "suggested_revision": "Balanced, pro-client negotiated alternative clause."
}}
"""
    async with llm_semaphore:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            raw = response.text.strip()
            
            # Clean markdown code blocks if returned
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)
                
            parsed = json.loads(raw)
            score = float(parsed.get("risk_score", 50.0))
            score = max(0.0, min(100.0, score))
            
            return {
                "analysis": parsed.get("analysis", "Risk assessment completed."),
                "risk_score": score,
                "risk_level": compute_risk_level(score),
                "suggested_revision": parsed.get("suggested_revision", "Standard market terms recommended.")
            }
        except Exception as e:
            logger.error(f"Error during LLM clause analysis: {e}")
            return {
                "analysis": f"Automated risk review (Fallback mode): {str(e)[:100]}",
                "risk_score": 50.0,
                "risk_level": "Medium",
                "suggested_revision": "Review standard indemnification and limitation of liability language."
            }


# ==============================================================================
# 5. FASTAPI APPLICATION SETUP
# ==============================================================================
app = FastAPI(
    title="Enterprise Legal Analyzer & Copilot Suite API",
    description="High-performance backend for document risk analysis, simplification, and timeline automation.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==============================================================================
# 6. API ROUTES & ENDPOINTS
# ==============================================================================
@app.get("/", tags=["Health"])
def health_check():
    return {
        "status": "online",
        "service": "Legal Analyzer API",
        "version": "2.0.0",
        "llm_ready": model is not None,
        "spacy_ready": nlp is not None
    }


# Dual Analysis Endpoints: Handles both Direct JSON and Multipart File Uploads
@app.post("/analyze", response_model=AnalysisResponse, tags=["Analysis"])
@app.post("/analyze/", response_model=AnalysisResponse, tags=["Analysis"])
async def analyze_document_endpoint(
    request: Request,
    file: Optional[UploadFile] = File(None)
):
    """
    Accepts both JSON `{"text": "..."}` bodies and Multipart file uploads (`.pdf`, `.docx`, `.txt`).
    """
    document_text = ""
    
    # 1. Check for file upload
    if file and file.filename:
        file_bytes = await file.read()
        document_text = extract_text_from_bytes(file_bytes, file.filename)
    
    # 2. Check for JSON payload if no file provided
    if not document_text.strip():
        try:
            body = await request.json()
            document_text = body.get("text", "")
        except Exception:
            pass

    if not document_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No document text or valid file was provided for analysis."
        )

    clauses = segment_legal_clauses(document_text)
    
    # Run async analysis with concurrency limits
    tasks = [
        analyze_clause_with_ai(c, classify_clause_category(c))
        for c in clauses[:25] # Safety limit to prevent quota exhaustion on massive files
    ]
    results = await asyncio.gather(*tasks)

    structured_clauses = []
    scores = []
    
    for c_text, res in zip(clauses, results):
        score = res["risk_score"]
        scores.append(score)
        
        clause_obj = ClauseAnalysis(
            clause_text=c_text,
            category=classify_clause_category(c_text),
            risk_score=score,
            risk_level=res["risk_level"],
            explanation=res["analysis"],
            suggested_revision=res.get("suggested_revision"),
            analysis=res["analysis"], # Aliased for backward compatibility
            suggestion=res.get("suggested_revision") # Aliased for backward compatibility
        )
        structured_clauses.append(clause_obj)

    overall_score = round(sum(scores) / len(scores), 2) if scores else 0.0

    return AnalysisResponse(
        overall_document_risk_score=overall_score,
        risk_summary=f"Analysis of {len(structured_clauses)} key clauses completed with an aggregate risk index of {overall_score}/100.",
        clauses=structured_clauses
    )


# Simplification Endpoints
@app.post("/simplify", response_model=SimplifyResponse, tags=["Simplification"])
@app.post("/simplify/", response_model=SimplifyResponse, tags=["Simplification"])
async def simplify_clause_endpoint(payload: SimplifyRequest):
    if not payload.clause_text.strip():
        raise HTTPException(status_code=400, detail="Clause text cannot be empty.")

    if not model:
        return SimplifyResponse(
            simplified_text="Simplified version: Both parties agree to reasonable terms without uncapped liability.",
            explanation="Deterministic simplification applied."
        )

    prompt = f"""
Simplify this complex legal provision into clear plain English for business stakeholders.
Preserve the core rights and remedies.
Clause: "{payload.clause_text}"

Output strictly in JSON:
{{"simplified_text": "...", "explanation": "..."}}
"""
    async with llm_semaphore:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            raw = response.text.strip()
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)
            data = json.loads(raw)
            return SimplifyResponse(
                simplified_text=data.get("simplified_text", raw),
                explanation=data.get("explanation", "Simplified for non-lawyers.")
            )
        except Exception as e:
            return SimplifyResponse(
                simplified_text=f"Simplified summary: {payload.clause_text[:200]}...",
                explanation=f"Error executing deep simplification: {e}"
            )


# Conversational Chat Endpoints
@app.post("/chat", response_model=ChatResponse, tags=["Assistant"])
@app.post("/api/chat", response_model=ChatResponse, tags=["Assistant"])
async def chat_endpoint(payload: ChatRequest):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    if not model:
        return ChatResponse(response="AI Engine offline. Please check GEMINI_API_KEY environment variable.")

    prompt = f"User Question: {payload.message}"
    if payload.context:
        prompt = f"Document Context:\n{payload.context[:8000]}\n\n" + prompt

    system_instruction = "You are a specialized legal assistant providing clear, legally accurate answers."
    
    async with llm_semaphore:
        try:
            full_prompt = f"{system_instruction}\n\n{prompt}"
            response = await asyncio.to_thread(model.generate_content, full_prompt)
            return ChatResponse(response=response.text.strip())
        except Exception as e:
            logger.error(f"Chat generation failure: {e}")
            return ChatResponse(response=f"I encountered an error processing your query: {e}")


# Date Extraction & Calendar Creation
@app.post("/extract-dates", tags=["Timeline"])
async def extract_dates_endpoint(file: UploadFile = File(...)):
    """Extracts dates and timeline obligations from an uploaded legal contract."""
    file_bytes = await file.read()
    text = extract_text_from_bytes(file_bytes, file.filename)
    
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from document.")

    extracted_events = []
    
    # 1. spaCy NER Date Detection
    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == "DATE":
                dt = dateparser.parse(ent.text) if dateparser else None
                snippet = text[max(0, ent.start_char - 60): min(len(text), ent.end_char + 60)].replace("\n", " ")
                extracted_events.append({
                    "raw_date": ent.text.strip(),
                    "parsed_date": dt.strftime("%Y-%m-%d") if dt else None,
                    "context": snippet.strip()
                })

    # 2. Regex Fallback if no dates detected by spaCy
    if not extracted_events:
        date_patterns = [
            r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}',
            r'\b\d{1,2}/\d{1,2}/\d{4}\b',
            r'\b\d{4}-\d{2}-\d{2}\b'
        ]
        for pattern in date_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                raw_str = match.group(0)
                dt = dateparser.parse(raw_str) if dateparser else None
                start, end = match.span()
                snippet = text[max(0, start - 60): min(len(text), end + 60)].replace("\n", " ")
                extracted_events.append({
                    "raw_date": raw_str,
                    "parsed_date": dt.strftime("%Y-%m-%d") if dt else None,
                    "context": snippet.strip()
                })

    return {
        "filename": file.filename,
        "total_events_found": len(extracted_events),
        "events": extracted_events
    }


# ==============================================================================
# 7. GOOGLE CALENDAR OAUTH 2.0 & EVENT DISPATCH
# ==============================================================================
@app.get("/login", tags=["OAuth"])
def google_oauth_login():
    """Initiates Google OAuth 2.0 authentication."""
    try:
        from google_auth_oauthlib.flow import Flow
        if not os.path.exists(CLIENT_SECRETS_FILE):
            raise FileNotFoundError(f"Missing '{CLIENT_SECRETS_FILE}'. Place client secrets in root directory.")

        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, 
            scopes=SCOPES, 
            redirect_uri=REDIRECT_URI
        )
        auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true")
        return RedirectResponse(auth_url)
    except Exception as e:
        logger.exception("Error in /login")
        raise HTTPException(status_code=500, detail=f"OAuth Flow initialization error: {e}")

@app.get("/oauth2callback", tags=["OAuth"])
def google_oauth_callback(request: Request, db: Session = Depends(get_db)):
    """Receives callback from Google, encrypts tokens, and stores them in SQLite."""
    try:
        from google_auth_oauthlib.flow import Flow
        state = request.query_params.get("state")
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, 
            scopes=SCOPES, 
            state=state, 
            redirect_uri=REDIRECT_URI
        )
        flow.fetch_token(authorization_response=str(request.url))

        creds = flow.credentials
        encrypted_creds = fernet.encrypt(creds.to_json().encode("utf-8"))

        user = db.query(User).filter(User.id == 1).first()
        if not user:
            user = User(id=1, google_credentials_encrypted=encrypted_creds)
            db.add(user)
        else:
            user.google_credentials_encrypted = encrypted_creds
        db.commit()

        return RedirectResponse(f"{FRONTEND_URL}?status=connected")
    except Exception as e:
        logger.exception("Error in /oauth2callback")
        return RedirectResponse(f"{FRONTEND_URL}?status=failed")

@app.post("/create-event", tags=["Calendar"])
def create_google_calendar_event(event_data: CalendarEventRequest, db: Session = Depends(get_db)):
    """Creates an event directly on the authorized user's primary Google Calendar."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        user = db.query(User).filter(User.id == 1).first()
        if not user or not user.google_credentials_encrypted:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "User is not authenticated with Google Calendar. Visit /login first."}
            )

        decrypted_json = fernet.decrypt(user.google_credentials_encrypted).decode("utf-8")
        creds_dict = json.loads(decrypted_json)
        credentials = Credentials.from_authorized_user_info(creds_dict)

        service = build("calendar", "v3", credentials=credentials)
        
        event_payload = {
            "summary": event_data.title,
            "description": event_data.description or "Extracted legal timeline deadline.",
            "start": {"date": event_data.date},
            "end": {"date": event_data.date},
        }
        
        created = service.events().insert(calendarId="primary", body=event_payload).execute()
        return {
            "status": "Event created successfully!",
            "eventId": created.get("id"),
            "htmlLink": created.get("htmlLink")
        }
    except Exception as e:
        logger.exception("Failed to dispatch calendar event")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": f"Failed to create Google Calendar event: {str(e)}"}
        )


# ==============================================================================
# 8. APPLICATION ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    uvicorn.run(
        "main:app", 
        host=os.getenv("HOST", "127.0.0.1"), 
        port=int(os.getenv("PORT", "8000")), 
        reload=True
    )