"""Acrescenta a coluna 'percentil' (CiteScore, Scopus) ao data/artigos.csv.

Lê:      data/artigos.csv  e  percentis_periodicos.csv (na raiz do projeto)
Grava:   data/artigos_percentil_final.csv
O artigos.csv original NÃO é alterado.

Uso (na raiz do projeto):
    py src/percentil_scopus.py
"""
import csv
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARTIGOS = RAIZ / "data" / "artigos.csv"
PERCENTIS = RAIZ / "percentis_periodicos.csv"
SAIDA = RAIZ / "data" / "artigos_percentil_final.csv"


def carregar_percentis(caminho):
    por_periodico = {}
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            valor = (linha.get("percentil") or "").strip().replace(",", ".").replace("%", "")
            try:
                float(valor)
            except ValueError:
                continue  # vazio, "-" ou texto: ignora
            por_periodico[linha["source"].strip().casefold()] = valor
    return por_periodico


def main():
    for caminho in (ARTIGOS, PERCENTIS):
        if not caminho.exists():
            print(f"Arquivo não encontrado: {caminho}")
            return

    percentis = carregar_percentis(PERCENTIS)
    with ARTIGOS.open(encoding="utf-8", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)
        campos = list(leitor.fieldnames or [])
        linhas = list(leitor)

    if "percentil" not in campos:
        campos.append("percentil")

    com_percentil = 0
    for linha in linhas:
        fonte = (linha.get("source") or "").strip().casefold()
        linha["percentil"] = percentis.get(fonte, "")
        if linha["percentil"]:
            com_percentil += 1

    with SAIDA.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=campos)
        escritor.writeheader()
        escritor.writerows(linhas)

    print(f"Gravado em {SAIDA}")
    print(f"Percentil preenchido: {com_percentil} de {len(linhas)} artigos")


if __name__ == "__main__":
    main()
