"""Motor de Otimização Matemática (MILP via PuLP/CBC) para a Nota CAPES — Engenharias III."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import pulp

from app_capes.collectors import (
    CORTES_FWCI,
    CORTES_PERCENTIL,
    chave_nome,
    primeiro_ultimo,
    to_float,
)
from app_capes.config import AppConfig

PESO_FAIXA = {1: 1.0, 2: 0.875, 3: 0.75, 4: 0.625, 5: 0.5, 6: 0.375, 7: 0.25, 8: 0.125}
FAIXA_QUALIS = {q: i for i, q in enumerate(["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"], start=1)}
PESO_SEM_INDICADOR = 0.05

PESO_213 = 0.25 * 0.70
PESO_D = 0.25 * 0.20 * 0.25
PESO_241 = 0.30 * 0.50
PESO_231 = 0.25 * 0.50
PESO_232 = 0.25 * 0.15
DESEMPATE = 1e-6

NIVEL_ALUNO = {"mestrado": "dissertacao", "doutorado": "tese", "doutorado direto": "tese"}


def _faixa_f(fwci: float | None) -> int | None:
    if fwci is None or fwci < 0:
        return None
    return next((i for i, c in enumerate(CORTES_FWCI, start=1) if fwci >= c), 8)


def _faixa_p(percentil: float | None) -> int | None:
    if percentil is None or percentil < 0:
        return None
    return next((i for i, c in enumerate(CORTES_PERCENTIL, start=1) if percentil >= c), 8)


def _peso(faixa: int | None) -> float:
    return PESO_FAIXA.get(faixa, 0.0) if faixa else 0.0


def _rotulo(prefixo: str, faixa: int | None) -> str:
    return f"{prefixo}{faixa}" if faixa else "-"


@dataclass
class ArtigoOpt:
    doi: str
    titulo: str
    ano: int | None
    fwci: float | None
    percentil: float | None
    source: str
    qualis: str = ""
    veiculo_modo: str = "percentil"
    docentes: set[str] = field(default_factory=set)

    @property
    def f(self) -> int | None:
        return _faixa_f(self.fwci)

    @property
    def p(self) -> int | None:
        if self.veiculo_modo == "qualis":
            return FAIXA_QUALIS.get(self.qualis)
        return _faixa_p(self.percentil)

    @property
    def chave_ordem(self) -> tuple[float, float]:
        return (_peso(self.f), _peso(self.p))


@dataclass
class TituladoOpt:
    nome: str
    nivel: str  # "tese" ou "dissertacao"
    orientador: str
    ano_defesa: int
    candidatos: list[tuple[str, float | None]] = field(default_factory=list)


def _casar_nomes(nomes: list[str], alvos: list[str]) -> dict[str, str]:
    por_chave = {chave_nome(a): a for a in alvos}
    por_extremos: dict[tuple[str, str], list[str]] = {}
    for alvo in alvos:
        ext = primeiro_ultimo(alvo)
        if ext:
            por_extremos.setdefault(ext, []).append(alvo)
    res = {}
    for n in nomes:
        ck = chave_nome(n)
        if ck in por_chave:
            res[n] = por_chave[ck]
        else:
            cands = por_extremos.get(primeiro_ultimo(n) or ("", ""), [])
            if len(cands) == 1:
                res[n] = cands[0]
    return res


def run_capes_optimization(
    artigos_docentes_csv: Path,
    alunos_artigos_csv: Path,
    similaridade_csv: Path,
    cfg: AppConfig,
    log: Callable[[str], None] = print,
) -> dict[str, pd.DataFrame | dict]:
    """Executa a otimização MILP (PuLP/CBC) do Quesito 2 CAPES e salva os resultados."""
    out_dir = Path(cfg.output_dir) / f"otimizacao_{cfg.ano_inicio}_{cfg.ano_fim}_{cfg.veiculo_estrato}"
    out_dir.mkdir(parents=True, exist_ok=True)

    alunos_artigos = pd.read_csv(alunos_artigos_csv, encoding="utf-8-sig")
    alunos_artigos["docente"] = alunos_artigos["docente"].fillna("")
    artigos_docentes = pd.read_csv(artigos_docentes_csv, encoding="utf-8-sig")

    # Percentis auxiliares
    perc_map: dict[str, float] = {}
    for p_cand in (Path(cfg.output_dir) / "percentis_periodicos.csv", Path(cfg.percentis_csv) if cfg.percentis_csv else None):
        if p_cand and p_cand.exists():
            tp = pd.read_csv(p_cand, encoding="utf-8-sig")
            for s, v in zip(tp.get("source", []), tp.get("percentil", [])):
                fv = to_float(v)
                if fv is not None and str(s).strip():
                    perc_map[str(s).strip().casefold()] = fv

    # Constrói mapa de artigos
    docentes_por_doi: dict[str, set[str]] = {}
    prof_col = "professor" if "professor" in artigos_docentes.columns else "docente"
    for prof, doi in zip(artigos_docentes.get(prof_col, []), artigos_docentes.get("doi", [])):
        if isinstance(doi, str) and doi.strip():
            docentes_por_doi.setdefault(doi.strip().lower(), set()).add(chave_nome(prof))

    extras = (
        artigos_docentes[artigos_docentes["doi"].notna()]
        .assign(doi=lambda d: d["doi"].astype(str).str.lower())
        .drop_duplicates("doi")
        .set_index("doi")
    )

    artigos: dict[str, ArtigoOpt] = {}
    for doi, grupo in alunos_artigos[alunos_artigos["doi"].notna()].groupby(alunos_artigos["doi"].astype(str).str.lower()):
        if not doi.strip():
            continue
        linha = grupo.iloc[0]
        extra = extras.loc[doi] if doi in extras.index else {}

        def obter(campo: str):
            for f in (linha, extra):
                val = f.get(campo) if hasattr(f, "get") else None
                if to_float(val) is not None or (isinstance(val, str) and val.strip()):
                    return val
            return None

        src = str(obter("source") or "").strip()
        perc = to_float(obter("percentil"))
        if perc is None and src:
            perc = perc_map.get(src.casefold())
        ano_f = to_float(obter("ano_publicacao_scopus") or obter("ano_lattes"))
        artigos[doi] = ArtigoOpt(
            doi=doi,
            titulo=str(obter("artigo") or ""),
            ano=int(ano_f) if ano_f else None,
            fwci=to_float(obter("fwci")),
            percentil=perc,
            source=src,
            qualis=str(obter("qualis") or "").strip().upper(),
            veiculo_modo=cfg.veiculo_estrato,
            docentes={d for d in grupo["docente"].map(chave_nome) if d} | docentes_por_doi.get(doi, set()),
        )

    todos_anos = list(range(cfg.ano_inicio, cfg.ano_fim + 1))
    nomes_docentes = sorted(artigos_docentes[prof_col].dropna().unique())
    docentes = {chave_nome(n): todos_anos for n in nomes_docentes if chave_nome(n)}

    # Carrega titulados do período
    titulados: list[TituladoOpt] = []
    if cfg.alunos_csv and Path(cfg.alunos_csv).exists():
        df_al = pd.read_csv(cfg.alunos_csv, encoding="utf-8-sig")
        df_al["Defesa_num"] = pd.to_numeric(df_al.get("Defesa"), errors="coerce")
        df_tit = df_al[df_al["Situação"].eq("Egresso") & df_al["Defesa_num"].between(cfg.ano_inicio, cfg.ano_fim)]
        for _, r_t in df_tit.drop_duplicates("Name").iterrows():
            niv = NIVEL_ALUNO.get(str(r_t.get("Nível") or "").strip().casefold())
            if niv:
                titulados.append(
                    TituladoOpt(
                        nome=str(r_t["Name"]).split(",")[0].strip(),
                        nivel=niv,
                        orientador=chave_nome(r_t.get("Orientador") or ""),
                        ano_defesa=int(r_t["Defesa_num"]),
                    )
                )

    # Carrega similaridades
    sims: dict[tuple[str, str, str], float] = {}
    if similaridade_csv and similaridade_csv.exists():
        df_s = pd.read_csv(similaridade_csv, encoding="utf-8-sig")
        for _, rs in df_s.iterrows():
            mat_norm = chave_nome(rs.get("material") or rs.get("nivel_trabalho") or "")
            niv_s = "tese" if "tese" in mat_norm else "dissertacao"
            ck_s = (chave_nome(rs.get("autor")), str(rs.get("doi") or "").lower(), niv_s)
            v_sim = to_float(rs.get("cosine_similarity"))
            if v_sim is not None:
                sims[ck_s] = max(sims.get(ck_s, -1.0), v_sim)

    primeiros = alunos_artigos[alunos_artigos["primeiro_autor"].astype(str).isin(["1", "True", "true"])]
    mapa_nomes = _casar_nomes(sorted(primeiros["aluno"].dropna().unique()), [t.nome for t in titulados])
    autores_sim = sorted({k[0] for k in sims})
    mapa_sim = _casar_nomes([t.nome for t in titulados], autores_sim)

    for t in titulados:
        dois_t = sorted({
            str(d).lower() for a, d in zip(primeiros["aluno"], primeiros["doi"])
            if pd.notna(d) and mapa_nomes.get(a) == t.nome
        })
        au_key = chave_nome(mapa_sim.get(t.nome, t.nome))
        for d in dois_t:
            mesmo = sims.get((au_key, d, t.nivel))
            qualq = max((v for (ak, dk, _), v in sims.items() if ak == au_key and dk == d), default=None)
            t.candidatos.append((d, mesmo if mesmo is not None else qualq))

    n_entradas = max(1, sum(len(anos) for anos in docentes.values()))
    K = max(1, round(0.6 * n_entradas))
    n_ind = max(1, cfg.max_teses + cfg.max_dissertacoes)
    sim_min = cfg.sim_min if cfg.sim_min > 0 else None
    fwci_max = max(0.5, cfg.fwci_max)

    artigos_241 = [
        a for a in artigos.values()
        if a.ano and cfg.ano_inicio <= a.ano <= cfg.ano_fim and a.f and (a.docentes & docentes.keys())
    ]

    pares_213: list[tuple[TituladoOpt, ArtigoOpt, float | None]] = []
    for t in titulados:
        ini_j = cfg.ano_inicio - 2 if t.nivel == "tese" else cfg.ano_inicio - 1
        for doi, sim in t.candidatos:
            a = artigos.get(doi)
            if not a or not a.ano or not (ini_j <= a.ano <= cfg.ano_fim + 1):
                continue
            if sim_min is not None and (sim is None or sim < sim_min):
                continue
            pares_213.append((t, a, sim))

    # Formulação MILP no PuLP
    prob = pulp.LpProblem("nota_capes_ppgpo", pulp.LpMaximize)
    x = {
        (a.doi, d): pulp.LpVariable(f"x_{i}_{j}", cat="Binary")
        for i, a in enumerate(artigos_241)
        for j, d in enumerate(sorted(docentes))
        if d in a.docentes
    }
    na_lista = {a.doi: pulp.lpSum(v for (doi, _), v in x.items() if doi == a.doi) for a in artigos_241}
    na_amostra = {a.doi: pulp.LpVariable(f"z_{i}", cat="Binary") for i, a in enumerate(artigos_241)}

    for i, a in enumerate(artigos_241):
        prob += na_lista[a.doi] <= 1, f"sem_rep_{i}"
        prob += na_amostra[a.doi] <= na_lista[a.doi], f"amostra_lista_{i}"

    for j, (d, anos) in enumerate(sorted(docentes.items())):
        vars_d = [(doi, v) for (doi, dd), v in x.items() if dd == d]
        prob += pulp.lpSum(v for _, v in vars_d) <= len(anos), f"vagas_{j}"
        if cfg.um_por_ano:
            for ano in todos_anos:
                lim = 1 if ano in anos else 0
                prob += pulp.lpSum(v for doi, v in vars_d if artigos[doi].ano == ano) <= lim, f"ano_{j}_{ano}"

    prob += pulp.lpSum(na_amostra.values()) <= K, "tam_amostra"
    for i, a in enumerate(artigos_241):
        for k_idx, b in enumerate(artigos_241):
            if b.chave_ordem > a.chave_ordem:
                prob += na_amostra[a.doi] <= na_amostra[b.doi] + 1 - na_lista[b.doi], f"ord_{i}_{k_idx}"

    ibad = pulp.lpSum(_peso(a.f) * na_amostra[a.doi] for a in artigos_241) / K
    ibpd = pulp.lpSum(_peso(a.p) * na_amostra[a.doi] for a in artigos_241) / K
    ind_241 = 0.5 * ibad + 0.5 * ibpd

    u = {(t.nome, a.doi): pulp.LpVariable(f"u_{i}", cat="Binary") for i, (t, a, _) in enumerate(pares_213)}
    for i, t in enumerate(titulados):
        prob += pulp.lpSum(v for (nm, _), v in u.items() if nm == t.nome) <= 1, f"uma_prod_{i}"
    for niv, lim in (("tese", cfg.max_teses), ("dissertacao", cfg.max_dissertacoes)):
        prob += pulp.lpSum(u[(t.nome, a.doi)] for t, a, _ in pares_213 if t.nivel == niv) <= lim, f"max_{niv}"

    orientadores = sorted({t.orientador for t, _, _ in pares_213 if t.orientador})
    rep_vars = {o: pulp.LpVariable(f"r_{i}", lowBound=0) for i, o in enumerate(orientadores)}
    for i, o in enumerate(orientadores):
        prob += rep_vars[o] >= pulp.lpSum(u[(t.nome, a.doi)] for t, a, _ in pares_213 if t.orientador == o) - 1, f"rep_{i}"

    ibv = pulp.lpSum(_peso(a.p) * u[(t.nome, a.doi)] for t, a, _ in pares_213) / n_ind
    iba = pulp.lpSum(min(a.fwci or 0.0, fwci_max) * u[(t.nome, a.doi)] for t, a, _ in pares_213) / (fwci_max * n_ind)
    ind_213 = 0.5 * ibv + 0.5 * iba
    ind_d = (n_ind - pulp.lpSum(rep_vars.values())) / n_ind

    desemp = DESEMPATE * pulp.lpSum(na_lista.values())
    prob += PESO_241 * ind_241 + PESO_213 * ind_213 + PESO_D * ind_d + desemp
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=120))

    lista_otima = [(artigos[doi], d) for (doi, d), v in x.items() if (v.value() or 0) > 0.5]
    amostra_otima = {doi for doi, v in na_amostra.items() if (v.value() or 0) > 0.5}
    ind_otima = [(t, a) for t, a, _ in pares_213 if (u[(t.nome, a.doi)].value() or 0) > 0.5]
    sim_lookup = {(t.nome, a.doi): s for t, a, s in pares_213}

    # Linha de base gulosa sem coordenação
    livres = sorted(artigos_241, key=lambda a: a.chave_ordem, reverse=True)
    usados_b, lista_base = set(), []
    for d in sorted(docentes):
        prop = [a for a in livres if d in a.docentes and a.doi not in usados_b]
        if cfg.um_por_ano:
            por_ano: dict[int, ArtigoOpt] = {}
            for a in prop:
                if a.ano in docentes[d]:
                    por_ano.setdefault(a.ano, a)
            prop = list(por_ano.values())
        for a in prop[: len(docentes[d])]:
            usados_b.add(a.doi)
            lista_base.append((a, d))

    def pont_213(a: ArtigoOpt) -> float:
        return 0.5 * _peso(a.p) + 0.5 * min(a.fwci or 0.0, fwci_max) / fwci_max

    melhores_b: dict[str, tuple[TituladoOpt, ArtigoOpt]] = {}
    for t, a, _ in pares_213:
        if t.nome not in melhores_b or pont_213(a) > pont_213(melhores_b[t.nome][1]):
            melhores_b[t.nome] = (t, a)
    ind_base = []
    for niv, lim in (("tese", cfg.max_teses), ("dissertacao", cfg.max_dissertacoes)):
        do_niv = sorted((p for p in melhores_b.values() if p[0].nivel == niv), key=lambda p: pont_213(p[1]), reverse=True)
        ind_base += do_niv[:lim]

    def eval_sol(l241, i213) -> dict:
        ord_241 = sorted([a for a, _ in l241], key=lambda a: a.chave_ordem, reverse=True)[:K]
        v_ibad = sum(_peso(a.f) for a in ord_241) / K
        v_ibpd = sum(_peso(a.p) for a in ord_241) / K
        v_241 = 0.5 * v_ibad + 0.5 * v_ibpd

        v_ibv = sum(_peso(a.p) for _, a in i213) / n_ind
        v_iba = sum(min(a.fwci or 0.0, fwci_max) for _, a in i213) / (fwci_max * n_ind)
        v_213 = 0.5 * v_ibv + 0.5 * v_iba
        cnt: dict[str, int] = {}
        for t, _ in i213:
            if t.orientador:
                cnt[t.orientador] = cnt.get(t.orientador, 0) + 1
        rep = sum(max(0, c - 1) for c in cnt.values())
        v_d = (n_ind - rep) / n_ind
        obj = PESO_241 * v_241 + PESO_213 * v_213 + PESO_D * v_d
        return {
            "cenario": "",
            "NP": n_entradas,
            "NP60": K,
            "artigos_na_lista": len(l241),
            "IBAD": round(v_ibad, 4),
            "IBPD": round(v_ibpd, 4),
            "IND_2.4.1": round(v_241, 4),
            "indicacoes_2.1.3": len(i213),
            "IBV": round(v_ibv, 4),
            "IBA": round(v_iba, 4),
            "IND_2.1.3": round(v_213, 4),
            "D": round(v_d, 4),
            "objetivo_decisoes": round(obj, 4),
        }

    r_opt = eval_sol(lista_otima, ind_otima)
    r_opt["cenario"] = "Otimizado (MILP)"
    r_bas = eval_sol(lista_base, ind_base)
    r_bas["cenario"] = "Linha de Base (Sem Coordenação)"

    # 2.3.1 e 2.3.2
    def melhor_art(t: TituladoOpt) -> ArtigoOpt | None:
        ops = [
            artigos[d] for d, s in t.candidatos
            if d in artigos and artigos[d].ano and cfg.ano_inicio <= artigos[d].ano <= cfg.ano_fim
            and (sim_min is None or (s is not None and s >= sim_min))
        ]
        return max(ops, key=lambda a: a.chave_ordem, default=None)

    doutores = [t for t in titulados if t.nivel == "tese"]
    ent_231 = []
    for t in doutores:
        a = melhor_art(t)
        ent_231.append({
            "titulado": t.nome,
            "ano_defesa": t.ano_defesa,
            "doi": a.doi if a else "",
            "artigo": a.titulo if a else "",
            "faixa_F": _rotulo("F", a.f) if a else "-",
            "faixa_P": _rotulo("P", a.p) if a else "-",
            "pF": (_peso(a.f) if a.f else PESO_SEM_INDICADOR) if a else 0.0,
            "pP": (_peso(a.p) if a.p else PESO_SEM_INDICADOR) if a else 0.0,
        })
    nt_amostra = max(1, round(0.6 * max(len(doutores), 10)))
    top_231 = sorted(ent_231, key=lambda e: (e["pF"], e["pP"]), reverse=True)[:nt_amostra]
    for e in ent_231:
        e["na_amostra_60"] = e in top_231
    ind_231_val = 0.5 * (sum(e["pF"] for e in top_231) / nt_amostra) + 0.5 * (sum(e["pP"] for e in top_231) / nt_amostra)

    mestres = [t for t in titulados if t.nivel == "dissertacao"]
    ent_232 = []
    for t in mestres:
        a = melhor_art(t)
        ent_232.append({
            "titulado": t.nome,
            "ano_defesa": t.ano_defesa,
            "doi": a.doi if a else "",
            "artigo": a.titulo if a else "",
            "faixa_F": _rotulo("F", a.f) if a else "-",
            "pontua": bool(a and a.f),
        })
    ind_232_val = sum(e["pontua"] for e in ent_232) / len(mestres) if mestres else 0.0

    for row_r in (r_opt, r_bas):
        row_r["IND_2.3.1"] = round(ind_231_val, 4)
        row_r["IND_2.3.2"] = round(ind_232_val, 4)
        row_r["total_quesito_2"] = round(
            row_r["objetivo_decisoes"] + PESO_231 * ind_231_val + PESO_232 * ind_232_val,
            4,
        )

    df_resumo = pd.DataFrame([r_opt, r_bas])
    df_241 = pd.DataFrame([
        {
            "docente_alocado": d,
            "doi": a.doi,
            "artigo": a.titulo,
            "ano": a.ano,
            "source": a.source,
            "fwci": a.fwci,
            "faixa_F": _rotulo("F", a.f),
            "percentil": a.percentil,
            "qualis": a.qualis,
            "faixa_P": _rotulo("P", a.p),
            "na_amostra_60": a.doi in amostra_otima,
            "docentes_coautores": len(a.docentes & docentes.keys()),
        }
        for a, d in sorted(lista_otima, key=lambda p: (p[1], p[0].doi))
    ])
    df_213 = pd.DataFrame([
        {
            "titulado": t.nome,
            "nivel": t.nivel,
            "orientador": t.orientador,
            "ano_defesa": t.ano_defesa,
            "doi": a.doi,
            "artigo": a.titulo,
            "ano_artigo": a.ano,
            "fwci": a.fwci,
            "faixa_F": _rotulo("F", a.f),
            "percentil": a.percentil,
            "qualis": a.qualis,
            "faixa_P": _rotulo("P", a.p),
            "similaridade_tese": round(sim_lookup.get((t.nome, a.doi)) or 0.0, 4),
        }
        for t, a in ind_otima
    ])
    df_231 = pd.DataFrame(ent_231)
    df_232 = pd.DataFrame(ent_232)

    df_resumo.to_csv(out_dir / "resumo.csv", index=False, encoding="utf-8-sig")
    df_241.to_csv(out_dir / "lista_2_4_1.csv", index=False, encoding="utf-8-sig")
    df_213.to_csv(out_dir / "indicacoes_2_1_3.csv", index=False, encoding="utf-8-sig")
    df_231.to_csv(out_dir / "lista_2_3_1.csv", index=False, encoding="utf-8-sig")
    df_232.to_csv(out_dir / "lista_2_3_2.csv", index=False, encoding="utf-8-sig")

    ganho = r_opt["objetivo_decisoes"] - r_bas["objetivo_decisoes"]
    log(
        f"Otimização concluída ({pulp.LpStatus[prob.status]})! "
        f"Total Quesito 2: {r_opt['total_quesito_2']:.4f} vs Base {r_bas['total_quesito_2']:.4f} (Ganho: {ganho:+.4f})."
    )

    return {
        "resumo": df_resumo,
        "lista_241": df_241,
        "indicacoes_213": df_213,
        "lista_231": df_231,
        "lista_232": df_232,
        "metrics_opt": r_opt,
        "metrics_base": r_bas,
        "out_dir": out_dir,
    }
