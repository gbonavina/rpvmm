#!/usr/bin/env python3
"""Acrescenta FWCI do SciVal a um CSV existente, sem alterar o arquivo original."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ENDPOINT = "https://api.elsevier.com/analytics/scival/publication/metrics"
METRIC = "FieldWeightedCitationImpact"


def read_fwci(payload: object, publication_id: str) -> tuple[str, str]:
    """Aceita apenas um valor agregado explícito da métrica e do ID solicitado."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return "", "resposta_inesperada"
    matches = [r for r in payload["results"] if isinstance(r, dict)
               and isinstance(r.get("publication"), dict)
               and str(r["publication"].get("id", "")) == publication_id]
    if not matches:
        return "", "publicacao_nao_retornada"
    if len(matches) != 1 or not isinstance(matches[0].get("metrics"), list):
        return "", "resposta_inesperada"
    metrics = [m for m in matches[0]["metrics"]
               if isinstance(m, dict) and m.get("metricType") == METRIC]
    if not metrics:
        return "", "metrica_nao_retornada"
    if len(metrics) != 1:
        return "", "resposta_inesperada"
    value = metrics[0].get("value")
    if value is None:
        return "", "fwci_indisponivel" if "value" in metrics[0] else "formato_nao_reconhecido"
    if isinstance(value, bool):
        return "", "formato_nao_reconhecido"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "", "formato_nao_reconhecido"
    if not math.isfinite(numeric) or numeric < 0:
        return "", "formato_nao_reconhecido"
    return str(value), "coletado"


def fetch_fwci(publication_id: str, key: str, inst_token: str, year_range: str) -> tuple[str, str]:
    params = {"metricTypes": METRIC, "publicationIds": publication_id,
              "byYear": "false", "yearRange": year_range, "includeSelfCitations": "true"}
    headers = {"Accept": "application/json", "X-ELS-APIKey": key}
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    request = Request(ENDPOINT + "?" + urlencode(params), headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f"HTTP {exc.code}: acesso à API SciVal negado; verifique a habilitação da chave e o token institucional.") from exc
        if exc.code == 429:
            raise RuntimeError("HTTP 429: limite de consultas atingido; tente novamente mais tarde.") from exc
        return "", f"erro_http_{exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Falha de rede ao consultar o SciVal; verifique a conexão/VPN.") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("A API SciVal retornou uma resposta JSON inválida.") from exc
    return read_fwci(payload, publication_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Consulta FWCI de publicações no SciVal por scopus_id.")
    parser.add_argument("--input", required=True, type=Path, help="CSV com scopus_id e fwci.")
    parser.add_argument("--output", required=True, type=Path, help="Novo CSV; não sobrescreve arquivos.")
    parser.add_argument("--year-range", default="10yrs", choices=("3yrs", "3yrsAndCurrent", "3yrsAndCurrentAndFuture", "5yrs", "5yrsAndCurrent", "5yrsAndCurrentAndFuture", "10yrs"), help="Janela de cálculo da API; padrão 10yrs.")
    parser.add_argument("--delay", type=float, default=0.2, help="Intervalo entre consultas em segundos.")
    args = parser.parse_args()
    key = os.environ.get("SCIVAL_API_KEY") or os.environ.get("SCOPUS_API_KEY", "")
    inst_token = os.environ.get("SCIVAL_INSTTOKEN") or os.environ.get("SCOPUS_INSTTOKEN", "")
    if not key:
        parser.error("Defina SCIVAL_API_KEY ou SCOPUS_API_KEY no CMD.")
    if args.input.resolve() == args.output.resolve() or args.output.exists():
        parser.error("A saída já existe ou é igual à entrada. Escolha outro nome para não sobrescrever dados.")
    try:
        with args.input.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            fields = list(reader.fieldnames or [])
            if not {"scopus_id", "fwci"}.issubset(fields):
                parser.error("O arquivo precisa das colunas scopus_id e fwci.")
            rows = list(reader)
    except OSError as exc:
        parser.error(str(exc))

    cache: dict[str, tuple[str, str]] = {}
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        publication_id = (row.get("scopus_id") or "").strip()
        if not publication_id.isdigit():
            row["fwci_status"] = "scopus_id_ausente_ou_invalido"
            continue
        if publication_id not in cache:
            print(f"Consultando publicação {publication_id}...", flush=True)
            try:
                cache[publication_id] = fetch_fwci(publication_id, key, inst_token, args.year_range)
            except RuntimeError as exc:
                print(f"Erro: {exc} Nenhum arquivo de saída foi criado.", file=sys.stderr)
                return 1
            time.sleep(max(args.delay, 0))
        value, status = cache[publication_id]
        if status == "coletado":
            row["fwci"] = value
        row["fwci_status"] = status
        row["fwci_consulta_utc"] = timestamp
        row["fwci_fonte"] = "SciVal Publication Lookup API"
        row["fwci_janela"] = args.year_range

    for col in ("fwci_status", "fwci_consulta_utc", "fwci_fonte", "fwci_janela"):
        if col not in fields:
            fields.append(col)
    try:
        with args.output.open("x", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        print(f"Erro ao salvar: {exc}", file=sys.stderr)
        return 1
    collected = sum(row.get("fwci_status") == "coletado" for row in rows)
    print(f"Concluído: {collected}/{len(rows)} linhas com FWCI coletado; {len(cache)} publicações consultadas. Saída: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
