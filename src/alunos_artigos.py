from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LATTES_DIR = DATA_DIR / "lattes"
ARTICLES_PATH = DATA_DIR / "artigos.csv"
STUDENTS_PATH = ROOT / "ALUNOS-Completo.csv"
OUTPUT_PATH = DATA_DIR / "artigos_com_alunos.csv"

OUTPUT_FIELDS = [
    "professor",
    "artigo",
    "doi",
    "aluno",
    "autor_no_lattes",
    "metodo_correspondencia",
]


def normalize_text(value: str | None) -> str:
    """Remove acentos, pontuação e diferenças de maiúsculas/minúsculas."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(
        r"(10\.\d{4,9}/[^\s\"'<>]+)",
        value,
        re.IGNORECASE,
    )
    doi = match.group(1) if match else value.strip()
    return doi.rstrip(").,;").casefold()


def load_students(path: Path = STUDENTS_PATH) -> tuple[dict[str, list[str]], dict[tuple[str, str], list[str]]]:
    """Cria índices para nomes completos e para sobrenome + iniciais."""
    by_full_name: dict[str, list[str]] = defaultdict(list)
    by_signature: dict[tuple[str, str], list[str]] = defaultdict(list)

    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            name = (row.get("Name") or "").strip()
            if not name:
                continue

            by_full_name[normalize_text(name)].append(name)

            for signature in student_signatures(name):
                by_signature[signature].append(name)

    return dict(by_full_name), dict(by_signature)


def student_signatures(name: str) -> set[tuple[str, str]]:
    """
    Gera possíveis pares (sobrenome, iniciais).

    Ex.: "Humberto Gimenes Macedo" gera:
    ("macedo", "hg"), entre outras formas.
    """
    tokens = normalize_text(name).split()
    if len(tokens) < 2:
        return set()

    signatures: set[tuple[str, str]] = set()

    # Considera de 1 a 3 últimas palavras como sobrenome.
    # Isso cobre nomes como "Massafera Souza".
    max_surname_words = min(3, len(tokens) - 1)

    for number_of_surname_words in range(1, max_surname_words + 1):
        surname = " ".join(tokens[-number_of_surname_words:])
        given_names = tokens[:-number_of_surname_words]

        if not given_names:
            continue

        initials = "".join(word[0] for word in given_names)

        # Forma completa das iniciais.
        signatures.add((surname, initials))

        # O Lattes às vezes abrevia apenas parte dos prenomes.
        if len(initials) >= 2:
            signatures.add((surname, initials[:2]))

        if initials:
            signatures.add((surname, initials[:1]))

    return signatures


def author_full_name(author: str) -> str:
    """
    Converte "SOBRENOME, NOME" para "nome sobrenome",
    permitindo comparação com a lista de alunos.
    """
    raw = re.sub(r"\s+", " ", author.strip(" ."))

    if "," in raw:
        surname, given_names = raw.split(",", 1)
        return normalize_text(f"{given_names} {surname}")

    return normalize_text(raw)


def author_signature(author: str) -> tuple[str, str] | None:
    """
    Extrai (último sobrenome, iniciais dos prenomes) de um autor abreviado.

    Ex.: "PEREIRA, J. L. J" -> ("pereira", "jlj")
    """
    raw = re.sub(r"\s+", " ", author.strip(" ."))

    if "," not in raw:
        return None

    surname, given_names = raw.split(",", 1)
    surname_tokens = normalize_text(surname).split()
    given_tokens = normalize_text(given_names).split()

    if not surname_tokens or not given_tokens:
        return None

    initials = "".join(token[0] for token in given_tokens)
    return surname_tokens[-1], initials


def match_student(
    author: str,
    by_full_name: dict[str, list[str]],
    by_signature: dict[tuple[str, str], list[str]],
) -> tuple[str | None, str | None]:
    """Retorna aluno correspondente e método usado, evitando matches ambíguos."""
    full_name = author_full_name(author)
    exact = by_full_name.get(full_name, [])

    if len(exact) == 1:
        return exact[0], "nome_completo"

    signature = author_signature(author)

    # Não usamos apenas uma inicial, pois isso produziria muitos falsos positivos.
    if signature:
        surname, initials = signature

        if len(initials) >= 2:
            candidates = by_signature.get((surname, initials), [])

            if len(candidates) == 1:
                return candidates[0], "sobrenome_iniciais"

    return None, None


def authors_from_lattes_article(article) -> list[str]:
    """
    Extrai os autores do texto bibliográfico do Lattes.

    O formato encontrado nos HTMLs é semelhante a:
    PROFESSOR 2026 AUTOR 1 ; AUTOR 2 ; AUTOR 3 . Título. Revista...
    """
    transform = article.select_one("span.transform")

    if not transform:
        return []

    text = re.sub(r"\s+", " ", transform.get_text(" ", strip=True)).strip()

    # A primeira ocorrência de ". Título." separa autores do título.
    match = re.search(r"\s\.\s+(.+?)\.\s+", text)

    if not match:
        return []

    authors_text = text[:match.start()].strip()

    # Retira o nome do pesquisador do Lattes e o ano.
    authors_text = re.sub(
        r"^.*?\b(?:19|20)\d{2}\s+",
        "",
        authors_text,
        count=1,
    )

    return [
        author.strip(" .")
        for author in authors_text.split(";")
        if author.strip(" .")
    ]


def doi_from_lattes_article(article) -> str:
    link = article.select_one("a.icone-doi[href]")

    if link:
        doi = normalize_doi(link.get("href"))
        if doi:
            return doi

    cited = article.select_one("span.citado[cvuri]")

    if cited:
        from urllib.parse import parse_qs

        query = parse_qs(cited.get("cvuri", "").split("?", 1)[-1])
        doi = normalize_doi((query.get("doi") or [""])[0])

        if doi:
            return doi

    text = article.get_text(" ", strip=True)
    match = re.search(
        r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+",
        text,
        re.IGNORECASE,
    )

    return normalize_doi(match.group(0)) if match else ""


def load_articles(path: Path = ARTICLES_PATH) -> dict[str, list[dict]]:
    """Indexa artigos existentes por DOI."""
    by_doi: dict[str, list[dict]] = defaultdict(list)

    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            doi = normalize_doi(row.get("doi"))

            if not doi:
                continue

            by_doi[doi].append(row)

    return dict(by_doi)


def collect_coauthors(
    articles_by_doi: dict[str, list[dict]],
    by_full_name: dict[str, list[str]],
    by_signature: dict[tuple[str, str], list[str]],
) -> list[dict]:
    """Cruza os artigos existentes com autores encontrados nos Lattes."""
    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for html_path in sorted(LATTES_DIR.glob("*.html")):
        soup = BeautifulSoup(
            html_path.read_text(encoding="utf-8", errors="ignore"),
            "html.parser",
        )

        professor_element = soup.select_one("h2.nome")
        professor_from_lattes = (
            professor_element.get_text(" ", strip=True)
            if professor_element
            else ""
        )

        for article in soup.select("div.artigo-completo"):
            doi = doi_from_lattes_article(article)

            if not doi or doi not in articles_by_doi:
                continue

            authors = authors_from_lattes_article(article)

            for author in authors:
                student, method = match_student(
                    author,
                    by_full_name,
                    by_signature,
                )

                if not student:
                    continue

                for article_row in articles_by_doi[doi]:
                    professor = article_row.get("professor") or professor_from_lattes
                    title = article_row.get("artigo") or ""

                    key = (doi, professor, student)

                    if key in seen:
                        continue

                    seen.add(key)

                    results.append(
                        {
                            "professor": professor,
                            "artigo": title,
                            "doi": article_row.get("doi") or doi,
                            "aluno": student,
                            "autor_no_lattes": author,
                            "metodo_correspondencia": method,
                        }
                    )

    results.sort(
        key=lambda row: (
            row["professor"].casefold(),
            row["artigo"].casefold(),
            row["aluno"].casefold(),
        )
    )

    return results


def save_results(rows: list[dict], path: Path = OUTPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=OUTPUT_FIELDS,
        )
        writer.writeheader()
        writer.writerows(rows)

    return path


def run() -> None:
    if not ARTICLES_PATH.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {ARTICLES_PATH}")

    if not STUDENTS_PATH.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {STUDENTS_PATH}")

    if not LATTES_DIR.exists():
        raise FileNotFoundError(f"Pasta não encontrada: {LATTES_DIR}")

    by_full_name, by_signature = load_students()
    articles_by_doi = load_articles()

    results = collect_coauthors(
        articles_by_doi,
        by_full_name,
        by_signature,
    )

    output = save_results(results)

    print(f"Artigos analisados: {len(articles_by_doi)} DOIs")
    print(f"Artigos com pelo menos um aluno: {len(set(row['doi'] for row in results))}")
    print(f"Relações professor-aluno encontradas: {len(results)}")
    print(f"Resultado salvo em: {output}")


if __name__ == "__main__":
    run()
