"""Similaridade de cossenos entre teses/dissertações e artigos dos seus autores.

Para cada artigo de aluno, procura o autor (aluno) na tabela completa de teses
(`data/materiais.csv`, todos os anos) em vez de usar um recorte por ano. Só
entram trabalhos de pós-graduação (dissertações e teses); TG, trabalhos de
curso e livros são ignorados.

O texto do artigo é o título seguido do resumo (`resumo_scopus`) quando ele
existe; a coluna `usa_resumo` indica cada caso. Também é gravada a
similaridade só com o título (`cosine_similarity_titulo`) para comparação.

Uso (na raiz do projeto):
    uv run python src/similaridade_teses.py \
        --artigos resultados_2021_2024/alunos_artigos_scival_2021_2024_com_fwci.csv \
        --saida resultados_2021_2024/thesis_articles_similarity_2021_2024.csv
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
MATERIAIS = RAIZ / "data" / "materiais.csv"
MODELO = "sentence-transformers/all-MiniLM-L6-v2"

CONECTORES = {"da", "das", "de", "do", "dos", "e"}
# Sufixos de patente/posto que aparecem em alguns cadastros ("Thiago ..., Cap").
SUFIXOS = {"cap", "eb", "ten", "maj", "cel"}
COLUNAS_SAIDA = [
    "autor", "material", "titulo", "ano", "artigo", "doi", "ano_publicacao_scopus",
    "primeiro_autor", "usa_resumo", "cosine_similarity", "cosine_similarity_titulo",
]


def chave_nome(valor: object) -> str:
    """Nome sem acentos, caixa ou espaços extras; ignora o que vem após vírgula."""
    texto = str(valor or "").split(",")[0]
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.casefold().split())


def tokens_nome(valor: object) -> list[str]:
    palavras = re.findall(r"[a-z]+", chave_nome(valor))
    return [p for p in palavras if p not in CONECTORES and p not in SUFIXOS]


def primeiro_ultimo(valor: object) -> tuple[str, str] | None:
    partes = tokens_nome(valor)
    return (partes[0], partes[-1]) if len(partes) >= 2 else None


def normalizar_material(valor: object) -> str:
    # O Sophia destaca termos da busca e o scraper antigo inseria espaços
    # dentro das palavras ("aca dê mico"); remover espaços resolve a comparação.
    return re.sub(r"\s+", "", chave_nome(valor))


def eh_pos_graduacao(material: object) -> bool:
    chave = normalizar_material(material)
    return chave.startswith("dissertacao") or chave.startswith("tese")


def nivel_do_material(material: object) -> str:
    return "tese" if normalizar_material(material).startswith("tese") else "dissertacao"


def carregar_teses(caminho: Path = MATERIAIS) -> pd.DataFrame:
    teses = pd.read_csv(caminho)
    teses = teses[teses["material"].map(eh_pos_graduacao)].copy()
    teses["author_key"] = teses["autor"].map(chave_nome)
    teses["nivel"] = teses["material"].map(nivel_do_material)
    return (
        teses.loc[:, ["autor", "author_key", "material", "nivel", "titulo", "ano"]]
        .drop_duplicates(subset=["author_key", "titulo"])
        .reset_index(drop=True)
    )


def mapear_autores(nomes: pd.Series, teses: pd.DataFrame) -> pd.Series:
    """Associa cada nome de aluno à chave de autor da tabela de teses.

    Primeiro tenta o nome completo normalizado; se não houver, aceita o mesmo
    primeiro nome e sobrenome final, desde que apenas um autor seja compatível.
    """
    chaves = set(teses["author_key"])
    por_extremos: dict[tuple[str, str], set[str]] = {}
    for chave in chaves:
        extremos = primeiro_ultimo(chave)
        if extremos:
            por_extremos.setdefault(extremos, set()).add(chave)

    def mapear(nome: object) -> str:
        chave = chave_nome(nome)
        if chave in chaves:
            return chave
        candidatos = por_extremos.get(primeiro_ultimo(nome) or ("", ""), set())
        return next(iter(candidatos)) if len(candidatos) == 1 else ""

    return nomes.map(mapear)


def texto_artigo(titulo: object, resumo: object) -> str:
    titulo = str(titulo or "").strip()
    resumo = "" if pd.isna(resumo) else str(resumo).strip()
    return f"{titulo}. {resumo}" if resumo else titulo


def carregar_artigos(caminho: Path, teses: pd.DataFrame) -> pd.DataFrame:
    artigos = pd.read_csv(caminho, encoding="utf-8-sig")
    if "resumo_scopus" not in artigos.columns:
        artigos["resumo_scopus"] = ""
    artigos["resumo_scopus"] = artigos["resumo_scopus"].fillna("").astype(str)
    artigos["author_key"] = mapear_autores(artigos["aluno"], teses)
    artigos["primeiro_autor"] = artigos["primeiro_autor"].astype(str).eq("1")
    artigos["usa_resumo"] = artigos["resumo_scopus"].str.strip().ne("")
    artigos["texto"] = [
        texto_artigo(t, r) for t, r in zip(artigos["artigo"], artigos["resumo_scopus"])
    ]
    return (
        artigos.loc[:, [
            "aluno", "author_key", "artigo", "doi", "ano_publicacao_scopus",
            "primeiro_autor", "usa_resumo", "texto",
        ]]
        .drop_duplicates(subset=["aluno", "doi"])
        .reset_index(drop=True)
    )


def calcular_similaridade(artigos: pd.DataFrame, teses: pd.DataFrame, modelo=None) -> pd.DataFrame:
    if modelo is None:
        from sentence_transformers import SentenceTransformer

        modelo = SentenceTransformer(MODELO)

    teses = teses.assign(thesis_idx=np.arange(len(teses)))
    artigos = artigos.assign(article_idx=np.arange(len(artigos)))
    pares = teses.merge(artigos[artigos["author_key"].ne("")], on="author_key", how="inner")
    if pares.empty:
        return pd.DataFrame(columns=COLUNAS_SAIDA)

    # Embeddings normalizados: a similaridade de cossenos é o produto interno.
    def codificar(textos: list[str]) -> np.ndarray:
        return modelo.encode(textos, normalize_embeddings=True)

    v_tese = codificar(teses["titulo"].tolist())
    v_artigo = codificar(artigos["texto"].tolist())
    v_titulo = codificar(artigos["artigo"].astype(str).tolist())

    idx_t = pares["thesis_idx"].to_numpy()
    idx_a = pares["article_idx"].to_numpy()
    pares["cosine_similarity"] = np.sum(v_tese[idx_t] * v_artigo[idx_a], axis=1)
    pares["cosine_similarity_titulo"] = np.sum(v_tese[idx_t] * v_titulo[idx_a], axis=1)

    return (
        pares.loc[:, COLUNAS_SAIDA]
        .sort_values(["autor", "cosine_similarity"], ascending=[True, False])
        .reset_index(drop=True)
    )


def autores_sem_tese(artigos: pd.DataFrame) -> list[str]:
    return sorted(artigos.loc[artigos["author_key"].eq(""), "aluno"].unique())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artigos", type=Path, required=True, help="CSV aluno × artigo (alunos_artigos_scival*).")
    parser.add_argument("--saida", type=Path, required=True, help="CSV de saída com as similaridades.")
    parser.add_argument("--teses", type=Path, default=MATERIAIS, help="Tabela de teses (padrão: data/materiais.csv).")
    args = parser.parse_args()

    teses = carregar_teses(args.teses)
    artigos = carregar_artigos(args.artigos, teses)
    similaridade = calcular_similaridade(artigos, teses)
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    similaridade.to_csv(args.saida, index=False)

    com_resumo = int(artigos["usa_resumo"].sum())
    print(f"{len(similaridade)} pares tese × artigo gravados em {args.saida}")
    print(f"Artigos com resumo: {com_resumo} de {len(artigos)}")
    faltantes = autores_sem_tese(artigos)
    if faltantes:
        print(f"Alunos sem tese/dissertação em {args.teses.name}: {', '.join(faltantes)}")


if __name__ == "__main__":
    main()
