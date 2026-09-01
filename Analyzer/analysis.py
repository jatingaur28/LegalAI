# analyzer/analysis.py
import re
import json
import asyncio
import logging
from typing import Optional
import time

import google.generativeai as genai
from google.generativeai.types import GenerationConfig

logger = logging.getLogger(__name__)

CLAUSE_CATEGORIES = {
    "Liability": ["liability", "indemnify", "indemnification", "hold harmless"],
    "Confidentiality": ["confidentiality", "confidential", "non-disclosure"],
    "Termination": ["termination", "terminate", "cancellation"],
    "Governing Law": ["governing law", "jurisdiction", "venue"],
}

def segment_clauses(document_text: str) -> list[str]:
    pattern = r'(\n\s*(?:SECTION|ARTICLE|CLAUSE)\s+[\w\d]+[.:]?\s*|\n\s*\d+\.\s+)'
    clauses = re.split(pattern, document_text, flags=re.IGNORECASE)
    segmented_clauses = []
    i = 1
    while i < len(clauses):
        full_clause = (clauses[i] + clauses[i + 1]).strip()
        if len(full_clause) > 20:
            segmented_clauses.append(full_clause)
        i += 2
    if not segmented_clauses:
        return [p.strip() for p in document_text.split('\n\n') if len(p.strip()) > 20]
    return segmented_clauses[:40]  # increased limit

def classify_clause(clause_text: str) -> str:
    clause_lower = clause_text.lower()
    for category, keywords in CLAUSE_CATEGORIES.items():
        if any(keyword in clause_lower for keyword in keywords):
            return category
    return "Other"

# Retry helper
async def _call_with_retries(coro_fn, retries=2, base_delay=0.8):
    last_exc = None
    for i in range(retries + 1):
        try:
            return await coro_fn()
        except Exception as e:
            last_exc = e
            if i < retries:
                wait = base_delay * (2 ** i)
                logger.warning(f"LLM call failed (attempt {i+1}/{retries+1}): {e}; retrying in {wait:.2f}s")
                await asyncio.sleep(wait)
    raise last_exc

async def get_llm_analysis(clause_text: str, category: str, model, sem: asyncio.Semaphore) -> dict:
    generation_config = GenerationConfig(response_mime_type="application/json")
    prompt = f"""
Analyze the risk of the following legal clause, categorized as '{category}', for the signing party.
Return a JSON object with:
1. "risk_score": integer from 0-100.
2. "risk_level": "Low", "Medium", "High", or "Critical".
3. "explanation": one-sentence explanation.
4. "simplified_explanation": the explanation in simple plain English.
5. "suggested_revision": If risk_score > 60 provide a safer alternative wording, else null.
Clause: \"\"\"{clause_text}\"\"\"
"""

    async def _single_call():
        async with sem:
            return await model.generate_content_async(prompt, generation_config=generation_config)

    try:
        response = await _call_with_retries(_single_call, retries=2)
        raw_text = getattr(response, "text", "") or str(response)
        # Try parse as JSON
        parsed = None
        try:
            parsed = json.loads(raw_text)
        except Exception:
            # attempt to find JSON substring
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(raw_text[start:end+1])
                except Exception:
                    parsed = None

        if not parsed:
            # fallback: return textual explanation
            return {
                "risk_score": -1,
                "risk_level": "Unknown",
                "explanation": raw_text[:500] or "No usable response",
                "simplified_explanation": None,
                "suggested_revision": None
            }

        risk_score = int(parsed.get("risk_score", -1))
        risk_level = parsed.get("risk_level", "Unknown")
        explanation = parsed.get("explanation", "") or ""
        simplified = parsed.get("simplified_explanation")
        suggested = parsed.get("suggested_revision")

        return {
            "risk_score": risk_score,
            "risk_level": risk_level,
            "explanation": explanation,
            "simplified_explanation": simplified,
            "suggested_revision": suggested
        }
    except Exception as e:
        logger.exception("Error calling Gemini for analysis")
        return {
            "risk_score": -1,
            "risk_level": "Error",
            "explanation": f"Error calling Gemini API: {e}",
            "simplified_explanation": None,
            "suggested_revision": None
        }

async def get_simplification(clause_text: str, model, sem: asyncio.Semaphore) -> dict:
    prompt = f"Rewrite the following legal clause in clear plain English for a non-lawyer:\n\n\"\"\"{clause_text}\"\"\""
    async def _single_call():
        async with sem:
            return await model.generate_content_async(prompt)
    try:
        response = await _call_with_retries(_single_call, retries=2)
        raw_text = getattr(response, "text", "") or str(response)
        return {"simplified_text": raw_text}
    except Exception as e:
        logger.exception("Error calling Gemini for simplification")
        return {"simplified_text": f"Error during simplification: {e}"}

# Simple chat helper used by /api/chat
async def get_chat_response(message: str, model, sem: asyncio.Semaphore) -> str:
    prompt = f"You are a helpful legal assistant. Answer succinctly.\nUser: {message}\nAssistant:"
    async def _single_call():
        async with sem:
            return await model.generate_content_async(prompt)
    try:
        response = await _call_with_retries(_single_call, retries=2)
        return getattr(response, "text", "") or str(response)
    except Exception as e:
        logger.exception("Chat failed")
        return f"Error: {e}"
