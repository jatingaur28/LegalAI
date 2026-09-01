import streamlit as st
import requests
import io
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from typing import Optional, Dict, Any

# Document Parsers
from PyPDF2 import PdfReader
try:
    from docx import Document
except ImportError:
    Document = None

# ==============================================================================
# 1. PAGE CONFIGURATION & STATE
# ==============================================================================
st.set_page_config(
    page_title="Clarity | Enterprise Legal AI", 
    page_icon="⚖️", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Session State to prevent data loss on tab switches
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "extracted_text" not in st.session_state:
    st.session_state.extracted_text = ""

BACKEND_URL = st.secrets.get("BACKEND_URL", "http://localhost:8000").rstrip("/")

# ==============================================================================
# 2. DESIGN SYSTEM & CSS (DARK GLASSMORPHISM)
# ==============================================================================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');

    /* Global Typography & Colors */
    html, body, [class*="css"], .stApp {
        font-family: 'Inter', sans-serif;
        background-color: #09090b;
        color: #f4f4f5;
    }

    /* Animated Gradient Title */
    .title-gradient {
        background: linear-gradient(90deg, #818cf8 0%, #c084fc 50%, #818cf8 100%);
        background-size: 200% auto;
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        animation: shine 4s linear infinite;
        font-weight: 800;
        font-size: 2.8rem;
        margin-bottom: 0rem;
    }
    @keyframes shine { to { background-position: 200% center; } }

    /* Glass Cards */
    .glass-card {
        background: rgba(24, 24, 27, 0.6);
        backdrop-filter: blur(12px);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    }

    /* Metrics Override */
    [data-testid="stMetric"] {
        background: rgba(24, 24, 27, 0.6);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 1.2rem;
        backdrop-filter: blur(12px);
    }
    [data-testid="stMetricLabel"] { color: #a1a1aa !important; font-weight: 600; }
    [data-testid="stMetricValue"] { color: #ffffff !important; font-weight: 700; }

    /* Primary Button */
    .stButton > button {
        background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    .stButton > button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 15px rgba(99, 102, 241, 0.4);
    }

    /* Badges */
    .badge {
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
    }
    .badge-critical { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid #ef4444; }
    .badge-high { background: rgba(249, 115, 22, 0.2); color: #fdba74; border: 1px solid #f97316; }
    .badge-medium { background: rgba(234, 179, 8, 0.2); color: #fde047; border: 1px solid #eab308; }
    .badge-low { background: rgba(34, 197, 94, 0.2); color: #86efac; border: 1px solid #22c55e; }
    
    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent;
        border-radius: 8px;
        color: #a1a1aa;
        padding: 8px 16px;
        border: none !important;
    }
    .stTabs [aria-selected="true"] {
        background-color: rgba(255, 255, 255, 0.1) !important;
        color: #ffffff !important;
    }
</style>
""", unsafe_allow_html=True)

# ==============================================================================
# 3. UTILITY FUNCTIONS
# ==============================================================================
def extract_text(file) -> str:
    """Safely extracts text from uploaded PDF, DOCX, or TXT files."""
    try:
        if file.type == "application/pdf":
            reader = PdfReader(io.BytesIO(file.read()))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        elif "word" in file.type:
            if Document is None:
                st.error("python-docx is not installed. Please `pip install python-docx`")
                return ""
            doc = Document(io.BytesIO(file.read()))
            return "\n".join([p.text for p in doc.paragraphs])
        else:
            return file.read().decode("utf-8", errors="replace")
    except Exception as e:
        st.error(f"Failed to read file: {e}")
        return ""

def api_post(endpoint: str, payload: Dict[str, Any], timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Handles API requests with built-in error catching."""
    try:
        response = requests.post(f"{BACKEND_URL}{endpoint}", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Backend Connection Error: {e}")
        return None

def get_risk_badge(score: float, level_str: str) -> str:
    """Returns HTML for a color-coded risk badge."""
    lvl = level_str.capitalize()
    if not lvl or lvl == "N/a":
        if score >= 7.5 or score >= 75: lvl = "Critical"
        elif score >= 5.5 or score >= 55: lvl = "High"
        elif score >= 3.5 or score >= 35: lvl = "Medium"
        else: lvl = "Low"

    badge_class = f"badge-{lvl.lower()}"
    return f"<span class='badge {badge_class}'>{lvl} Risk</span>"

# ==============================================================================
# 4. HEADER & SIDEBAR
# ==============================================================================
col_logo, col_title = st.columns([0.5, 9.5])
with col_logo:
    st.markdown("<h1 style='margin-bottom:0;'>⚖️</h1>", unsafe_allow_html=True)
with col_title:
    st.markdown("<h1 class='title-gradient'>Clarity AI</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color: #a1a1aa; margin-top: -10px; font-size: 1.1rem;'>Enterprise Legal Risk & Contract Intelligence</p>", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### ⚙️ System Status")
    st.info(f"🔗 **Endpoint:** `{BACKEND_URL}`")
    
    if st.button("📡 Ping Backend", use_container_width=True):
        try:
            r = requests.get(f"{BACKEND_URL}/", timeout=5)
            if r.status_code == 200:
                st.success("✅ Backend Online & Reachable")
            else:
                st.warning(f"⚠️ Backend returned status {r.status_code}")
        except Exception:
            st.error("❌ Backend Offline")
    
    st.markdown("---")
    st.caption("Ensure your FastAPI backend is running via `uvicorn main:app --reload` before executing analysis.")

# ==============================================================================
# 5. MAIN TABS
# ==============================================================================
tab_audit, tab_simplify, tab_chat = st.tabs([
    "📊 Document Audit", 
    "✏️ Clause Simplifier", 
    "💬 AI Legal Counsel"
])

# ----------------- TAB 1: DOCUMENT AUDIT -----------------
with tab_audit:
    st.markdown("### Autonomous Contract Risk Analysis")
    
    uploaded_file = st.file_uploader("Upload Agreement (TXT, PDF, DOCX)", type=["txt", "pdf", "docx"])
    
    if uploaded_file:
        with st.spinner("Extracting text..."):
            st.session_state.extracted_text = extract_text(uploaded_file)
            
    if st.session_state.extracted_text:
        with st.expander("👁️ Preview Extracted Text"):
            text = st.session_state.extracted_text
            st.text_area("Content", value=(text[:2000] + "\n\n...[TRUNCATED]" if len(text) > 2000 else text), height=200, disabled=True)

        if st.button("🚀 Execute Risk Audit", type="primary", use_container_width=True):
            with st.spinner("Analyzing clauses and calculating liability matrices (this may take up to 3 minutes)..."):
                result = api_post("/analyze/", {"text": st.session_state.extracted_text}, timeout=180)
                if result:
                    st.session_state.analysis_result = result
                    st.toast("Analysis Complete!", icon="✅")

    # Render Dashboard if data exists
    if st.session_state.analysis_result:
        res = st.session_state.analysis_result
        clauses = res.get("clauses", [])
        avg_score = res.get("overall_document_risk_score", 0.0)
        
        st.markdown("---")
        st.markdown("### 📈 Risk Dashboard")
        
        # KPIs
        c1, c2, c3 = st.columns(3)
        c1.metric("Overall Risk Score", f"{avg_score:.1f}")
        c2.metric("Clauses Analyzed", len(clauses))
        c3.metric("Critical Flags", sum(1 for c in clauses if c.get("risk_score", 0) > 7 or c.get("risk_score", 0) > 70))

        # Visualizations
        if clauses:
            col_chart1, col_chart2 = st.columns(2)
            df = pd.DataFrame(clauses)
            
            # Normalize risk_score to 100 scale if it's on a 10 scale
            if df['risk_score'].max() <= 10.0:
                df['normalized_score'] = df['risk_score'] * 10
            else:
                df['normalized_score'] = df['risk_score']

            with col_chart1:
                # Gauge Chart
                fig_gauge = go.Figure(go.Indicator(
                    mode="gauge+number",
                    value=avg_score * 10 if avg_score <= 10 else avg_score,
                    title={'text': "Aggregate Risk Index", 'font': {'color': '#f4f4f5'}},
                    gauge={
                        'axis': {'range': [0, 100], 'tickcolor': "#a1a1aa"},
                        'bar': {'color': "#818cf8"},
                        'bgcolor': "rgba(0,0,0,0)",
                        'borderwidth': 0,
                        'steps': [
                            {'range': [0, 40], 'color': "rgba(34, 197, 94, 0.3)"},
                            {'range': [40, 70], 'color': "rgba(234, 179, 8, 0.3)"},
                            {'range': [70, 100], 'color': "rgba(239, 68, 68, 0.3)"}
                        ]
                    }
                ))
                fig_gauge.update_layout(paper_bgcolor="rgba(0,0,0,0)", font={'color': '#f4f4f5'}, height=250, margin=dict(t=40, b=10, l=10, r=10))
                st.plotly_chart(fig_gauge, use_container_width=True)

            with col_chart2:
                # Distribution Chart
                if 'category' in df.columns:
                    fig_bar = px.bar(
                        df.groupby('category')['normalized_score'].mean().reset_index(),
                        x='normalized_score', y='category', orientation='h',
                        title="Average Risk by Category",
                        color_discrete_sequence=['#c084fc']
                    )
                    fig_bar.update_layout(
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                        font={'color': '#f4f4f5'}, height=250, margin=dict(t=40, b=10, l=10, r=10),
                        xaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
                        yaxis=dict(title="")
                    )
                    st.plotly_chart(fig_bar, use_container_width=True)

        # Clause Breakdown
        st.markdown("### 📑 Clause Breakdown")
        for i, c in enumerate(clauses):
            score = c.get('risk_score', 0)
            level = c.get('risk_level', '')
            badge = get_risk_badge(score, level)
            
            with st.expander(f"{c.get('category', 'Provision')} (Risk Score: {score})"):
                st.markdown(f"{badge}", unsafe_allow_html=True)
                st.markdown("<br>", unsafe_allow_html=True)
                
                st.markdown("##### 🔍 Legal Analysis")
                st.info(c.get("explanation") or c.get("analysis", "No analysis provided."))
                
                if c.get("suggested_revision") or c.get("suggestion"):
                    st.markdown("##### 💡 Suggested Revision")
                    st.success(c.get("suggested_revision") or c.get("suggestion"))
                
                st.markdown("##### 📜 Original Text")
                st.caption(f"> {c.get('clause_text', '')}")

# ----------------- TAB 2: CLAUSE SIMPLIFIER -----------------
with tab_simplify:
    st.markdown("### Plain-English Clause Translator")
    st.write("Convert dense legalese into clear, actionable business language without losing core legal meaning.")
    
    col_input, col_output = st.columns(2, gap="medium")
    
    with col_input:
        clause_text = st.text_area("Input Legal Clause:", height=250, placeholder="e.g., 'In no event shall either party be liable to the other for any indirect, consequential, or punitive damages...'")
        
        if st.button("✨ Simplify Logic", use_container_width=True):
            if clause_text.strip():
                with st.spinner("Translating legalese..."):
                    result = api_post("/simplify/", {"clause_text": clause_text})
                    if result:
                        st.session_state.simplified_result = result
            else:
                st.warning("Please enter text to simplify.")

    with col_output:
        st.markdown("""<div class="glass-card" style="min-height: 250px;">
            <h4 style="margin-top:0;">Output</h4>""", unsafe_allow_html=True)
            
        if hasattr(st.session_state, 'simplified_result') and st.session_state.simplified_result:
            res = st.session_state.simplified_result
            st.success(res.get("simplified_text", ""))
            st.markdown("**Explanation of Changes:**")
            st.caption(res.get("explanation", ""))
        else:
            st.caption("Awaiting input. Simplified text will appear here.")
            
        st.markdown("</div>", unsafe_allow_html=True)

# ----------------- TAB 3: LEGAL COUNSEL CHAT -----------------
with tab_chat:
    st.markdown("### Contextual Legal Assistant")
    st.caption("Ask general legal terminology questions or discuss drafting strategies.")
    
    # Render chat history
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"], avatar="⚖️" if msg["role"] == "assistant" else "👤"):
            st.markdown(msg["content"])
            
    # Chat Input
    if prompt := st.chat_input("e.g., What is a 'Severability' clause?"):
        # Add user message to state and display
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)
            
        # Call Backend
        with st.chat_message("assistant", avatar="⚖️"):
            with st.spinner("Analyzing..."):
                # Use /chat for standard completion (some backends use /api/chat, adjust if necessary)
                endpoint = "/api/chat" if "/api/chat" in BACKEND_URL else "/chat" 
                
                # Check backend routes from the user's previous codes, both /chat and /api/chat were used
                # Attempting standard /chat
                result = api_post("/chat", {"message": prompt}) 
                if not result:
                    # Fallback to /api/chat if /chat fails (404)
                    result = api_post("/api/chat", {"message": prompt})
                
                if result:
                    response_text = result.get("response", "No response received.")
                    st.markdown(response_text)
                    st.session_state.chat_messages.append({"role": "assistant", "content": response_text})