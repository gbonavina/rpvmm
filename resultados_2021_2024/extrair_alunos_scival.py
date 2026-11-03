#!/usr/bin/env python3
"""Encontra alunos orientados que coautoraram artigos de docentes no SciVal.

Entrada: CSV de artigos de docentes (DOI, FWCI etc.) e lista consolidada de
orientandos Lattes, sem pós-doutorandos. Os autores e seus Author IDs são
recuperados no Scopus pelo DOI. Cada saída é uma evidência aluno × artigo.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/{doi}"
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
CONNECTORS = {"da", "das", "de", "do", "dos", "e"}
OUTPUT_COLUMNS = [
    "aluno", "docente", "doi", "eid", "scopus_id", "artigo", "resumo_scopus", "source", "qualis",
    "citations", "fwci", "ano_publicacao_scopus", "scopus_author_id_aluno", "nome_autor_scopus", "posicao_autor",
    "primeiro_autor", "tipo_correspondencia", "pontuacao_correspondencia",
    "evidencia_orientacao_lattes", "lattes_id_orientando", "tipos_orientacao", "status_orientacao",
    "autores_do_artigo",
]
UNMATCHED_COLUMNS = ["docente", "doi", "artigo", "motivo"]
ERROR_COLUMNS = ["docente", "doi", "artigo", "erro"]


def text(value: object) -> str:
    return str(value or "").strip()


def tokens(value: str) -> list[str]:
    return [word.casefold() for word in WORD_RE.findall(value) if word.casefold() not in CONNECTORS]


def norm(value: str) -> str:
    return " ".join(tokens(value))


def name_match(person: str, author: str) -> tuple[str, int]:
    left, right = tokens(person), tokens(author)
    if not left or not right or left[-1] != right[-1]:
        return "", 0
    if left == right:
        return "nome completo", 100
    if left[0] == right[0]:
        return "primeiro nome e sobrenome", 85
    if left[0][0:1] == right[0][0:1]:
        return "inicial e sobrenome", 55
    return "", 0


def publication_year(core: dict[str, object]) -> int | None:
    """Obtém o ano da publicação segundo os metadados do Scopus."""
    for field in ("prism:coverDate", "prism:coverDisplayDate", "prism:publicationDate"):
        match = re.search(r"\b(19|20)\d{2}\b", text(core.get(field)))
        if match:
            return int(match.group(0))
    return None


def flatten_text(value: object) -> str:
    """Converte a estrutura XML/JSON de texto retornada pelo Scopus em texto simples."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for key, item in value.items() if not key.startswith("@"))
    return ""


def abstract_for_response(response: object, core: object) -> str:
    """Obtém o resumo nas variações de estrutura usadas pela API Abstract Retrieval."""
    if not isinstance(response, dict):
        return ""
    head = response.get("item", {}).get("bibrecord", {}).get("head", {})
    candidates = (
        head.get("abstracts") if isinstance(head, dict) else None,
        head.get("ce:abstract") if isinstance(head, dict) else None,
        response.get("abstracts"),
        core.get("dc:description") if isinstance(core, dict) else None,
        core.get("description") if isinstance(core, dict) else None,
    )
    for candidate in candidates:
        abstract = html.unescape(re.sub(r"<[^>]+>", " ", flatten_text(candidate)))
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if abstract:
            return abstract
    return ""


class ScopusError(RuntimeError):
    pass


def authors_for_doi(doi: str, api_key: str, inst_token: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key}
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    url = f"{ABSTRACT_URL.format(doi=quote(doi, safe='/'))}?{urlencode({'view': 'META_ABS'})}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:350]
        raise ScopusError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ScopusError(f"Falha de rede: {exc.reason}") from exc
    response = payload.get("abstracts-retrieval-response", {})
    core = response.get("coredata", {}) if isinstance(response, dict) else {}
    raw_authors = response.get("authors", {}).get("author", []) if isinstance(response, dict) else []
    if isinstance(raw_authors, dict):
        raw_authors = [raw_authors]
    authors = []
    for fallback_position, item in enumerate(raw_authors, start=1):
        if not isinstance(item, dict):
            continue
        name = " ".join(filter(None, [text(item.get("ce:given-name") or item.get("given-name")), text(item.get("ce:surname") or item.get("surname"))]))
        name = name or text(item.get("ce:indexed-name") or item.get("indexed-name"))
        authors.append({
            "id": text(item.get("@auid") or item.get("auid")), "name": name,
            "position": text(item.get("@seq") or item.get("seq")) or str(fallback_position),
        })
    return {
        "eid": text(core.get("eid")),
        "scopus_id": text(core.get("dc:identifier")).removeprefix("SCOPUS_ID:"),
        "ano_publicacao_scopus": str(publication_year(core) or ""),
        "resumo_scopus": abstract_for_response(response, core),
    }, authors


def load_students(path: Path) -> dict[str, list[dict[str, str]]]:
    """Indexa alunos por docente Lattes; a entrada já exclui pós-docs."""
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            student = text(row.get("aluno"))
            for professor in text(row.get("orientadores_lattes")).split(" | "):
                if student and professor:
                    result[norm(professor)].append({
                        "aluno": student, "lattes_id_orientando": text(row.get("lattes_id_orientando")),
                        "tipos_orientacao": text(row.get("tipos_orientacao")), "status_orientacao": text(row.get("status_orientacao")),
                    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrai alunos coautores a partir de artigos SciVal de docentes.")
    parser.add_argument("--articles", required=True, type=Path, help="CSV SciVal com professor, artigo, DOI e FWCI.")
    parser.add_argument("--students", required=True, type=Path, help="orientandos_lattes_consolidados.csv; pós-docs já excluídos.")
    parser.add_argument("--output", required=True, type=Path, help="CSV aluno × artigo encontrado.")
    parser.add_argument("--unmatched-output", required=True, type=Path, help="Artigos sem aluno orientado correspondente.")
    parser.add_argument("--errors-output", required=True, type=Path, help="Falhas de consulta Scopus.")
    parser.add_argument("--api-key", help="Chave Scopus; prefira SCOPUS_API_KEY.")
    parser.add_argument("--inst-token", help="Token institucional; prefira SCOPUS_INSTTOKEN.")
    parser.add_argument("--start-year", type=int, default=2025, help="Inclui somente artigos publicados a partir deste ano; padrão 2025.")
    parser.add_argument("--min-score", type=int, choices=(55, 85, 100), default=85, help="Pontuação mínima de nome; padrão 85 (primeiro nome e sobrenome).")
    parser.add_argument("--delay", type=float, default=0.15, help="Pausa entre DOIs.")
    args = parser.parse_args()
    api_key = args.api_key or os.environ.get("SCOPUS_API_KEY", "")
    inst_token = args.inst_token or os.environ.get("SCOPUS_INSTTOKEN", "")
    if not api_key:
        parser.error("Defina SCOPUS_API_KEY ou use --api-key.")
    try:
        students_by_teacher = load_students(args.students)
        with args.articles.open(encoding="utf-8-sig", newline="") as handle:
            articles = list(csv.DictReader(handle))
    except OSError as exc:
        parser.error(str(exc))

    outputs, unmatched, errors, cache = [], [], [], {}
    seen = set()
    usable = [row for row in articles if text(row.get("professor")) and text(row.get("doi"))]
    for index, article in enumerate(usable, start=1):
        teacher, doi = text(article.get("professor")), text(article.get("doi")).lower()
        students = students_by_teacher.get(norm(teacher), [])
        if not students:
            unmatched.append({"docente": teacher, "doi": doi, "artigo": text(article.get("artigo")), "motivo": "Nenhum aluno orientado Lattes associado a este docente."})
            continue
        try:
            if doi not in cache:
                cache[doi] = authors_for_doi(doi, api_key, inst_token)
                time.sleep(max(0, args.delay))
            metadata, authors = cache[doi]
        except ScopusError as exc:
            errors.append({"docente": teacher, "doi": doi, "artigo": text(article.get("artigo")), "erro": str(exc)})
            continue
        publication_year_value = int(metadata["ano_publicacao_scopus"]) if metadata["ano_publicacao_scopus"] else None
        if publication_year_value is None:
            unmatched.append({"docente": teacher, "doi": doi, "artigo": text(article.get("artigo")), "motivo": "Ano de publicação ausente nos metadados Scopus; artigo excluído do recorte."})
            continue
        if publication_year_value < args.start_year:
            unmatched.append({"docente": teacher, "doi": doi, "artigo": text(article.get("artigo")), "motivo": f"Publicado em {publication_year_value}; fora do recorte a partir de {args.start_year}."})
            continue
        author_list = " | ".join(author["name"] for author in authors if author["name"])
        matched = 0
        for student in students:
            for author in authors:
                kind, score = name_match(student["aluno"], author["name"])
                if score < args.min_score:
                    continue
                record_key = (student["aluno"], teacher, doi, author["id"] or author["name"])
                if record_key in seen:
                    continue
                seen.add(record_key)
                matched += 1
                outputs.append({
                    "aluno": student["aluno"], "docente": teacher, "doi": doi, **metadata,
                    "artigo": text(article.get("artigo")), "resumo_scopus": metadata["resumo_scopus"],
                    "source": text(article.get("source")),
                    "qualis": text(article.get("qualis")), "citations": text(article.get("citations")), "fwci": text(article.get("fwci")),
                    "scopus_author_id_aluno": author["id"], "nome_autor_scopus": author["name"], "posicao_autor": author["position"],
                    "primeiro_autor": "1" if author["position"] == "1" else "0", "tipo_correspondencia": kind,
                    "pontuacao_correspondencia": str(score), "evidencia_orientacao_lattes": "1",
                    "lattes_id_orientando": student["lattes_id_orientando"], "tipos_orientacao": student["tipos_orientacao"],
                    "status_orientacao": student["status_orientacao"], "autores_do_artigo": author_list,
                })
        if not matched:
            unmatched.append({"docente": teacher, "doi": doi, "artigo": text(article.get("artigo")), "motivo": "Nenhum nome de aluno orientado coincidiu com os autores Scopus."})
        print(f"[{index}/{len(usable)}] {doi}: {matched} aluno(s)", flush=True)

    for path, columns, rows in ((args.output, OUTPUT_COLUMNS, outputs), (args.unmatched_output, UNMATCHED_COLUMNS, unmatched), (args.errors_output, ERROR_COLUMNS, errors)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    print(f"Concluído: {len(outputs)} evidência(s) aluno × artigo; {len(unmatched)} artigo(s) sem correspondência; {len(errors)} erro(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
