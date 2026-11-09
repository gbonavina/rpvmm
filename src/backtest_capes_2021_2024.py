"""Conceitos (MB/B/R/F/I) que o Programa receberia em 2021-2024 nos itens de produção.

Aplica as regras e faixas efetivamente usadas pela Engenharias III no ciclo
2021-2024 (Relatório de Avaliação CAPES, publicado em 27/05/2026), que são
diferentes da ficha 2025-2028: estratos Qualis 2021-2024, amostra dos 50%
maiores estratos e teto de FWCI 1,5.

Cada item mistura subitens calculáveis com os dados do projeto e subitens sem
dados (bancas, justificativas, tecnologias, projetos, índice h). Para estes, o
script mostra o intervalo: pior caso (nota mínima) e melhor caso (nota máxima),
e o conceito correspondente a cada extremo.

Uso: uv run python src/backtest_capes_2021_2024.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src.otimizacao_capes import (  # noqa: E402
    PASTA, associar_candidatos, carregar_artigos, carregar_docentes, carregar_percentis,
    carregar_titulados, primeiro_existente, similaridades,
)
from src.similaridade_teses import chave_nome  # noqa: E402

PESO_QUALIS = {"A1": 1.0, "A2": 0.875, "A3": 0.75, "A4": 0.625, "B1": 0.5, "B2": 0.375, "B3": 0.25, "B4": 0.125}
FWCI_TETO = 1.5
SIM_MIN = 0.4

# Faixas do relatório 2021-2024 (limite inferior de MB, B, R, F).
FAIXAS = {
    "2.1": (8.00, 7.00, 5.50, 4.00),  # escala 0-10
    "2.2": (0.85, 0.75, 0.45, 0.20),
    "2.4": (0.85, 0.75, 0.40, 0.20),
}
CONCEITOS = ("Muito Bom", "Bom", "Regular", "Fraco", "Insuficiente")


def conceito(item: str, valor: float) -> str:
    return next((c for c, corte in zip(CONCEITOS, FAIXAS[item]) if valor >= corte), CONCEITOS[-1])


def grau_norm(grau: int) -> float:
    return (grau - 1) / 4


def main() -> None:
    alunos_artigos = pd.read_csv(primeiro_existente(
        PASTA / "alunos_artigos_2021_2024_final.csv", PASTA / "alunos_artigos_2021_2024_ampliado.csv",
        PASTA / "alunos_artigos_scival_2021_2024_completo.csv",
        PASTA / "alunos_artigos_scival_2021_2024_com_fwci.csv"),
        encoding="utf-8-sig")
    artigos_docentes = pd.read_csv(primeiro_existente(
        PASTA / "artigos_docentes_2021_2024_enriquecido.csv", PASTA / "artigos_docentes_2021_2024.csv"), encoding="utf-8-sig")
    artigos = carregar_artigos(alunos_artigos, artigos_docentes, carregar_percentis(RAIZ / "percentis_periodicos.csv"))
    qualis = {str(d).lower(): str(q).strip() for d, q in zip(alunos_artigos["doi"], alunos_artigos.get("qualis", [])) if isinstance(q, str)}

    def wq(doi: str) -> float:
        return PESO_QUALIS.get(qualis.get(doi, ""), 0.0)

    titulados = carregar_titulados(RAIZ / "ALUNOS-Completo.csv", 2021, 2024)
    associar_candidatos(titulados, alunos_artigos, similaridades(PASTA / "thesis_articles_similarity_2021_2024.csv"))

    # Para 2.2.1 basta coautoria (a regra antiga não exige primeiro autor).
    coautorias: dict[str, set[str]] = {}
    for aluno, doi in zip(alunos_artigos["aluno"], alunos_artigos["doi"]):
        coautorias.setdefault(chave_nome(aluno), set()).add(str(doi).lower())
    sims = similaridades(PASTA / "thesis_articles_similarity_2021_2024.csv")

    def associados(t, ini, fim, so_primeiro):
        dois = [d for d, _ in t.candidatos] if so_primeiro else coautorias.get(chave_nome(t.nome), set())
        saida = []
        for d in dois:
            a = artigos.get(d)
            s = max((v for (au, dd, _), v in sims.items() if au == chave_nome(t.nome) and dd == d), default=None)
            if a and a.ano and ini <= a.ano <= fim and s is not None and s >= SIM_MIN:
                saida.append(a)
        return saida

    # ---- Item 2.1: subitem 2.1.3 (indicação de 7 teses + 3 dissertações) ----
    def nota_213(a) -> float:
        return 0.5 * wq(a.doi) + 0.5 * min(a.fwci or 0, FWCI_TETO) / FWCI_TETO

    melhores = {}
    for t in titulados:
        opcoes = associados(t, 2019, 2025, so_primeiro=True)
        if opcoes:
            melhores[t.nome] = (t, max(opcoes, key=nota_213))
    indicados = []
    for nivel, limite in (("tese", 7), ("dissertacao", 3)):
        do_nivel = sorted((p for p in melhores.values() if p[0].nivel == nivel), key=lambda p: nota_213(p[1]), reverse=True)
        indicados += do_nivel[:limite]
    n = 10
    s213a = 10 / n * sum(wq(a.doi) if wq(a.doi) else 0.10 for _, a in indicados)
    s213b = 10 / (FWCI_TETO * n) * sum(min(a.fwci or 0, FWCI_TETO) for _, a in indicados)
    s213 = 0.5 * s213a + 0.5 * s213b
    ind21 = (0.7 * s213, 0.7 * s213 + 0.2 * 10 + 0.1 * 10)  # 2.1.1 e 2.1.2 de 0 a 10

    # ---- Item 2.2: 2.2.1 (doutores) e 2.2.2 (mestres) ----
    doutores = [t for t in titulados if t.nivel == "tese"]
    lista_221 = sorted((max((wq(a.doi) for a in associados(t, 2021, 2024, False)), default=0.0) for t in doutores), reverse=True)
    amostra = lista_221[: math.ceil(0.5 * len(lista_221))]
    i221 = sum(amostra) / max(10, sum(1 for v in amostra if v > 0))
    mestres = [t for t in titulados if t.nivel == "dissertacao"]
    i222 = sum(any(wq(a.doi) > 0 for a in associados(t, 2021, 2024, False)) for t in mestres) / len(mestres)
    base22 = 0.6 * i221 + 0.15 * i222
    ind22 = (base22 + 0.25 * grau_norm(1), base22 + 0.25 * grau_norm(5))

    # ---- Item 2.4: 2.4.2 (até 4 artigos por docente com discente) ----
    docentes = carregar_docentes(None, sorted(artigos_docentes["professor"].unique()), 2021, 2024)
    elegiveis = sorted(
        (a for a in artigos.values() if a.ano and 2021 <= a.ano <= 2024 and wq(a.doi) > 0 and a.docentes & docentes.keys()),
        key=lambda a: wq(a.doi), reverse=True)
    usados, vagas, lista_242 = set(), {d: len(v) for d, v in docentes.items()}, []
    for a in elegiveis:  # atribuição gulosa = ótima aqui (vagas sobrando, ver otimizacao_capes)
        d = next((d for d in sorted(a.docentes & docentes.keys()) if vagas[d] > 0), None)
        if d and a.doi not in usados:
            usados.add(a.doi)
            vagas[d] -= 1
            lista_242.append(wq(a.doi))
    entradas = sum(len(v) for v in docentes.values())
    npubl = entradas / 2
    i242 = sum(sorted(lista_242, reverse=True)[: int(npubl)]) / npubl
    ind24 = (0.4 * i242, 0.4 * i242 + 0.20 + 0.25 + 0.15)  # 2.4.1, 2.4.3 (h2), 2.4.4 de grau 1 a 5

    print("Backtest 2021-2024 com as regras e faixas do Relatório CAPES (Engenharias III)\n")
    print(f"2.1.3: {len(indicados)} trabalhos indicados | Qualis (a) = {s213a:.2f}/10, FWCI (b) = {s213b:.2f}/10, subitem = {s213:.2f}/10")
    print(f"2.2.1: {len(doutores)} doutores, índice = {i221:.3f} | 2.2.2: {len(mestres)} mestres, fração com publicação = {i222:.3f}")
    print(f"2.4.2: {len(lista_242)} artigos com discentes na lista de {entradas} entradas, índice = {i242:.3f}\n")
    linhas = [
        ("2.1 Teses e dissertações", "2.1", ind21, "2.1.1 e 2.1.2 (justificativas, bancas)"),
        ("2.2 Produção de discentes/egressos", "2.2", ind22, "2.2.3 (tecnologias)"),
        ("2.4 Produção do corpo docente", "2.4", ind24, "2.4.1 (projetos), 2.4.3 (h2), 2.4.4 (tecnologias)"),
    ]
    tabela = pd.DataFrame([{
        "item": nome, "pior caso": round(lo, 3), "conceito pior": conceito(item, lo),
        "melhor caso": round(hi, 3), "conceito melhor": conceito(item, hi), "subitens sem dados": falta,
    } for nome, item, (lo, hi), falta in linhas])
    print(tabela.to_string(index=False))
    saida = PASTA / "otimizacao" / "conceitos_2021_2024.csv"
    saida.parent.mkdir(parents=True, exist_ok=True)
    tabela.to_csv(saida, index=False)
    print(f"\nGravado em {saida}")


if __name__ == "__main__":
    main()
