# Bulk Document Downloader

A Streamlit app for downloading files from HTTP/HTTPS links. Import links from a
CSV, paste one link per line, or combine both sources. Downloads keep the
original file format and can be saved individually or together as a ZIP.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

In the app, upload a CSV and select its URL column and optional filename column.
The app also supports CSV files without a filename column and links pasted
directly into the text area.

Only public HTTP/HTTPS hosts are accepted. Per-file and total-run size limits
are configurable; links that fail are reported separately from successful
downloads.
"# extract-tool" 
