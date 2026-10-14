#!/usr/bin/env python3
"""Testa acesso ao endpoint Scopus usado pelo pipeline, sem consultar o dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="Testa acesso Scopus sem exibir credenciais.")
    parser.add_argument("--api-key", help="Chave Scopus; prefira SCOPUS_API_KEY.")
    parser.add_argument("--inst-token", help="Token institucional; prefira SCOPUS_INSTTOKEN.")
    parser.add_argument("--doi", default="10.1016/S0014-5793(01)03313-0", help="DOI de teste; padrão é o exemplo oficial da Elsevier.")
    args = parser.parse_args()
    api_key = args.api_key or os.environ.get("SCOPUS_API_KEY", "")
    inst_token = args.inst_token or os.environ.get("SCOPUS_INSTTOKEN", "")
    if not api_key:
        parser.error("Defina SCOPUS_API_KEY ou use --api-key.")
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key}
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    url = "https://api.elsevier.com/content/abstract/doi/" + quote(args.doi, safe="")
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        title = payload.get("abstracts-retrieval-response", {}).get("coredata", {}).get("dc:title", "")
        print("Acesso ao Scopus Abstract Retrieval aceito.")
        print("Token institucional enviado:", "sim" if inst_token else "não")
        if title:
            print("Documento de teste localizado.")
        return 0
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"Falha de autenticação HTTP {exc.code}: {details}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Falha de rede: {exc.reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
