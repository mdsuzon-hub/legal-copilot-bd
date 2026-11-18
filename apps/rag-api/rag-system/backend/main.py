"""
Production RAG Backend - FastAPI + ChromaDB + SentenceTransformers
Free LLM via Groq API (free tier) or HuggingFace Inference API
"""

import os
import uuid
import logging
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import httpx
import PyPDF2
import docx
import io
import json
import re
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rag-backend")

# ─── Config ─────────────────────────────────────────────────────────────────
CHROMA_PATH      = os.getenv("CHROMA_PATH", "./chroma_db")
COLLECTION_NAME  = os.getenv("COLLECTION_NAME", "rag_documents")
EMBED_MODEL      = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")          # Free tier
GROQ_MODEL       = os.getenv("GROQ_MODEL", "llama3-8b-8192")
HF_API_KEY       = os.getenv("HF_API_KEY", "")            # HuggingFace fallback
HF_MODEL         = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.2")
CHUNK_SIZE       = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP    = int(os.getenv("CHUNK_OVERLAP", "64"))
TOP_K            = int(os.getenv("TOP_K", "5"))
MAX_TOKENS       = int(os.getenv("MAX_TOKENS", "1024"))
ALLOWED_ORIGINS  = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://localhost:8080").split(",")

# ─── Global singletons ──────────────────────────────────────────────────────
embed_model: Optional[SentenceTransformer] = None
chroma_client: Optional[chromadb.PersistentClient] = None
collection = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global embed_model, chroma_client, collection
    logger.info("Loading embedding model: %s", EMBED_MODEL)
    embed_model = SentenceTransformer(EMBED_MODEL)
    logger.info("Connecting to ChromaDB at: %s", CHROMA_PATH)
    chroma_client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False)
    )
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )
    logger.info("ChromaDB collection '%s' ready. Docs: %d", COLLECTION_NAME, collection.count())
    yield
    logger.info("Shutting down RAG backend.")

# ─── App ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Production RAG API",
    version="1.0.0",
    description="Retrieval-Augmented Generation API with ChromaDB + SentenceTransformers",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Pydantic Schemas ────────────────────────────────────────────────────────
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    collection_id: Optional[str] = None

class QueryResponse(BaseModel):
    answer: str
    sources: List[dict]
    query: str
    model_used: str

class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    chunk_count: int
    metadata: dict

class DeleteRequest(BaseModel):
    doc_id: str

class StatsResponse(BaseModel):
    total_chunks: int
    collection_name: str
    embed_model: str
    llm_provider: str

# ─── Helpers ─────────────────────────────────────────────────────────────────
def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks."""
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i : i + size])
        chunks.append(chunk)
        i += size - overlap
    return [c for c in chunks if len(c.strip()) > 20]


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract text from PDF, DOCX, or plain text files."""
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    elif ext == "docx":
        doc = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        return file_bytes.decode("utf-8", errors="ignore")


async def call_groq(prompt: str, context: str) -> str:
    """Call Groq API (free tier) with llama3."""
    system = (
        "You are a helpful assistant. Answer the user's question based ONLY on the "
        "provided context. If the context doesn't contain enough information, say so. "
        "Be concise and accurate."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": f"Context:\n{context}\n\nQuestion: {prompt}"},
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0.2},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def call_huggingface(prompt: str, context: str) -> str:
    """Call HuggingFace Inference API (free tier fallback)."""
    full_prompt = (
        f"[INST] Answer based on the context below.\n\nContext:\n{context}\n\nQuestion: {prompt} [/INST]"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://api-inference.huggingface.co/models/{HF_MODEL}",
            headers={"Authorization": f"Bearer {HF_API_KEY}"},
            json={"inputs": full_prompt, "parameters": {"max_new_tokens": MAX_TOKENS, "temperature": 0.2}},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            text = data[0].get("generated_text", "")
            # Strip prompt echo
            return text.split("[/INST]")[-1].strip()
        return str(data)


async def generate_answer(prompt: str, context: str) -> tuple[str, str]:
    """Try Groq first, fall back to HuggingFace."""
    if GROQ_API_KEY:
        try:
            answer = await call_groq(prompt, context)
            return answer, f"Groq ({GROQ_MODEL})"
        except Exception as e:
            logger.warning("Groq failed: %s — falling back to HuggingFace", e)
    if HF_API_KEY:
        try:
            answer = await call_huggingface(prompt, context)
            return answer, f"HuggingFace ({HF_MODEL})"
        except Exception as e:
            logger.error("HuggingFace failed: %s", e)
    # Last resort: return raw context summary
    return f"Based on retrieved context:\n\n{context[:800]}...", "context-only"


# ─── Routes ──────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "collection_count": collection.count() if collection else 0}


@app.get("/stats", response_model=StatsResponse)
async def stats():
    provider = "Groq" if GROQ_API_KEY else ("HuggingFace" if HF_API_KEY else "context-only")
    return StatsResponse(
        total_chunks=collection.count(),
        collection_name=COLLECTION_NAME,
        embed_model=EMBED_MODEL,
        llm_provider=provider,
    )


@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    """Upload and index a document (PDF, DOCX, TXT)."""
    allowed = {"pdf", "docx", "txt", "md"}
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: .{ext}")

    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(413, "File too large (max 10 MB)")

    text = extract_text(raw, file.filename)
    if not text.strip():
        raise HTTPException(422, "Could not extract text from file")

    chunks = chunk_text(text)
    doc_id = str(uuid.uuid4())

    embeddings = embed_model.encode(chunks, show_progress_bar=False).tolist()
    ids        = [f"{doc_id}_{i}" for i in range(len(chunks))]
    metadatas  = [{"doc_id": doc_id, "filename": file.filename, "chunk_index": i} for i in range(len(chunks))]

    collection.add(documents=chunks, embeddings=embeddings, ids=ids, metadatas=metadatas)
    logger.info("Indexed '%s' → %d chunks (doc_id=%s)", file.filename, len(chunks), doc_id)

    return {"doc_id": doc_id, "filename": file.filename, "chunks_indexed": len(chunks)}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """RAG query: retrieve relevant chunks and generate an answer."""
    if collection.count() == 0:
        raise HTTPException(404, "No documents indexed yet. Please upload documents first.")

    query_embedding = embed_model.encode([req.query]).tolist()
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(req.top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    docs      = results["documents"][0]
    metas     = results["metadatas"][0]
    distances = results["distances"][0]

    sources = []
    context_parts = []
    seen_docs = set()
    for doc, meta, dist in zip(docs, metas, distances):
        score = round(1 - dist, 4)
        sources.append({
            "doc_id":      meta.get("doc_id"),
            "filename":    meta.get("filename"),
            "chunk_index": meta.get("chunk_index"),
            "score":       score,
            "excerpt":     doc[:200] + ("..." if len(doc) > 200 else ""),
        })
        context_parts.append(f"[Source: {meta.get('filename')}]\n{doc}")
        seen_docs.add(meta.get("doc_id"))

    context = "\n\n---\n\n".join(context_parts)
    answer, model_used = await generate_answer(req.query, context)

    return QueryResponse(answer=answer, sources=sources, query=req.query, model_used=model_used)


@app.get("/documents", response_model=List[DocumentInfo])
async def list_documents():
    """List all indexed documents."""
    if collection.count() == 0:
        return []
    all_items = collection.get(include=["metadatas"])
    docs: dict[str, DocumentInfo] = {}
    for meta in all_items["metadatas"]:
        did = meta.get("doc_id", "unknown")
        if did not in docs:
            docs[did] = DocumentInfo(
                doc_id=did,
                filename=meta.get("filename", "unknown"),
                chunk_count=0,
                metadata={"filename": meta.get("filename")},
            )
        docs[did].chunk_count += 1
    return list(docs.values())


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Delete a document and all its chunks from the index."""
    all_items = collection.get(include=["metadatas"])
    ids_to_delete = [
        all_items["ids"][i]
        for i, meta in enumerate(all_items["metadatas"])
        if meta.get("doc_id") == doc_id
    ]
    if not ids_to_delete:
        raise HTTPException(404, "Document not found")
    collection.delete(ids=ids_to_delete)
    logger.info("Deleted doc_id=%s (%d chunks)", doc_id, len(ids_to_delete))
    return {"deleted": len(ids_to_delete), "doc_id": doc_id}


@app.post("/ingest-text")
async def ingest_text(
    text: str = Form(...),
    filename: str = Form(default="manual_input.txt"),
):
    """Directly ingest raw text without file upload."""
    chunks = chunk_text(text)
    if not chunks:
        raise HTTPException(422, "Text too short to index")
    doc_id     = str(uuid.uuid4())
    embeddings = embed_model.encode(chunks, show_progress_bar=False).tolist()
    ids        = [f"{doc_id}_{i}" for i in range(len(chunks))]
    metadatas  = [{"doc_id": doc_id, "filename": filename, "chunk_index": i} for i in range(len(chunks))]
    collection.add(documents=chunks, embeddings=embeddings, ids=ids, metadatas=metadatas)
    return {"doc_id": doc_id, "filename": filename, "chunks_indexed": len(chunks)}
