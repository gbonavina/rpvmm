#!/usr/bin/env python3
"""Extrai orientações dos HTMLs Lattes já baixados, sem acessar o Lattes web."""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import unicodedata
from pathlib import Path

LATTES_ID_RE = re.compile(r"lattes\.cnpq\.br/(\d+)")
YEAR_RE = re.compile(r"(?:Início|Inicio)\s*:\s*(\d{4})", re.I)
NAME_RE = re.compile(r"^\s*([^\.]{3,120}?)\s*\.\s*")
COLUMNS = [
    "docente", "lattes_id_docente", "arquivo_lattes_docente", "secao_lattes",
    "status_orientacao", "tipo_orientacao", "orientando", "lattes_id_orientando",
    "titulo", "ano_inicio", "instituicao", "papel_docente", "texto_lattes",
    "aluno_csv", "tipo_correspondencia_aluno",
]
POSTDOC_COLUMNS = [
    "docente", "lattes_id_docente", "arquivo_lattes_docente", "orientando",
    "aluno_csv", "lattes_id_orientando", "texto_lattes", "motivo_exclusao",
]


def clean(value: str) -> str:
    return " ".join(value.replace("_", " ").split())


def key(value: str) -> str:
    value = unicodedata.normalize("NFKD", clean(value).casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def lattes_id(href: str | None) -> str:
    found = LATTES_ID_RE.search(href or "")
    return found.group(1) if found else ""


def plain_html(value: str) -> str:
    value = re.sub(r"<br[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return clean(html.unescape(value))


def load_professors(path: Path) -> dict[str, list[str]]:
    by_id: dict[str, list[str]] = {}
    for item in json.loads(path.read_text(encoding="utf-8")):
        lid = str(item.get("lattes_id") or "")
        name = str(item.get("name") or "").strip()
        if lid and name:
            by_id.setdefault(lid, []).append(name)
    return by_id


def load_students(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = {}
        for row in reader:
            name = (row.get("Name") or row.get("Nome") or "").strip()
            if name:
                names.setdefault(key(name), name)
        return names


def orientation_rows(html_path: Path, professor_id: str, professor: str, students: dict[str, str]):
    source = html_path.read_text(encoding="utf-8", errors="ignore")
    start_match = re.search(r'<a\s+name="Orientacoes"', source, re.I)
    if not start_match:
        return []
    section = source[start_match.start():]
    next_section = re.search(r'<div class="title-wrapper">\s*<a name="(?!Orientacoes)', section, re.I)
    if next_section:
        section = section[:next_section.start()]
    rows = []
    for item in re.finditer(r'<span class="transform">(.*?)</span>', section, re.I | re.S):
        fragment = item.group(1)
        raw = plain_html(fragment)
        if not raw or not ("Orientador" in raw or "orientador" in raw):
            continue
        match = NAME_RE.match(raw)
        if not match:
            continue
        advisee = clean(match.group(1))
        if len(advisee.split()) < 2:
            continue
        prefix = section[:item.start()]
        status_text = plain_html(prefix[max(prefix.rfind("Orientações e supervisões em andamento"), prefix.rfind("Orientações e supervisões concluídas")):])
        status = "em andamento" if "andamento" in status_text.casefold() else "concluída"
        types = re.findall(r'<div class="cita-artigos">\s*<b>(.*?)</b>', prefix, re.I | re.S)
        degree = plain_html(types[-1]) if types else ""
        link = re.search(r'<a[^>]+class="icone-lattes"[^>]+href="([^"]+)"', fragment, re.I)
        role = "coorientador" if re.search(r"coorientador", raw, re.I) else "orientador"
        year = YEAR_RE.search(raw)
        title = clean(raw[len(match.group(0)):].split("Início:", 1)[0].split("Inicio:", 1)[0])
        institution = ""
        if " - " in raw:
            institution = clean(raw.split(" - ", 1)[1].split(",", 1)[0])
        matched = students.get(key(advisee), "")
        rows.append({
            "docente": professor, "lattes_id_docente": professor_id,
            "arquivo_lattes_docente": html_path.name, "secao_lattes": "Orientações",
            "status_orientacao": status, "tipo_orientacao": degree,
            "orientando": advisee, "lattes_id_orientando": lattes_id(link.group(1) if link else None),
            "titulo": title, "ano_inicio": year.group(1) if year else "",
            "instituicao": institution, "papel_docente": role, "texto_lattes": raw,
            "aluno_csv": matched, "tipo_correspondencia_aluno": "nome normalizado exato" if matched else "sem correspondência",
        })
    return rows


def postdoc_rows(html_path: Path, professor_id: str, professor: str, students: dict[str, str]):
    """Lê a subseção Supervisão de pós-doutorado, que não traz '(Orientador)'."""
    source = html_path.read_text(encoding="utf-8", errors="ignore")
    start_match = re.search(r'<a\s+name="Orientacoes"', source, re.I)
    if not start_match:
        return []
    section = source[start_match.start():]
    next_section = re.search(r'<div class="title-wrapper">\s*<a name="(?!Orientacoes)', section, re.I)
    if next_section:
        section = section[:next_section.start()]
    rows = []
    for item in re.finditer(r'<span class="transform">(.*?)</span>', section, re.I | re.S):
        prefix = section[:item.start()]
        headings = re.findall(r'<div class="cita-artigos">\s*<b>(.*?)</b>', prefix, re.I | re.S)
        heading = plain_html(headings[-1]) if headings else ""
        if not re.search(r"p[óo]s[-\s]*dout", heading, re.I):
            continue
        raw = plain_html(item.group(1))
        match = NAME_RE.match(raw)
        if not match:
            continue
        advisee = clean(match.group(1))
        if len(advisee.split()) < 2:
            continue
        link = re.search(r'<a[^>]+class="icone-lattes"[^>]+href="([^"]+)"', item.group(1), re.I)
        rows.append({
            "docente": professor, "lattes_id_docente": professor_id,
            "arquivo_lattes_docente": html_path.name, "orientando": advisee,
            "aluno_csv": students.get(key(advisee), ""),
            "lattes_id_orientando": lattes_id(link.group(1) if link else None),
            "texto_lattes": raw, "motivo_exclusao": "Supervisão de pós-doutorado.",
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrai orientações de HTMLs Lattes locais.")
    parser.add_argument("--lattes-dir", required=True, type=Path)
    parser.add_argument("--professors", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--students", type=Path, help="CSV institucional para marcar orientandos presentes.")
    parser.add_argument("--unmapped-output", type=Path, help="Relatório de HTMLs sem professor no JSON.")
    parser.add_argument("--postdocs-output", type=Path, help="Relatório de supervisões de pós-doutorado excluídas.")
    args = parser.parse_args()
    by_id = load_professors(args.professors)
    students = load_students(args.students)
    rows, unmapped, postdocs = [], [], []
    for html_path in sorted(args.lattes_dir.glob("*.html")):
        professor_id = html_path.stem
        names = by_id.get(professor_id, [])
        if not names:
            unmapped.append({"arquivo_lattes": html_path.name, "lattes_id_docente": professor_id})
            continue
        for professor in names:
            rows.extend(orientation_rows(html_path, professor_id, professor, students))
            postdocs.extend(postdoc_rows(html_path, professor_id, professor, students))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader(); writer.writerows(rows)
    if args.unmapped_output:
        with args.unmapped_output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["arquivo_lattes", "lattes_id_docente"])
            writer.writeheader(); writer.writerows(unmapped)
    if args.postdocs_output:
        with args.postdocs_output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=POSTDOC_COLUMNS)
            writer.writeheader(); writer.writerows(postdocs)
    print(f"Orientações extraídas: {len(rows)}; pós-doutorados excluídos: {len(postdocs)}; HTMLs sem docente mapeado: {len(unmapped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
