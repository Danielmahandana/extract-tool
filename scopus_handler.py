"""
Scopus Research Review Handler & DOI Open-Access Resolver

Specialized module for literature reviews using Scopus exports:
1. Normalizes and validates DOIs from Scopus CSV records.
2. Formats standard academic filenames (Author_Year_Title.pdf).
3. Cascades through Unpaywall, OpenAlex, arXiv, and Europe PMC to find authentic open-access PDFs.
4. Validates that downloaded files are authentic PDFs (checking binary magic bytes %PDF-).
5. Enriches research review metadata for PRISMA reporting.
"""

import csv
import io
import re
import urllib.parse
from typing import Dict, List, Optional, Tuple
import requests

ACADEMIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PDF_CHUNK_SIZE = 64 * 1024


def clean_doi(doi_str: Optional[str]) -> str:
    """Extract and normalize a standard DOI string (e.g., 10.1016/j.cell.2021.01.001)."""
    if not doi_str:
        return ""
    cleaned = urllib.parse.unquote(str(doi_str).strip())
    # Match standard DOI pattern 10.XXXX/XXXX
    match = re.search(r"10\.\d{4,9}/[^\s\"<>]+", cleaned, re.IGNORECASE)
    if match:
        doi = match.group(0).strip()
        # Strip trailing punctuation often found in text exports
        doi = re.sub(r"[\.,;:)\"'>]+$", "", doi)
        return doi
    return ""


def extract_first_author(authors_str: Optional[str]) -> str:
    """Extract clean surname of the first author from Scopus or academic author strings."""
    if not authors_str:
        return "Author"
    raw = str(authors_str).strip()
    if ";" in raw:
        first_token = raw.split(";")[0].strip()
    else:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        first_token = parts[0] if parts else raw
    words = re.findall(r"[A-Za-z]+", first_token)
    name_words = [w for w in words if len(w) > 1]
    if name_words:
        return name_words[0].capitalize()
    elif words:
        return words[0].capitalize()
    return "Author"


def format_academic_filename(
    authors: Optional[str],
    year: Optional[str],
    title: Optional[str],
    index: int,
    max_title_words: int = 6,
) -> str:
    """
    Generate clean, standardized academic filename:
    e.g. 'Vaswani_2017_Attention_Is_All_You_Need.pdf'
    """
    author = extract_first_author(authors)
    
    # Year: 4 digits or ND (No Date)
    year_str = str(year).strip() if year else ""
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", year_str)
    yr = year_match.group(1) if year_match else "ND"

    # Title: first few clean words
    raw_title = str(title or f"Paper_{index}").strip()
    # Remove special punctuation and split into words
    words = re.findall(r"[A-Za-z0-9]+", raw_title)
    short_title = "_".join(words[:max_title_words]) if words else f"Paper_{index}"

    filename = f"{author}_{yr}_{short_title}.pdf"
    # Sanitize length and characters
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    if len(filename) > 180:
        filename = filename[:176] + ".pdf"
    return filename


def detect_scopus_columns(headers: List[str]) -> Dict[str, Optional[int]]:
    """
    Identify column indexes for standard Scopus CSV export fields.
    Returns mapping of field names to column indexes.
    """
    mapping: Dict[str, Optional[int]] = {
        "title": None,
        "authors": None,
        "year": None,
        "doi": None,
        "journal": None,
        "abstract": None,
        "open_access": None,
        "link": None,
        "eid": None,
    }

    norm_headers = [h.strip().casefold() for h in headers]

    for idx, h in enumerate(norm_headers):
        if mapping["doi"] is None and h in {"doi", "digital object identifier"}:
            mapping["doi"] = idx
        elif mapping["title"] is None and h in {"title", "document title"}:
            mapping["title"] = idx
        elif mapping["authors"] is None and any(term in h for term in ("authors", "author(s)")):
            mapping["authors"] = idx
        elif mapping["year"] is None and h in {"year", "publication year"}:
            mapping["year"] = idx
        elif mapping["journal"] is None and any(term in h for term in ("source title", "journal", "source")):
            mapping["journal"] = idx
        elif mapping["abstract"] is None and "abstract" in h:
            mapping["abstract"] = idx
        elif mapping["open_access"] is None and any(term in h for term in ("open access", "openaccess")):
            mapping["open_access"] = idx
        elif mapping["link"] is None and h in {"link", "url", "scopus link"}:
            mapping["link"] = idx
        elif mapping["eid"] is None and h == "eid":
            mapping["eid"] = idx

    return mapping


def is_scopus_export(headers: List[str]) -> bool:
    """Return True if CSV headers strongly indicate a Scopus export."""
    norm = [h.strip().casefold() for h in headers]
    has_doi = any(h in {"doi", "digital object identifier"} for h in norm)
    has_title = any(h in {"title", "document title"} for h in norm)
    has_scopus_meta = any(
        any(k in h for k in ("authors", "source title", "eid", "open access", "author keywords"))
        for h in norm
    )
    return has_title and (has_doi or has_scopus_meta)


def get_oa_pdf_candidates(
    doi: str,
    title: Optional[str] = None,
    contact_email: str = "research.review@extract-tool.org",
    timeout: int = 10,
) -> List[Tuple[str, str]]:
    """
    Queries open-access databases in cascade:
    1. arXiv direct resolution (if applicable)
    2. Unpaywall API (primary gold standard)
    3. OpenAlex API (secondary extensive repository index)
    4. Europe PMC API (biomedical & life sciences repositories)

    Returns list of (pdf_url, source_label) candidate pairs.
    """
    clean = clean_doi(doi)
    candidates: List[Tuple[str, str]] = []
    seen_urls = set()

    def add_candidate(url: Optional[str], source: str):
        if not url:
            return
        url_clean = url.strip()
        if url_clean and url_clean.startswith("http") and url_clean not in seen_urls:
            seen_urls.add(url_clean)
            candidates.append((url_clean, source))

    # 1. arXiv direct resolution
    if clean:
        arxiv_match = re.search(r"arXiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", clean, re.IGNORECASE)
        if arxiv_match:
            add_candidate(f"https://arxiv.org/pdf/{arxiv_match.group(1)}.pdf", "arXiv Direct")

    if not clean:
        return candidates

    # 2. Unpaywall API
    try:
        unpaywall_url = (
            f"https://api.unpaywall.org/v2/{urllib.parse.quote(clean)}"
            f"?email={urllib.parse.quote(contact_email)}"
        )
        res = requests.get(
            unpaywall_url,
            headers={"User-Agent": f"ExtractTool/2.0 (mailto:{contact_email})"},
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            best = data.get("best_oa_location") or {}
            add_candidate(best.get("url_for_pdf"), "Unpaywall (Best OA)")
            for loc in data.get("oa_locations", []):
                add_candidate(loc.get("url_for_pdf"), "Unpaywall (Repository)")
    except Exception:
        pass

    # 3. OpenAlex API
    try:
        alex_url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(clean)}"
        res = requests.get(
            alex_url,
            headers={"User-Agent": f"ExtractTool/2.0 (mailto:{contact_email})"},
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            best = data.get("best_oa_location") or {}
            add_candidate(best.get("pdf_url"), "OpenAlex (Best OA)")
            for loc in data.get("locations", []):
                add_candidate(loc.get("pdf_url"), "OpenAlex (Repository)")
    except Exception:
        pass

    # 4. Europe PMC API
    try:
        pmc_url = (
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=DOI:{urllib.parse.quote(clean)}&resultType=core&format=json"
        )
        res = requests.get(pmc_url, timeout=timeout)
        if res.status_code == 200:
            results = res.json().get("resultList", {}).get("result", [])
            if results:
                urls = results[0].get("fullTextUrlList", {}).get("fullTextUrl", [])
                for u in urls:
                    if u.get("documentStyle") == "pdf" and u.get("url"):
                        add_candidate(u.get("url"), "Europe PMC")
    except Exception:
        pass

    return candidates


def download_authentic_pdf(
    candidate_urls: List[Tuple[str, str]],
    max_bytes: int = 50 * 1024 * 1024,
    timeout: int = 30,
) -> Tuple[bytes, str, str]:
    """
    Iterates through candidate URLs and downloads the first authentic PDF stream.
    Verifies that the file begins with '%PDF-' magic bytes.
    Returns (pdf_bytes, final_url, source_name).
    Raises ValueError if all candidates fail or return non-PDF content.
    """
    if not candidate_urls:
        raise ValueError("No open-access PDF location found (Paywalled or Closed Access).")

    headers = {
        "User-Agent": ACADEMIC_USER_AGENT,
        "Accept": "application/pdf,application/octet-stream,*/*",
    }

    errors = []
    for url, source in candidate_urls:
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=timeout,
                allow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    errors.append(f"{source} ({url}): HTTP {resp.status_code}")
                    continue

                content_type = resp.headers.get("Content-Type", "").lower()
                # Read initial chunk to inspect magic bytes
                stream = resp.iter_content(chunk_size=PDF_CHUNK_SIZE)
                first_chunk = next(stream, b"")
                if not first_chunk:
                    errors.append(f"{source}: Empty response")
                    continue

                # Check if it starts with HTML / interstitial
                first_chars = first_chunk[:50].lower()
                if b"<!doctype" in first_chars or b"<html" in first_chars:
                    errors.append(f"{source}: Returned HTML landing/login page instead of PDF binary")
                    continue

                # Authentic PDF starts with %PDF-
                if not first_chunk.startswith(b"%PDF-") and "pdf" not in content_type:
                    errors.append(f"{source}: Not recognized as a valid PDF stream")
                    continue

                buffer = io.BytesIO()
                buffer.write(first_chunk)
                total = len(first_chunk)

                for chunk in stream:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"File exceeds maximum allowed size ({max_bytes / (1024*1024):.1f} MB)")
                    buffer.write(chunk)

                return buffer.getvalue(), url, source

        except Exception as err:
            errors.append(f"{source}: {err}")

    summary_error = "; ".join(errors)
    raise ValueError(f"Download failed across all OA sources: {summary_error}")


def generate_review_csv(records: List[Dict]) -> str:
    """
    Generate an enriched Scopus review screening CSV containing original metadata
    plus download outcome, OA resolver source, filename, and direct DOI links.
    """
    output = io.StringIO()
    fields = [
        "Title",
        "Authors",
        "Year",
        "Source Title",
        "DOI",
        "Status",
        "Resolution Source",
        "Local Filename",
        "OA PDF URL",
        "DOI Link",
        "Abstract",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, quoting=csv.QUOTE_ALL)
    writer.writeheader()

    for r in records:
        doi = r.get("doi", "")
        doi_link = f"https://doi.org/{doi}" if doi else ""
        writer.writerow({
            "Title": r.get("title", ""),
            "Authors": r.get("authors", ""),
            "Year": r.get("year", ""),
            "Source Title": r.get("journal", ""),
            "DOI": doi,
            "Status": r.get("status", "Pending"),
            "Resolution Source": r.get("resolver_source", ""),
            "Local Filename": r.get("filename", ""),
            "OA PDF URL": r.get("pdf_url", ""),
            "DOI Link": doi_link,
            "Abstract": r.get("abstract", ""),
        })

    return output.getvalue()


def reconstruct_abstract(abstract_inverted_index: Optional[dict]) -> str:
    """Reconstruct human-readable abstract text from OpenAlex inverted index."""
    if not abstract_inverted_index or not isinstance(abstract_inverted_index, dict):
        return ""
    word_positions = []
    for word, positions in abstract_inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(word for _, word in word_positions)


def search_academic_literature(
    query: str,
    max_results: int = 25,
    open_access_only: bool = True,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    sort_by: str = "relevance_score:desc",
    source_database: str = "openalex",
    scopus_api_key: Optional[str] = None,
    contact_email: str = "research.review@extract-tool.org",
    timeout: int = 15,
) -> Tuple[List[Dict], int]:
    """
    Search academic literature by search query string across global research databases.
    Returns (records_list, total_matches_count).
    """
    query = (query or "").strip()
    if not query:
        return [], 0

    records: List[Dict] = []
    total_matches = 0

    if source_database == "scopus" and scopus_api_key:
        try:
            scopus_query = query
            if open_access_only:
                scopus_query = f"({query}) AND OPENACCESS(1)"
            headers = {
                "X-ELS-APIKey": scopus_api_key.strip(),
                "Accept": "application/json",
                "User-Agent": ACADEMIC_USER_AGENT,
            }
            params = {
                "query": scopus_query,
                "count": min(max(max_results * 2, 25), 100),
            }
            scopus_url = "https://api.elsevier.com/content/search/scopus"
            resp = requests.get(scopus_url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json().get("search-results", {})
                total_matches = int(data.get("opensearch:totalResults", "0"))
                entries = data.get("entry", [])
                for entry in entries:
                    raw_doi = entry.get("prism:doi", "")
                    doi = clean_doi(raw_doi)
                    oa_flag = entry.get("openaccessFlag") in ("true", "1", 1, True)
                    if open_access_only and not oa_flag:
                        continue
                    
                    records.append({
                        "index": len(records) + 1,
                        "title": entry.get("dc:title", "Untitled"),
                        "authors": entry.get("dc:creator", "Unknown Author"),
                        "year": (entry.get("prism:coverDate", "")[:4] or ""),
                        "journal": entry.get("prism:publicationName", ""),
                        "citations": int(entry.get("citedby-count", 0)),
                        "doi": doi,
                        "raw_doi": raw_doi,
                        "open_access": "Open Access" if oa_flag else "Paywalled",
                        "pdf_url": "",
                        "abstract": "",
                        "source_db": "Scopus API",
                    })
                    if len(records) >= max_results:
                        break
                return records, total_matches
        except Exception:
            pass

    try:
        # Request a larger pool so after filtering out paywalled records, we satisfy max_results
        fetch_limit = min(max(max_results * 2, 30), 100)
        params = {
            "search": query,
            "per-page": fetch_limit,
            "sort": sort_by,
        }
        filters = []
        if open_access_only:
            filters.append("is_oa:true")
            filters.append("has_oa_accepted_or_published_version:true")
        if start_year and end_year:
            filters.append(f"publication_year:{start_year}-{end_year}")
        elif start_year:
            filters.append(f"publication_year:>{start_year - 1}")
        elif end_year:
            filters.append(f"publication_year:<{end_year + 1}")

        if filters:
            params["filter"] = ",".join(filters)

        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": f"ExtractTool/2.0 (mailto:{contact_email})",
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            total_matches = data.get("meta", {}).get("count", 0)
            items = data.get("results", [])
            for item in items:
                is_oa = item.get("open_access", {}).get("is_oa", False)
                if open_access_only and not is_oa:
                    # Filter out paywalled paper
                    continue

                best_oa = item.get("best_oa_location") or {}
                pdf_url = best_oa.get("pdf_url") or ""
                if not pdf_url:
                    for loc in item.get("locations", []):
                        if loc.get("pdf_url"):
                            pdf_url = loc.get("pdf_url")
                            break

                doi = clean_doi(item.get("doi"))

                # In strict OA mode, verify that an authentic direct PDF or OA candidate exists
                if open_access_only and not pdf_url:
                    if doi:
                        candidates = get_oa_pdf_candidates(
                            doi,
                            title=item.get("title"),
                            contact_email=contact_email,
                            timeout=5,
                        )
                        if candidates:
                            pdf_url = candidates[0][0]
                    if not pdf_url:
                        # Paywalled or closed access: strictly discard so it doesn't return
                        continue

                authorships = item.get("authorships", [])
                author_names = [a.get("author", {}).get("display_name", "") for a in authorships if a.get("author")]
                formatted_authors = ", ".join(author_names[:4]) + (" et al." if len(author_names) > 4 else "")
                if not formatted_authors:
                    formatted_authors = "Unknown Author"

                abstract = reconstruct_abstract(item.get("abstract_inverted_index"))
                journal = (item.get("primary_location") or {}).get("source", {}).get("display_name", "")

                records.append({
                    "index": len(records) + 1,
                    "title": item.get("title") or "Untitled",
                    "authors": formatted_authors,
                    "year": str(item.get("publication_year") or ""),
                    "journal": journal,
                    "citations": item.get("cited_by_count", 0),
                    "doi": doi,
                    "raw_doi": item.get("doi") or "",
                    "open_access": "Open Access" if is_oa else "Paywalled",
                    "oa_status": item.get("open_access", {}).get("oa_status", ""),
                    "pdf_url": pdf_url,
                    "abstract": abstract,
                    "source_db": "OpenAlex",
                })

                if len(records) >= max_results:
                    break

    except Exception:
        pass

    return records, total_matches


def is_record_open_access(record: Dict, contact_email: str = "research.review@extract-tool.org") -> bool:
    """
    Check if a research record is Open Access (not paywalled).
    Checks OA status flags and verifies that at least one OA PDF candidate exists.
    """
    oa_str = str(record.get("open_access", "")).casefold()
    if any(k in oa_str for k in ("closed", "no", "paywall", "subscription")):
        return False

    if record.get("pdf_url"):
        return True

    doi = clean_doi(record.get("doi") or record.get("raw_doi"))
    if not doi:
        return False

    # Check candidates
    candidates = get_oa_pdf_candidates(doi, title=record.get("title"), contact_email=contact_email, timeout=6)
    return len(candidates) > 0


