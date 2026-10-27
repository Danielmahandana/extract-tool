# Literature Research Review & Bulk Document Extractor

A specialized Streamlit application for researchers and systematic review teams: enter search strings to automatically discover relevant literature, or ingest existing Scopus CSV exports, and bulk-download authentic full-text PDFs with complete review datasets.

---

## 🔍 Search Literature by String & Auto-Download

Enter any research topic, keywords, or boolean search string (e.g. `machine learning systematic review`, `climate change agriculture resilience`):

1. **Global Scholarly Index**: Queries 250M+ peer-reviewed scholarly works (via OpenAlex, with optional Elsevier Scopus Search API integration).
2. **Strict Paywall Filtering**: Automatically filters out all paywalled and closed-access publications. Only confirmed open-access papers with verified downloadable full-text PDFs are returned.
3. **Screening Table**: Inspect abstracts, citation counts, publication years, journals, and DOIs directly in the app.
4. **Auto-Download Full-Text PDFs**:
   - Queries Open-Access resolvers (**Unpaywall**, **OpenAlex**, **arXiv**, **Europe PMC**).
   - Validates authentic binary PDF magic bytes (`%PDF-`), rejecting login or paywall pages.
   - Formats filenames academically (`Author_Year_Title.pdf`).
   - Packages all retrieved papers in a ZIP archive (`research_papers_<topic>.zip`).
   - Exports the full review dataset (with abstracts, citations, and DOI links) as CSV for PRISMA flowchart documentation.

---

## 📁 Ingest Scopus / Review CSV Exports

For researchers who already ran queries in Scopus or Web of Science and exported a CSV:

1. **Auto-Maps Columns**: Detects `Title`, `Authors`, `Year`, `DOI`, `Source Title / Journal`, `Abstract`, and `Open Access`.
2. **DOI Cascade**: Automatically finds and downloads available open-access PDFs.
3. **Enriched CSV Export**: Combines the original Scopus metadata with local filenames, download outcomes, and direct DOI links for quick institutional access to paywalled papers.

---

## 🌐 General Bulk URL Downloader

- Download any documents or files from public HTTP/HTTPS links.
- Upload any generic CSV with custom URL and filename columns, or paste links directly.
- Download individual files or all files bundled into a single ZIP archive.

---

## 🚀 Running Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

---

## 🧪 Testing

Run the automated test suite verifying search, DOI resolution, and PDF downloads:

```powershell
python test_scopus_handler.py
```
