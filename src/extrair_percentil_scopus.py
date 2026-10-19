
"""Extrai o Highest Percentile dos periódicos usando a API da Elsevier.

Lê: data/artigos.csv
Usa: .env com ELSEVIER_API_KEY
Grava: percentis_periodicos.csv
"""

import csv
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


# --------------------------------------------------
# CONFIGURAÇÃO
# --------------------------------------------------

RAIZ = Path(__file__).resolve().parent.parent
ARTIGOS = RAIZ / "data" / "artigos.csv"
PERCENTIS = RAIZ / "percentis_periodicos.csv"

URL_BUSCA = "https://api.elsevier.com/content/serial/title"
URL_ISSN = "https://api.elsevier.com/content/serial/title/issn"

ANO_ATUAL = datetime.now().year
CAMPOS = ["source", "percentil", "ano", "categoria"]

# ISSNs para buscas diretas.
ISSNS_CONHECIDOS = {
    "nature": "00280836",
    "networks": "00283045",
}


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------

def lista(valor):
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def normalizar_titulo(titulo):
    """Ignora acentos, espaços, hífens e pontuação."""
    titulo = unicodedata.normalize("NFKD", str(titulo))
    titulo = "".join(
        caractere
        for caractere in titulo
        if not unicodedata.combining(caractere)
    )
    return re.sub(r"[^a-z0-9]", "", titulo.casefold())


def consultar_api(url, chave, params=None):
    resposta = requests.get(
        url,
        params=params,
        headers={
            "X-ELS-APIKey": chave,
            "Accept": "application/json",
        },
        timeout=30,
    )
    resposta.raise_for_status()
    return resposta.json()


def percorrer_dicionarios(dados):
    """Percorre dicionários em estruturas aninhadas."""
    if isinstance(dados, dict):
        yield dados
        for valor in dados.values():
            yield from percorrer_dicionarios(valor)
    elif isinstance(dados, list):
        for item in dados:
            yield from percorrer_dicionarios(item)


# --------------------------------------------------
# BUSCA DO PERIÓDICO
# --------------------------------------------------

def extrair_entradas(dados):
    return lista(
        dados
        .get("serial-metadata-response", {})
        .get("entry", [])
    )


def localizar_por_issn(source, issn, chave):
    """Consulta o periódico diretamente pelo ISSN."""
    dados = consultar_api(
        f"{URL_ISSN}/{issn}",
        chave,
        {"view": "CITESCORE"},
    )

    fontes = extrair_entradas(dados)

    for fonte in fontes:
        titulo_api = (fonte.get("dc:title") or "").strip()

        if titulo_api:
            print(f"Fonte encontrada por ISSN: {titulo_api}")
            return fonte

    print(f"Nenhuma fonte retornada para {source}, ISSN {issn}.")
    return None


def localizar_fonte(source, chave):
    """Localiza a fonte pelo título ou por ISSN conhecido."""
    titulo_normalizado = normalizar_titulo(source)

    if titulo_normalizado in ISSNS_CONHECIDOS:
        fonte = localizar_por_issn(
            source,
            ISSNS_CONHECIDOS[titulo_normalizado],
            chave,
        )

        if fonte is not None:
            return fonte

        print(f"Tentando também a busca por título: {source}")

    dados = consultar_api(
        URL_BUSCA,
        chave,
        {
            "title": source,
            "view": "CITESCORE",
            "count": 25,
        },
    )

    fontes = extrair_entradas(dados)

    for fonte in fontes:
        titulo_api = (fonte.get("dc:title") or "").strip()

        if normalizar_titulo(titulo_api) == titulo_normalizado:
            print(f"Fonte encontrada: {titulo_api}")
            return fonte

    print(f"Título não encontrado na API: {source}")
    return None


# --------------------------------------------------
# DETALHES DO CITESCORE
# --------------------------------------------------

def obter_detalhes(fonte, chave):
    """Consulta os dados detalhados do periódico por ISSN."""
    issn = fonte.get("prism:issn") or fonte.get("prism:eIssn")

    if isinstance(issn, list):
        issn = next((valor for valor in issn if valor), None)

    if not issn:
        print(f"ISSN não encontrado: {fonte.get('dc:title')}")
        return None

    issn = str(issn).replace("-", "").replace(" ", "")

    dados = consultar_api(
        f"{URL_ISSN}/{issn}",
        chave,
        {"view": "CITESCORE"},
    )

    entradas = extrair_entradas(dados)
    return entradas[0] if entradas else None


# --------------------------------------------------
# EXTRAÇÃO DO PERCENTIL
# --------------------------------------------------

def obter_ano(info):
    try:
        return int(info.get("@year", info.get("year")))
    except (TypeError, ValueError):
        return None


def obter_status(info):
    return str(
        info.get("@status", info.get("status", ""))
    ).strip().casefold()


def obter_categoria(codigo, detalhes):
    if not codigo:
        return "Não identificada"

    for area in lista(detalhes.get("subject-area")):
        codigo_area = str(
            area.get("@code", area.get("code", ""))
        )

        if codigo_area == str(codigo):
            return area.get("$", area.get("name", "Não identificada"))

    return "Não identificada"


def extrair_percentis(info_ano, detalhes):
    candidatos = []

    for item in percorrer_dicionarios(info_ano):
        valor = item.get("percentile", item.get("@percentile"))

        if valor is None:
            continue

        try:
            percentil = int(float(valor))
        except (TypeError, ValueError):
            continue

        if not 0 <= percentil <= 100:
            continue

        codigo = item.get(
            "subjectCode",
            item.get("@subjectCode", ""),
        )

        candidatos.append({
            "percentil": percentil,
            "categoria": obter_categoria(codigo, detalhes),
        })

    return candidatos


def consultar_highest_percentile(source, chave):
    fonte = localizar_fonte(source, chave)

    if fonte is None:
        return None

    detalhes = obter_detalhes(fonte, chave)

    if detalhes is None:
        print(f"Detalhes indisponíveis: {source}")
        return None

    bloco_anos = detalhes.get("citeScoreYearInfoList", {})

    if isinstance(bloco_anos, dict):
        informacoes = bloco_anos.get("citeScoreYearInfo", [])
    else:
        informacoes = bloco_anos

    anos_com_percentis = []

    for info in lista(informacoes):
        if not isinstance(info, dict):
            continue

        ano = obter_ano(info)

        if ano is None or ano > ANO_ATUAL:
            continue

        candidatos = extrair_percentis(info, detalhes)

        if candidatos:
            anos_com_percentis.append({
                "ano": ano,
                "status": obter_status(info),
                "candidatos": candidatos,
            })

    if not anos_com_percentis:
        print(f"Percentil não encontrado na API: {source}")
        return None

    concluidos = [
        registro
        for registro in anos_com_percentis
        if registro["status"] == "complete"
    ]

    disponiveis = concluidos or anos_com_percentis

    registro = max(disponiveis, key=lambda item: item["ano"])

    if not concluidos:
        print(
            f"ATENÇÃO: nenhum ano marcado como concluído para {source}; "
            "usando o ano mais recente com percentil."
        )

    melhor = max(
        registro["candidatos"],
        key=lambda item: item["percentil"],
    )

    print(
        f"{source}: Highest Percentile {melhor['percentil']}% "
        f"(ano {registro['ano']}; categoria: {melhor['categoria']})"
    )

    return {
        "source": source,
        "percentil": str(melhor["percentil"]),
        "ano": str(registro["ano"]),
        "categoria": melhor["categoria"],
    }


# --------------------------------------------------
# LEITURA DO CSV DE ARTIGOS
# --------------------------------------------------

def carregar_sources():
    with ARTIGOS.open(
        encoding="utf-8-sig",
        newline="",
    ) as arquivo:
        leitor = csv.DictReader(arquivo)

        if not leitor.fieldnames or "source" not in leitor.fieldnames:
            raise ValueError(
                "O arquivo data/artigos.csv não possui a coluna 'source'."
            )

        return sorted({
            (linha.get("source") or "").strip()
            for linha in leitor
            if (linha.get("source") or "").strip()
        })


# --------------------------------------------------
# EXECUÇÃO PRINCIPAL
# --------------------------------------------------

def main():
    load_dotenv(RAIZ / ".env")
    chave = os.getenv("ELSEVIER_API_KEY")

    if not chave:
        print("ERRO: configure ELSEVIER_API_KEY no arquivo .env.")
        return

    if not ARTIGOS.exists():
        print(f"Arquivo não encontrado: {ARTIGOS}")
        return

    sources = carregar_sources()

    if not sources:
        print("Nenhum periódico encontrado na coluna source.")
        return

    resultados = []
    falhas = 0

    for indice, source in enumerate(sources, start=1):
        print(f"\n[{indice}/{len(sources)}] Consultando: {source}")

        try:
            resultado = consultar_highest_percentile(source, chave)

            if resultado is None:
                resultado = {
                    "source": source,
                    "percentil": "",
                    "ano": "",
                    "categoria": "",
                }
                falhas += 1

            resultados.append(resultado)

        except requests.RequestException as erro:
            print(f"Erro de comunicação para {source}: {erro}")

            resultados.append({
                "source": source,
                "percentil": "",
                "ano": "",
                "categoria": "",
            })
            falhas += 1

        except (ValueError, KeyError, TypeError) as erro:
            print(f"Erro ao processar {source}: {erro}")

            resultados.append({
                "source": source,
                "percentil": "",
                "ano": "",
                "categoria": "",
            })
            falhas += 1

        time.sleep(0.5)

    with PERCENTIS.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as arquivo:
        escritor = csv.DictWriter(
            arquivo,
            fieldnames=CAMPOS,
        )
        escritor.writeheader()
        escritor.writerows(resultados)

    preenchidos = sum(
        bool(item["percentil"])
        for item in resultados
    )

    print("\n" + "=" * 50)
    print("RESUMO DA EXECUÇÃO")
    print("=" * 50)
    print(f"Arquivo atualizado: {PERCENTIS}")
    print(f"Ano da execução: {ANO_ATUAL}")
    print(f"Periódicos consultados: {len(sources)}")
    print(f"Percentis encontrados: {preenchidos}")
    print(f"Consultas sem resultado válido: {falhas}")


if __name__ == "__main__":
    main()
