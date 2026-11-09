"""Identifica discentes e egressos do Programa entre os autores Scopus dos artigos dos docentes.

O pipeline anterior (`resultados_2021_2024/extrair_alunos_scival.py`) só
reconhecia o aluno quando ele era orientando, segundo o Lattes, do mesmo
docente do artigo. A Eng3-Ficha.pdf (subitem 2.4.1) pede coautoria "com
discentes ou egressos do Programa", sem exigir o orientador, e os subitens
2.1.3 e 2.3 dependem de o titulado ser o primeiro autor. Este script:

- usa o cadastro completo (ALUNOS-Completo.csv): mestrado, doutorado e
  doutorado direto, com situação Ativo ou Egresso. Pós-doutorado não é
  discente; desligados ficam fora porque o cadastro não informa até quando
  foram discentes;
- só aceita o artigo se ele foi publicado no ano de entrada do aluno ou depois;
- compara nomes de forma estrita: primeiro nome e último sobrenome iguais, e
  cada outra parte do nome Scopus precisa estar no nome do aluno (ou ser a
  inicial de uma parte). "Carlos Henrique Gomes Ferreira" não casa com
  "Carlos Eduardo de Morais Ferreira"; "Rafael Françozo" casa com
  "Rafael Verão Françozo".

Os metadados (FWCI, fonte, Qualis, percentil, resumo) vêm do arquivo de
artigos de docentes já enriquecido por `src/enriquecer_scopus.py`.

Uso (na raiz do projeto, com SCOPUS_API_KEY/ELSEVIER_API_KEY):
    uv run python src/coautores_discentes.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from resultados_2021_2024.extrair_alunos_scival import ScopusError, authors_for_doi  # noqa: E402
from src.similaridade_teses import tokens_nome  # noqa: E402

PASTA = RAIZ / "resultados_2021_2024"
NIVEIS = {"Mestrado", "Doutorado", "Doutorado Direto"}
SITUACOES = {"Ativo", "Egresso"}
COLUNAS = [
    "aluno", "docente", "doi", "eid", "scopus_id", "artigo", "resumo_scopus", "source", "issn", "qualis",
    "percentil", "citations", "fwci", "ano_publicacao_scopus", "scopus_author_id_aluno", "nome_autor_scopus",
    "posicao_autor", "primeiro_autor", "tipo_correspondencia", "situacao_aluno", "nivel_aluno",
    "ano_entrada", "autores_do_artigo",
]


def nomes_compativeis(aluno: str, autor: str) -> str:
    """'' se incompatível; senão 'nome completo' ou 'nome abreviado'."""
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


def carregar_alunos(caminho: Path) -> pd.DataFrame:
    alunos = pd.read_csv(caminho)
    alunos = alunos[alunos["Nível"].isin(NIVEIS) & alunos["Situação"].isin(SITUACOES)].copy()
    alunos["nome"] = alunos["Name"].str.split(",").str[0].str.strip()
    alunos["ano_entrada"] = alunos["Entrada"].astype(str).str.extract(r"(\d{4})")[0].astype(float)
    # Uma linha por pessoa: a entrada mais antiga em curso de pós-graduação.
    alunos = alunos.sort_values("ano_entrada").groupby("nome", as_index=False).agg(
        situacao_aluno=("Situação", "first"), nivel_aluno=("Nível", lambda v: " | ".join(sorted(set(v)))),
        ano_entrada=("ano_entrada", "min"),
    )
    return alunos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artigos", type=Path, default=PASTA / "artigos_docentes_2021_2024_enriquecido.csv")
    parser.add_argument("--alunos", type=Path, default=RAIZ / "ALUNOS-Completo.csv")
    parser.add_argument("--saida", type=Path, default=PASTA / "alunos_artigos_2021_2024_ampliado.csv")
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()

    load_dotenv(RAIZ / ".env")
    chave = os.getenv("SCOPUS_API_KEY") or os.getenv("ELSEVIER_API_KEY") or ""
    token = os.getenv("SCOPUS_INSTTOKEN", "")
    if not chave:
        parser.error("Defina SCOPUS_API_KEY ou ELSEVIER_API_KEY.")

    artigos = pd.read_csv(args.artigos, encoding="utf-8-sig", dtype=str)
    artigos = artigos[artigos["doi"].notna()].assign(doi=lambda d: d["doi"].str.strip().str.lower())
    alunos = carregar_alunos(args.alunos)

    linhas, sem_autores = [], []
    dois = sorted(artigos["doi"].unique())
    for indice, doi in enumerate(dois, start=1):
        try:
            _, autores = authors_for_doi(doi, chave, token)
        except ScopusError as exc:
            sem_autores.append((doi, str(exc)[:60]))
            continue
        time.sleep(max(args.delay, 0))
        meta = artigos[artigos["doi"].eq(doi)].iloc[0]
        ano = pd.to_numeric(meta.get("ano_publicacao_scopus"), errors="coerce")
        lista_autores = " | ".join(a["name"] for a in autores if a["name"])
        for autor in autores:
            for aluno in alunos.itertuples():
                tipo = nomes_compativeis(aluno.nome, autor["name"])
                if not tipo or (pd.notna(ano) and pd.notna(aluno.ano_entrada) and ano < aluno.ano_entrada):
                    continue
                for docente in artigos.loc[artigos["doi"].eq(doi), "professor"].unique():
                    linhas.append({
                        **{c: meta.get(c, "") for c in COLUNAS if c in meta.index},
                        "aluno": aluno.nome, "docente": docente, "doi": doi,
                        "scopus_author_id_aluno": autor["id"], "nome_autor_scopus": autor["name"],
                        "posicao_autor": autor["position"], "primeiro_autor": "1" if autor["position"] == "1" else "0",
                        "tipo_correspondencia": tipo, "situacao_aluno": aluno.situacao_aluno,
                        "nivel_aluno": aluno.nivel_aluno, "ano_entrada": int(aluno.ano_entrada) if pd.notna(aluno.ano_entrada) else "",
                        "autores_do_artigo": lista_autores,
                    })
        print(f"[{indice}/{len(dois)}] {doi}", flush=True)

    saida = pd.DataFrame(linhas, columns=COLUNAS).drop_duplicates(["aluno", "docente", "doi"])
    saida.to_csv(args.saida, index=False, encoding="utf-8-sig")
    print(f"\n{saida['doi'].nunique()} artigos com {saida['aluno'].nunique()} discentes/egressos; "
          f"{len(sem_autores)} DOIs sem retorno da Scopus. Gravado em {args.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
