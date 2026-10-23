import csv
import ipaddress
import io
import mimetypes
import os
import re
import socket
import time
import zipfile
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

import requests
import streamlit as st


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
CHUNK_SIZE = 64 * 1024
USER_AGENT = "DocumentDownloader/1.0 (Streamlit)"


def parse_csv(file_bytes):
    """Return CSV headers and rows, decoding common spreadsheet exports."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("cp1252")

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


def main():
    st.set_page_config(page_title="Bulk Document Downloader", page_icon="📥", layout="wide")
    st.title("Bulk Document Downloader")
    st.write(
        "Download documents and other files from HTTP/HTTPS links. "
        "Import links from a CSV, paste them directly, or use both."
    )

    csv_file = st.file_uploader("Import a CSV file", type=["csv"])
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


if __name__ == "__main__":
    main()
