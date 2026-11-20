#!/usr/bin/env python3
"""
========================================================
  PDF RAG — Terminal Chat (Free Local Embeddings + Groq)
========================================================
  HOW TO USE
  ----------
  1. Get a FREE Groq API key at: https://console.groq.com
  2. Install deps:
       pip install groq chromadb pdfplumber pypdf sentence-transformers
  3. Run:
       python rag.py                         # interactive setup
       python rag.py --pdf path/to/doc.pdf   # skip prompt
       python rag.py --pdf doc.pdf --reset   # re-index (wipe old DB)
========================================================
"""

import os
import sys
import argparse
import textwrap
from pathlib import Path
from typing import List, Dict, Tuple

# ── Dependency check ────────────────────────────────────────────────────────

def _check_deps():
    missing = []
    for pkg, imp in [("groq", "groq"), ("chromadb", "chromadb"), 
                     ("pdfplumber", "pdfplumber"), ("sentence_transformers", "sentence_transformers")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n[ERROR] Missing packages: {', '.join(missing)}")
        print(f"  Run:  pip install {' '.join(missing)}\n")
        sys.exit(1)

_check_deps()

import groq
import chromadb
import pdfplumber
from sentence_transformers import SentenceTransformer

# ── Config ────────────────────────────────────────────────────────────────

# Use a free, local embedding model
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"  # Small, fast, good quality (384-dim)
# Alternative models (uncomment to use):
# EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"  # Better quality, 384-dim
# EMBED_MODEL_NAME = "intfloat/e5-small-v2"    # Another good option

CHAT_MODEL    = "openai/gpt-oss-120b"  # free on Groq
CHUNK_SIZE    = 600    # characters per chunk
CHUNK_OVERLAP = 120    # overlap between chunks
TOP_K         = 5      # how many chunks to retrieve per query
DB_DIR        = "./rag_chroma_db"          # local vector DB folder
COLLECTION    = "pdf_rag"

BANNER = r"""
 ____  ____  ___    ____      _     ____
|  _ \|  _ \|  _\  |  __ \  / \   / ___|
| |_) | |_) | |_)  | |__) |/ _ \ | |  _
|  __/|  _ <|  _\  |  _  // ___ \| |_| |
|_|   |_| \_\_|    |_| \_/_/   \_\\____|

  PDF RAG — Terminal Chat  |  Local Embeddings + Groq LLM
"""

SYSTEM_PROMPT = """\
You are a precise question-answering assistant.
Your ONLY job is to answer the user's question using the CONTEXT CHUNKS provided below.

Rules:
1. Answer ONLY from the context. Do NOT use outside knowledge.
2. If the answer is not in the context, say: "I don't have enough information in the document to answer that."
3. Keep answers concise and factual.
4. At the end of your answer, cite the source page numbers like: [Pages: 3, 7]
5. Never make up or infer facts not present in the context.
"""

# ── PDF Extraction ─────────────────────────────────────────────────────────

def extract_pdf_pages(pdf_path: str) -> List[Dict]:
    """Extract text from each page, returning list of {page, text}."""
    pages = []
    print(f"\n  [PDF] Reading: {pdf_path}")
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                pages.append({"page": i, "text": text})
            _progress(i, total, "  Extracting pages")
    print(f"\n  [PDF] {len(pages)}/{total} pages had extractable text")
    return pages

# ── Chunking ───────────────────────────────────────────────────────────────

def make_chunks(pages: List[Dict], chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP) -> List[Dict]:
    """
    Split page text into overlapping character chunks.
    Each chunk carries its source page number(s).
    """
    chunks = []
    for page_info in pages:
        text = page_info["text"]
        page_num = page_info["page"]
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk_text = text[start:end].strip()
            if len(chunk_text) > 40:  # skip tiny fragments
                chunks.append({
                    "id": f"p{page_num}_s{start}",
                    "text": chunk_text,
                    "page": page_num,
                    "start": start,
                })
            start += chunk_size - overlap
    return chunks

# ── Local Embeddings ────────────────────────────────────────────────────

class LocalEmbedder:
    """Wrapper for local sentence transformer embeddings."""
    
    def __init__(self, model_name: str = EMBED_MODEL_NAME):
        print(f"\n  [Embed] Loading local model: {model_name} (first time may download)...")
        self.model = SentenceTransformer(model_name)
        print(f"  [Embed] Model loaded. Dimension: {self.model.get_sentence_embedding_dimension()}")
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Embed texts using local model."""
        # Convert to list of floats for ChromaDB compatibility
        embeddings = self.model.encode(texts, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]

# ── Vector DB (ChromaDB) ───────────────────────────────────────────────────

def get_collection(reset=False):
    db = chromadb.PersistentClient(path=DB_DIR)
    if reset:
        try:
            db.delete_collection(COLLECTION)
            print("  [DB] Old collection wiped.")
        except Exception:
            pass
    col = db.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    return col

def index_chunks(embedder: LocalEmbedder, collection, chunks: List[Dict]):
    """Embed and insert chunks into ChromaDB."""
    print(f"\n  [DB] Indexing {len(chunks)} chunks …")
    texts = [c["text"] for c in chunks]
    ids   = [c["id"]   for c in chunks]
    metas = [{"page": c["page"]} for c in chunks]

    # Embed in batches
    batch_size = 32
    all_embs = []
    total = len(chunks)
    
    for i in range(0, total, batch_size):
        batch_texts = texts[i:i+batch_size]
        embs = embedder.embed_texts(batch_texts)
        all_embs.extend(embs)
        _progress(min(i + batch_size, total), total, "  Embedding")

    # Insert in batches
    ins_batch = 100
    for i in range(0, total, ins_batch):
        collection.add(
            ids=ids[i:i+ins_batch],
            embeddings=all_embs[i:i+ins_batch],
            documents=texts[i:i+ins_batch],
            metadatas=metas[i:i+ins_batch],
        )
    print(f"\n  [DB] Indexed {total} chunks. DB saved to: {DB_DIR}")

def retrieve(embedder: LocalEmbedder, collection, query: str, top_k=TOP_K) -> List[Dict]:
    """Embed query and retrieve top-k similar chunks."""
    q_emb = embedder.embed_texts([query])[0]
    results = collection.query(
        query_embeddings=[q_emb],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append({
            "text": doc,
            "page": meta["page"],
            "score": round(1 - dist, 4),  # cosine similarity
        })
    return chunks

# ── RAG Query ──────────────────────────────────────────────────────────────

def build_context(chunks: List[Dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(f"--- Chunk {i} (Page {c['page']}, relevance {c['score']}) ---\n{c['text']}")
    return "\n\n".join(parts)

def ask_rag(groq_client: groq.Groq, embedder: LocalEmbedder, collection, 
            question: str, history: List[Dict], top_k: int) -> Tuple[str, List[Dict]]:
    """Full RAG pipeline: retrieve → build context → LLM answer."""
    chunks = retrieve(embedder, collection, question, top_k)
    context = build_context(chunks)

    # Build message list: system + history (last 6 turns) + new user message
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history[-6:]:
        messages.append(turn)
    messages.append({
        "role": "user",
        "content": f"CONTEXT CHUNKS:\n{context}\n\nQUESTION:\n{question}",
    })

    resp = groq_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        temperature=0.1,   # low temp = less hallucination
        max_tokens=1024,
    )
    answer = resp.choices[0].message.content.strip()

    # Update history
    history.append({"role": "user",      "content": question})
    history.append({"role": "assistant", "content": answer})

    return answer, chunks

# ── Terminal UI helpers ────────────────────────────────────────────────────

def _progress(done, total, label=""):
    pct = int(done / total * 30)
    bar = "█" * pct + "░" * (30 - pct)
    print(f"\r{label} [{bar}] {done}/{total}", end="", flush=True)

def _wrap(text: str, width=80, indent="  ") -> str:
    lines = text.split("\n")
    wrapped = []
    for line in lines:
        if line.strip() == "":
            wrapped.append("")
        else:
            wrapped.append(textwrap.fill(line, width=width, initial_indent=indent, subsequent_indent=indent))
    return "\n".join(wrapped)

def _print_answer(answer: str, chunks: List[Dict]):
    pages = sorted(set(c["page"] for c in chunks))
    print("\n" + "─" * 60)
    print(_wrap(answer))
    print("\n  📄 Retrieved from pages:", ", ".join(str(p) for p in pages))
    print("─" * 60 + "\n")

def _print_chunks_debug(chunks: List[Dict]):
    print("\n  [DEBUG] Retrieved chunks:")
    for i, c in enumerate(chunks, 1):
        preview = c["text"][:80].replace("\n", " ")
        print(f"    {i}. Page {c['page']}  score={c['score']}  » {preview}…")
    print()

# ── Setup wizard ───────────────────────────────────────────────────────────

def get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        print("  [✓] GROQ_API_KEY found in environment.")
        return key
    print("\n  Groq API key not found in environment.")
    print("  Get a FREE key at: https://console.groq.com/keys")
    key = input("  Enter your Groq API key: ").strip()
    if not key:
        print("[ERROR] No API key provided. Exiting.")
        sys.exit(1)
    return key

def get_pdf_path(arg_pdf: str = None) -> str:
    if arg_pdf:
        if not Path(arg_pdf).exists():
            print(f"[ERROR] File not found: {arg_pdf}")
            sys.exit(1)
        return arg_pdf
    path = input("\n  Enter path to PDF file: ").strip().strip('"').strip("'")
    if not Path(path).exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)
    return path

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PDF RAG — Terminal Chat (Local Embeddings + Groq)")
    parser.add_argument("--pdf",   help="Path to PDF file to index")
    parser.add_argument("--reset", action="store_true", help="Wipe existing vector DB and re-index")
    parser.add_argument("--debug", action="store_true", help="Show retrieved chunks for each query")
    parser.add_argument("--top-k", type=int, default=TOP_K, help=f"Chunks to retrieve (default {TOP_K})")
    args = parser.parse_args()

    print(BANNER)

    # ── API key for Groq (LLM only)
    api_key = get_api_key()
    groq_client = groq.Groq(api_key=api_key)

    # ── Local embedder (no API key needed!)
    embedder = LocalEmbedder()

    # ── Collection
    collection = get_collection(reset=args.reset)

    # ── Check if DB already has content
    count = collection.count()
    if count > 0 and not args.reset:
        print(f"\n  [DB] Found existing index with {count} chunks.")
        choice = input("  Use existing index? [Y/n]: ").strip().lower()
        if choice in ("n", "no"):
            pdf_path = get_pdf_path(args.pdf)
            collection = get_collection(reset=True)
            pages  = extract_pdf_pages(pdf_path)
            chunks = make_chunks(pages)
            index_chunks(embedder, collection, chunks)
        else:
            print("  [✓] Using existing index.")
    else:
        # ── Index a new PDF
        pdf_path = get_pdf_path(args.pdf)
        pages  = extract_pdf_pages(pdf_path)
        if not pages:
            print("[ERROR] No text extracted from PDF. Is it a scanned image-only PDF?")
            print("  Tip: Use OCR tools (e.g., ocrmypdf) to add a text layer first.")
            sys.exit(1)
        chunks = make_chunks(pages)
        print(f"  [Chunks] Created {len(chunks)} chunks  "
              f"(size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP})")
        index_chunks(embedder, collection, chunks)

    # ── Chat loop
    print("\n" + "═" * 60)
    print("  ✅  RAG ready! Ask questions about your PDF.")
    print("  Commands:  'quit' / 'exit' to leave")
    print("             'clear' to reset conversation history")
    print("             'chunks' to toggle chunk debug view")
    print("             'top N' to change number of chunks retrieved")
    print("═" * 60 + "\n")

    history = []
    debug   = args.debug
    top_k   = args.top_k

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Bye!\n")
            break

        if not question:
            continue

        q_lower = question.lower()

        if q_lower in ("quit", "exit", "q"):
            print("\n  Bye!\n")
            break

        if q_lower == "clear":
            history = []
            print("  [✓] Conversation history cleared.\n")
            continue

        if q_lower == "chunks":
            debug = not debug
            print(f"  [✓] Chunk debug: {'ON' if debug else 'OFF'}\n")
            continue

        if q_lower.startswith("top "):
            try:
                top_k = int(q_lower.split()[1])
                print(f"  [✓] Top-K set to {top_k}\n")
            except (ValueError, IndexError):
                print("  Usage: top <number>  e.g.  top 3\n")
            continue

        print("  [Thinking…]", end="\r", flush=True)
        try:
            answer, chunks = ask_rag(groq_client, embedder, collection, question, history, top_k)
        except Exception as e:
            print(f"\n  [Error] {e}\n")
            continue

        if debug:
            _print_chunks_debug(chunks)

        _print_answer(answer, chunks)


if __name__ == "__main__":
    main()