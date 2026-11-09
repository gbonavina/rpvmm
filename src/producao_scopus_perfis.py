"""Levanta a produção 2019-2025 pelos perfis Scopus de titulados e docentes.

A Eng3-Ficha.pdf determina que o levantamento seja feito na Scopus/SciVal a
partir do Scopus ID de cada doutor titulado (2.3.1) e de cada docente
permanente (2.4.1); a produção não fica restrita ao que está no Lattes dos
docentes. Este script:

1. resolve o Scopus Author ID de cada docente e de cada titulado do quadriênio
   (Author Search por sobrenome + primeiro nome). Um perfil só é aceito se o
   nome for compatível e se o perfil tiver documento com afiliação ITA/UNIFESP
   ou coautoria com um docente do programa (evita homônimos);
2. busca os documentos de cada perfil (Scopus Search, visão COMPLETE), apenas
   artigos, revisões e trabalhos em eventos (a ficha fala em "artigos
   científicos publicados em periódicos ou eventos") e com DOI;
3. para documentos de titulados, registra a posição do titulado (primeiro
   autor) e os docentes coautores; para documentos de docentes, procura
   discentes/egressos entre os coautores com as mesmas regras de
   `src/coautores_discentes.py`;
4. completa FWCI (SciVal), percentil, Qualis e resumo, e une o resultado ao
   arquivo ampliado em `alunos_artigos_2021_2024_final.csv`.

Uso: uv run python src/producao_scopus_perfis.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from resultados_2021_2024.adicionar_fwci_scival import fetch_fwci  # noqa: E402
from src.coautores_discentes import COLUNAS, carregar_alunos, nomes_compativeis  # noqa: E402
from src.enriquecer_scopus import completar_percentis  # noqa: E402
from src.otimizacao_capes import carregar_titulados  # noqa: E402
from src.similaridade_teses import chave_nome, tokens_nome  # noqa: E402
from src.transform import load_qualis, load_qualis_issn, qualis_de  # noqa: E402

PASTA = RAIZ / "resultados_2021_2024"
AUTHOR_SEARCH = "https://api.elsevier.com/content/search/author"
SCOPUS_SEARCH = "https://api.elsevier.com/content/search/scopus"
SUBTIPOS = {"ar": "artigo", "re": "revisão", "cp": "evento"}
AFILIACOES = re.compile(
    r"tecnologico de aeronautica|\bita\b|federal de sao paulo|unifesp|dcta|ciencia e tecnologia aeroespacial"
)
COLUNAS_SAIDA = COLUNAS + ["tipo_documento", "origem"]


def sem_acento(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in texto if not unicodedata.combining(c))


class Scopus:
    def __init__(self, chave: str, token: str, delay: float, cache: Path):
        self.headers = {"X-ELS-APIKey": chave, "Accept": "application/json"}
        if token:
            self.headers["X-ELS-Insttoken"] = token
        self.delay = delay
        self.cache_path = cache
        self.cache = json.loads(cache.read_text()) if cache.exists() else {}

    def get(self, url: str, params: dict) -> dict:
        chave = url + "?" + json.dumps(params, sort_keys=True)
        if chave not in self.cache:
            resposta = requests.get(url, params=params, headers=self.headers, timeout=60)
            if resposta.status_code in (401, 403, 429):
                raise RuntimeError(f"HTTP {resposta.status_code} em {url}: {resposta.text[:200]}")
            self.cache[chave] = resposta.json() if resposta.ok else {}
            time.sleep(self.delay)
        return self.cache[chave]

    def salvar(self) -> None:
        self.cache_path.write_text(json.dumps(self.cache))

    def autores(self, nome: str) -> list[dict]:
        partes = tokens_nome(nome)
        if len(partes) < 2:
            return []
        query = f"AUTHLASTNAME({sem_acento(partes[-1])}) AND AUTHFIRST({sem_acento(partes[0])})"
        entradas, inicio = [], 0
        while inicio < 200:
            dados = self.get(AUTHOR_SEARCH, {"query": query, "count": 25, "start": inicio})
            lote = dados.get("search-results", {}).get("entry", [])
            if not lote or "error" in lote[0]:
                break
            entradas += lote
            total = int(dados["search-results"].get("opensearch:totalResults", 0))
            inicio += 25
            if inicio >= total:
                break
        candidatos = []
        for e in entradas:
            pref = e.get("preferred-name", {})
            nome_scopus = f"{pref.get('given-name', '')} {pref.get('surname', '')}"
            if nomes_compativeis(nome, nome_scopus):
                candidatos.append({"id": e["dc:identifier"].split(":")[-1], "nome": nome_scopus})
        return candidatos

    def documentos(self, autor_id: str, ini: int, fim: int) -> list[dict]:
        query = f"AU-ID({autor_id}) AND PUBYEAR > {ini - 1} AND PUBYEAR < {fim + 1}"
        docs, inicio = [], 0
        while True:
            dados = self.get(SCOPUS_SEARCH, {"query": query, "view": "COMPLETE", "count": 25, "start": inicio})
            lote = dados.get("search-results", {}).get("entry", [])
            if not lote or "error" in lote[0]:
                break
            docs += lote
            inicio += 25
            if inicio >= int(dados["search-results"].get("opensearch:totalResults", 0)):
                break
        return docs


def autores_do_doc(doc: dict) -> list[dict]:
    saida = []
    for a in doc.get("author", []) or []:
        nome = f"{a.get('given-name') or ''} {a.get('surname') or ''}".strip() or a.get("authname", "")
        saida.append({"id": str(a.get("authid", "")), "nome": nome, "seq": str(a.get("@seq", ""))})
    return saida


def afiliacao_programa(doc: dict) -> bool:
    return any(AFILIACOES.search(sem_acento(a.get("affilname") or "").casefold()) for a in doc.get("affiliation", []) or [])


def ano_doc(doc: dict) -> int | None:
    m = re.match(r"(\d{4})", str(doc.get("prism:coverDate", "")))
    return int(m.group(1)) if m else None


def issn_doc(doc: dict) -> str:
    return str(doc.get("prism:issn") or doc.get("prism:eIssn") or "").replace("-", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inicio", type=int, default=2021)
    parser.add_argument("--fim", type=int, default=2024)
    parser.add_argument("--artigos-docentes", type=Path, default=PASTA / "artigos_docentes_2021_2024_enriquecido.csv")
    parser.add_argument("--ampliado", type=Path, default=PASTA / "alunos_artigos_2021_2024_ampliado.csv")
    parser.add_argument("--saida", type=Path, default=PASTA / "alunos_artigos_2021_2024_final.csv")
    parser.add_argument("--perfis", type=Path, default=PASTA / "perfis_scopus_2021_2024.csv")
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()

    load_dotenv(RAIZ / ".env")
    chave = os.getenv("SCOPUS_API_KEY") or os.getenv("ELSEVIER_API_KEY") or ""
    token = os.getenv("SCOPUS_INSTTOKEN", "")
    if not chave:
        parser.error("Defina SCOPUS_API_KEY ou ELSEVIER_API_KEY.")
    api = Scopus(chave, token, args.delay, PASTA / ".cache_scopus_perfis.json")
    ini_docs, fim_docs = args.inicio - 2, args.fim + 1  # janela do 2.1.3 (teses: 2019-2025)

    docentes = sorted(pd.read_csv(args.artigos_docentes, encoding="utf-8-sig")["professor"].unique())
    titulados = carregar_titulados(RAIZ / "ALUNOS-Completo.csv", args.inicio, args.fim)
    alunos = carregar_alunos(RAIZ / "ALUNOS-Completo.csv")

    perfis, docs_por_autor = [], {}

    def resolver(pessoa: str, papel: str, ids_docentes: set[str]) -> list[str]:
        aceitos = []
        for cand in api.autores(pessoa):
            docs = api.documentos(cand["id"], ini_docs - 4, fim_docs)
            confirma = [d for d in docs if afiliacao_programa(d) or any(a["id"] in ids_docentes for a in autores_do_doc(d))]
            completo = nomes_compativeis(pessoa, cand["nome"]) == "nome completo"
            # Titulado com nome abreviado no Scopus ("Thiago Dos Santos") só é aceito se a
            # maioria dos documentos confirmar o vínculo; docentes têm nomes distintivos.
            aceito = bool(confirma) and (papel == "docente" or completo or len(confirma) >= 0.5 * len(docs))
            perfis.append({"pessoa": pessoa, "papel": papel, "scopus_id": cand["id"], "nome_scopus": cand["nome"],
                           "documentos": len(docs), "documentos_confirmando": len(confirma), "aceito": aceito})
            if aceito:
                aceitos.append(cand["id"])
                docs_por_autor[cand["id"]] = [d for d in docs if ini_docs <= (ano_doc(d) or 0) <= fim_docs]
        return aceitos

    try:
        print("Resolvendo perfis de docentes...", flush=True)
        ids_docente: dict[str, str] = {}
        for d in docentes:
            for i in resolver(d, "docente", set()):
                ids_docente[i] = d
        print(f"  {len(set(ids_docente.values()))}/{len(docentes)} docentes com perfil", flush=True)
        print("Resolvendo perfis de titulados...", flush=True)
        ids_titulado: dict[str, str] = {}
        for t in titulados:
            for i in resolver(t.nome, "titulado", set(ids_docente)):
                ids_titulado[i] = t.nome
        print(f"  {len(set(ids_titulado.values()))}/{len(titulados)} titulados com perfil", flush=True)
    finally:
        api.salvar()
    pd.DataFrame(perfis).to_csv(args.perfis, index=False)

    # ---- documentos -> linhas aluno × docente × artigo ----
    entrada_por_aluno = dict(zip(alunos["nome"], alunos["ano_entrada"]))
    info_aluno = {r.nome: r for r in alunos.itertuples()}
    linhas, docs_info = [], {}
    for autor_id, docs in docs_por_autor.items():
        for doc in docs:
            doi = str(doc.get("prism:doi") or "").strip().lower()
            if not doi or doc.get("subtype") not in SUBTIPOS:
                continue
            autores = autores_do_doc(doc)
            docs_info[doi] = doc
            coautores_docentes = sorted({ids_docente[a["id"]] for a in autores if a["id"] in ids_docente}) or [""]
            # discentes/egressos: titulados pelo ID; demais pelo nome (regras de coautores_discentes)
            achados = {}
            for a in autores:
                if a["id"] in ids_titulado:
                    # Perfis Scopus podem misturar homônimos ("M. Pereira" em artigo de medicina):
                    # sem docente coautor, o nome no próprio artigo precisa ser compatível.
                    if coautores_docentes != [""] or nomes_compativeis(ids_titulado[a["id"]], a["nome"]):
                        achados[ids_titulado[a["id"]]] = (a, "scopus id")
                elif a["id"] not in ids_docente:
                    for nome in info_aluno:
                        tipo = nomes_compativeis(nome, a["nome"])
                        ano, entrada = ano_doc(doc), entrada_por_aluno.get(nome)
                        confiavel = tipo == "nome completo" or afiliacao_programa(doc)
                        if tipo and confiavel and not (ano and pd.notna(entrada) and ano < entrada):
                            achados.setdefault(nome, (a, tipo))
            for aluno, (a, tipo) in achados.items():
                info = info_aluno.get(aluno)
                for docente in coautores_docentes:
                    linhas.append({
                        "aluno": aluno, "docente": docente, "doi": doi, "eid": doc.get("eid", ""),
                        "scopus_id": str(doc.get("dc:identifier", "")).split(":")[-1],
                        "artigo": doc.get("dc:title", ""), "resumo_scopus": doc.get("dc:description", "") or "",
                        "source": doc.get("prism:publicationName", ""), "issn": issn_doc(doc),
                        "citations": doc.get("citedby-count", ""), "ano_publicacao_scopus": ano_doc(doc) or "",
                        "scopus_author_id_aluno": a["id"], "nome_autor_scopus": a["nome"], "posicao_autor": a["seq"],
                        "primeiro_autor": "1" if a["seq"] == "1" else "0", "tipo_correspondencia": tipo,
                        "situacao_aluno": getattr(info, "situacao_aluno", ""), "nivel_aluno": getattr(info, "nivel_aluno", ""),
                        "ano_entrada": int(info.ano_entrada) if info is not None and pd.notna(info.ano_entrada) else "",
                        "autores_do_artigo": " | ".join(x["nome"] for x in autores),
                        "tipo_documento": SUBTIPOS[doc["subtype"]], "origem": "perfil scopus",
                    })
    novos = pd.DataFrame(linhas, columns=COLUNAS_SAIDA)

    # FWCI (SciVal), Qualis e percentil para os documentos novos
    fwci = {}
    for sid in sorted(set(novos["scopus_id"]) - {""}):
        try:
            valor, status = fetch_fwci(sid, chave, token, "10yrs")
        except RuntimeError as exc:
            print(f"Aviso SciVal: {exc}")
            break
        fwci[sid] = valor if status == "coletado" else ""
        time.sleep(args.delay)
    novos["fwci"] = novos["scopus_id"].map(fwci).fillna("")
    qualis, qualis_issn = load_qualis(), load_qualis_issn()
    novos["qualis"] = [qualis_de(s, i, qualis, qualis_issn) for s, i in zip(novos["source"], novos["issn"])]
    fontes = {}
    for s, i in zip(novos["source"], novos["issn"]):
        if s:
            fontes[s] = fontes.get(s) or i
    percentis = completar_percentis(fontes, chave)
    novos["percentil"] = novos["source"].map(lambda s: percentis.get(str(s).casefold(), ""))

    ampliado = pd.read_csv(args.ampliado, encoding="utf-8-sig", dtype=str).assign(origem="lattes docentes")
    ampliado["tipo_documento"] = ampliado.get("tipo_documento", "artigo")
    final = pd.concat([ampliado, novos.astype(str)], ignore_index=True)
    # Mantém a versão do Lattes quando o par já existia (metadados já conferidos).
    final = final.drop_duplicates(["aluno", "docente", "doi"], keep="first")
    # Um titulado sem docente coautor e com o mesmo artigo já ligado a docente é redundante.
    com_docente = set(zip(final.loc[final["docente"].ne(""), "aluno"], final.loc[final["docente"].ne(""), "doi"]))
    redundante = pd.Series([(a, d) in com_docente for a, d in zip(final["aluno"], final["doi"])], index=final.index)
    final = final[~(final["docente"].eq("") & redundante)]
    final.to_csv(args.saida, index=False, encoding="utf-8-sig")

    print(f"\nPerfis: {args.perfis}")
    print(f"Documentos novos: {novos['doi'].nunique()} | final: {final['doi'].nunique()} artigos, "
          f"{final['aluno'].nunique()} discentes/egressos (antes: {ampliado['doi'].nunique()} / {ampliado['aluno'].nunique()})")
    print(f"Gravado em {args.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
