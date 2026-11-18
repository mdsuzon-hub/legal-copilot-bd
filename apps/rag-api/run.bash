# Interactive setup (it will prompt for key + PDF path)
python apps/rag-api/rag_pdf.py

# Or pass arguments directly
python apps/rag-api/rag_pdf.py --pdf my_document.pdf

# Force re-index if you want to swap PDFs
python apps/rag-api/rag_pdf.py --pdf new_doc.pdf --reset

# Start the API server
python apps/rag-api/rag_server.py