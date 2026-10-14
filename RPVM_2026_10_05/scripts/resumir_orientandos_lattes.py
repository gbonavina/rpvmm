#!/usr/bin/env python3
"""Consolida as orientações Lattes em uma linha por aluno do cadastro."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


COLUMNS = [
    "aluno", "quantidade_orientacoes_lattes", "orientadores_lattes",
    "lattes_ids_docentes", "lattes_id_orientando", "status_orientacao",
    "tipos_orientacao", "arquivos_lattes_origem",
]
EXCLUDED_COLUMNS = [
    "aluno", "orientando_lattes", "docente", "tipo_orientacao",
    "status_orientacao", "arquivo_lattes_origem", "motivo_exclusao",
]


def values(records: list[dict[str, str]], column: str) -> str:
    return " | ".join(sorted({str(record.get(column) or "").strip() for record in records if str(record.get(column) or "").strip()}))


def is_postdoc(row: dict[str, str]) -> bool:
    content = " ".join(str(row.get(column) or "") for column in ("tipo_orientacao", "texto_lattes", "titulo"))
    normalized = content.casefold().replace("-", " ")
    return "pós dout" in normalized or "pos dout" in normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera uma lista única de orientandos a partir dos Lattes dos docentes.")
    parser.add_argument("--orientations", required=True, type=Path, help="CSV produzido por extrair_orientacoes_lattes.py.")
    parser.add_argument("--output", required=True, type=Path, help="CSV consolidado, uma linha por aluno.")
    parser.add_argument("--excluded-postdocs-output", type=Path, help="Auditoria de orientações de pós-doutorado excluídas.")
    parser.add_argument("--postdocs", type=Path, help="CSV produzido por extrair_orientacoes_lattes.py para excluir pessoas supervisionadas em pós-doutorado.")
    args = parser.parse_args()
    try:
        with args.orientations.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    postdoc_students = set()
    if args.postdocs:
        try:
            with args.postdocs.open(encoding="utf-8-sig", newline="") as handle:
                postdoc_students = {str(row.get("aluno_csv") or "").strip() for row in csv.DictReader(handle) if str(row.get("aluno_csv") or "").strip()}
        except OSError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 2
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    excluded = []
    for row in rows:
        student = str(row.get("aluno_csv") or "").strip()
        if not student:
            continue
        if student in postdoc_students or is_postdoc(row):
            excluded.append({
                "aluno": student, "orientando_lattes": str(row.get("orientando") or "").strip(),
                "docente": str(row.get("docente") or "").strip(), "tipo_orientacao": str(row.get("tipo_orientacao") or "").strip(),
                "status_orientacao": str(row.get("status_orientacao") or "").strip(),
                "arquivo_lattes_origem": str(row.get("arquivo_lattes_docente") or "").strip(),
                "motivo_exclusao": "Supervisão de pós-doutorado.",
            })
            continue
        groups[student].append(row)
    output = []
    for student, records in sorted(groups.items(), key=lambda item: item[0].casefold()):
        output.append({
            "aluno": student,
            "quantidade_orientacoes_lattes": str(len(records)),
            "orientadores_lattes": values(records, "docente"),
            "lattes_ids_docentes": values(records, "lattes_id_docente"),
            "lattes_id_orientando": values(records, "lattes_id_orientando"),
            "status_orientacao": values(records, "status_orientacao"),
            "tipos_orientacao": values(records, "tipo_orientacao"),
            "arquivos_lattes_origem": values(records, "arquivo_lattes_docente"),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(output)
    if args.excluded_postdocs_output:
        with args.excluded_postdocs_output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXCLUDED_COLUMNS)
            writer.writeheader()
            writer.writerows(excluded)
    print(f"Concluído: {len(output)} alunos distintos consolidados; {len(excluded)} orientação(ões) de pós-doutorado excluída(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
