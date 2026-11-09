"""Completa um CSV de artigos com os metadados usados no quadriênio atual.

Para cada DOI consulta a Scopus Abstract Retrieval API e preenche, quando
vazios: `scopus_id`, `eid`, `ano_publicacao_scopus`, `resumo_scopus`,
`source` (periódico) e `citations`. Em seguida:

- `fwci`: SciVal Publication Lookup (mesma função de
  `resultados_2021_2024/adicionar_fwci_scival.py`), só se estiver vazio;
- `qualis`: `qualis_21-24.xlsx`, pelo nome do periódico;
- `percentil`: `percentis_periodicos.csv`; periódicos ausentes são consultados
  na Serial Title API e acrescentados a esse arquivo.

O arquivo de entrada não é alterado. Requer SCOPUS_API_KEY (ou
ELSEVIER_API_KEY, também lida do .env) e, fora da rede institucional,
SCOPUS_INSTTOKEN.

Uso (na raiz do projeto):
    uv run python src/enriquecer_scopus.py \
        --entrada resultados_2021_2024/artigos_docentes_2021_2024.csv \
        --saida resultados_2021_2024/artigos_docentes_2021_2024_enriquecido.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from dotenv import load_dotenv  # noqa: E402

from resultados_2021_2024.adicionar_fwci_scival import fetch_fwci  # noqa: E402
from resultados_2021_2024.extrair_alunos_scival import abstract_for_response, publication_year  # noqa: E402
from src.transform import load_qualis, load_qualis_issn, normalize_doi, qualis_de  # noqa: E402

ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/{doi}"
PERCENTIS = RAIZ / "percentis_periodicos.csv"
CAMPOS_PERCENTIS = ["source", "percentil", "ano", "categoria"]
CAMPOS_NOVOS = [
    "scopus_id", "eid", "ano_publicacao_scopus", "resumo_scopus",
    "source", "issn", "citations", "fwci", "qualis", "percentil", "scopus_status",
]


def texto(valor: object) -> str:
    return str(valor or "").strip()


def consultar_scopus(doi: str, chave: str, token: str) -> tuple[dict[str, str], str]:
    headers = {"Accept": "application/json", "X-ELS-APIKey": chave}
    if token:
        headers["X-ELS-Insttoken"] = token
    url = f"{ABSTRACT_URL.format(doi=quote(doi, safe='/'))}?{urlencode({'view': 'META_ABS'})}"
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as resposta:
            payload = json.loads(resposta.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f"HTTP {exc.code}: acesso negado à Scopus; verifique a chave/VPN/token.") from exc
        if exc.code == 429:
            raise RuntimeError("HTTP 429: limite de consultas atingido.") from exc
        return {}, f"erro_http_{exc.code}"
    except URLError as exc:
        raise RuntimeError(f"Falha de rede ao consultar a Scopus: {exc.reason}") from exc

    resposta = payload.get("abstracts-retrieval-response", {})
    core = resposta.get("coredata", {}) if isinstance(resposta, dict) else {}
    ano = publication_year(core)
    return {
        "scopus_id": texto(core.get("dc:identifier")).removeprefix("SCOPUS_ID:"),
        "eid": texto(core.get("eid")),
        "ano_publicacao_scopus": str(ano or ""),
        "resumo_scopus": abstract_for_response(resposta, core),
        "source": texto(core.get("prism:publicationName")),
        "citations": texto(core.get("citedby-count")),
        "issn": issn_do_core(core),
    }, "coletado"


def carregar_percentis(caminho: Path = PERCENTIS) -> list[dict[str, str]]:
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


def percentil_valido(valor: object) -> str:
    bruto = texto(valor).replace(",", ".").replace("%", "")
    try:
        float(bruto)
    except ValueError:
        return ""
    return bruto


def issn_do_core(core: dict) -> str:
    for campo in ("prism:issn", "prism:eIssn"):
        valor = core.get(campo)
        if isinstance(valor, list):
            valor = next((v for v in valor if v), "")
        valor = texto(valor).split()[0] if texto(valor) else ""
        if valor:
            return valor.replace("-", "")
    return ""


def completar_percentis(sources: dict[str, str], chave: str, caminho: Path = PERCENTIS) -> dict[str, str]:
    """Retorna periódico → percentil, consultando a API para os que faltam.

    `sources` mapeia periódico → ISSN (Scopus). A busca por título falha em
    nomes genéricos ("Processes", "Sensors"); nesses casos usa o ISSN, inclusive
    para periódicos que já estavam no arquivo sem percentil.
    """
    import src.extrair_percentil_scopus as percentil_api

    linhas = carregar_percentis(caminho)
    por_fonte = {texto(l.get("source")).casefold(): l for l in linhas}
    pendentes = sorted(
        s for s in sources
        if s and (s.casefold() not in por_fonte
                  or (not percentil_valido(por_fonte[s.casefold()].get("percentil")) and sources[s]))
    )
    for source in pendentes:
        print(f"Percentil: consultando {source}...", flush=True)
        if sources[source]:
            percentil_api.ISSNS_CONHECIDOS[percentil_api.normalizar_titulo(source)] = sources[source]
        try:
            resultado = percentil_api.consultar_highest_percentile(source, chave)
        except Exception as exc:  # erros de rede/formato não devem parar o lote
            print(f"  falhou: {exc}")
            resultado = None
        resultado = resultado or {"source": source, "percentil": "", "ano": "", "categoria": ""}
        if source.casefold() in por_fonte:
            por_fonte[source.casefold()].update(resultado)
        else:
            linhas.append(resultado)
            por_fonte[source.casefold()] = resultado
        time.sleep(0.5)
    if pendentes:
        with caminho.open("w", encoding="utf-8-sig", newline="") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=CAMPOS_PERCENTIS, extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(linhas)
    return {
        texto(l.get("source")).casefold(): percentil_valido(l.get("percentil"))
        for l in linhas
        if percentil_valido(l.get("percentil"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Completa artigos com metadados Scopus/SciVal, Qualis e percentil.")
    parser.add_argument("--entrada", required=True, type=Path, help="CSV com coluna doi.")
    parser.add_argument("--saida", required=True, type=Path, help="Novo CSV (a entrada não é alterada).")
    parser.add_argument("--sem-fwci", action="store_true", help="Não consulta o SciVal para FWCI vazio.")
    parser.add_argument("--sobrescrever", action="store_true", help="Substitui valores já preenchidos pelos da Scopus.")
    parser.add_argument("--year-range", default="10yrs", help="Janela do FWCI no SciVal (padrão 10yrs).")
    parser.add_argument("--delay", type=float, default=0.2, help="Pausa entre consultas, em segundos.")
    args = parser.parse_args()

    load_dotenv(RAIZ / ".env")
    chave = os.getenv("SCOPUS_API_KEY") or os.getenv("ELSEVIER_API_KEY") or ""
    token = os.getenv("SCOPUS_INSTTOKEN", "")
    if not chave:
        parser.error("Defina SCOPUS_API_KEY ou ELSEVIER_API_KEY (ambiente ou .env).")
    if args.entrada.resolve() == args.saida.resolve():
        parser.error("A saída deve ser diferente da entrada.")

    with args.entrada.open(encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)
        campos = list(leitor.fieldnames or [])
        linhas = list(leitor)
    if "doi" not in campos:
        parser.error("A entrada precisa da coluna doi.")
    campos += [c for c in CAMPOS_NOVOS if c not in campos]

    metadados: dict[str, tuple[dict[str, str], str]] = {}
    fwci_cache: dict[str, tuple[str, str]] = {}
    dois = sorted({normalize_doi(l.get("doi")).lower() for l in linhas} - {""})
    try:
        for indice, doi in enumerate(dois, start=1):
            metadados[doi] = consultar_scopus(doi, chave, token)
            print(f"[{indice}/{len(dois)}] {doi}: {metadados[doi][1]}", flush=True)
            time.sleep(max(args.delay, 0))
    except RuntimeError as exc:
        print(f"Erro: {exc} Nenhum arquivo foi gravado.", file=sys.stderr)
        return 1

    for linha in linhas:
        doi = normalize_doi(linha.get("doi")).lower()
        dados, status = metadados.get(doi, ({}, "sem_doi"))
        linha["scopus_status"] = status
        for campo, valor in dados.items():
            if valor and (args.sobrescrever or not texto(linha.get(campo))):
                linha[campo] = valor

        scopus_id = texto(linha.get("scopus_id"))
        if not args.sem_fwci and not texto(linha.get("fwci")) and scopus_id.isdigit():
            if scopus_id not in fwci_cache:
                try:
                    fwci_cache[scopus_id] = fetch_fwci(scopus_id, chave, token, args.year_range)
                except RuntimeError as exc:
                    print(f"Aviso SciVal: {exc} FWCI vazios permanecem vazios.", file=sys.stderr)
                    args.sem_fwci = True
                    continue
                time.sleep(max(args.delay, 0))
            valor, status_fwci = fwci_cache[scopus_id]
            if status_fwci == "coletado":
                linha["fwci"] = valor

    qualis, qualis_issn = load_qualis(), load_qualis_issn()
    fontes: dict[str, str] = {}
    for linha in linhas:
        source = texto(linha.get("source"))
        if source:
            fontes[source] = fontes.get(source) or texto(linha.get("issn"))
    percentis = completar_percentis(fontes, chave)
    for linha in linhas:
        source = texto(linha.get("source"))
        if source and (args.sobrescrever or not texto(linha.get("qualis"))):
            linha["qualis"] = qualis_de(source, texto(linha.get("issn")), qualis, qualis_issn)
        linha["percentil"] = percentis.get(source.casefold(), "") if source else ""

    args.saida.parent.mkdir(parents=True, exist_ok=True)
    with args.saida.open("w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=campos, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(linhas)

    def contar(campo: str) -> int:
        return sum(bool(texto(l.get(campo))) for l in linhas)

    print(
        f"Gravado {args.saida} ({len(linhas)} linhas): source {contar('source')}, citations {contar('citations')}, "
        f"fwci {contar('fwci')}, qualis {contar('qualis')}, percentil {contar('percentil')}, resumo {contar('resumo_scopus')}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
