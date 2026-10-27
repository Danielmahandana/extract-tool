import csv
import io
import ipaddress
import mimetypes
import os
import re
import socket
import time
import zipfile
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests
import streamlit as st

import scopus_handler

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
CHUNK_SIZE = 64 * 1024
USER_AGENT = "DocumentDownloader/1.0 (Streamlit)"


def parse_csv(file_bytes):
    """Return CSV headers and rows, decoding common spreadsheet exports."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("cp1252", errors="replace")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    rows = list(reader)
    if not rows:
        raise ValueError("The CSV file is empty.")

    headers = [header.strip() for header in rows[0]]
    if not any(headers):
        raise ValueError("The CSV file does not have a header row.")
    return headers, rows[1:]


def _column_index(columns, preferred_terms):
    for index, column in enumerate(columns):
        normalized = column.casefold()
        if any(term in normalized for term in preferred_terms):
            return index
    return 0


def _source_extension(url, content_disposition, content_type):
    if content_disposition:
        message = Message()
        message["content-disposition"] = content_disposition
        disposition_name = message.get_filename()
        if disposition_name:
            extension = os.path.splitext(unquote(disposition_name))[1]
            if extension:
                return extension

    path_name = os.path.basename(urlparse(url).path)
    extension = os.path.splitext(unquote(path_name))[1]
    if extension and re.fullmatch(r"\.[A-Za-z0-9]{1,12}", extension):
        return extension

    mime_type = (content_type or "").split(";", 1)[0].strip().lower()
    if mime_type:
        return mimetypes.guess_extension(mime_type) or ".bin"
    return ".bin"


def safe_filename(name, fallback="document"):
    name = unquote(str(name or "")).replace("\\", "/")
    name = os.path.basename(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    stem, extension = os.path.splitext(name)
    if len(name) > 180:
        name = (stem[: max(1, 180 - len(extension))] + extension)[:180]
    return name


def make_filename(url, headers, custom_name, index):
    content_disposition = headers.get("Content-Disposition", "")
    content_type = headers.get("Content-Type", "")
    extension = _source_extension(url, content_disposition, content_type)

    if custom_name:
        name = safe_filename(custom_name, f"document_{index}")
        if not os.path.splitext(name)[1]:
            name += extension
        return name

    if content_disposition:
        message = Message()
        message["content-disposition"] = content_disposition
        disposition_name = message.get_filename()
        if disposition_name:
            return safe_filename(disposition_name, f"document_{index}")

    path_name = os.path.basename(urlparse(url).path)
    if path_name and re.fullmatch(r"\.[A-Za-z0-9]{1,12}", os.path.splitext(path_name)[1]):
        return safe_filename(path_name, f"document_{index}")
    return safe_filename(f"document_{index}{extension}")


def _validate_public_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only complete HTTP or HTTPS URLs are supported.")
        if parsed.username or parsed.password:
            raise ValueError("URLs containing embedded credentials are not supported.")
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"Invalid URL: {error}") from error

    hostname = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            ]
        except OSError as error:
            raise ValueError(f"Could not resolve the URL host: {error}") from error

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Links to private, local, or reserved network addresses are not allowed.")


def download_document(url, custom_name, index, max_bytes, timeout):
    current_url = url.strip()
    for redirect_number in range(MAX_REDIRECTS + 1):
        _validate_public_url(current_url)
        try:
            response = requests.get(
                current_url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            raise ValueError(f"Request failed: {error}") from error

        with response:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(f"HTTP {response.status_code} redirect did not include a Location header.")
                if redirect_number == MAX_REDIRECTS:
                    raise ValueError(f"The link exceeded the {MAX_REDIRECTS}-redirect limit.")
                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()
            except requests.RequestException as error:
                raise ValueError(f"HTTP error: {error}") from error

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > max_bytes:
                    raise ValueError(f"File exceeds the size limit ({max_bytes / (1024 * 1024):.1f} MB).")

            output = io.BytesIO()
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(f"File exceeds the size limit ({max_bytes / (1024 * 1024):.1f} MB).")
                output.write(chunk)

            filename = make_filename(
                current_url,
                response.headers,
                custom_name,
                index,
            )
            return filename, output.getvalue(), response.headers.get("Content-Type", "")

    raise ValueError("The link could not be downloaded.")


def unique_name(filename, used_names):
    stem, extension = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while candidate.casefold() in used_names:
        candidate = f"{stem}_{counter}{extension}"
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def collect_links(csv_rows, url_column, name_column, pasted_links):
    links = []
    for row_number, row in enumerate(csv_rows, start=2):
        url = row[url_column].strip() if url_column < len(row) else ""
        if not url:
            continue
        custom_name = row[name_column].strip() if name_column is not None and name_column < len(row) else ""
        links.append((url, custom_name, f"CSV row {row_number}"))

    for line_number, line in enumerate(pasted_links.splitlines(), start=1):
        url = line.strip()
        if url:
            links.append((url, "", f"Pasted link {line_number}"))
    return links


# ---------------------------------------------------------------------------
# Reusable Downloader & Results Presentation for Papers
# ---------------------------------------------------------------------------

def run_paper_downloads(
    to_process,
    naming_format,
    contact_email,
    max_pdf_mb,
    delay,
    filter_paywalled=True,
    session_key="search_review_results",
):
    progress_bar = st.progress(0)
    status_box = st.empty()

    results = []
    paywalled_count = 0
    used_names = set()
    max_bytes = int(max_pdf_mb) * 1024 * 1024

    for pos, rec in enumerate(to_process, start=1):
        title_display = rec["title"][:50] + "..." if len(rec["title"]) > 50 else rec["title"]
        status_box.write(f"Downloading ({pos}/{len(to_process)}): **{rec.get('doi') or 'Direct'}** — *{title_display}*")

        # Determine academic filename
        if "Year_Author" in naming_format:
            author = scopus_handler.extract_first_author(rec.get("authors"))
            yr = rec.get("year") or "ND"
            words = re.findall(r"[A-Za-z0-9]+", rec.get("title", ""))
            short_title = "_".join(words[:6]) if words else f"Paper_{pos}"
            base_fn = f"{yr}_{author}_{short_title}.pdf"
        elif "Author_Title" in naming_format:
            author = scopus_handler.extract_first_author(rec.get("authors"))
            words = re.findall(r"[A-Za-z0-9]+", rec.get("title", ""))
            short_title = "_".join(words[:6]) if words else f"Paper_{pos}"
            base_fn = f"{author}_{short_title}.pdf"
        elif "DOI" in naming_format and rec.get("doi"):
            base_fn = f"{safe_filename(rec['doi'], f'Paper_{pos}')}.pdf"
        else:
            base_fn = scopus_handler.format_academic_filename(
                rec.get("authors"), rec.get("year"), rec.get("title"), pos
            )

        final_fn = unique_name(base_fn, used_names)

        record_result = {
            **rec,
            "filename": final_fn,
            "status": "Pending",
            "resolver_source": "",
            "pdf_url": rec.get("pdf_url") or "",
            "pdf_bytes": None,
            "error": "",
        }

        # Build candidate URLs
        candidates = []
        if rec.get("pdf_url"):
            candidates.append((rec["pdf_url"], "Direct OA Link"))

        if rec.get("doi"):
            oa_candidates = scopus_handler.get_oa_pdf_candidates(
                rec["doi"],
                title=rec.get("title"),
                contact_email=contact_email,
                timeout=12,
            )
            for c_url, c_src in oa_candidates:
                if not any(c_url == existing[0] for existing in candidates):
                    candidates.append((c_url, c_src))

        if not candidates:
            record_result["status"] = "Paywalled / Closed Access"
            record_result["error"] = "No open-access repository or publisher PDF found."
            paywalled_count += 1
        else:
            try:
                pdf_bytes, final_url, resolver_src = scopus_handler.download_authentic_pdf(
                    candidates,
                    max_bytes=max_bytes,
                    timeout=25,
                )
                record_result["status"] = "Downloaded"
                record_result["pdf_bytes"] = pdf_bytes
                record_result["pdf_url"] = final_url
                record_result["resolver_source"] = resolver_src
            except Exception as err:
                record_result["status"] = "Paywalled / Failed"
                record_result["error"] = str(err)
                paywalled_count += 1

        if not filter_paywalled or record_result["status"] == "Downloaded":
            results.append(record_result)

        progress_bar.progress(pos / len(to_process))
        if delay > 0 and pos < len(to_process):
            time.sleep(float(delay))

    st.session_state[session_key] = results
    if filter_paywalled and paywalled_count > 0:
        status_box.success(
            f"🎉 Run complete! Retained {len(results)} verified Open-Access PDFs ({paywalled_count} paywalled papers filtered out)."
        )
    else:
        status_box.success("🎉 Download run complete! Review results below.")


def render_paper_results(results_list, archive_name="research_papers.zip", report_name="research_review_report.csv"):
    if not results_list:
        return

    downloaded = [r for r in results_list if r["status"] == "Downloaded"]
    paywalled = [r for r in results_list if "Paywalled" in r["status"]]
    failed = [r for r in results_list if r["status"] not in ("Downloaded",) and "Paywalled" not in r["status"]]

    st.markdown("---")
    st.subheader("📊 Systematic Review Retrieval Summary")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Processed", len(results_list))
    m2.metric("✅ OA PDFs Downloaded", len(downloaded))
    m3.metric("🔒 Paywalled / Institutional", len(paywalled))
    rate = f"{(len(downloaded) / max(1, len(results_list))) * 100:.1f}%"
    m4.metric("Open-Access Rate", rate)

    # Download Package Buttons
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if downloaded:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
                for r in downloaded:
                    zip_file.writestr(r["filename"], r["pdf_bytes"])
            st.download_button(
                label=f"📦 Download All {len(downloaded)} PDFs (ZIP Archive)",
                data=archive.getvalue(),
                file_name=archive_name,
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
        else:
            st.info("No open-access PDFs downloaded in this run.")

    with btn_col2:
        enriched_csv_str = scopus_handler.generate_review_csv(results_list)
        st.download_button(
            label="📑 Download Full Review Dataset (CSV)",
            data=enriched_csv_str,
            file_name=report_name,
            mime="text/csv",
            use_container_width=True,
            help="Exports dataset with titles, authors, years, abstracts, download statuses, OA URLs, and DOI links for PRISMA flowchart documentation.",
        )

    # Breakdown tabs
    tab_downloaded, tab_paywalled, tab_all = st.tabs([
        f"✅ Downloaded ({len(downloaded)})",
        f"🔒 Paywalled / Institutional ({len(paywalled)})",
        f"All Records ({len(results_list)})",
    ])

    with tab_downloaded:
        if downloaded:
            for item in downloaded:
                with st.expander(f"📄 {item['filename']} ({item['resolver_source']})"):
                    st.markdown(f"**Title**: {item['title']}")
                    st.markdown(f"**Authors**: {item.get('authors', '')} ({item.get('year', '')})")
                    if item.get("doi"):
                        st.markdown(f"**DOI**: [{item['doi']}](https://doi.org/{item['doi']})")
                    st.markdown(f"**Resolved Source**: `{item['resolver_source']}`")
                    st.markdown(f"**Source URL**: [{item['pdf_url']}]({item['pdf_url']})")
                    if item.get("abstract"):
                        st.markdown(f"**Abstract**: {item['abstract']}")
                    st.download_button(
                        label=f"📥 Download {item['filename']}",
                        data=item["pdf_bytes"],
                        file_name=item["filename"],
                        mime="application/pdf",
                        key=f"dl_pdf_{item['filename']}_{item.get('index', 0)}",
                    )
        else:
            st.write("No papers downloaded yet.")

    with tab_paywalled:
        if paywalled:
            st.caption(
                "These papers could not be found in public open-access repositories. "
                "Click the direct DOI link to access via your university / institutional library proxy."
            )
            paywalled_data = [
                {
                    "Title": r["title"][:60] + "...",
                    "Authors": scopus_handler.extract_first_author(r.get("authors")),
                    "Year": r.get("year", ""),
                    "DOI Link": f"https://doi.org/{r['doi']}" if r.get("doi") else "N/A",
                    "Reason": r["error"] or "Paywalled / Closed Access",
                }
                for r in paywalled
            ]
            st.dataframe(pd.DataFrame(paywalled_data), use_container_width=True, hide_index=True)
        else:
            st.write("No paywalled papers found.")

    with tab_all:
        all_table_data = [
            {
                "Title": r["title"][:50] + "...",
                "First Author": scopus_handler.extract_first_author(r.get("authors")),
                "Year": r.get("year", ""),
                "Status": r["status"],
                "Source": r["resolver_source"] or "N/A",
                "Filename": r["filename"] if r["status"] == "Downloaded" else "-",
                "DOI": r.get("doi") or "N/A",
            }
            for r in results_list
        ]
        st.dataframe(pd.DataFrame(all_table_data), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 1: Search Literature by String & Auto-Download
# ---------------------------------------------------------------------------

def render_search_literature_tab():
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); 
                    padding: 1.25rem 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; border: 1px solid #334155;">
            <h3 style="margin: 0 0 0.5rem 0; color: #f8fafc; font-size: 1.25rem;">
                🔍 Search Scholarly Database & Auto-Download Literature
            </h3>
            <p style="margin: 0; color: #94a3b8; font-size: 0.95rem; line-height: 1.4;">
                Type any search query or research topic. The tool queries global scholarly indexes (250M+ peer-reviewed works), 
                identifies relevant papers and sites, and automatically downloads the full-text PDFs and complete metadata.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Search query input
    query = st.text_input(
        "Enter Research Topic or Search String",
        value=st.session_state.get("active_query", ""),
        placeholder="e.g. machine learning systematic review, climate change crop resilience, CAR-T cell immunotherapy",
        help="Type keywords, authors, or a research question to search peer-reviewed literature.",
    )

    # Quick search prompt chips
    st.caption("Quick example topics:")
    chip_col1, chip_col2, chip_col3, chip_col4 = st.columns(4)
    with chip_col1:
        if st.button("🤖 Machine Learning Review", use_container_width=True):
            st.session_state["active_query"] = "machine learning systematic review"
            st.rerun()
    with chip_col2:
        if st.button("🧬 CRISPR Gene Therapy", use_container_width=True):
            st.session_state["active_query"] = "CRISPR gene editing cancer therapy"
            st.rerun()
    with chip_col3:
        if st.button("🌱 Climate Change Agriculture", use_container_width=True):
            st.session_state["active_query"] = "climate change resilience agriculture"
            st.rerun()
    with chip_col4:
        if st.button("⚡ Renewable Energy Storage", use_container_width=True):
            st.session_state["active_query"] = "renewable energy battery storage grid"
            st.rerun()

    # Advanced Search Filters
    with st.expander("Search Filters & Database Options", expanded=False):
        f_col1, f_col2, f_col3 = st.columns(3)
        with f_col1:
            db_source = st.selectbox(
                "Scholarly Database Source",
                ["OpenAlex Global Index (250M+ Works, Open & Free)", "Scopus Search API (Requires Elsevier Key)"],
            )
            scopus_key = ""
            if "Scopus" in db_source:
                scopus_key = st.text_input("Elsevier API Key", type="password", help="Enter your personal or institutional Elsevier API key.")
        with f_col2:
            max_results = st.number_input("Max Papers to Retrieve", min_value=5, max_value=200, value=25, step=5)
            sort_by = st.selectbox(
                "Sort Results By",
                ["relevance_score:desc", "cited_by_count:desc", "publication_year:desc"],
                format_func=lambda x: {
                    "relevance_score:desc": "Most Relevant",
                    "cited_by_count:desc": "Most Cited",
                    "publication_year:desc": "Most Recent",
                }[x],
            )
        with f_col3:
            oa_only = st.checkbox(
                "Strict Open-Access Only (Filter out all paywalled papers)",
                value=True,
                help="Excludes all paywalled papers so only confirmed open-access publications with downloadable PDFs return.",
            )
            year_range = st.slider("Publication Year Range", min_value=1990, max_value=2026, value=(2018, 2026))

    # Search Trigger
    if st.button("🔍 Find Relevant Papers", type="primary", disabled=not query.strip()):
        with st.spinner(f"Searching scholarly databases for: '{query}'..."):
            db_type = "scopus" if "Scopus" in db_source else "openalex"
            records, total_matches = scopus_handler.search_academic_literature(
                query=query.strip(),
                max_results=int(max_results),
                open_access_only=oa_only,
                start_year=year_range[0],
                end_year=year_range[1],
                sort_by=sort_by,
                source_database=db_type,
                scopus_api_key=scopus_key,
            )
            st.session_state["search_found_records"] = records
            st.session_state["search_total_matches"] = total_matches
            st.session_state["search_query_used"] = query.strip()
            st.session_state["search_oa_only"] = oa_only

    # Display Found Papers for Screening
    records = st.session_state.get("search_found_records", [])
    total_matches = st.session_state.get("search_total_matches", 0)
    query_used = st.session_state.get("search_query_used", "")
    oa_used = st.session_state.get("search_oa_only", True)

    if records:
        st.markdown("---")
        st.markdown(
            f"#### 📚 Found **{total_matches:,}** matching scholarly works for *'{query_used}'* (Displaying top {len(records)})"
        )
        if oa_used:
            st.success(
                f"✓ **Paywall Filter Active**: All {len(records)} papers shown are verified Open Access with direct full-text PDFs. All paywalled papers were filtered out."
            )
        st.caption("Select papers to download, inspect abstracts, and configure naming:")

        # Screening Dataframe
        df_display = pd.DataFrame([
            {
                "Download": True,
                "#": r["index"],
                "Title": r["title"][:75] + ("..." if len(r["title"]) > 75 else ""),
                "First Author": scopus_handler.extract_first_author(r["authors"]),
                "Year": r["year"],
                "Citations": r.get("citations", 0),
                "Journal / Venue": r["journal"][:35] + ("..." if len(r["journal"]) > 35 else ""),
                "DOI": r["doi"] or "(Missing DOI)",
                "OA PDF Available": "✅ Yes" if r.get("pdf_url") else "🔍 Auto-Resolve",
            }
            for r in records
        ])

        edited_df = st.data_editor(
            df_display,
            column_config={
                "Download": st.column_config.CheckboxColumn("Select", help="Check to download this paper", default=True),
            },
            disabled=["#", "Title", "First Author", "Year", "Citations", "Journal / Venue", "DOI", "OA PDF Available"],
            use_container_width=True,
            hide_index=True,
            key="screening_editor",
        )

        selected_indices = set(edited_df[edited_df["Download"]]["#"].tolist())
        selected_records = [r for r in records if r["index"] in selected_indices]

        # Abstract Inspector
        with st.expander("📖 Inspect Paper Abstracts & Details", expanded=False):
            for r in records:
                st.markdown(f"**#{r['index']} — {r['title']}** ({r['authors']}, {r['year']})")
                if r.get("abstract"):
                    st.markdown(f"> *{r['abstract']}*")
                else:
                    st.caption("No abstract preview available.")
                if r.get("doi"):
                    st.markdown(f"DOI: [{r['doi']}](https://doi.org/{r['doi']})")
                st.markdown("---")

        # Download Settings & Action
        st.markdown("### ⚙️ Download Configuration")
        opt_col1, opt_col2, opt_col3 = st.columns(3)
        with opt_col1:
            naming_format = st.selectbox(
                "Academic Filename Format",
                [
                    "Author_Year_Title.pdf (Recommended)",
                    "Year_Author_Title.pdf",
                    "Author_Title.pdf",
                    "DOI.pdf",
                ],
                key="search_naming_fmt",
            )
        with opt_col2:
            contact_email = st.text_input(
                "Polite Pool Contact Email",
                value="research.review@extract-tool.org",
                key="search_email",
            )
        with opt_col3:
            delay = st.number_input(
                "Request Delay (seconds)",
                min_value=0.0,
                max_value=5.0,
                value=0.5,
                step=0.1,
                key="search_delay",
            )

        if st.button(f"🚀 Download {len(selected_records)} Selected Papers & Data", type="primary", disabled=not selected_records):
            run_paper_downloads(
                selected_records,
                naming_format,
                contact_email,
                max_pdf_mb=50,
                delay=delay,
                session_key="search_download_results",
            )

    # Render results
    render_paper_results(
        st.session_state.get("search_download_results", []),
        archive_name=f"research_papers_{safe_filename(query_used, 'review')}.zip",
        report_name=f"review_dataset_{safe_filename(query_used, 'review')}.csv",
    )


# ---------------------------------------------------------------------------
# Tab 2: Upload Scopus / Review CSV Export
# ---------------------------------------------------------------------------

def render_scopus_upload_tab():
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); 
                    padding: 1.25rem 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; border: 1px solid #334155;">
            <h3 style="margin: 0 0 0.5rem 0; color: #f8fafc; font-size: 1.25rem;">
                📁 Ingest Existing Scopus CSV or DOI List
            </h3>
            <p style="margin: 0; color: #94a3b8; font-size: 0.95rem; line-height: 1.4;">
                Already exported your literature search from Scopus, Web of Science, or PubMed? 
                Upload the CSV export here to auto-resolve DOIs, name PDFs academically, and bulk-download full-texts.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_upload, col_sample = st.columns([3, 1])
    with col_upload:
        scopus_file = st.file_uploader(
            "Upload Scopus CSV Export",
            type=["csv"],
            key="scopus_file_uploader",
            help="Export literature records from Scopus as CSV and upload here.",
        )
    with col_sample:
        st.write("")
        st.write("")
        load_sample = st.button("Load Sample Dataset", help="Load sample research papers from Scopus for testing")

    csv_bytes = None
    if scopus_file is not None:
        csv_bytes = scopus_file.getvalue()
    elif load_sample:
        sample_path = os.path.join(os.path.dirname(__file__), "sample_scopus_export.csv")
        if os.path.exists(sample_path):
            with open(sample_path, "rb") as f:
                csv_bytes = f.read()
            st.session_state["scopus_sample_loaded"] = True

    if "scopus_sample_loaded" in st.session_state and csv_bytes is None:
        sample_path = os.path.join(os.path.dirname(__file__), "sample_scopus_export.csv")
        if os.path.exists(sample_path):
            with open(sample_path, "rb") as f:
                csv_bytes = f.read()

    pasted_dois = st.text_area(
        "Or paste DOIs directly (one DOI per line)",
        placeholder="10.1371/journal.pone.0215754\n10.48550/arXiv.1706.03762\nhttps://doi.org/10.1016/j.cell.2021.01.001",
        height=100,
        help="Paste DOI strings or DOI URLs if you don't have a CSV file.",
    )

    records = []
    if csv_bytes is not None:
        try:
            headers, raw_rows = parse_csv(csv_bytes)
            col_map = scopus_handler.detect_scopus_columns(headers)
            is_scopus = scopus_handler.is_scopus_export(headers)

            if is_scopus:
                st.success(f"✓ Detected Scopus literature export: {len(raw_rows)} records found.")
            else:
                st.info(f"Custom CSV loaded: {len(raw_rows)} records. Auto-mapped columns where available.")

            with st.expander("Column Mapping Settings", expanded=not is_scopus):
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    doi_idx = st.selectbox(
                        "DOI Column",
                        [None] + list(range(len(headers))),
                        index=col_map["doi"] + 1 if col_map["doi"] is not None else 0,
                        format_func=lambda i: "None" if i is None else headers[i],
                    )
                with c2:
                    title_idx = st.selectbox(
                        "Title Column",
                        [None] + list(range(len(headers))),
                        index=col_map["title"] + 1 if col_map["title"] is not None else 0,
                        format_func=lambda i: "None" if i is None else headers[i],
                    )
                with c3:
                    authors_idx = st.selectbox(
                        "Authors Column",
                        [None] + list(range(len(headers))),
                        index=col_map["authors"] + 1 if col_map["authors"] is not None else 0,
                        format_func=lambda i: "None" if i is None else headers[i],
                    )
                with c4:
                    year_idx = st.selectbox(
                        "Year Column",
                        [None] + list(range(len(headers))),
                        index=col_map["year"] + 1 if col_map["year"] is not None else 0,
                        format_func=lambda i: "None" if i is None else headers[i],
                    )

            # Build record list
            for idx, row in enumerate(raw_rows, start=1):
                raw_doi = row[doi_idx].strip() if doi_idx is not None and doi_idx < len(row) else ""
                cleaned_doi = scopus_handler.clean_doi(raw_doi)
                title = row[title_idx].strip() if title_idx is not None and title_idx < len(row) else f"Paper {idx}"
                authors = row[authors_idx].strip() if authors_idx is not None and authors_idx < len(row) else ""
                year = row[year_idx].strip() if year_idx is not None and year_idx < len(row) else ""
                journal = (
                    row[col_map["journal"]].strip()
                    if col_map["journal"] is not None and col_map["journal"] < len(row)
                    else ""
                )
                abstract = (
                    row[col_map["abstract"]].strip()
                    if col_map["abstract"] is not None and col_map["abstract"] < len(row)
                    else ""
                )
                oa_status = (
                    row[col_map["open_access"]].strip()
                    if col_map["open_access"] is not None and col_map["open_access"] < len(row)
                    else ""
                )

                records.append({
                    "index": idx,
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "journal": journal,
                    "doi": cleaned_doi,
                    "raw_doi": raw_doi,
                    "abstract": abstract,
                    "open_access": oa_status,
                })

        except Exception as e:
            st.error(f"Error parsing CSV: {e}")

    # Add pasted DOIs
    if pasted_dois.strip():
        start_idx = len(records) + 1
        for line_num, line in enumerate(pasted_dois.splitlines(), start=1):
            line_str = line.strip()
            if not line_str:
                continue
            cleaned = scopus_handler.clean_doi(line_str)
            if cleaned:
                records.append({
                    "index": start_idx + line_num - 1,
                    "title": f"DOI Record ({cleaned})",
                    "authors": "Unknown",
                    "year": "",
                    "journal": "",
                    "doi": cleaned,
                    "raw_doi": line_str,
                    "abstract": "",
                    "open_access": "",
                })

    if not records:
        st.info("👆 Upload a Scopus CSV file, click 'Load Sample Dataset', or paste DOIs above to begin.")
        return

    # Review Screening Table & Search Filter
    st.subheader(f"Literature Review Records ({len(records)} found)")
    filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])
    with filter_col1:
        search_query = st.text_input("🔍 Filter by Keyword", "")
    with filter_col2:
        only_with_doi = st.checkbox("Only valid DOIs", value=True)
    with filter_col3:
        filter_paywall = st.checkbox(
            "Strict Open-Access Only",
            value=True,
            help="Filters out paywalled and closed access records so only open-access papers are shown.",
        )

    filtered_records = records
    if only_with_doi:
        filtered_records = [r for r in filtered_records if r["doi"]]
    if filter_paywall:
        filtered_records = [
            r for r in filtered_records
            if not any(k in str(r.get("open_access", "")).casefold() for k in ("closed", "no", "subscription", "paywall"))
        ]
    if search_query.strip():
        q = search_query.strip().casefold()
        filtered_records = [
            r for r in filtered_records
            if q in r["title"].casefold()
            or q in r["authors"].casefold()
            or q in r["journal"].casefold()
            or q in r["abstract"].casefold()
        ]

    # Show preview table
    preview_df = pd.DataFrame([
        {
            "#": r["index"],
            "Title": r["title"][:70] + ("..." if len(r["title"]) > 70 else ""),
            "First Author": scopus_handler.extract_first_author(r["authors"]),
            "Year": r["year"],
            "Journal": r["journal"][:35] + ("..." if len(r["journal"]) > 35 else ""),
            "DOI": r["doi"] or "(Missing DOI)",
            "Scopus OA": r["open_access"] or "Unknown",
        }
        for r in filtered_records
    ])
    st.dataframe(preview_df, use_container_width=True, hide_index=True)

    # Resolution & Download Options
    st.markdown("### ⚙️ Full-Text Download Settings")
    opt_col1, opt_col2, opt_col3 = st.columns(3)
    with opt_col1:
        max_papers = st.number_input(
            "Max papers to download",
            min_value=1,
            max_value=1000,
            value=min(100, max(1, len(filtered_records))),
            key="upload_max_papers",
        )
    with opt_col2:
        contact_email = st.text_input(
            "Contact Email (for OA polite pool)",
            value="extract-tool@research.org",
            key="upload_email",
        )
    with opt_col3:
        naming_format = st.selectbox(
            "Academic File Naming Format",
            [
                "Author_Year_Title.pdf (Recommended)",
                "Year_Author_Title.pdf",
                "Author_Title.pdf",
                "DOI.pdf",
            ],
            key="upload_naming_fmt",
        )

    delay = st.number_input(
        "Polite delay between requests (seconds)",
        min_value=0.0,
        max_value=5.0,
        value=0.5,
        step=0.1,
        key="upload_delay",
    )

    if st.button("🚀 Download Open-Access Full-Text PDFs", type="primary", disabled=not filtered_records):
        to_process = filtered_records[: int(max_papers)]
        run_paper_downloads(
            to_process,
            naming_format,
            contact_email,
            max_pdf_mb=50,
            delay=delay,
            filter_paywalled=filter_paywall,
            session_key="upload_download_results",
        )

    render_paper_results(
        st.session_state.get("upload_download_results", []),
        archive_name="scopus_review_papers.zip",
        report_name="scopus_review_enriched_report.csv",
    )


# ---------------------------------------------------------------------------
# Tab 3: General Bulk URL Downloader
# ---------------------------------------------------------------------------

def render_general_downloader_tab():
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); 
                    padding: 1.25rem 1.5rem; border-radius: 12px; margin-bottom: 1.5rem; border: 1px solid #334155;">
            <h3 style="margin: 0 0 0.5rem 0; color: #f8fafc; font-size: 1.25rem;">
                🌐 General Bulk URL Downloader
            </h3>
            <p style="margin: 0; color: #94a3b8; font-size: 0.95rem; line-height: 1.4;">
                Download documents and files from HTTP/HTTPS links. Import links from a generic CSV, paste them directly, or both.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    csv_file = st.file_uploader("Import a generic CSV file", type=["csv"], key="general_csv_uploader")
    csv_rows = []
    url_column = None
    name_column = None

    if csv_file is not None:
        try:
            columns, csv_rows = parse_csv(csv_file.getvalue())
            column_options = list(range(len(columns)))
            url_column = st.selectbox(
                "CSV column containing URLs",
                column_options,
                index=_column_index(columns, ("url", "link")),
                format_func=lambda index: columns[index] or f"Column {index + 1}",
            )
            name_options = [None] + column_options
            name_column = st.selectbox(
                "Optional column to use for file names",
                name_options,
                index=next(
                    (
                        position + 1
                        for position, column in enumerate(columns)
                        if any(term in column.casefold() for term in ("title", "name", "filename"))
                    ),
                    0,
                ),
                format_func=lambda index: "No custom names" if index is None else (
                    columns[index] or f"Column {index + 1}"
                ),
            )
            st.caption(f"CSV loaded: {len(csv_rows)} data rows.")
        except (UnicodeError, csv.Error, ValueError) as error:
            st.error(f"Could not read this CSV: {error}")
            csv_rows = []
            url_column = None

    pasted_links = st.text_area(
        "Or paste links (one URL per line)",
        placeholder="https://example.com/document.pdf",
        height=120,
    )
    max_files = st.number_input("Maximum links to process", min_value=1, max_value=500, value=100)
    max_file_mb = st.number_input(
        "Maximum size per file (MB)",
        min_value=1,
        max_value=500,
        value=50,
    )
    max_total_mb = st.number_input(
        "Maximum total download size per run (MB)",
        min_value=1,
        max_value=500,
        value=200,
    )
    timeout = st.number_input("Request timeout (seconds)", min_value=5, max_value=180, value=30)
    delay = st.number_input(
        "Delay between downloads (seconds)",
        min_value=0.0,
        max_value=10.0,
        value=0.5,
        step=0.1,
    )

    links = collect_links(
        csv_rows,
        url_column if url_column is not None else -1,
        name_column,
        pasted_links,
    )
    st.caption(f"{len(links)} link(s) ready.")

    if st.button("Download files", type="primary", disabled=not links):
        selected_links = links[: int(max_files)]
        if len(links) > len(selected_links):
            st.warning(f"Only the first {len(selected_links)} links will be processed.")

        results = []
        used_names = set()
        progress = st.progress(0)
        status = st.empty()
        max_file_bytes = int(max_file_mb) * 1024 * 1024
        max_total_bytes = int(max_total_mb) * 1024 * 1024
        downloaded_bytes = 0

        for position, (url, custom_name, source) in enumerate(selected_links, start=1):
            status.write(f"Downloading {position} of {len(selected_links)}: {url}")
            result = {"source": source, "url": url, "filename": "", "data": None, "content_type": "", "error": ""}
            remaining_bytes = max_total_bytes - downloaded_bytes
            if remaining_bytes <= 0:
                result["error"] = f"The total download limit of {int(max_total_mb)} MB has been reached."
            else:
                try:
                    filename, data, content_type = download_document(
                        url,
                        custom_name,
                        position,
                        min(max_file_bytes, remaining_bytes),
                        int(timeout),
                    )
                    result.update(
                        filename=unique_name(filename, used_names),
                        data=data,
                        content_type=content_type,
                    )
                    downloaded_bytes += len(data)
                except (ValueError, requests.RequestException) as error:
                    result["error"] = str(error)
            results.append(result)
            progress.progress(position / len(selected_links))
            if delay and position < len(selected_links):
                time.sleep(float(delay))

        st.session_state["download_results"] = results
        status.write("Download run complete.")

    results = st.session_state.get("download_results", [])
    if results:
        successful = [result for result in results if result["data"] is not None]
        failed = [result for result in results if result["error"]]
        st.subheader("Results")
        st.write(f"Downloaded: **{len(successful)}** · Failed: **{len(failed)}**")

        if successful:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
                for result in successful:
                    zip_file.writestr(result["filename"], result["data"])
            st.download_button(
                "Download all successful files (ZIP)",
                data=archive.getvalue(),
                file_name="downloaded_documents.zip",
                mime="application/zip",
            )
            for result in successful:
                st.download_button(
                    f"Download {result['filename']}",
                    data=result["data"],
                    file_name=result["filename"],
                    mime=result["content_type"] or "application/octet-stream",
                    key=f"download-{result['filename']}",
                )

        if failed:
            st.subheader("Failed links")
            st.table(
                [
                    {"Source": result["source"], "URL": result["url"], "Error": result["error"]}
                    for result in failed
                ]
            )


def main():
    st.set_page_config(
        page_title="Literature Research Review & Bulk Document Extractor",
        page_icon="📚",
        layout="wide",
    )

    tab_search, tab_scopus_upload, tab_general = st.tabs([
        "🔍 Search Literature (By String Query)",
        "📁 Ingest Scopus / Review CSV",
        "🌐 General Bulk URL Downloader",
    ])

    with tab_search:
        render_search_literature_tab()

    with tab_scopus_upload:
        render_scopus_upload_tab()

    with tab_general:
        render_general_downloader_tab()


if __name__ == "__main__":
    main()
