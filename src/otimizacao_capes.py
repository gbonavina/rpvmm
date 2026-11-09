"""Modelo de programação inteira mista para maximizar a nota CAPES (Engenharias III).

Implementa as partes da Ficha de Avaliação 2025-2028 (Eng3-Ficha.pdf) que
dependem de escolhas do Programa e podem ser calculadas com os dados do
projeto, e as aplica ao quadriênio 2021-2024 (backtracking):

Decisões otimizadas
  2.4.1 Publicações do corpo docente (IBAD/IBPD): atribuir artigos com
        coautoria discente a docentes. Cada docente tem uma vaga por ano como
        permanente (até 4), um artigo só pode ser contabilizado por um docente,
        e apenas os 60% melhores da lista (ordem F1..F8, desempate P1..P8)
        pontuam.
  2.1.3 Produção associada a teses e dissertações (IBV/IBA): escolher até 7
        teses e 3 dissertações, cada uma com um artigo em que o titulado é o
        primeiro autor e cuja associação à tese é demonstrada (similaridade de
        cossenos tese × artigo >= --sim-min).
  2.1.1 (índice D): penaliza repetição de orientador nas indicações acima.

Indicadores calculados (sem decisão do Programa)
  2.3.1 Publicações associadas às teses (IBAT/IBPT) e
  2.3.2 Publicações associadas às dissertações.

A função objetivo é a soma das contribuições ao quesito 2 (pesos da ficha:
2.1 = 25%, 2.1.3 = 70%, 2.1.1 = 20% com D = 25%; 2.4 = 30%, 2.4.1 = 50%).
A nota final CAPES depende da estratificação a posteriori entre programas e de
itens qualitativos, então o valor é uma medida relativa, não a nota.

Uso (na raiz do projeto):
    uv run python src/otimizacao_capes.py
    uv run python src/otimizacao_capes.py --sim-min 0.5 --fwci-max 2 --um-por-ano
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pulp

import sys

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from src.similaridade_teses import chave_nome, nivel_do_material, primeiro_ultimo  # noqa: E402

PASTA = RAIZ / "resultados_2021_2024"

# Pesos das faixas F1..F8 (FWCI) e P1..P8 (percentil CiteScore/JIF), item 2.1.3.
PESO_FAIXA = {1: 1.0, 2: 0.875, 3: 0.75, 4: 0.625, 5: 0.5, 6: 0.375, 7: 0.25, 8: 0.125}
CORTES_FWCI = [1.0, 0.875, 0.75, 0.625, 0.5, 0.375, 0.25, 0.0]
CORTES_PERCENTIL = [95, 90, 85, 75, 62.5, 50, 37.5]
PESO_SEM_INDICADOR = 0.05  # N_sFWCI / N_sCS nos itens 2.3.1

# Peso de cada componente dentro do quesito 2.
PESO_213 = 0.25 * 0.70
PESO_D = 0.25 * 0.20 * 0.25
PESO_241 = 0.30 * 0.50
PESO_231 = 0.25 * 0.50
PESO_232 = 0.25 * 0.15
DESEMPATE = 1e-6

# Backtest: A1..B4 do Qualis 2021-2024 no lugar de P1..P8 (C/NP = entrada nula).
FAIXA_QUALIS = {q: i for i, q in enumerate(["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"], start=1)}
VEICULO = "percentil"

NIVEL_ALUNO = {"mestrado": "dissertacao", "doutorado": "tese", "doutorado direto": "tese"}


# ----------------------------------------------------------------------------
# Estratificação
# ----------------------------------------------------------------------------

def numero(valor: object) -> float | None:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return None
    texto = str(valor).strip().replace(",", ".").replace("%", "")
    try:
        return float(texto)
    except ValueError:
        return None


def faixa_fwci(fwci: float | None) -> int | None:
    if fwci is None or fwci < 0:
        return None
    return next(i for i, corte in enumerate(CORTES_FWCI, start=1) if fwci >= corte)


def faixa_percentil(percentil: float | None) -> int | None:
    if percentil is None:
        return None
    return next((i for i, corte in enumerate(CORTES_PERCENTIL, start=1) if percentil >= corte), 8)


def peso(faixa: int | None) -> float:
    return PESO_FAIXA.get(faixa, 0.0) if faixa else 0.0


def rotulo(prefixo: str, faixa: int | None) -> str:
    return f"{prefixo}{faixa}" if faixa else "-"


# ----------------------------------------------------------------------------
# Dados
# ----------------------------------------------------------------------------

@dataclass
class Artigo:
    doi: str
    titulo: str
    ano: int | None
    fwci: float | None
    percentil: float | None
    source: str
    docentes: set[str] = field(default_factory=set)
    qualis: str = ""

    @property
    def f(self) -> int | None:
        return faixa_fwci(self.fwci)

    @property
    def p(self) -> int | None:
        """Estrato do veículo: percentil (ficha 2025-2028) ou Qualis 2021-2024 no backtest."""
        if VEICULO == "qualis":
            return FAIXA_QUALIS.get(self.qualis)
        return faixa_percentil(self.percentil)

    @property
    def chave_ordem(self) -> tuple[float, float]:
        """Ordem da ficha: maior faixa F e, em empate, maior faixa P."""
        return (peso(self.f), peso(self.p))


@dataclass
class Titulado:
    nome: str
    nivel: str  # "tese" ou "dissertacao"
    orientador: str
    ano_defesa: int
    candidatos: list[tuple[str, float | None]] = field(default_factory=list)  # (doi, similaridade)


def primeiro_existente(*caminhos: Path) -> Path:
    return next((c for c in caminhos if c.exists()), caminhos[-1])


def casar_nomes(nomes: list[str], alvos: list[str]) -> dict[str, str]:
    """nome -> alvo, por nome normalizado ou por primeiro+último nome único."""
    por_chave = {chave_nome(a): a for a in alvos}
    por_extremos: dict[tuple[str, str], list[str]] = {}
    for alvo in alvos:
        extremos = primeiro_ultimo(alvo)
        if extremos:
            por_extremos.setdefault(extremos, []).append(alvo)
    resultado = {}
    for nome in nomes:
        if chave_nome(nome) in por_chave:
            resultado[nome] = por_chave[chave_nome(nome)]
        else:
            candidatos = por_extremos.get(primeiro_ultimo(nome) or ("", ""), [])
            if len(candidatos) == 1:
                resultado[nome] = candidatos[0]
    return resultado


def carregar_percentis(caminho: Path) -> dict[str, float]:
    if not caminho.exists():
        return {}
    tabela = pd.read_csv(caminho, encoding="utf-8-sig")
    return {
        str(s).strip().casefold(): v
        for s, v in zip(tabela["source"], tabela["percentil"].map(numero))
        if v is not None
    }


def carregar_docentes(caminho: Path | None, nomes: list[str], ini: int, fim: int) -> dict[str, list[int]]:
    """docente -> anos como permanente. Sem arquivo: todos os docentes, todos os anos."""
    todos = list(range(ini, fim + 1))
    if caminho is None:
        return {chave_nome(n): todos for n in nomes}
    tabela = pd.read_csv(caminho, encoding="utf-8-sig", dtype=str)
    docentes = {}
    for _, linha in tabela.iterrows():
        anos = str(linha.get("anos") or "").strip()
        if not anos or anos.lower() == "nan":
            lista = todos
        elif ";" in anos or len(anos) == 4:
            lista = [int(a) for a in anos.split(";") if a.strip()]
        else:  # quantidade de anos
            lista = todos[: int(anos)]
        docentes[chave_nome(linha["docente"])] = lista
    return docentes


def carregar_artigos(alunos_artigos: pd.DataFrame, artigos_docentes: pd.DataFrame, percentis: dict[str, float]) -> dict[str, Artigo]:
    docentes_por_doi: dict[str, set[str]] = {}
    for prof, doi in zip(artigos_docentes["professor"], artigos_docentes["doi"]):
        if isinstance(doi, str) and doi.strip():
            docentes_por_doi.setdefault(doi.strip().lower(), set()).add(chave_nome(prof))
    extras = artigos_docentes.assign(doi=artigos_docentes["doi"].str.lower()).drop_duplicates("doi").set_index("doi")

    artigos: dict[str, Artigo] = {}
    for doi, grupo in alunos_artigos.groupby(alunos_artigos["doi"].str.lower()):
        linha = grupo.iloc[0]
        extra = extras.loc[doi] if doi in extras.index else {}

        def campo(nome: str):
            for fonte in (linha, extra):
                valor = fonte.get(nome) if hasattr(fonte, "get") else None
                if numero(valor) is not None or (isinstance(valor, str) and valor.strip()):
                    return valor
            return None

        source = str(campo("source") or "").strip()
        percentil = numero(campo("percentil"))
        if percentil is None and source:
            percentil = percentis.get(source.casefold())
        ano = numero(linha.get("ano_publicacao_scopus"))
        artigos[doi] = Artigo(
            doi=doi,
            titulo=str(linha["artigo"]),
            ano=int(ano) if ano else None,
            fwci=numero(campo("fwci")),
            percentil=percentil,
            source=source,
            qualis=str(campo("qualis") or "").strip().upper(),
            docentes=set(grupo["docente"].map(chave_nome)) | docentes_por_doi.get(doi, set()),
        )
    return artigos


def carregar_titulados(caminho: Path, ini: int, fim: int) -> list[Titulado]:
    alunos = pd.read_csv(caminho)
    alunos = alunos[alunos["Situação"].eq("Egresso") & alunos["Defesa"].between(ini, fim)]
    titulados = []
    for _, linha in alunos.drop_duplicates("Name").iterrows():
        nivel = NIVEL_ALUNO.get(str(linha["Nível"]).strip().casefold())
        if nivel:
            titulados.append(Titulado(
                nome=str(linha["Name"]).strip(), nivel=nivel,
                orientador=chave_nome(linha["Orientador"]), ano_defesa=int(linha["Defesa"]),
            ))
    return titulados


def similaridades(caminho: Path) -> dict[tuple[str, str, str], float]:
    """(chave do autor, doi, nível do trabalho) -> maior similaridade."""
    if not caminho.exists():
        return {}
    tabela = pd.read_csv(caminho)
    resultado: dict[tuple[str, str, str], float] = {}
    for _, linha in tabela.iterrows():
        chave = (chave_nome(linha["autor"]), str(linha["doi"]).lower(), nivel_do_material(linha["material"]))
        resultado[chave] = max(resultado.get(chave, -1.0), float(linha["cosine_similarity"]))
    return resultado


def associar_candidatos(titulados: list[Titulado], alunos_artigos: pd.DataFrame, sims: dict) -> None:
    """Preenche os artigos em que cada titulado é primeiro autor, com a similaridade."""
    primeiros = alunos_artigos[alunos_artigos["primeiro_autor"].astype(str).eq("1")]
    mapa = casar_nomes(sorted(primeiros["aluno"].unique()), [t.nome for t in titulados])
    autores_sim = sorted({k[0] for k in sims})
    mapa_sim = casar_nomes([t.nome for t in titulados], autores_sim)
    for titulado in titulados:
        dois = sorted({
            str(d).lower() for a, d in zip(primeiros["aluno"], primeiros["doi"]) if mapa.get(a) == titulado.nome
        })
        autor = chave_nome(mapa_sim.get(titulado.nome, titulado.nome))
        for doi in dois:
            mesmo_nivel = sims.get((autor, doi, titulado.nivel))
            # Sem trabalho do mesmo nível no acervo, usa o outro (ex.: cadastro divergente).
            qualquer = max((v for (a, d, _), v in sims.items() if a == autor and d == doi), default=None)
            titulado.candidatos.append((doi, mesmo_nivel if mesmo_nivel is not None else qualquer))


# ----------------------------------------------------------------------------
# Avaliação (mesmas fórmulas usadas no modelo; serve também para conferir)
# ----------------------------------------------------------------------------

def avaliar_241(lista: list[Artigo], n_entradas: int, tamanho_amostra: int) -> dict:
    ordenada = sorted(lista, key=lambda a: a.chave_ordem, reverse=True)[:tamanho_amostra]
    ibad = sum(peso(a.f) for a in ordenada) / tamanho_amostra
    ibpd = sum(peso(a.p) for a in ordenada) / tamanho_amostra
    return {"NP": n_entradas, "NP60": tamanho_amostra, "artigos_na_lista": len(lista),
            "IBAD": ibad, "IBPD": ibpd, "IND_2.4.1": 0.5 * ibad + 0.5 * ibpd}


def avaliar_213(indicacoes: list[tuple[Titulado, Artigo]], n_ind: int, fwci_max: float, so_mestrado: bool = False) -> dict:
    ibv = sum(peso(a.p) for _, a in indicacoes) / n_ind
    iba = sum(min(a.fwci or 0.0, fwci_max) for _, a in indicacoes) / (fwci_max * n_ind)
    pesos = (0.8, 0.2) if so_mestrado else (0.5, 0.5)
    contagem: dict[str, int] = {}
    for t, _ in indicacoes:
        contagem[t.orientador] = contagem.get(t.orientador, 0) + 1
    repetidos = sum(max(0, c - 1) for c in contagem.values())
    return {"IBV": ibv, "IBA": iba, "IND_2.1.3": pesos[0] * ibv + pesos[1] * iba,
            "n_repeticao_orientador": repetidos, "D": (n_ind - repetidos) / n_ind}


def objetivo(r241: dict, r213: dict) -> float:
    return PESO_241 * r241["IND_2.4.1"] + PESO_213 * r213["IND_2.1.3"] + PESO_D * r213["D"]


def melhor_artigo(titulado: Titulado, artigos: dict[str, Artigo], ini: int, fim: int, sim_min: float | None) -> Artigo | None:
    opcoes = [
        artigos[d] for d, s in titulado.candidatos
        if d in artigos and artigos[d].ano and ini <= artigos[d].ano <= fim
        and (sim_min is None or (s is not None and s >= sim_min))
    ]
    return max(opcoes, key=lambda a: a.chave_ordem, default=None)


def avaliar_231(titulados, artigos, ini, fim, sim_min) -> tuple[dict, list[dict]]:
    doutores = [t for t in titulados if t.nivel == "tese"]
    entradas = []
    for t in doutores:
        a = melhor_artigo(t, artigos, ini, fim, sim_min)
        entradas.append({
            "titulado": t.nome, "doi": a.doi if a else "", "faixa_F": rotulo("F", a.f) if a else "-",
            "faixa_P": rotulo("P", a.p) if a else "-",
            "pF": (peso(a.f) if a.f else PESO_SEM_INDICADOR) if a else 0.0,
            "pP": (peso(a.p) if a.p else PESO_SEM_INDICADOR) if a else 0.0,
        })
    nt = max(len(doutores), 10)
    amostra_n = round(0.6 * nt)
    amostra = sorted(entradas, key=lambda e: (e["pF"], e["pP"]), reverse=True)[:amostra_n]
    for e in entradas:
        e["na_amostra"] = e in amostra
    ibat = sum(e["pF"] for e in amostra) / amostra_n
    ibpt = sum(e["pP"] for e in amostra) / amostra_n
    return {"NT": len(doutores), "NT60": amostra_n, "IBAT": ibat, "IBPT": ibpt,
            "IND_2.3.1": 0.5 * ibat + 0.5 * ibpt}, entradas


def avaliar_232(titulados, artigos, ini, fim, sim_min) -> tuple[dict, list[dict]]:
    mestres = [t for t in titulados if t.nivel == "dissertacao"]
    entradas = []
    for t in mestres:
        a = melhor_artigo(t, artigos, ini, fim, sim_min)
        entradas.append({"titulado": t.nome, "doi": a.doi if a else "",
                         "faixa_F": rotulo("F", a.f) if a else "-", "pontua": bool(a and a.f)})
    nd = len(mestres)
    valor = sum(e["pontua"] for e in entradas) / nd if nd else 0.0
    return {"ND": nd, "IND_2.3.2": valor}, entradas


# ----------------------------------------------------------------------------
# Modelo
# ----------------------------------------------------------------------------

@dataclass
class Instancia:
    artigos: dict[str, Artigo]
    docentes: dict[str, list[int]]
    titulados: list[Titulado]
    ini: int
    fim: int
    sim_min: float | None
    fwci_max: float
    max_teses: int = 7
    max_dissertacoes: int = 3
    um_por_ano: bool = False

    @property
    def n_entradas(self) -> int:
        return sum(len(anos) for anos in self.docentes.values())

    @property
    def tamanho_amostra(self) -> int:
        return max(1, round(0.6 * self.n_entradas))

    @property
    def n_ind(self) -> int:
        return self.max_teses + self.max_dissertacoes

    def artigos_241(self) -> list[Artigo]:
        """Artigos do quadriênio com coautoria discente, FWCI e ao menos um docente permanente."""
        return [
            a for a in self.artigos.values()
            if a.ano and self.ini <= a.ano <= self.fim and a.f and a.docentes & self.docentes.keys()
        ]

    def pares_213(self) -> list[tuple[Titulado, Artigo, float | None]]:
        pares = []
        for t in self.titulados:
            # Janela de aceitação da ficha (2023-2029 teses, 2024-2029 dissertações),
            # deslocada para o quadriênio analisado.
            ini = self.ini - 2 if t.nivel == "tese" else self.ini - 1
            for doi, sim in t.candidatos:
                a = self.artigos.get(doi)
                if not a or not a.ano or not (ini <= a.ano <= self.fim + 1):
                    continue
                if self.sim_min is not None and (sim is None or sim < self.sim_min):
                    continue
                pares.append((t, a, sim))
        return pares


def resolver(inst: Instancia, tempo_max: int = 120) -> dict:
    prob = pulp.LpProblem("nota_capes_eng3", pulp.LpMaximize)

    # --- 2.4.1: atribuição artigo -> docente --------------------------------
    artigos = inst.artigos_241()
    x = {
        (a.doi, d): pulp.LpVariable(f"x_{i}_{j}", cat="Binary")
        for i, a in enumerate(artigos)
        for j, d in enumerate(sorted(inst.docentes))
        if d in a.docentes
    }
    na_lista = {a.doi: pulp.lpSum(v for (doi, _), v in x.items() if doi == a.doi) for a in artigos}
    na_amostra = {a.doi: pulp.LpVariable(f"z_{i}", cat="Binary") for i, a in enumerate(artigos)}
    K = inst.tamanho_amostra

    for i, a in enumerate(artigos):
        prob += na_lista[a.doi] <= 1, f"sem_repeticao_{i}"  # coautoria: conta para um docente
        prob += na_amostra[a.doi] <= na_lista[a.doi], f"amostra_lista_{i}"
    for j, (d, anos) in enumerate(sorted(inst.docentes.items())):
        vars_d = [(doi, v) for (doi, dd), v in x.items() if dd == d]
        prob += pulp.lpSum(v for _, v in vars_d) <= len(anos), f"vagas_{j}"
        if inst.um_por_ano:
            for ano in range(inst.ini, inst.fim + 1):
                limite = 1 if ano in anos else 0
                prob += pulp.lpSum(v for doi, v in vars_d if inst.artigos[doi].ano == ano) <= limite, f"ano_{j}_{ano}"
    prob += pulp.lpSum(na_amostra.values()) <= K, "tamanho_amostra"
    # A amostra são as K melhores entradas por (F, P): se 'a' entra e 'b' é
    # estritamente melhor e está na lista, 'b' também tem que entrar.
    for i, a in enumerate(artigos):
        for k, b in enumerate(artigos):
            if b.chave_ordem > a.chave_ordem:
                prob += na_amostra[a.doi] <= na_amostra[b.doi] + 1 - na_lista[b.doi], f"ordem_{i}_{k}"

    ibad = pulp.lpSum(peso(a.f) * na_amostra[a.doi] for a in artigos) / K
    ibpd = pulp.lpSum(peso(a.p) * na_amostra[a.doi] for a in artigos) / K
    ind_241 = 0.5 * ibad + 0.5 * ibpd

    # --- 2.1.3 + D: indicação de teses/dissertações -------------------------
    pares = inst.pares_213()
    u = {(t.nome, a.doi): pulp.LpVariable(f"u_{i}", cat="Binary") for i, (t, a, _) in enumerate(pares)}
    for i, t in enumerate(inst.titulados):
        prob += pulp.lpSum(v for (nome, _), v in u.items() if nome == t.nome) <= 1, f"uma_producao_{i}"
    por_nivel = {"tese": inst.max_teses, "dissertacao": inst.max_dissertacoes}
    for nivel, limite in por_nivel.items():
        prob += pulp.lpSum(u[(t.nome, a.doi)] for t, a, _ in pares if t.nivel == nivel) <= limite, f"max_{nivel}"
    orientadores = sorted({t.orientador for t, _, _ in pares})
    repeticao = {o: pulp.LpVariable(f"r_{i}", lowBound=0) for i, o in enumerate(orientadores)}
    for i, o in enumerate(orientadores):
        prob += repeticao[o] >= pulp.lpSum(u[(t.nome, a.doi)] for t, a, _ in pares if t.orientador == o) - 1, f"rep_{i}"

    ibv = pulp.lpSum(peso(a.p) * u[(t.nome, a.doi)] for t, a, _ in pares) / inst.n_ind
    iba = pulp.lpSum(min(a.fwci or 0.0, inst.fwci_max) * u[(t.nome, a.doi)] for t, a, _ in pares) / (inst.fwci_max * inst.n_ind)
    ind_213 = 0.5 * ibv + 0.5 * iba
    indice_d = (inst.n_ind - pulp.lpSum(repeticao.values())) / inst.n_ind

    # Desempate mínimo: entre soluções de mesma nota, preferir listas completas.
    desempate = DESEMPATE * pulp.lpSum(na_lista.values())
    prob += PESO_241 * ind_241 + PESO_213 * ind_213 + PESO_D * indice_d + desempate
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=tempo_max))
    status = pulp.LpStatus[prob.status]
    if prob.status != pulp.LpStatusOptimal:
        raise RuntimeError(f"Solver terminou com status {status}")

    lista = [(inst.artigos[doi], d) for (doi, d), v in x.items() if v.value() > 0.5]
    indicacoes = [(t, a) for t, a, _ in pares if u[(t.nome, a.doi)].value() > 0.5]
    return {
        "status": status,
        "valor": pulp.value(prob.objective) - pulp.value(desempate),
        "lista_241": lista,
        "amostra_241": {doi for doi, v in na_amostra.items() if v.value() > 0.5},
        "indicacoes_213": indicacoes,
        "similaridade": {(t.nome, a.doi): s for t, a, s in pares},
        "n_variaveis": len(prob.variables()),
        "n_restricoes": len(prob.constraints),
    }


def linha_de_base(inst: Instancia) -> dict:
    """Sem coordenação: cada docente (ordem alfabética) indica seus melhores artigos
    ainda livres; teses/dissertações com melhor artigo, ignorando o orientador."""
    livres = sorted(inst.artigos_241(), key=lambda a: a.chave_ordem, reverse=True)
    usados, lista = set(), []
    for d in sorted(inst.docentes):
        proprios = [a for a in livres if d in a.docentes and a.doi not in usados]
        if inst.um_por_ano:  # a melhor de cada ano em que foi permanente
            por_ano: dict[int, Artigo] = {}
            for a in proprios:
                if a.ano in inst.docentes[d]:
                    por_ano.setdefault(a.ano, a)
            proprios = list(por_ano.values())
        for a in proprios[: len(inst.docentes[d])]:
            usados.add(a.doi)
            lista.append((a, d))

    def pontuacao(a: Artigo) -> float:
        return 0.5 * peso(a.p) + 0.5 * min(a.fwci or 0.0, inst.fwci_max) / inst.fwci_max

    melhores: dict[str, tuple[Titulado, Artigo]] = {}
    for t, a, _ in inst.pares_213():
        if t.nome not in melhores or pontuacao(a) > pontuacao(melhores[t.nome][1]):
            melhores[t.nome] = (t, a)
    indicacoes = []
    for nivel, limite in (("tese", inst.max_teses), ("dissertacao", inst.max_dissertacoes)):
        do_nivel = sorted((p for p in melhores.values() if p[0].nivel == nivel), key=lambda p: pontuacao(p[1]), reverse=True)
        indicacoes += do_nivel[:limite]
    return {"lista_241": lista, "indicacoes_213": indicacoes}


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Maximiza os indicadores CAPES (Eng. III) para o quadriênio 2021-2024.")
    parser.add_argument("--alunos-artigos", type=Path, default=primeiro_existente(
        PASTA / "alunos_artigos_2021_2024_final.csv", PASTA / "alunos_artigos_2021_2024_ampliado.csv",
        PASTA / "alunos_artigos_scival_2021_2024_completo.csv",
        PASTA / "alunos_artigos_scival_2021_2024_com_fwci.csv"))
    parser.add_argument("--artigos-docentes", type=Path, default=primeiro_existente(
        PASTA / "artigos_docentes_2021_2024_enriquecido.csv", PASTA / "artigos_docentes_2021_2024.csv"))
    parser.add_argument("--similaridade", type=Path, default=PASTA / "thesis_articles_similarity_2021_2024.csv")
    parser.add_argument("--alunos", type=Path, default=RAIZ / "ALUNOS-Completo.csv")
    parser.add_argument("--percentis", type=Path, default=RAIZ / "percentis_periodicos.csv")
    parser.add_argument("--docentes", type=Path, help="CSV docente,anos (anos = quantidade ou lista 2021;2022). Padrão: todos, 4 anos.")
    parser.add_argument("--inicio", type=int, default=2021)
    parser.add_argument("--fim", type=int, default=2024)
    parser.add_argument("--sim-min", type=float, default=0.4, help="Similaridade mínima tese × artigo para demonstrar associação.")
    parser.add_argument("--sem-associacao", action="store_true", help="Não exige similaridade mínima (aceita qualquer artigo como primeiro autor).")
    parser.add_argument("--fwci-max", type=float, default=3.0, help="Teto f do FWCI no IBA (definido pela área na avaliação).")
    parser.add_argument("--um-por-ano", action="store_true", help="No 2.4.1, cada vaga do docente só aceita artigo publicado naquele ano.")
    parser.add_argument("--veiculo", choices=("percentil", "qualis"), default="percentil",
                        help="Estrato do veículo: percentil P1..P8 (ficha) ou Qualis 2021-2024 (backtest).")
    parser.add_argument("--saida", type=Path, default=PASTA / "otimizacao")
    args = parser.parse_args()

    global VEICULO
    VEICULO = args.veiculo
    alunos_artigos = pd.read_csv(args.alunos_artigos, encoding="utf-8-sig")
    alunos_artigos["docente"] = alunos_artigos["docente"].fillna("")
    artigos_docentes = pd.read_csv(args.artigos_docentes, encoding="utf-8-sig")
    artigos = carregar_artigos(alunos_artigos, artigos_docentes, carregar_percentis(args.percentis))
    docentes = carregar_docentes(args.docentes, sorted(artigos_docentes["professor"].unique()), args.inicio, args.fim)
    titulados = carregar_titulados(args.alunos, args.inicio, args.fim)
    associar_candidatos(titulados, alunos_artigos, similaridades(args.similaridade))
    sim_min = None if args.sem_associacao else args.sim_min

    inst = Instancia(artigos, docentes, titulados, args.inicio, args.fim, sim_min, args.fwci_max, um_por_ano=args.um_por_ano)
    otimo = resolver(inst)
    base = linha_de_base(inst)

    resultados = {}
    for nome, sol in (("otimizado", otimo), ("linha_de_base", base)):
        r241 = avaliar_241([a for a, _ in sol["lista_241"]], inst.n_entradas, inst.tamanho_amostra)
        r213 = avaliar_213(sol["indicacoes_213"], inst.n_ind, inst.fwci_max)
        resultados[nome] = {**r241, **r213, "objetivo": objetivo(r241, r213)}
    if abs(resultados["otimizado"]["objetivo"] - otimo["valor"]) > 1e-6:
        raise RuntimeError("Valor do solver difere da avaliação independente; verifique o modelo.")

    r231, lista_231 = avaliar_231(titulados, artigos, args.inicio, args.fim, sim_min)
    r232, lista_232 = avaliar_232(titulados, artigos, args.inicio, args.fim, sim_min)

    args.saida.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "docente": d, "doi": a.doi, "artigo": a.titulo, "ano": a.ano, "source": a.source,
        "fwci": a.fwci, "faixa_F": rotulo("F", a.f), "percentil": a.percentil, "qualis": a.qualis, "faixa_P": rotulo("P", a.p),
        "na_amostra_60": a.doi in otimo["amostra_241"], "docentes_coautores": len(a.docentes & docentes.keys()),
    } for a, d in sorted(otimo["lista_241"], key=lambda p: (p[1], p[0].doi))]).to_csv(args.saida / "lista_2_4_1.csv", index=False)
    pd.DataFrame([{
        "titulado": t.nome, "nivel": t.nivel, "orientador": t.orientador, "defesa": t.ano_defesa,
        "doi": a.doi, "artigo": a.titulo, "ano": a.ano, "fwci": a.fwci, "faixa_F": rotulo("F", a.f),
        "percentil": a.percentil, "faixa_P": rotulo("P", a.p), "similaridade": otimo["similaridade"].get((t.nome, a.doi)),
    } for t, a in otimo["indicacoes_213"]]).to_csv(args.saida / "indicacoes_2_1_3.csv", index=False)
    pd.DataFrame(lista_231).to_csv(args.saida / "lista_2_3_1.csv", index=False)
    pd.DataFrame(lista_232).to_csv(args.saida / "lista_2_3_2.csv", index=False)
    resumo = pd.DataFrame(resultados).T
    resumo.to_csv(args.saida / "resumo.csv")

    sem_percentil = sum(a.p is None for a in inst.artigos_241())
    print(f"Quadriênio {args.inicio}-{args.fim} | {len(docentes)} docentes, NP={inst.n_entradas}, amostra 60%={inst.tamanho_amostra}")
    print(f"2.4.1: {len(inst.artigos_241())} artigos elegíveis ({sem_percentil} sem estrato de veículo: {args.veiculo})")
    print(f"2.1.3: {len(titulados)} titulados, {len(inst.pares_213())} pares titulado × artigo elegíveis")
    print(f"Modelo: {otimo['n_variaveis']} variáveis, {otimo['n_restricoes']} restrições, status {otimo['status']}\n")
    colunas = ["artigos_na_lista", "IBAD", "IBPD", "IND_2.4.1", "IBV", "IBA", "IND_2.1.3", "D", "objetivo"]
    print(resumo[colunas].astype(float).round(4).to_string())
    ganho = resultados["otimizado"]["objetivo"] - resultados["linha_de_base"]["objetivo"]
    print(f"\nGanho do modelo sobre a linha de base: {ganho:+.4f} (pontos do quesito 2, escala 0-1)")
    print(f"\nIndicadores sem decisão: IND_2.3.1={r231['IND_2.3.1']:.4f} (NT={r231['NT']}), "
          f"IND_2.3.2={r232['IND_2.3.2']:.4f} (ND={r232['ND']})")
    total = resultados["otimizado"]["objetivo"] + PESO_231 * r231["IND_2.3.1"] + PESO_232 * r232["IND_2.3.2"]
    print(f"Contribuição quantitativa total ao quesito 2: {total:.4f} de {PESO_241 + PESO_213 + PESO_D + PESO_231 + PESO_232:.4f} possíveis")
    print(f"\nArquivos em {args.saida}")


if __name__ == "__main__":
    main()
