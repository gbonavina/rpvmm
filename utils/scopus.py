from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

import csv
import time
from pathlib import Path

FIREFOX_BIN = "/snap/firefox/current/usr/lib/firefox/firefox"  # localização do meu navegador, de outra pessoa pode ser diferente

SCOPUS_URL = "https://www.scopus.com/pages/home#basic"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARTICLES_PATH = DATA_DIR / "artigos.csv"
RESULTS_PATH = DATA_DIR / "scopus_fwci.csv"
PAUSE_BETWEEN_SEARCHES = 5
SOURCE_CELL = '[data-component="document-source"]'
SOURCE_LINK = 'a[href*="/sourceid/"]'
SOURCE_SPAN = "span.Typography_typography__CrPwo.Typography_sans__ELnaL"


def make_driver(*, headless: bool = True) -> webdriver.Firefox:
    options = Options()
    if headless:
        options.add_argument("-headless")
    options.binary_location = FIREFOX_BIN
    return webdriver.Firefox(options=options)


def load_dois(path: Path = ARTICLES_PATH) -> list[str]:
    seen: set[str] = set()
    dois: list[str] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            if not doi or doi in seen:
                continue
            seen.add(doi)
            dois.append(doi)
    return dois


def search_doi(wd: webdriver.Firefox, doi: str) -> tuple[str | None, str | None, str | None]:
    wait = WebDriverWait(wd, 15)
    wd.get(SCOPUS_URL)
    select_el = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, '[data-testid="select-search-within"]')
        )
    )
    Select(select_el).select_by_value("DOI")
    search_input = wait.until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, '[data-testid="search-field-input"]')
        )
    )
    search_input.clear()
    search_input.send_keys(doi)
    search_input.send_keys(Keys.RETURN)
    link = wait_for_result_link(wd)
    if link is None:
        return None, None, None
    source = wait_for_source(wd, link)
    link.click()
    citations, fwci = get_metrics(wd)
    return citations, fwci, source


def wait_for_result_link(wd: webdriver.Firefox):
    wait = WebDriverWait(wd, 15)
    try:
        return wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'a[href*="/pages/publications/"]')
            )
        )
    except TimeoutException:
        print("  no result link found", flush=True)
        return None


def wait_for_source(wd: webdriver.Firefox, link) -> str | None:
    try:
        WebDriverWait(wd, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, f"{SOURCE_CELL} {SOURCE_LINK}")
            )
        )
    except TimeoutException:
        print("  source not found", flush=True)
        return None
    source = source_from_result_row(link)
    if not source:
        print("  source not found", flush=True)
    return source


def source_from_result_row(link) -> str | None:
    try:
        row = link.find_element(By.XPATH, "./ancestor::tr[1]")
        cell = row.find_element(By.CSS_SELECTOR, SOURCE_CELL)
    except NoSuchElementException:
        try:
            cell = link.parent.find_element(By.CSS_SELECTOR, SOURCE_CELL)
        except NoSuchElementException:
            return None
    try:
        span = cell.find_element(By.CSS_SELECTOR, f"{SOURCE_LINK} {SOURCE_SPAN}")
        text = (span.text or "").strip()
        if text:
            return text.split("\n")[0].strip()
    except NoSuchElementException:
        pass
    try:
        a = cell.find_element(By.CSS_SELECTOR, SOURCE_LINK)
        text = (a.text or "").strip()
        return text.split("\n")[0].strip() or None
    except NoSuchElementException:
        return None


def count_texts(wd: webdriver.Firefox) -> list[str]:
    return [
        el.text.strip()
        for el in wd.find_elements(By.CSS_SELECTOR, '[data-testid="unclickable-count"]')
    ]


def get_metrics(wd: webdriver.Firefox) -> tuple[str | None, str | None]:
    try:
        WebDriverWait(wd, 15).until(
            lambda d: len(count_texts(d)) >= 1 and count_texts(d)[0]
        )
    except TimeoutException:
        print("  citations/FWCI not found", flush=True)
        return None, None

    texts = count_texts(wd)
    citations = texts[0] or None
    fwci = texts[1] if len(texts) >= 2 and texts[1] else None
    return citations, fwci


def save_results(rows: list[dict], path: Path = RESULTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["doi", "citations", "fwci", "source"])
        writer.writeheader()
        writer.writerows(rows)


def load_results(path: Path = RESULTS_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {
            (row.get("doi") or "").strip(): row
            for row in csv.DictReader(f)
            if (row.get("doi") or "").strip()
        }


def _rows_in_doi_order(dois: list[str], by_doi: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for doi in dois:
        row = by_doi.get(doi)
        if not row:
            continue
        rows.append(
            {
                "doi": doi,
                "citations": row.get("citations") or "",
                "fwci": row.get("fwci") or "",
                "source": row.get("source") or "",
            }
        )
        seen.add(doi)
    for doi, row in by_doi.items():
        if doi not in seen:
            rows.append(
                {
                    "doi": doi,
                    "citations": row.get("citations") or "",
                    "fwci": row.get("fwci") or "",
                    "source": row.get("source") or "",
                }
            )
    return rows


def open_scopus_search() -> None:
    dois = load_dois()
    by_doi = load_results()
    wd: webdriver.Firefox | None = None
    try:
        for i, doi in enumerate(dois, start=1):
            prev = by_doi.get(doi, {})
            if prev.get("source"):
                print(f"skip {i}/{len(dois)} {doi} (já tem source)", flush=True)
                continue
            if wd is None:
                wd = make_driver(headless=False)
            print(f"search {i}/{len(dois)} {doi}", flush=True)
            citations, fwci, source = search_doi(wd, doi)
            print(f"  citations={citations} FWCI={fwci} source={source}", flush=True)
            by_doi[doi] = {
                "doi": doi,
                "citations": citations or prev.get("citations") or "",
                "fwci": fwci or prev.get("fwci") or "",
                "source": source or "",
            }
            save_results(_rows_in_doi_order(dois, by_doi))
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        save_results(_rows_in_doi_order(dois, by_doi))
    finally:
        if wd is not None:
            wd.quit()


if __name__ == "__main__":
    open_scopus_search()
