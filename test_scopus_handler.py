import csv
from scopus_handler import (
    clean_doi,
    extract_first_author,
    format_academic_filename,
    detect_scopus_columns,
    is_scopus_export,
    get_oa_pdf_candidates,
    download_authentic_pdf,
    generate_review_csv,
    search_academic_literature,
    reconstruct_abstract,
)

def test_handler():
    print("Testing clean_doi...")
    assert clean_doi("https://doi.org/10.1016/j.cell.2021.01.001") == "10.1016/j.cell.2021.01.001"
    assert clean_doi("10.1371/journal.pone.0215754,") == "10.1371/journal.pone.0215754"
    assert clean_doi("DOI: 10.1002/j.1538-7305.1948.tb01338.x;") == "10.1002/j.1538-7305.1948.tb01338.x"
    print("clean_doi passed!")

    print("Testing extract_first_author...")
    assert extract_first_author("Vaswani A., Shazeer N.") == "Vaswani"
    assert extract_first_author("Ioannidis J.P.A.") == "Ioannidis"
    assert extract_first_author("Shannon C.E.") == "Shannon"
    print("extract_first_author passed!")

    print("Testing format_academic_filename...")
    fn = format_academic_filename("Vaswani A., Shazeer N.", "2017", "Attention Is All You Need", 1)
    assert fn == "Vaswani_2017_Attention_Is_All_You_Need.pdf", fn
    print("format_academic_filename passed:", fn)

    print("Testing Scopus CSV parsing...")
    with open("sample_scopus_export.csv", "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        headers = reader[0]
        rows = reader[1:]

    assert is_scopus_export(headers) is True
    cols = detect_scopus_columns(headers)
    assert cols["title"] is not None
    assert cols["doi"] is not None
    assert cols["authors"] is not None
    assert cols["year"] is not None
    print("Scopus detection passed! Columns:", cols)

    # Test OA resolution on Vaswani (arXiv) and Ioannidis (PLOS)
    row0 = rows[0]
    doi0 = clean_doi(row0[cols["doi"]])
    candidates0 = get_oa_pdf_candidates(doi0)
    print(f"Candidates for {doi0}:", candidates0)
    assert len(candidates0) > 0

    pdf_bytes, final_url, src = download_authentic_pdf(candidates0, max_bytes=10*1024*1024)
    assert pdf_bytes.startswith(b"%PDF-")
    print(f"Successfully downloaded authentic PDF ({len(pdf_bytes)} bytes) from {src} ({final_url})")

    row1 = rows[1]
    doi1 = clean_doi(row1[cols["doi"]])
    candidates1 = get_oa_pdf_candidates(doi1)
    print(f"Candidates for {doi1}:", candidates1)
    assert len(candidates1) > 0
    pdf_bytes1, final_url1, src1 = download_authentic_pdf(candidates1, max_bytes=10*1024*1024)
    assert pdf_bytes1.startswith(b"%PDF-")
    print(f"Successfully downloaded authentic PDF ({len(pdf_bytes1)} bytes) from {src1} ({final_url1})")

    # Test literature search by string with strict open-access filtering
    print("Testing search_academic_literature with strict OA filtering...")
    search_results, total = search_academic_literature("machine learning systematic review", max_results=3, open_access_only=True)
    assert total > 0, "Expected non-zero total matches"
    assert len(search_results) == 3, f"Expected 3 results, got {len(search_results)}"
    for r in search_results:
        assert r["open_access"] == "Open Access", f"Expected Open Access, got {r['open_access']}"
        assert r["pdf_url"], f"Expected direct PDF link, got empty for {r['title']}"
    print(f"Strict OA search passed! All {len(search_results)} results have verified OA status and direct PDF URLs.")

    # Test report generation
    records = [
        {"title": "Attention Is All You Need", "authors": "Vaswani A.", "year": "2017", "journal": "NeurIPS", "doi": doi0, "status": "Downloaded", "resolver_source": src, "filename": fn, "pdf_url": final_url, "abstract": "Test abstract"},
        {"title": "Shannon Paper", "authors": "Shannon C.E.", "year": "1948", "journal": "BSTJ", "doi": "10.1002/test", "status": "Paywalled / No OA PDF", "resolver_source": "", "filename": "", "pdf_url": "", "abstract": "Info theory"}
    ]
    csv_report = generate_review_csv(records)
    assert "Attention Is All You Need" in csv_report
    assert "Paywalled / No OA PDF" in csv_report
    print("generate_review_csv passed!")

if __name__ == "__main__":
    test_handler()
    print("All tests passed successfully!")
