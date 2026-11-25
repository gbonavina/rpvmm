"""Motor unificado de coleta bibliométrica (Lattes, Scopus, SciVal, Crossref, Qualis, Sophia/ITA e Correlação)."""

from __future__ import annotations

import csv
import html
import json
import math
import re
import threading
import time
import unicodedata
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlencode

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from openpyxl import load_workbook

from app_capes.config import AppConfig

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]

ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/{doi}"
SCIVAL_FWCI_URL = "https://api.elsevier.com/analytics/scival/publication/metrics"
SERIAL_TITLE_URL = "https://api.elsevier.com/content/serial/title"
SERIAL_ISSN_URL = "https://api.elsevier.com/content/serial/title/issn"
AUTHOR_SEARCH_URL = "https://api.elsevier.com/content/search/author"
SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
CROSSREF_URL = "https://api.crossref.org/works/{doi}"

CORTES_FWCI = [1.0, 0.875, 0.75, 0.625, 0.5, 0.375, 0.25, 0.0]
CORTES_PERCENTIL = [95.0, 90.0, 85.0, 75.0, 62.5, 50.0, 37.5]

ESTRATO_RANK = {
    "A1": 0, "A2": 1, "A3": 2, "A4": 3,
    "B1": 4, "B2": 5, "B3": 6, "B4": 7, "B5": 8, "C": 9,
}

DOI_HREF_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)
DOI_TEXT_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
LATTES_ID_RE = re.compile(r"lattes\.cnpq\.br/(\d+)")
NAME_INST_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<institution>[^)]+)\)\s*$")
PAREN_RE = re.compile(r"\([^)]*\)")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

CONECTORES = {"da", "das", "de", "do", "dos", "e"}
SUFIXOS_PATENTE = {"cap", "eb", "ten", "maj", "cel", "jr", "filho", "neto"}
NIVEIS_POS = {"Mestrado", "Doutorado", "Doutorado Direto"}
SITUACOES_VALIDAS = {"Ativo", "Egresso"}
SUBTIPOS_SCOPUS = {"ar": "artigo", "re": "revisão", "cp": "evento"}
AFILIACOES_PPGPO = re.compile(
    r"tecnologico de aeronautica|\bita\b|federal de sao paulo|unifesp|dcta|ciencia e tecnologia aeroespacial"
)

CRITICAL_LATTES_SECTIONS = (
    "ProducaoBibliografica",
    "ProducaoTecnica",
    "ArtigosCompletos",
    "ProjetosPesquisa",
    "AtuacaoProfissional",
    "FormacaoAcademicaTitulacao",
    "LinhaPesquisa",
)

ISSNS_CONHECIDOS_DEFAULT = {
    "nature": "00280836",
    "networks": "00283045",
}


# ==============================================================================
# Utilitários de Texto, DOI e Estratos CAPES
# ==============================================================================

def clean_text(value: object) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split())


def strip_accents(value: object) -> str:
    norm = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in norm if not unicodedata.combining(c))


def chave_nome(valor: object) -> str:
    texto = str(valor or "").split(",")[0]
    return " ".join(strip_accents(texto).casefold().split())


def tokens_nome(valor: object) -> list[str]:
    palavras = re.findall(r"[a-z]+", chave_nome(valor))
    return [p for p in palavras if p not in CONECTORES and p not in SUFIXOS_PATENTE]


def primeiro_ultimo(valor: object) -> tuple[str, str] | None:
    partes = tokens_nome(valor)
    return (partes[0], partes[-1]) if len(partes) >= 2 else None


def nomes_compativeis(aluno: str, autor: str) -> str:
    """Retorna '' se incompatível; caso contrário 'nome completo' ou 'nome abreviado'."""
    a, s = tokens_nome(aluno), tokens_nome(autor)
    if len(a) < 2 or len(s) < 2 or a[0] != s[0] or a[-1] != s[-1]:
        return ""
    if a == s:
        return "nome completo"
    meio = a[1:-1]
    for parte in s[1:-1]:
        if parte in meio or (len(parte) == 1 and any(m.startswith(parte) for m in meio)):
            continue
        return ""
    return "nome abreviado"


def normalize_doi(value: object) -> str:
    if not value:
        return ""
    text = html.unescape(unquote(str(value))).strip()
    if not text or text.lower() == "nan":
        return ""
    match = DOI_HREF_RE.search(text)
    doi = match.group(1) if match else text
    doi = doi.rstrip(").,:;\"'")
    return doi if doi.startswith("10.") else ""


def normalize_source(value: object) -> str:
    folded = strip_accents(PAREN_RE.sub(" ", str(value or ""))).casefold()
    folded = folded.replace("&", " and ")
    return NON_ALNUM_RE.sub(" ", folded).strip()


def to_float(value: object) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    raw = str(value).strip().replace(",", ".").replace("%", "")
    if not raw or raw.lower() == "nan":
        return None
    try:
        num = float(raw)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def faixa_fwci_str(fwci_val: object) -> str:
    num = to_float(fwci_val)
    if num is None or num < 0:
        return ""
    idx = next((i for i, corte in enumerate(CORTES_FWCI, start=1) if num >= corte), 8)
    return f"F{idx}"


def faixa_percentil_str(perc_val: object) -> str:
    num = to_float(perc_val)
    if num is None or num < 0:
        return ""
    idx = next((i for i, corte in enumerate(CORTES_PERCENTIL, start=1) if num >= corte), 8)
    return f"P{idx}"


# ==============================================================================
# Qualis Unificado (.xlsx e .csv)
# ==============================================================================

def _better_estrato(current: str, candidate: str) -> bool:
    return ESTRATO_RANK.get(candidate, 99) < ESTRATO_RANK.get(current, 99)


def load_qualis_unified(path: Path | str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Retorna (by_issn, by_title) suportando tanto .xlsx quanto .csv da Sucupira."""
    if not path:
        return {}, {}
    qpath = Path(path)
    if not qpath.exists():
        return {}, {}

    by_issn: dict[str, str] = {}
    by_title: dict[str, str] = {}

    if qpath.suffix.lower() in {".xlsx", ".xlsm"}:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style")
            wb = load_workbook(qpath, read_only=True, data_only=True)
        for row in wb.active.iter_rows(min_row=2, max_row=600_000, max_col=4, values_only=True):
            if not row:
                continue
            issn = str(row[0] or "").replace("-", "").strip().upper()
            title = str(row[1] or "").strip()
            estrato = str(row[3] or "").strip().upper()
            if not estrato:
                continue
            if issn and (issn not in by_issn or _better_estrato(by_issn[issn], estrato)):
                by_issn[issn] = estrato
            tkey = normalize_source(title)
            if len(tkey) > 2 and (tkey not in by_title or _better_estrato(by_title[tkey], estrato)):
                by_title[tkey] = estrato
        wb.close()
    else:
        # CSV (ex.: QUALIS 2021-2024.csv com ';' e latin-1 ou utf-8)
        for enc in ("utf-8-sig", "latin-1", "cp1252"):
            try:
                with qpath.open(encoding=enc, newline="") as fh:
                    sample = fh.read(4096)
                    fh.seek(0)
                    sep = ";" if sample.count(";") >= sample.count(",") else ","
                    reader = csv.reader(fh, delimiter=sep)
                    header = next(reader, [])
                    if not header:
                        break
                    for row in reader:
                        if len(row) < 4:
                            continue
                        issn = str(row[0] or "").replace("-", "").strip().upper()
                        title = str(row[1] or "").strip()
                        estrato = str(row[3] or "").strip().upper()
                        if not estrato:
                            continue
                        if issn and (issn not in by_issn or _better_estrato(by_issn[issn], estrato)):
                            by_issn[issn] = estrato
                        tkey = normalize_source(title)
                        if len(tkey) > 2 and (tkey not in by_title or _better_estrato(by_title[tkey], estrato)):
                            by_title[tkey] = estrato
                break
            except UnicodeDecodeError:
                continue

    return by_issn, by_title


def resolve_qualis(source: str, issn: str, by_issn: dict[str, str], by_title: dict[str, str]) -> str:
    for raw_issn in re.split(r"[;,|\s]+", str(issn or "")):
        clean_i = raw_issn.replace("-", "").strip().upper()
        if clean_i and clean_i in by_issn:
            return by_issn[clean_i]
    if source:
        return by_title.get(normalize_source(source), "")
    return ""


# ==============================================================================
# Diagnóstico de Conexão API (Scopus, Serial Title, SciVal e Crossref)
# ==============================================================================

def test_elsevier_connection(api_key: str, inst_token: str = "") -> dict[str, tuple[bool, str]]:
    """Testa conectividade e permissões da chave Elsevier nos 3 serviços + Crossref."""
    results: dict[str, tuple[bool, str]] = {}
    key = api_key.strip()
    token = inst_token.strip()

    # 1. Crossref (público)
    try:
        r = requests.get(
            CROSSREF_URL.format(doi="10.1016/j.ejor.2024.03.030"),
            headers={"User-Agent": "PPGPO-CAPES-Collector/1.0"},
            timeout=15,
        )
        results["Crossref API (Pública)"] = (
            r.status_code == 200,
            f"HTTP {r.status_code} — OK (fallback de DOI/ISSN ativo)" if r.status_code == 200 else f"HTTP {r.status_code}",
        )
    except Exception as exc:
        results["Crossref API (Pública)"] = (False, f"Erro de rede: {exc}")

    if not key:
        results["Scopus Abstract API"] = (False, "API Key não informada.")
        results["Elsevier Serial Title API (Percentil)"] = (False, "API Key não informada.")
        results["SciVal Metrics API (FWCI)"] = (False, "API Key não informada.")
        return results

    headers = {"Accept": "application/json", "X-ELS-APIKey": key}
    if token:
        headers["X-ELS-Insttoken"] = token

    # 2. Scopus Abstract Retrieval
    try:
        r = requests.get(
            ABSTRACT_URL.format(doi="10.1016/j.ejor.2024.03.030"),
            params={"view": "META_ABS"},
            headers=headers,
            timeout=20,
        )
        if r.status_code == 200:
            results["Scopus Abstract API"] = (True, "Conectado! Metadados, citações, autores e resumos liberados.")
        elif r.status_code in (401, 403):
            # Testa visão básica caso sem VPN/InstToken
            r_basic = requests.get(
                ABSTRACT_URL.format(doi="10.1016/j.ejor.2024.03.030"),
                headers=headers,
                timeout=20,
            )
            if r_basic.status_code == 200:
                results["Scopus Abstract API"] = (
                    True,
                    "Conectado (visão básica). Conecte à VPN UNIFESP/ITA ou adicione InstToken para resumos completos.",
                )
            else:
                results["Scopus Abstract API"] = (
                    False,
                    f"HTTP {r.status_code} — Verifique sua API Key ou conecte-se à VPN institucional (UNIFESP/ITA).",
                )
        else:
            results["Scopus Abstract API"] = (False, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        results["Scopus Abstract API"] = (False, f"Falha de rede: {exc}")

    # 3. Serial Title API (CiteScore Percentil)
    try:
        r = requests.get(
            f"{SERIAL_ISSN_URL}/03772217",
            headers=headers,
            timeout=20,
        )
        if r.status_code == 200:
            results["Elsevier Serial Title API (Percentil)"] = (True, "Conectado! Coleta de Percentil CiteScore P1..P8 ativa.")
        else:
            results["Elsevier Serial Title API (Percentil)"] = (False, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        results["Elsevier Serial Title API (Percentil)"] = (False, f"Falha de rede: {exc}")

    # 4. SciVal Publication Lookup API (FWCI)
    try:
        r = requests.get(
            SCIVAL_FWCI_URL,
            params={
                "metricTypes": "FieldWeightedCitationImpact",
                "publicationIds": "85189281794",
                "byYear": "false",
                "yearRange": "10yrs",
                "includeSelfCitations": "true",
            },
            headers=headers,
            timeout=20,
        )
        if r.status_code == 200:
            results["SciVal Metrics API (FWCI)"] = (True, "Conectado! Coleta direta de FWCI F1..F8 liberada.")
        elif r.status_code in (401, 403):
            results["SciVal Metrics API (FWCI)"] = (
                False,
                f"HTTP {r.status_code} — SciVal requer VPN institucional ou habilitação SciVal na chave (o app manterá FWCIs já existentes).",
            )
        else:
            results["SciVal Metrics API (FWCI)"] = (False, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        results["SciVal Metrics API (FWCI)"] = (False, f"Falha de rede: {exc}")

    return results


# ==============================================================================
# WebDriver Multiplataforma (Windows / Linux / macOS — Edge, Chrome, Firefox)
# ==============================================================================

def make_browser_driver(browser: str = "auto", headless: bool = False):
    """Inicializa Selenium WebDriver compatível com Windows sem caminho fixo /snap/."""
    from selenium import webdriver

    choice = (browser or "auto").lower().strip()
    candidates = [choice] if choice in {"edge", "chrome", "firefox"} else ["edge", "chrome", "firefox"]
    last_err: Exception | None = None

    for name in candidates:
        try:
            if name == "edge":
                opts = webdriver.EdgeOptions()
                if headless:
                    opts.add_argument("--headless=new")
                return webdriver.Edge(options=opts)
            if name == "chrome":
                opts = webdriver.ChromeOptions()
                if headless:
                    opts.add_argument("--headless=new")
                return webdriver.Chrome(options=opts)
            if name == "firefox":
                opts = webdriver.FirefoxOptions()
                if headless:
                    opts.add_argument("-headless")
                for win_ff in (
                    Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"),
                    Path(r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"),
                    Path("/snap/firefox/current/usr/lib/firefox/firefox"),
                ):
                    if win_ff.exists():
                        opts.binary_location = str(win_ff)
                        break
                return webdriver.Firefox(options=opts)
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(
        f"Não foi possível abrir o navegador ({choice}). Verifique se Edge, Chrome ou Firefox está instalado. Detalhe: {last_err}"
    )


# ==============================================================================
# Etapa 1: Coleta de Docentes, Currículos Lattes e Orientações
# ==============================================================================

def _looks_like_cv(html_str: str) -> bool:
    low = html_str.lower()
    if "<body" not in low:
        return False
    return (
        'class="infpessoa"' in low
        or "última atualização do currículo" in low
        or "ultima atualizacao do curriculo" in low
    )


def _missing_lattes_sections(html_str: str) -> list[str]:
    missing = []
    for sec in CRITICAL_LATTES_SECTIONS:
        if f'href="#{sec}"' in html_str and f'name="{sec}"' not in html_str and f'id="{sec}"' not in html_str:
            missing.append(sec)
    return missing


def _extract_cv_html_from_driver(driver) -> str:
    page = driver.page_source
    if _looks_like_cv(page):
        return page
    for frame in driver.find_elements("css selector", "iframe"):
        try:
            driver.switch_to.frame(frame)
            inner = driver.page_source
        except Exception:
            inner = ""
        finally:
            driver.switch_to.default_content()
        if _looks_like_cv(inner):
            return inner
    return page


def scrape_ppgpo_professors_and_lattes(
    cfg: AppConfig,
    only_missing_htmls: bool = True,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> Path:
    """Raspa a página de docentes do PPG-PO e baixa/atualiza os currículos HTML do Lattes."""
    lattes_dir = Path(cfg.lattes_dir)
    lattes_dir.mkdir(parents=True, exist_ok=True)
    prof_path = Path(cfg.professors_json) if cfg.professors_json else (Path(cfg.output_dir) / "professors.json")
    prof_path.parent.mkdir(parents=True, exist_ok=True)

    professors: list[dict] = []
    if prof_path.exists() and only_missing_htmls:
        try:
            professors = json.loads(prof_path.read_text(encoding="utf-8"))
            log(f"Lista de docentes carregada de {prof_path.name} ({len(professors)} registros).")
        except Exception:
            professors = []

    if not professors:
        log(f"Acessando página de docentes do PPG-PO: {cfg.ppgpo_docentes_url}")
        resp = requests.get(cfg.ppgpo_docentes_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        for link in soup.find_all("a", href=LATTES_ID_RE):
            box = link
            while box.parent and len(box.parent.find_all("a", href=LATTES_ID_RE)) == 1:
                box = box.parent
            if id(box) in seen:
                continue
            seen.add(id(box))
            block = box.select_one("div.tyJCtd") or box
            heading = block.find("p")
            raw_name = heading.get_text(strip=True) if heading else ""
            name, inst = raw_name, None
            if raw_name and (m := NAME_INST_RE.match(raw_name)):
                name, inst = m.group("name").strip(), m.group("institution").strip()
            href = link.get("href", "")
            lid_m = LATTES_ID_RE.search(href)
            lid = lid_m.group(1) if lid_m else None
            if name and lid:
                professors.append({
                    "name": name,
                    "institution": inst,
                    "lattes": href,
                    "lattes_id": lid,
                    "lattes_html": str(lattes_dir / f"{lid}.html") if (lattes_dir / f"{lid}.html").exists() else None,
                })
        prof_path.write_text(json.dumps(professors, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"Encontrados {len(professors)} docentes no site do PPG-PO.")

    # Verifica quais HTMLs precisam ser baixados no navegador
    to_download: list[tuple[str, str, str]] = []
    seen_lids = set()
    for p in professors:
        lid = str(p.get("lattes_id") or "")
        url = str(p.get("lattes") or (f"http://lattes.cnpq.br/{lid}" if lid else ""))
        if not lid or lid in seen_lids:
            continue
        seen_lids.add(lid)
        out_html = lattes_dir / f"{lid}.html"
        if only_missing_htmls and out_html.exists() and out_html.stat().st_size > 5000:
            continue
        to_download.append((lid, p.get("name", lid), url))

    if not to_download:
        log(f"Todos os {len(seen_lids)} currículos HTML do Lattes já estão disponíveis em {lattes_dir}.")
        return prof_path

    log(f"Abrindo navegador ({cfg.browser}) para baixar {len(to_download)} currículo(s) Lattes...")
    log("ATENÇÃO: Caso o Lattes exiba CAPTCHA, basta resolvê-lo na janela do navegador — o aplicativo detectará automaticamente!")
    driver = make_browser_driver(cfg.browser, headless=False)
    try:
        for idx, (lid, prof_name, url) in enumerate(to_download, start=1):
            if stop_event and stop_event.is_set():
                log("Download do Lattes interrompido pelo usuário.")
                break
            if progress:
                progress(idx, len(to_download), f"Baixando Lattes: {prof_name}")
            log(f"[{idx}/{len(to_download)}] Abrindo Lattes de {prof_name} ({lid})...")
            driver.get(url)

            deadline = time.monotonic() + 300
            last_len = -1
            stable_since: float | None = None
            warned_captcha = False

            while time.monotonic() < deadline:
                if stop_event and stop_event.is_set():
                    break
                cur_html = _extract_cv_html_from_driver(driver)
                if not _looks_like_cv(cur_html):
                    if not warned_captcha:
                        log(f"  -> Aguardando resolução de CAPTCHA / carregamento no navegador para {prof_name}...")
                        warned_captcha = True
                    time.sleep(1.0)
                    continue

                n = len(cur_html)
                complete = not _missing_lattes_sections(cur_html)
                if n != last_len:
                    last_len = n
                    stable_since = time.monotonic()
                elif complete and stable_since is not None and (time.monotonic() - stable_since) >= 4.0:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)
                    cur_html = _extract_cv_html_from_driver(driver)
                    out_file = lattes_dir / f"{lid}.html"
                    out_file.write_text(cur_html, encoding="utf-8")
                    log(f"  -> Salvo {out_file.name} ({len(cur_html):,} caracteres).")
                    break
                time.sleep(0.5)
            time.sleep(1.5)
    finally:
        driver.quit()

    for p in professors:
        lid = str(p.get("lattes_id") or "")
        out_html = lattes_dir / f"{lid}.html"
        p["lattes_html"] = str(out_html) if out_html.exists() else None
    prof_path.write_text(json.dumps(professors, indent=2, ensure_ascii=False), encoding="utf-8")
    return prof_path


def _parse_lattes_article_tag(article: Tag, year_min: int, year_max: int) -> dict | None:
    el = article.select_one('span.informacao-artigo[data-tipo-ordenacao="ano"]')
    if not el:
        return None
    raw_y = el.get_text(strip=True)
    if not raw_y.isdigit():
        return None
    year = int(raw_y)
    if year < year_min or year > year_max:
        return None

    # Título via cvuri ou texto da citação
    title = ""
    citado = article.select_one("span.citado[cvuri]")
    if citado:
        qs = parse_qs(citado["cvuri"].split("?", 1)[-1])
        title = clean_text((qs.get("titulo") or [""])[0])
    if not title:
        transform = article.select_one("span.transform")
        if transform:
            clone = BeautifulSoup(str(transform), "html.parser")
            root = clone.select_one("span.transform") or clone
            for junk in root.select("span.informacao-artigo, a.icone-producao, span.citado, sup"):
                junk.decompose()
            txt = clean_text(root.get_text(" ", strip=True))
            m = re.search(r"\s\.\s(.+?)\.\s", txt)
            title = m.group(1).strip() if m else ""
    if not title:
        return None

    # DOI
    doi = ""
    link = article.select_one("a.icone-doi[href]")
    if link:
        doi = normalize_doi(link.get("href"))
    if not doi and citado:
        qs = parse_qs(citado["cvuri"].split("?", 1)[-1])
        doi = normalize_doi((qs.get("doi") or [""])[0])
    if not doi:
        m_doi = DOI_TEXT_RE.search(article.get_text(" ", strip=True))
        doi = normalize_doi(m_doi.group(1) if m_doi else "")

    return {"artigo": title, "doi": doi, "ano_lattes": str(year)}


def extract_lattes_articles_and_orientations(
    cfg: AppConfig,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
) -> tuple[Path, Path]:
    """Extrai artigos dos docentes (no intervalo de anos) e orientandos elegíveis dos HTMLs Lattes."""
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lattes_dir = Path(cfg.lattes_dir)
    prof_path = Path(cfg.professors_json)

    professors: list[dict] = []
    if prof_path.exists():
        try:
            professors = json.loads(prof_path.read_text(encoding="utf-8"))
        except Exception:
            professors = []

    # Caso não exista professors.json, descobre diretamente pelos HTMLs na pasta lattes
    html_files = sorted(lattes_dir.glob("*.html")) if lattes_dir.exists() else []
    if not html_files:
        raise FileNotFoundError(f"Nenhum arquivo HTML encontrado na pasta Lattes: {lattes_dir}")

    prof_by_lid: dict[str, str] = {}
    seen_prof_names: set[str] = set()
    prof_entries: list[tuple[str, Path]] = []

    for p in professors:
        name = str(p.get("name") or "").strip()
        lid = str(p.get("lattes_id") or "").strip()
        if not name or name.casefold() in seen_prof_names:
            continue
        hpath = lattes_dir / f"{lid}.html" if lid else None
        if hpath and hpath.exists():
            seen_prof_names.add(name.casefold())
            prof_by_lid[lid] = name
            prof_entries.append((name, hpath))

    for hfile in html_files:
        if hfile.stem not in prof_by_lid:
            soup_tmp = BeautifulSoup(hfile.read_text(encoding="utf-8", errors="ignore"), "html.parser")
            h2 = soup_tmp.select_one("h2.nome")
            cname = clean_text(h2.get_text(strip=True)) if h2 else hfile.stem
            if cname.casefold() not in seen_prof_names:
                seen_prof_names.add(cname.casefold())
                prof_by_lid[hfile.stem] = cname
                prof_entries.append((cname, hfile))

    log(f"Processando {len(prof_entries)} currículos Lattes para o período {cfg.ano_inicio}–{cfg.ano_fim}...")

    article_rows: list[dict[str, str]] = []
    postdoc_names: set[str] = set()
    advised_by_student: dict[str, set[str]] = defaultdict(set)

    # Carrega nomes de alunos do cadastro institucional para cruzamento de orientações
    students_map: dict[str, str] = {}
    if cfg.alunos_csv and Path(cfg.alunos_csv).exists():
        df_al = pd.read_csv(cfg.alunos_csv, encoding="utf-8-sig")
        for raw_n in df_al.get("Name", []):
            if pd.notna(raw_n):
                clean_n = str(raw_n).split(",")[0].strip()
                students_map[chave_nome(clean_n)] = clean_n

    for idx, (prof_name, html_path) in enumerate(prof_entries, start=1):
        if progress:
            progress(idx, len(prof_entries), f"Lendo currículo: {prof_name}")
        raw_html = html_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(raw_html, "html.parser")

        seen_titles: set[str] = set()
        seen_dois: set[str] = set()
        for art_tag in soup.select("div.artigo-completo"):
            parsed = _parse_lattes_article_tag(art_tag, cfg.ano_inicio, cfg.ano_fim)
            if not parsed:
                continue
            t_key = NON_ALNUM_RE.sub(" ", strip_accents(parsed["artigo"]).casefold()).strip()
            d_key = parsed["doi"].casefold()
            if t_key in seen_titles or (d_key and d_key in seen_dois):
                continue
            seen_titles.add(t_key)
            if d_key:
                seen_dois.add(d_key)
            article_rows.append({
                "professor": prof_name,
                "artigo": parsed["artigo"],
                "doi": parsed["doi"],
                "ano_lattes": parsed["ano_lattes"],
            })

        # Extração exata da seção Orientações e Supervisões de Pós-Doutorado do Lattes
        m_sec = re.search(r'<a\s+name="Orientacoes"', raw_html, re.I)
        if m_sec:
            sec_html = raw_html[m_sec.start():]
            m_next = re.search(r'<div class="title-wrapper">\s*<a name="(?!Orientacoes)', sec_html, re.I)
            if m_next:
                sec_html = sec_html[:m_next.start()]
            for item_m in re.finditer(r'<span class="transform">(.*?)</span>', sec_html, re.I | re.S):
                frag_txt = clean_text(BeautifulSoup(item_m.group(1), "html.parser").get_text(" "))
                m_adv = re.match(r"^\s*([^\.]{3,120}?)\s*\.\s*", frag_txt)
                if not m_adv:
                    continue
                adv_ck = chave_nome(m_adv.group(1))
                if adv_ck not in students_map:
                    continue
                prefix = sec_html[:item_m.start()]
                headings = re.findall(r'<div class="cita-artigos">\s*<b>(.*?)</b>', prefix, re.I | re.S)
                deg_heading = clean_text(BeautifulSoup(headings[-1], "html.parser").get_text(" ")) if headings else ""
                if re.search(r"p[óo]s[-\s]*dout", deg_heading, re.I):
                    postdoc_names.add(students_map[adv_ck])
                elif "orientador" in frag_txt.casefold():
                    advised_by_student[students_map[adv_ck]].add(prof_name)

    # Preenche DOIs ausentes quando outro docente coautor cadastrou o mesmo título com DOI
    doi_by_title: dict[str, str] = {}
    for r in article_rows:
        if r["doi"]:
            tk = NON_ALNUM_RE.sub(" ", strip_accents(r["artigo"]).casefold()).strip()
            doi_by_title.setdefault(tk, r["doi"])
    for r in article_rows:
        if not r["doi"]:
            tk = NON_ALNUM_RE.sub(" ", strip_accents(r["artigo"]).casefold()).strip()
            r["doi"] = doi_by_title.get(tk, "")

    article_rows.sort(key=lambda x: (x["professor"].casefold(), x["artigo"].casefold()))

    art_out = out_dir / f"artigos_docentes_{cfg.ano_inicio}_{cfg.ano_fim}.csv"
    pd.DataFrame(article_rows, columns=["professor", "artigo", "doi", "ano_lattes"]).to_csv(
        art_out, index=False, encoding="utf-8-sig"
    )

    orient_rows = [
        {
            "aluno": aluno,
            "orientadores_lattes": " | ".join(sorted(profs)),
            "excluido_posdoc": aluno in postdoc_names,
        }
        for aluno, profs in sorted(advised_by_student.items())
        if aluno not in postdoc_names
    ]
    orient_out = out_dir / f"orientandos_lattes_{cfg.ano_inicio}_{cfg.ano_fim}.csv"
    pd.DataFrame(orient_rows).to_csv(orient_out, index=False, encoding="utf-8-sig")

    com_doi = sum(bool(r["doi"]) for r in article_rows)
    log(
        f"Extração Lattes concluída: {len(article_rows)} registros de artigos ({com_doi} com DOI) "
        f"e {len(orient_rows)} orientandos elegíveis (excluídos {len(postdoc_names)} pós-docs)."
    )
    return art_out, orient_out


# ==============================================================================
# Etapa 2: Consultas Scopus, Crossref (Fallback), SciVal (FWCI), Percentil e Qualis
# ==============================================================================

def _parse_scopus_authors(payload_resp: dict) -> list[dict[str, str]]:
    authors_out: list[dict[str, str]] = []
    seen_seq = set()

    # 1. Bloco authors -> author (contém todos os autores na ordem correta)
    authors_block = (payload_resp.get("authors") or {}).get("author") or []
    if isinstance(authors_block, dict):
        authors_block = [authors_block]
    for idx, au in enumerate(authors_block, start=1):
        if not isinstance(au, dict):
            continue
        pref = au.get("preferred-name") or {}
        given = clean_text(au.get("ce:given-name") or pref.get("ce:given-name") or "")
        surname = clean_text(au.get("ce:surname") or pref.get("ce:surname") or "")
        full = f"{given} {surname}".strip() or clean_text(au.get("ce:indexed-name") or "")
        seq = str(au.get("@seq") or idx)
        auid = str(au.get("@auid") or "")
        if full and seq not in seen_seq:
            seen_seq.add(seq)
            authors_out.append({"id": auid, "name": full, "position": seq})

    if authors_out:
        return authors_out

    # 2. Fallback coredata -> dc:creator
    creator = ((payload_resp.get("coredata") or {}).get("dc:creator") or {}).get("author") or []
    if isinstance(creator, dict):
        creator = [creator]
    for idx, au in enumerate(creator, start=1):
        if not isinstance(au, dict):
            continue
        given = clean_text(au.get("ce:given-name") or "")
        surname = clean_text(au.get("ce:surname") or "")
        full = f"{given} {surname}".strip() or clean_text(au.get("ce:indexed-name") or "")
        if full:
            authors_out.append({"id": str(au.get("@auid") or ""), "name": full, "position": str(idx)})
    return authors_out


def fetch_scopus_by_doi(doi: str, api_key: str, inst_token: str = "") -> tuple[dict, list[dict[str, str]], str]:
    """Consulta metadados, citações, resumo e lista completa de autores pelo DOI na Scopus."""
    if not api_key:
        return {}, [], "sem_chave"
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key.strip()}
    if inst_token.strip():
        headers["X-ELS-Insttoken"] = inst_token.strip()

    encoded_doi = quote(doi, safe="/")
    for view in ("FULL", "META_ABS", None):
        params = {"view": view} if view else {}
        try:
            r = requests.get(
                ABSTRACT_URL.format(doi=encoded_doi),
                params=params,
                headers=headers,
                timeout=35,
            )
            if r.status_code in (401, 403) and view is not None:
                continue  # Tenta visão menos restrita
            if r.status_code == 404:
                return {}, [], "nao_indexado_scopus"
            if r.status_code == 429:
                return {}, [], "limite_api_429"
            if not r.ok:
                return {}, [], f"erro_http_{r.status_code}"

            payload = r.json()
            resp = payload.get("abstracts-retrieval-response") or {}
            core = resp.get("coredata") or {}

            # Ano
            pub_year = ""
            for f_date in ("prism:coverDate", "prism:coverDisplayDate", "prism:publicationDate"):
                m_y = re.search(r"\b(19|20)\d{2}\b", str(core.get(f_date) or ""))
                if m_y:
                    pub_year = m_y.group(0)
                    break

            # Resumo
            abstract = clean_text(core.get("dc:description") or "")
            if not abstract:
                item_abs = (((resp.get("item") or {}).get("bibrecord") or {}).get("head") or {}).get("abstracts")
                if isinstance(item_abs, str):
                    abstract = clean_text(item_abs)

            # ISSN
            issn = ""
            for f_issn in ("prism:issn", "prism:eIssn"):
                val = core.get(f_issn)
                if isinstance(val, list):
                    val = next((v for v in val if v), "")
                val_s = str(val or "").strip().split()[0] if str(val or "").strip() else ""
                if val_s:
                    issn = val_s.replace("-", "")
                    break

            authors = _parse_scopus_authors(resp)
            meta = {
                "scopus_id": clean_text(core.get("dc:identifier")).removeprefix("SCOPUS_ID:"),
                "eid": clean_text(core.get("eid")),
                "artigo_scopus": clean_text(core.get("dc:title")),
                "ano_publicacao_scopus": pub_year,
                "resumo_scopus": abstract,
                "source": clean_text(core.get("prism:publicationName")),
                "issn": issn,
                "citations": clean_text(core.get("citedby-count")),
            }
            return meta, authors, "coletado_scopus"
        except requests.RequestException:
            continue

    return {}, [], "falha_scopus"


def fetch_crossref_fallback(doi: str) -> tuple[dict, list[dict[str, str]], str]:
    """Fallback público via Crossref API para obter periódico, ISSN, ano, título e autores."""
    try:
        r = requests.get(
            CROSSREF_URL.format(doi=quote(doi, safe="/")),
            headers={"User-Agent": "PPGPO-CAPES-Collector/1.0"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}, [], f"crossref_{r.status_code}"
        msg = r.json().get("message") or {}
        titles = msg.get("title") or []
        containers = msg.get("container-title") or []
        issns = [str(i).replace("-", "").strip() for i in (msg.get("ISSN") or []) if i]

        year = ""
        for date_field in ("published-print", "published-online", "issued", "created"):
            parts = ((msg.get(date_field) or {}).get("date-parts") or [[]])[0]
            if parts and str(parts[0]).isdigit():
                year = str(parts[0])
                break

        authors = []
        for idx, au in enumerate(msg.get("author") or [], start=1):
            given = clean_text(au.get("given") or "")
            family = clean_text(au.get("family") or "")
            full = f"{given} {family}".strip() or clean_text(au.get("name") or "")
            if full:
                authors.append({"id": "", "name": full, "position": str(idx)})

        abstract_raw = str(msg.get("abstract") or "")
        abstract_clean = clean_text(BeautifulSoup(abstract_raw, "html.parser").get_text(" ")) if abstract_raw else ""

        meta = {
            "artigo_scopus": clean_text(titles[0]) if titles else "",
            "ano_publicacao_scopus": year,
            "resumo_scopus": abstract_clean,
            "source": clean_text(containers[0]) if containers else "",
            "issn": ";".join(issns),
            "citations": str(msg.get("is-referenced-by-count", "")) if msg.get("is-referenced-by-count") is not None else "",
        }
        return meta, authors, "coletado_crossref"
    except Exception:
        return {}, [], "erro_crossref"


def fetch_scival_fwci(scopus_id: str, api_key: str, inst_token: str = "", year_range: str = "10yrs") -> tuple[str, str]:
    """Consulta o FWCI diretamente na SciVal Publication Metrics API."""
    if not api_key or not scopus_id or not scopus_id.isdigit():
        return "", "sem_id_ou_chave"
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key.strip()}
    if inst_token.strip():
        headers["X-ELS-Insttoken"] = inst_token.strip()
    try:
        r = requests.get(
            SCIVAL_FWCI_URL,
            params={
                "metricTypes": "FieldWeightedCitationImpact",
                "publicationIds": scopus_id,
                "byYear": "false",
                "yearRange": year_range,
                "includeSelfCitations": "true",
            },
            headers=headers,
            timeout=30,
        )
        if r.status_code in (401, 403):
            return "", "scival_sem_permissao"
        if r.status_code != 200:
            return "", f"scival_http_{r.status_code}"

        payload = r.json()
        results = payload.get("results") or []
        for item in results:
            pub = item.get("publication") or {}
            if str(pub.get("id") or "") == scopus_id:
                for m in item.get("metrics") or []:
                    if m.get("metricType") == "FieldWeightedCitationImpact":
                        val = m.get("value")
                        if val is not None and to_float(val) is not None:
                            return f"{float(val):.2f}", "coletado"
        return "", "fwci_indisponivel"
    except Exception:
        return "", "erro_rede_scival"


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _extract_highest_percentile_from_serial_entry(entry: dict) -> tuple[str, str, str]:
    """Extrai (percentil, ano, categoria) da resposta JSON da Serial Title API da Elsevier."""
    subject_names: dict[str, str] = {}
    for sa in entry.get("subject-area") or ([] if not isinstance(entry.get("subject-area"), dict) else [entry["subject-area"]]):
        if isinstance(sa, dict) and sa.get("@code"):
            subject_names[str(sa["@code"])] = clean_text(sa.get("$") or "")

    best_year = -1
    best_perc = -1.0
    best_cat = ""

    cs_info = entry.get("citeScoreYearInfoList") or {}
    year_list = cs_info.get("citeScoreYearInfo") or []
    if isinstance(year_list, dict):
        year_list = [year_list]

    for y_item in year_list:
        if not isinstance(y_item, dict):
            continue
        yr = int(y_item.get("@year") or 0)
        for d in _walk_dicts(y_item):
            if "citeScoreSubjectRank" in d:
                ranks = d["citeScoreSubjectRank"]
                if isinstance(ranks, dict):
                    ranks = [ranks]
                for rk in ranks:
                    p_val = to_float(rk.get("percentile"))
                    code = str(rk.get("subjectCode") or "")
                    if p_val is not None and (yr > best_year or (yr == best_year and p_val > best_perc)):
                        best_year = yr
                        best_perc = p_val
                        best_cat = subject_names.get(code, code)

    if best_perc >= 0:
        perc_fmt = str(int(best_perc)) if best_perc.is_integer() else f"{best_perc:.1f}"
        return perc_fmt, (str(best_year) if best_year > 0 else ""), best_cat
    return "", "", ""


def fetch_journal_percentile(source: str, issn: str, api_key: str, inst_token: str = "") -> dict[str, str]:
    """Consulta o Highest CiteScore Percentile de um periódico na Elsevier Serial Title API."""
    empty = {"source": source, "percentil": "", "ano": "", "categoria": ""}
    if not api_key:
        return empty

    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key.strip()}
    if inst_token.strip():
        headers["X-ELS-Insttoken"] = inst_token.strip()

    norm_key = NON_ALNUM_RE.sub("", strip_accents(source).casefold())
    issn_candidates = [
        i.replace("-", "").strip()
        for i in re.split(r"[;,|\s]+", str(issn or ""))
        if i.replace("-", "").strip()
    ]
    if norm_key in ISSNS_CONHECIDOS_DEFAULT:
        issn_candidates.insert(0, ISSNS_CONHECIDOS_DEFAULT[norm_key])

    # 1. Tenta busca exata por ISSN
    for clean_i in issn_candidates:
        try:
            r = requests.get(
                f"{SERIAL_ISSN_URL}/{clean_i}",
                params={"view": "CITESCORE"},
                headers=headers,
                timeout=25,
            )
            if r.ok:
                entries = (r.json().get("serial-metadata-response") or {}).get("entry") or []
                if isinstance(entries, dict):
                    entries = [entries]
                for ent in entries:
                    perc, yr, cat = _extract_highest_percentile_from_serial_entry(ent)
                    if perc:
                        return {"source": source, "percentil": perc, "ano": yr, "categoria": cat}
        except Exception:
            continue

    # 2. Fallback busca por título do periódico
    if source:
        try:
            r = requests.get(
                SERIAL_TITLE_URL,
                params={"title": source, "view": "CITESCORE"},
                headers=headers,
                timeout=25,
            )
            if r.ok:
                entries = (r.json().get("serial-metadata-response") or {}).get("entry") or []
                if isinstance(entries, dict):
                    entries = [entries]
                for ent in entries:
                    ent_title = NON_ALNUM_RE.sub("", strip_accents(ent.get("dc:title") or "").casefold())
                    if ent_title == norm_key or len(entries) == 1:
                        perc, yr, cat = _extract_highest_percentile_from_serial_entry(ent)
                        if perc:
                            return {"source": source, "percentil": perc, "ano": yr, "categoria": cat}
        except Exception:
            pass

    return empty


def load_percentile_cache(path: Path | str | None) -> dict[str, dict[str, str]]:
    cache: dict[str, dict[str, str]] = {}
    if not path:
        return cache
    p = Path(path)
    if not p.exists():
        return cache
    with p.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            src = clean_text(row.get("source") or "")
            if src:
                cache[src.casefold()] = {
                    "source": src,
                    "percentil": clean_text(row.get("percentil") or ""),
                    "ano": clean_text(row.get("ano") or ""),
                    "categoria": clean_text(row.get("categoria") or ""),
                }
    return cache


def save_percentile_cache(cache: dict[str, dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(cache.values(), key=lambda x: x["source"].casefold())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source", "percentil", "ano", "categoria"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def enrich_articles_full(
    input_csv: Path,
    output_csv: Path,
    cfg: AppConfig,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, dict[str, list[dict[str, str]]]]:
    """Enriquece uma tabela de artigos com Scopus, Crossref fallback, SciVal FWCI, CiteScore Percentil e Qualis."""
    df = pd.read_csv(input_csv, encoding="utf-8-sig", dtype=str).fillna("")
    rows = df.to_dict(orient="records")

    # Carrega cache local de FWCIs já conhecidos no repositório (caso SciVal esteja sem VPN no momento)
    local_fwci_fallback: dict[str, dict[str, str]] = {}
    for candidate_csv in (
        Path(cfg.output_dir).parent / "branches" / "main" / "data" / "scopus_fwci.csv",
        Path(cfg.output_dir).parent / "branches" / "main" / "data" / "artigos_percentil_final.csv",
        Path(cfg.output_dir).parent / "branches" / "main" / "resultados_2021_2024" / "artigos_docentes_2021_2024_enriquecido.csv",
        Path(cfg.output_dir).parent / "artigos.csv",
    ):
        if candidate_csv.exists():
            try:
                for r_old in csv.DictReader(candidate_csv.open(encoding="utf-8-sig", newline="")):
                    d_old = normalize_doi(r_old.get("doi")).lower()
                    if d_old:
                        cur = local_fwci_fallback.setdefault(d_old, {})
                        for k_f in ("fwci", "citations", "source", "scopus_id", "eid", "resumo_scopus", "ano_publicacao_scopus"):
                            if r_old.get(k_f) and not cur.get(k_f):
                                cur[k_f] = str(r_old[k_f]).strip()
            except Exception:
                continue

    dois = sorted({normalize_doi(r.get("doi")).lower() for r in rows} - {""})
    log(f"Iniciando enriquecimento bibliométrico para {len(dois)} DOIs únicos ({len(rows)} registros)...")

    scopus_meta_by_doi: dict[str, tuple[dict, str]] = {}
    authors_by_doi: dict[str, list[dict[str, str]]] = {}
    fwci_by_scopus_id: dict[str, str] = {}
    scival_available = bool(cfg.scopus_api_key.strip())

    for idx, doi in enumerate(dois, start=1):
        if stop_event and stop_event.is_set():
            log("Enriquecimento interrompido pelo usuário.")
            break
        if progress:
            progress(idx, max(len(dois), 1), f"Consultando DOI [{idx}/{len(dois)}]: {doi}")

        meta, authors, status = fetch_scopus_by_doi(doi, cfg.scopus_api_key, cfg.scopus_inst_token)

        # Fallback automático via Crossref se faltar ISSN, periódico ou autores
        if not meta.get("source") or not meta.get("issn") or not authors:
            cr_meta, cr_authors, cr_status = fetch_crossref_fallback(doi)
            for k, v in cr_meta.items():
                if v and not meta.get(k):
                    meta[k] = v
            if not authors and cr_authors:
                authors = cr_authors
            if status != "coletado_scopus" and cr_status == "coletado_crossref":
                status = "coletado_crossref"

        # Completa com cache local caso API esteja offline/sem VPN
        cached = local_fwci_fallback.get(doi, {})
        for k, v in cached.items():
            if v and not meta.get(k):
                meta[k] = v

        scopus_meta_by_doi[doi] = (meta, status)
        authors_by_doi[doi] = authors

        # SciVal FWCI
        sid = str(meta.get("scopus_id") or "")
        if scival_available and sid.isdigit() and (cfg.sobrescrever_metricas or not meta.get("fwci")):
            if sid not in fwci_by_scopus_id:
                fwci_val, fwci_st = fetch_scival_fwci(
                    sid, cfg.scopus_api_key, cfg.scopus_inst_token, cfg.fwci_year_range
                )
                if fwci_st == "scival_sem_permissao":
                    log("  -> Aviso: SciVal API retornou 401/403 (sem VPN/token SciVal). Usando FWCIs existentes quando disponíveis.")
                    scival_available = False
                else:
                    fwci_by_scopus_id[sid] = fwci_val
            if fwci_by_scopus_id.get(sid):
                meta["fwci"] = fwci_by_scopus_id[sid]

        time.sleep(max(cfg.api_delay, 0.05))

    # Carrega Qualis e Cache de Percentis
    log("Cruzando periódicos com base Qualis CAPES e Percentis CiteScore...")
    by_issn, by_title = load_qualis_unified(cfg.qualis_path)

    perc_out_path = Path(cfg.output_dir) / "percentis_periodicos.csv"
    perc_cache = load_percentile_cache(cfg.percentis_csv)
    perc_cache.update(load_percentile_cache(perc_out_path))

    # Identifica periódicos pendentes de percentil
    journals_issn: dict[str, str] = {}
    for meta, _ in scopus_meta_by_doi.values():
        src = clean_text(meta.get("source") or "")
        if src:
            journals_issn[src] = journals_issn.get(src) or clean_text(meta.get("issn") or "")

    pending_journals = [
        src for src in sorted(journals_issn)
        if src.casefold() not in perc_cache or to_float(perc_cache[src.casefold()].get("percentil")) is None
    ]
    if pending_journals and cfg.scopus_api_key.strip():
        for j_idx, j_src in enumerate(pending_journals, start=1):
            if stop_event and stop_event.is_set():
                break
            if progress:
                progress(j_idx, len(pending_journals), f"Buscando Percentil CiteScore: {j_src}")
            res_p = fetch_journal_percentile(
                j_src, journals_issn.get(j_src, ""), cfg.scopus_api_key, cfg.scopus_inst_token
            )
            if res_p.get("percentil") or j_src.casefold() not in perc_cache:
                perc_cache[j_src.casefold()] = res_p
            time.sleep(max(cfg.api_delay, 0.1))

    save_percentile_cache(perc_cache, perc_out_path)

    # Monta linhas enriquecidas finais
    out_columns = list(df.columns)
    for col_new in (
        "scopus_id", "eid", "ano_publicacao_scopus", "source", "issn",
        "citations", "fwci", "faixa_F", "percentil", "faixa_P", "qualis",
        "resumo_scopus", "scopus_status",
    ):
        if col_new not in out_columns:
            out_columns.append(col_new)

    for r in rows:
        doi_norm = normalize_doi(r.get("doi")).lower()
        meta, st = scopus_meta_by_doi.get(doi_norm, ({}, "sem_doi"))
        r["scopus_status"] = st
        if doi_norm:
            r["doi"] = doi_norm
        for k in ("scopus_id", "eid", "ano_publicacao_scopus", "source", "issn", "citations", "fwci", "resumo_scopus"):
            val = clean_text(meta.get(k) or "")
            if val and (cfg.sobrescrever_metricas or not clean_text(r.get(k) or "")):
                r[k] = val
        if not clean_text(r.get("artigo") or "") and meta.get("artigo_scopus"):
            r["artigo"] = meta["artigo_scopus"]
        if not clean_text(r.get("ano_publicacao_scopus") or "") and clean_text(r.get("ano_lattes") or ""):
            r["ano_publicacao_scopus"] = clean_text(r.get("ano_lattes"))

        src = clean_text(r.get("source") or "")
        issn_val = clean_text(r.get("issn") or "")
        if src or issn_val:
            q_val = resolve_qualis(src, issn_val, by_issn, by_title)
            if q_val and (cfg.sobrescrever_metricas or not clean_text(r.get("qualis") or "")):
                r["qualis"] = q_val

        if src and src.casefold() in perc_cache:
            p_val = perc_cache[src.casefold()].get("percentil", "")
            if p_val:
                r["percentil"] = p_val

        r["faixa_F"] = faixa_fwci_str(r.get("fwci"))
        r["faixa_P"] = faixa_percentil_str(r.get("percentil"))

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=out_columns).to_csv(output_csv, index=False, encoding="utf-8-sig")

    n_qualis = sum(bool(clean_text(r.get("qualis"))) for r in rows)
    n_perc = sum(bool(clean_text(r.get("percentil"))) for r in rows)
    n_fwci = sum(bool(clean_text(r.get("fwci"))) for r in rows)
    log(
        f"Enriquecimento salvo em {output_csv.name}: {len(rows)} registros | "
        f"Qualis: {n_qualis} | Percentil P1..P8: {n_perc} | FWCI F1..F8: {n_fwci}."
    )
    return output_csv, authors_by_doi


# ==============================================================================
# Etapa 3: Coautoria Discente/Egresso, Dados de Defesa e Perfis Scopus
# ==============================================================================

def load_students_registry(alunos_csv: Path | str) -> pd.DataFrame:
    """Carrega ALUNOS-Completo.csv consolidando nível, situação, entrada, defesa e orientador."""
    df = pd.read_csv(alunos_csv, encoding="utf-8-sig")
    df = df[df["Nível"].isin(NIVEIS_POS) & df["Situação"].isin(SITUACOES_VALIDAS)].copy()
    df["nome"] = df["Name"].astype(str).str.split(",").str[0].str.strip()
    df["ano_entrada"] = pd.to_numeric(df["Entrada"].astype(str).str.extract(r"(\d{4})")[0], errors="coerce")
    df["ano_defesa"] = pd.to_numeric(df.get("Defesa"), errors="coerce")
    df["orientador"] = df.get("Orientador", pd.Series([""] * len(df))).fillna("").astype(str).str.strip()

    grouped = (
        df.sort_values(["ano_entrada", "ano_defesa"], na_position="last")
        .groupby("nome", as_index=False)
        .agg(
            situacao_aluno=("Situação", lambda s: "Egresso" if "Egresso" in set(s) else s.iloc[0]),
            nivel_aluno=("Nível", lambda v: " | ".join(sorted(set(str(x) for x in v if pd.notna(x))))),
            ano_entrada=("ano_entrada", "min"),
            ano_defesa=("ano_defesa", "max"),
            orientador=("orientador", lambda v: next((x for x in v if x), "")),
        )
    )
    return grouped


def collect_student_coauthorship(
    enriched_faculty_csv: Path,
    cfg: AppConfig,
    authors_by_doi: dict[str, list[dict[str, str]]] | None = None,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> Path:
    """Identifica discentes e egressos coautores nos artigos dos docentes (e opcionalmente nos perfis Scopus)."""
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"alunos_artigos_{cfg.ano_inicio}_{cfg.ano_fim}.csv"

    alunos_df = load_students_registry(cfg.alunos_csv)
    art_df = pd.read_csv(enriched_faculty_csv, encoding="utf-8-sig", dtype=str).fillna("")
    art_df["doi_norm"] = art_df["doi"].map(lambda d: normalize_doi(d).lower())
    valid_art = art_df[art_df["doi_norm"].ne("")].copy()

    dois = sorted(valid_art["doi_norm"].unique())
    if authors_by_doi is None:
        authors_by_doi = {}

    # Também extrai autores dos HTMLs do Lattes caso algum artigo não tenha retornado autores via API
    lattes_authors_by_doi: dict[str, list[str]] = defaultdict(list)
    lattes_dir = Path(cfg.lattes_dir)
    if lattes_dir.exists():
        for hf in lattes_dir.glob("*.html"):
            soup = BeautifulSoup(hf.read_text(encoding="utf-8", errors="ignore"), "html.parser")
            for tag in soup.select("div.artigo-completo"):
                d_val = ""
                lk = tag.select_one("a.icone-doi[href]")
                if lk:
                    d_val = normalize_doi(lk.get("href")).lower()
                if not d_val:
                    continue
                tr = tag.select_one("span.transform")
                if tr:
                    txt = clean_text(tr.get_text(" ", strip=True))
                    head = txt.split(" . ")[0]
                    lattes_authors_by_doi[d_val] = [p.strip() for p in head.split(";") if p.strip()]

    cols_saida = [
        "aluno", "situacao_aluno", "nivel_aluno", "ano_entrada", "ano_defesa", "orientador",
        "primeiro_autor", "posicao_autor", "tipo_correspondencia", "docente",
        "artigo", "doi", "ano_publicacao_scopus", "source", "issn",
        "citations", "fwci", "faixa_F", "percentil", "faixa_P", "qualis",
        "scopus_id", "eid", "scopus_author_id_aluno", "nome_autor_scopus",
        "resumo_scopus", "autores_do_artigo", "origem",
    ]

    matched_rows: list[dict[str, str]] = []
    for idx, doi in enumerate(dois, start=1):
        if stop_event and stop_event.is_set():
            break
        if progress:
            progress(idx, max(len(dois), 1), f"Verificando coautoria discente [{idx}/{len(dois)}]")

        authors = authors_by_doi.get(doi) or []
        if not authors:
            _, authors, _ = fetch_scopus_by_doi(doi, cfg.scopus_api_key, cfg.scopus_inst_token)
            if not authors:
                _, authors, _ = fetch_crossref_fallback(doi)
            if not authors and doi in lattes_authors_by_doi:
                # Converte "SOBRENOME, NOME" do Lattes para "NOME SOBRENOME"
                for pos_i, raw_la in enumerate(lattes_authors_by_doi[doi], start=1):
                    if "," in raw_la:
                        sur, giv = raw_la.split(",", 1)
                        authors.append({"id": "", "name": f"{giv.strip()} {sur.strip()}", "position": str(pos_i)})
                    else:
                        authors.append({"id": "", "name": raw_la, "position": str(pos_i)})
            time.sleep(max(cfg.api_delay * 0.5, 0.05))

        meta = valid_art[valid_art["doi_norm"].eq(doi)].iloc[0]
        ano_pub = to_float(meta.get("ano_publicacao_scopus"))
        docentes_artigo = sorted(valid_art.loc[valid_art["doi_norm"].eq(doi), "professor"].unique())
        lista_autores_str = " | ".join(a["name"] for a in authors if a.get("name"))

        for au in authors:
            au_name = au.get("name", "")
            if not au_name:
                continue
            for al in alunos_df.itertuples():
                tipo = nomes_compativeis(al.nome, au_name)
                if not tipo:
                    continue
                if ano_pub is not None and pd.notna(al.ano_entrada) and ano_pub < al.ano_entrada:
                    continue
                for doc_nome in docentes_artigo:
                    matched_rows.append({
                        "aluno": al.nome,
                        "situacao_aluno": str(al.situacao_aluno),
                        "nivel_aluno": str(al.nivel_aluno),
                        "ano_entrada": str(int(al.ano_entrada)) if pd.notna(al.ano_entrada) else "",
                        "ano_defesa": str(int(al.ano_defesa)) if pd.notna(al.ano_defesa) else "",
                        "orientador": str(al.orientador),
                        "primeiro_autor": "1" if str(au.get("position")) == "1" else "0",
                        "posicao_autor": str(au.get("position") or ""),
                        "tipo_correspondencia": tipo,
                        "docente": doc_nome,
                        "artigo": meta.get("artigo", ""),
                        "doi": doi,
                        "ano_publicacao_scopus": meta.get("ano_publicacao_scopus", ""),
                        "source": meta.get("source", ""),
                        "issn": meta.get("issn", ""),
                        "citations": meta.get("citations", ""),
                        "fwci": meta.get("fwci", ""),
                        "faixa_F": meta.get("faixa_F") or faixa_fwci_str(meta.get("fwci")),
                        "percentil": meta.get("percentil", ""),
                        "faixa_P": meta.get("faixa_P") or faixa_percentil_str(meta.get("percentil")),
                        "qualis": meta.get("qualis", ""),
                        "scopus_id": meta.get("scopus_id", ""),
                        "eid": meta.get("eid", ""),
                        "scopus_author_id_aluno": au.get("id", ""),
                        "nome_autor_scopus": au_name,
                        "resumo_scopus": meta.get("resumo_scopus", ""),
                        "autores_do_artigo": lista_autores_str,
                        "origem": "lattes+scopus",
                    })

    # Opcional: Busca complementar por Perfis Scopus de Docentes e Titulados (eventos + egressos)
    if cfg.incluir_perfis_scopus and cfg.scopus_api_key.strip() and not (stop_event and stop_event.is_set()):
        log("Executando busca complementar pelos Perfis Scopus (Author ID) de docentes e titulados...")
        try:
            extra_rows = _search_scopus_profiles_production(
                cfg=cfg,
                docentes=sorted(art_df["professor"].unique()),
                alunos_df=alunos_df,
                log=log,
                stop_event=stop_event,
            )
            matched_rows.extend(extra_rows)
        except Exception as exc:
            log(f"  -> Aviso na busca por perfis Scopus: {exc}")

    df_out = pd.DataFrame(matched_rows, columns=cols_saida)
    if not df_out.empty:
        df_out = df_out.drop_duplicates(subset=["aluno", "docente", "doi"], keep="first")
    df_out.to_csv(out_csv, index=False, encoding="utf-8-sig")

    n_dois_al = df_out["doi"].nunique() if not df_out.empty else 0
    n_alunos = df_out["aluno"].nunique() if not df_out.empty else 0
    n_prim = int(df_out["primeiro_autor"].eq("1").sum()) if not df_out.empty else 0
    log(
        f"Coautoria discente concluída: {len(df_out)} vínculos ({n_dois_al} artigos únicos, "
        f"{n_alunos} discentes/egressos, {n_prim} registros como 1º autor)."
    )
    return out_csv


def _search_scopus_profiles_production(
    cfg: AppConfig,
    docentes: list[str],
    alunos_df: pd.DataFrame,
    log: LogCallback = print,
    stop_event: threading.Event | None = None,
) -> list[dict[str, str]]:
    """Busca documentos adicionais (incluindo conferências) pelos perfis Scopus de docentes e titulados."""
    headers = {"Accept": "application/json", "X-ELS-APIKey": cfg.scopus_api_key.strip()}
    if cfg.scopus_inst_token.strip():
        headers["X-ELS-Insttoken"] = cfg.scopus_inst_token.strip()

    ini_docs, fim_docs = cfg.ano_inicio - 2, cfg.ano_fim + 1
    by_issn, by_title = load_qualis_unified(cfg.qualis_path)
    perc_cache = load_percentile_cache(Path(cfg.output_dir) / "percentis_periodicos.csv")

    def query_author_candidates(person_name: str) -> list[dict]:
        parts = tokens_nome(person_name)
        if len(parts) < 2:
            return []
        q = f"AUTHLASTNAME({strip_accents(parts[-1])}) AND AUTHFIRST({strip_accents(parts[0])})"
        r = requests.get(AUTHOR_SEARCH_URL, params={"query": q, "count": 25}, headers=headers, timeout=30)
        time.sleep(max(cfg.api_delay, 0.1))
        if not r.ok:
            return []
        entries = (r.json().get("search-results") or {}).get("entry") or []
        cands = []
        for e in entries:
            if "error" in e:
                break
            pref = e.get("preferred-name") or {}
            sc_name = f"{pref.get('given-name', '')} {pref.get('surname', '')}".strip()
            if nomes_compativeis(person_name, sc_name):
                cands.append({"id": str(e.get("dc:identifier", "")).split(":")[-1], "nome": sc_name})
        return cands

    def query_author_docs(auid: str) -> list[dict]:
        q = f"AU-ID({auid}) AND PUBYEAR > {ini_docs - 5} AND PUBYEAR < {fim_docs + 1}"
        r = requests.get(
            SCOPUS_SEARCH_URL,
            params={"query": q, "view": "COMPLETE", "count": 25},
            headers=headers,
            timeout=40,
        )
        time.sleep(max(cfg.api_delay, 0.1))
        if not r.ok:
            return []
        entries = (r.json().get("search-results") or {}).get("entry") or []
        return [e for e in entries if "error" not in e]

    ids_docente: dict[str, str] = {}
    docs_por_autor: dict[str, list[dict]] = {}

    for d_name in docentes:
        if stop_event and stop_event.is_set():
            break
        for cand in query_author_candidates(d_name):
            docs = query_author_docs(cand["id"])
            conf = [
                doc for doc in docs
                if any(
                    AFILIACOES_PPGPO.search(strip_accents(af.get("affilname") or "").casefold())
                    for af in (doc.get("affiliation") or [])
                )
            ]
            if conf:
                ids_docente[cand["id"]] = d_name
                docs_por_autor[cand["id"]] = docs

    titulados_df = alunos_df[
        alunos_df["situacao_aluno"].eq("Egresso")
        & alunos_df["ano_defesa"].between(cfg.ano_inicio, cfg.ano_fim)
    ]
    ids_titulado: dict[str, str] = {}
    for t_row in titulados_df.itertuples():
        if stop_event and stop_event.is_set():
            break
        for cand in query_author_candidates(t_row.nome):
            docs = query_author_docs(cand["id"])
            conf = [
                doc for doc in docs
                if any(
                    AFILIACOES_PPGPO.search(strip_accents(af.get("affilname") or "").casefold())
                    for af in (doc.get("affiliation") or [])
                )
                or any(str(a.get("authid") or "") in ids_docente for a in (doc.get("author") or []))
            ]
            completo = nomes_compativeis(t_row.nome, cand["nome"]) == "nome completo"
            if conf and (completo or len(conf) >= 0.5 * max(len(docs), 1)):
                ids_titulado[cand["id"]] = t_row.nome
                docs_por_autor[cand["id"]] = docs

    info_aluno = {r.nome: r for r in alunos_df.itertuples()}
    extra_rows: list[dict[str, str]] = []
    for _, docs in docs_por_autor.items():
        for doc in docs:
            doi = normalize_doi(doc.get("prism:doi")).lower()
            if not doi or doc.get("subtype") not in SUBTIPOS_SCOPUS:
                continue
            m_y = re.match(r"(\d{4})", str(doc.get("prism:coverDate") or ""))
            yr = int(m_y.group(1)) if m_y else None
            if not yr or not (ini_docs <= yr <= fim_docs):
                continue

            doc_authors = []
            for a in doc.get("author") or []:
                nm = f"{a.get('given-name') or ''} {a.get('surname') or ''}".strip() or str(a.get("authname") or "")
                doc_authors.append({"id": str(a.get("authid") or ""), "nome": nm, "seq": str(a.get("@seq") or "")})

            co_docs = sorted({ids_docente[a["id"]] for a in doc_authors if a["id"] in ids_docente}) or [""]
            for a in doc_authors:
                matched_student = None
                match_type = ""
                if a["id"] in ids_titulado:
                    cand_st = ids_titulado[a["id"]]
                    if co_docs != [""] or nomes_compativeis(cand_st, a["nome"]):
                        matched_student, match_type = cand_st, "scopus id"
                elif a["id"] not in ids_docente:
                    for st_name, st_info in info_aluno.items():
                        tp = nomes_compativeis(st_name, a["nome"])
                        if tp == "nome completo" and not (pd.notna(st_info.ano_entrada) and yr < st_info.ano_entrada):
                            matched_student, match_type = st_name, tp
                            break
                if not matched_student:
                    continue
                st_info = info_aluno.get(matched_student)
                src = clean_text(doc.get("prism:publicationName") or "")
                issn_v = str(doc.get("prism:issn") or doc.get("prism:eIssn") or "").replace("-", "")
                sid = str(doc.get("dc:identifier") or "").split(":")[-1]
                fwci_v, _ = fetch_scival_fwci(sid, cfg.scopus_api_key, cfg.scopus_inst_token, cfg.fwci_year_range)
                perc_v = perc_cache.get(src.casefold(), {}).get("percentil", "") if src else ""
                q_v = resolve_qualis(src, issn_v, by_issn, by_title)
                for doc_nome in co_docs:
                    extra_rows.append({
                        "aluno": matched_student,
                        "situacao_aluno": getattr(st_info, "situacao_aluno", ""),
                        "nivel_aluno": getattr(st_info, "nivel_aluno", ""),
                        "ano_entrada": str(int(st_info.ano_entrada)) if st_info and pd.notna(st_info.ano_entrada) else "",
                        "ano_defesa": str(int(st_info.ano_defesa)) if st_info and pd.notna(st_info.ano_defesa) else "",
                        "orientador": getattr(st_info, "orientador", ""),
                        "primeiro_autor": "1" if a["seq"] == "1" else "0",
                        "posicao_autor": a["seq"],
                        "tipo_correspondencia": match_type,
                        "docente": doc_nome,
                        "artigo": clean_text(doc.get("dc:title") or ""),
                        "doi": doi,
                        "ano_publicacao_scopus": str(yr),
                        "source": src,
                        "issn": issn_v,
                        "citations": str(doc.get("citedby-count") or ""),
                        "fwci": fwci_v,
                        "faixa_F": faixa_fwci_str(fwci_v),
                        "percentil": perc_v,
                        "faixa_P": faixa_percentil_str(perc_v),
                        "qualis": q_v,
                        "scopus_id": sid,
                        "eid": str(doc.get("eid") or ""),
                        "scopus_author_id_aluno": a["id"],
                        "nome_autor_scopus": a["nome"],
                        "resumo_scopus": clean_text(doc.get("dc:description") or ""),
                        "autores_do_artigo": " | ".join(x["nome"] for x in doc_authors),
                        "origem": f"perfil scopus ({SUBTIPOS_SCOPUS.get(doc.get('subtype'), 'artigo')})",
                    })
    log(f"  -> Perfis Scopus: {len(extra_rows)} vínculos adicionais encontrados.")
    return extra_rows


# ==============================================================================
# Etapa 4: Teses/Dissertações (Sophia/ITA) & Correlação Tese/Defesa × Artigo
# ==============================================================================

SOPHIA_DETAIL_URL = "http://www.sophia.bibl.ita.br/biblioteca/index.asp"
SOPHIA_LINK_RE = re.compile(r"LinkDetalhes\([^,]+,\s*\d+\s*,\s*\d+\s*,\s*(\d+)")


def scrape_sophia_theses(
    cfg: AppConfig,
    only_new_students: bool = True,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> Path:
    """Coleta teses e dissertações (incluindo resumos) no catálogo Sophia da biblioteca do ITA."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    out_csv = Path(cfg.output_dir) / "teses_dissertacoes_sophia.csv"
    existing_rows: list[dict] = []
    already_searched: set[str] = set()

    for src_path in (out_csv, Path(cfg.materiais_csv) if cfg.materiais_csv else None):
        if src_path and src_path.exists():
            df_ex = pd.read_csv(src_path, encoding="utf-8-sig", dtype=str).fillna("")
            existing_rows = df_ex.to_dict(orient="records")
            already_searched = {chave_nome(a) for a in df_ex.get("autor", []) if a}
            break

    students_df = pd.read_csv(cfg.alunos_csv, encoding="utf-8-sig")
    students_df = students_df[students_df["Situação"] != "Desligado"].drop_duplicates(subset=["Name"])
    names_to_search = [
        str(n).split(",")[0].strip()
        for n in students_df["Name"]
        if pd.notna(n) and (not only_new_students or chave_nome(n) not in already_searched)
    ]

    if not names_to_search:
        if not out_csv.exists() and existing_rows:
            pd.DataFrame(existing_rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
        log(f"Base Sophia/ITA já está completa ({len(existing_rows)} registros em cache).")
        return out_csv

    log(f"Consultando catálogo Sophia/ITA para {len(names_to_search)} aluno(s)...")
    driver = make_browser_driver(cfg.browser, headless=True)
    rows = list(existing_rows)

    def quick_box(drv):
        try:
            drv.switch_to.alert.accept()
            return False
        except Exception:
            pass
        try:
            drv.switch_to.default_content()
            drv.switch_to.frame("mainFrame")
            box = drv.find_element(By.CSS_SELECTOR, "form[name='frm_rapida'] input[name='rapida_campo']")
            return box if box.is_displayed() else False
        except Exception:
            return False

    def results_ready(drv):
        try:
            drv.switch_to.alert.accept()
            return "alert"
        except Exception:
            pass
        try:
            drv.switch_to.default_content()
            drv.switch_to.frame("mainFrame")
            h = drv.page_source
            if any(m in h for m in ("Nenhum registro encontrado", "registros encontrados", "registro encontrado")):
                return h
        except Exception:
            pass
        return False

    try:
        with requests.Session() as http_sess:
            for idx, st_name in enumerate(names_to_search, start=1):
                if stop_event and stop_event.is_set():
                    break
                if progress:
                    progress(idx, len(names_to_search), f"Sophia/ITA: {st_name}")
                try:
                    driver.get(cfg.sophia_url)
                    box = WebDriverWait(driver, 25).until(quick_box)
                    box.clear()
                    box.send_keys(st_name)
                    driver.find_element(
                        By.CSS_SELECTOR, "form[name='frm_rapida'] input.button_busca[value='Buscar']"
                    ).click()
                    marker = WebDriverWait(driver, 25).until(results_ready)
                    if marker == "alert" or not isinstance(marker, str):
                        continue

                    soup = BeautifulSoup(marker, "html.parser")
                    for table in soup.select("#p_div_aba1_resultado table"):
                        fields = {}
                        body = table.find("tbody", recursive=False) or table
                        for tr in body.find_all("tr", recursive=False):
                            lb = tr.find("td", class_="td_ficha_esq", recursive=False)
                            vl = tr.find("td", class_="td_ficha_dir", recursive=False)
                            if lb and vl:
                                fields[lb.get_text(strip=True)] = vl
                        if "Título" not in fields:
                            continue
                        lk = fields["Título"].select_one("a.link_custom_negrito")
                        m_cod = SOPHIA_LINK_RE.search(lk.get("href", "")) if lk else None
                        cod_obra = m_cod.group(1) if m_cod else ""
                        resumo_obra = ""
                        if cod_obra:
                            try:
                                r_det = http_sess.get(
                                    SOPHIA_DETAIL_URL,
                                    params={
                                        "modo_busca": "rapida",
                                        "content": "detalhe",
                                        "codigo_obra": cod_obra,
                                        "tipo_obra": 1,
                                        "Servidor": 1,
                                    },
                                    headers={"User-Agent": "Mozilla/5.0"},
                                    timeout=20,
                                )
                                if r_det.ok:
                                    s_det = BeautifulSoup(r_det.content, "html.parser")
                                    for lb_d in s_det.select("td.td_detalhe_descricao_recuo, td.td_detalhe_descricao"):
                                        if lb_d.get_text(strip=True).replace("\xa0", "") == "Resumo":
                                            v_d = lb_d.find_next_sibling("td", class_="td_detalhe_valor")
                                            if v_d:
                                                resumo_obra = " ".join(v_d.get_text(" ").split())
                                            break
                            except Exception:
                                pass

                        assunto_cell = fields.get("Assuntos")
                        assunto_txt = ""
                        if assunto_cell:
                            lks = assunto_cell.select("a.link_custom")
                            assunto_txt = "; ".join(" ".join(x.get_text("").split()) for x in lks) if lks else " ".join(assunto_cell.get_text("").split())

                        rows.append({
                            "autor": st_name,
                            "material": " ".join(fields["Material"].get_text("").split()) if "Material" in fields else "",
                            "titulo": " ".join(fields["Título"].get_text("").split()),
                            "ano": " ".join(fields["Ano"].get_text("").split()) if "Ano" in fields else "",
                            "assunto": assunto_txt,
                            "codigo_obra": cod_obra,
                            "resumo": resumo_obra,
                        })
                except Exception as exc:
                    log(f"  -> Aviso Sophia ({st_name}): {exc}")
    finally:
        driver.quit()

    pd.DataFrame(rows, columns=["autor", "material", "titulo", "ano", "assunto", "codigo_obra", "resumo"]).to_csv(
        out_csv, index=False, encoding="utf-8-sig"
    )
    log(f"Base Sophia/ITA salva em {out_csv.name} ({len(rows)} registros totais).")
    return out_csv


def _encode_texts_embeddings(texts: list[str], log: LogCallback = print) -> tuple[np.ndarray, str]:
    """Codifica textos em vetores L2-normalizados usando FastEmbed (ONNX MiniLM), SentenceTransformers ou TF-IDF."""
    if not texts:
        return np.zeros((0, 1)), "vazio"

    # 1. Tenta FastEmbed (ONNXRuntime — leve e usa exatamente sentence-transformers/all-MiniLM-L6-v2)
    try:
        import os

        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
        vecs = np.array(list(model.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms, "all-MiniLM-L6-v2 (ONNX)"
    except Exception as exc_fe:
        log(f"  -> Nota: FastEmbed indisponível ({exc_fe}); tentando SentenceTransformers...")

    # 2. Tenta SentenceTransformers (PyTorch)
    try:
        from sentence_transformers import SentenceTransformer

        st_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        vecs = st_model.encode(texts, normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32), "all-MiniLM-L6-v2 (PyTorch)"
    except Exception:
        pass

    # 3. Fallback offline imediato via TF-IDF (scikit-learn)
    from sklearn.feature_extraction.text import TfidfVectorizer

    vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
    mat = vec.fit_transform(texts).toarray().astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms, "TF-IDF Offline"


def compute_thesis_article_correlation(
    student_articles_csv: Path,
    theses_csv: Path,
    cfg: AppConfig,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
) -> Path:
    """Calcula a correlação semântica entre Teses/Dissertações (com dados de Defesa) e Artigos publicados."""
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"correlacao_teses_artigos_{cfg.ano_inicio}_{cfg.ano_fim}.csv"

    if not theses_csv.exists():
        raise FileNotFoundError(f"Arquivo de teses não encontrado: {theses_csv}")
    if not student_articles_csv.exists():
        raise FileNotFoundError(f"Arquivo de artigos discentes não encontrado: {student_articles_csv}")

    if progress:
        progress(1, 3, "Carregando teses, defesas e artigos...")

    teses = pd.read_csv(theses_csv, encoding="utf-8-sig", dtype=str).fillna("")
    # Filtra apenas dissertações e teses
    teses["_mat_norm"] = teses["material"].map(lambda m: re.sub(r"\s+", "", chave_nome(m)))
    teses = teses[teses["_mat_norm"].str.startswith(("dissertacao", "tese"))].copy()
    teses["nivel_trabalho"] = np.where(teses["_mat_norm"].str.startswith("tese"), "Tese de Doutorado", "Dissertação de Mestrado")
    teses["author_key"] = teses["autor"].map(chave_nome)

    for c in ("codigo_obra", "resumo"):
        if c not in teses.columns:
            teses[c] = ""

    teses["titulo_en"] = teses["titulo"]
    teses["resumo_en"] = teses["resumo"]
    teses["traduzida"] = False
    if cfg.traducoes_csv and Path(cfg.traducoes_csv).exists():
        trad = pd.read_csv(cfg.traducoes_csv, encoding="utf-8-sig", dtype=str).fillna("")
        if "codigo_obra" in trad.columns:
            trad = trad.drop_duplicates("codigo_obra").set_index("codigo_obra")
            mask = teses["codigo_obra"].isin(trad.index) & teses["codigo_obra"].ne("")
            cods = teses.loc[mask, "codigo_obra"]
            teses.loc[mask, "titulo_en"] = cods.map(trad["titulo_en"]).replace("", np.nan).fillna(teses.loc[mask, "titulo"])
            teses.loc[mask, "resumo_en"] = cods.map(trad["resumo_en"]).replace("", np.nan).fillna(teses.loc[mask, "resumo"])
            teses.loc[mask, "traduzida"] = True

    teses["usa_resumo_tese"] = teses["resumo_en"].str.strip().ne("")
    teses["texto_tese_completo"] = [
        f"{t.strip()}. {r.strip()}" if r.strip() else t.strip()
        for t, r in zip(teses["titulo_en"], teses["resumo_en"])
    ]
    teses = teses.drop_duplicates(subset=["author_key", "titulo"]).reset_index(drop=True)

    # Mapeamento de autores (nome completo ou primeiro+último único)
    chaves_teses = set(teses["author_key"])
    por_extremos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ck in chaves_teses:
        ext = primeiro_ultimo(ck)
        if ext:
            por_extremos[ext].add(ck)

    def map_author_key(nome_aluno: str) -> str:
        ck = chave_nome(nome_aluno)
        if ck in chaves_teses:
            return ck
        cands = por_extremos.get(primeiro_ultimo(nome_aluno) or ("", ""), set())
        return next(iter(cands)) if len(cands) == 1 else ""

    artigos = pd.read_csv(student_articles_csv, encoding="utf-8-sig", dtype=str).fillna("")
    if artigos.empty:
        log("Nenhum artigo discente disponível para correlacionar com teses.")
        pd.DataFrame().to_csv(out_csv, index=False, encoding="utf-8-sig")
        return out_csv

    # Anexa dados de Defesa do cadastro ALUNOS-Completo caso não estejam preenchidos
    alunos_reg = load_students_registry(cfg.alunos_csv).set_index("nome") if cfg.alunos_csv and Path(cfg.alunos_csv).exists() else pd.DataFrame()

    artigos["author_key"] = artigos["aluno"].map(map_author_key)
    artigos["usa_resumo"] = artigos.get("resumo_scopus", pd.Series([""] * len(artigos))).str.strip().ne("")
    artigos["texto_artigo_completo"] = [
        f"{t.strip()}. {r.strip()}" if r.strip() else t.strip()
        for t, r in zip(artigos["artigo"], artigos.get("resumo_scopus", [""] * len(artigos)))
    ]
    artigos_unicos = artigos.drop_duplicates(subset=["aluno", "doi"]).reset_index(drop=True)

    teses = teses.assign(thesis_idx=np.arange(len(teses)))
    artigos_unicos = artigos_unicos.assign(article_idx=np.arange(len(artigos_unicos)))
    pares = teses.merge(artigos_unicos[artigos_unicos["author_key"].ne("")], on="author_key", how="inner", suffixes=("_tese", ""))

    cols_corr = [
        "autor", "situacao_aluno", "nivel_trabalho", "orientador", "ano_defesa",
        "material", "titulo", "ano", "artigo", "doi", "ano_publicacao_scopus",
        "primeiro_autor", "fwci", "faixa_F", "percentil", "faixa_P", "qualis",
        "usa_resumo", "usa_resumo_tese", "traduzida",
        "cosine_similarity", "cosine_similarity_titulo", "cosine_similarity_resumo_tese",
        "associado_tese",
    ]

    if pares.empty:
        pd.DataFrame(columns=cols_corr).to_csv(out_csv, index=False, encoding="utf-8-sig")
        log("Nenhum par Tese × Artigo encontrado para os autores do período.")
        return out_csv

    if progress:
        progress(2, 3, "Calculando embeddings neurais de similaridade Tese × Artigo...")

    # Codifica todos os textos em uma única passagem do modelo para máxima velocidade
    all_texts = (
        teses["titulo_en"].tolist()
        + teses["texto_tese_completo"].tolist()
        + artigos_unicos["texto_artigo_completo"].tolist()
        + artigos_unicos["artigo"].tolist()
    )
    all_vecs, engine_name = _encode_texts_embeddings(all_texts, log=log)

    n_t = len(teses)
    n_a = len(artigos_unicos)
    v_tese_titulo = all_vecs[0:n_t]
    v_tese_full = all_vecs[n_t : 2 * n_t]
    v_art_full = all_vecs[2 * n_t : 2 * n_t + n_a]
    v_art_titulo = all_vecs[2 * n_t + n_a :]

    idx_t = pares["thesis_idx"].to_numpy()
    idx_a = pares["article_idx"].to_numpy()

    sim_padrao = np.sum(v_tese_titulo[idx_t] * v_art_full[idx_a], axis=1)
    sim_titulo = np.sum(v_tese_titulo[idx_t] * v_art_titulo[idx_a], axis=1)
    sim_resumo_tese = np.sum(v_tese_full[idx_t] * v_art_full[idx_a], axis=1)

    pares["cosine_similarity"] = np.round(sim_padrao, 4)
    pares["cosine_similarity_titulo"] = np.round(sim_titulo, 4)
    pares["cosine_similarity_resumo_tese"] = np.round(sim_resumo_tese, 4)
    pares["primeiro_autor"] = pares["primeiro_autor"].astype(str).isin(["1", "True", "true"])
    pares["associado_tese"] = pares["cosine_similarity"] >= cfg.sim_min

    # Preenche defesa/orientador do registro institucional se vazio
    if not alunos_reg.empty:
        for col_fill, reg_col in (("ano_defesa", "ano_defesa"), ("orientador", "orientador"), ("situacao_aluno", "situacao_aluno")):
            if col_fill not in pares.columns:
                pares[col_fill] = ""
            for i_row, row_p in pares.iterrows():
                if not str(row_p.get(col_fill) or "").strip() and row_p["aluno"] in alunos_reg.index:
                    val_r = alunos_reg.loc[row_p["aluno"], reg_col]
                    if pd.notna(val_r):
                        pares.at[i_row, col_fill] = str(int(val_r)) if isinstance(val_r, float) else str(val_r)

    for c in cols_corr:
        if c not in pares.columns:
            pares[c] = ""

    final_df = (
        pares.loc[:, cols_corr]
        .sort_values(["autor", "cosine_similarity"], ascending=[True, False])
        .reset_index(drop=True)
    )
    final_df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    if progress:
        progress(3, 3, "Correlação Tese × Artigo concluída!")
    n_assoc = int(final_df["associado_tese"].sum())
    log(
        f"Correlação Tese/Defesa × Artigo concluída via {engine_name}: "
        f"{len(final_df)} pares avaliados ({n_assoc} com similaridade >= {cfg.sim_min:.2f})."
    )
    return out_csv


# ==============================================================================
# Consulta Rápida Avulsa de DOIs
# ==============================================================================

def lookup_dois_batch(
    raw_dois: list[str],
    cfg: AppConfig,
    log: LogCallback = print,
    progress: ProgressCallback | None = None,
) -> pd.DataFrame:
    """Consulta imediatamente uma lista de DOIs avulsos trazendo todas as métricas CAPES."""
    clean_dois = []
    for item in raw_dois:
        d = normalize_doi(item).lower()
        if d and d not in clean_dois:
            clean_dois.append(d)

    if not clean_dois:
        return pd.DataFrame()

    tmp_in = Path(cfg.output_dir) / "_consulta_rapida_in.csv"
    tmp_out = Path(cfg.output_dir) / "consulta_rapida_resultados.csv"
    tmp_in.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"doi": d, "artigo": ""} for d in clean_dois]).to_csv(tmp_in, index=False, encoding="utf-8-sig")

    _, authors_map = enrich_articles_full(tmp_in, tmp_out, cfg, log=log, progress=progress)
    try:
        tmp_in.unlink(missing_ok=True)
    except Exception:
        pass

    df_res = pd.read_csv(tmp_out, encoding="utf-8-sig", dtype=str).fillna("")
    df_res["autores"] = df_res["doi"].map(
        lambda d: " | ".join(a["name"] for a in authors_map.get(d, []))
    )
    cols_order = [
        "doi", "artigo", "ano_publicacao_scopus", "source", "issn",
        "citations", "fwci", "faixa_F", "percentil", "faixa_P", "qualis",
        "autores", "resumo_scopus", "scopus_status",
    ]
    cols_present = [c for c in cols_order if c in df_res.columns]
    return df_res[cols_present]
