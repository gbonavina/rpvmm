
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

chave = os.getenv("ELSEVIER_API_KEY")

if not chave:
    raise RuntimeError(
        "Configure ELSEVIER_API_KEY no arquivo .env."
    )

url = "https://api.elsevier.com/content/serial/title"

parametros = {
    "title": "Journal of Informetrics",
    "view": "CITESCORE",
    "count": 5,
}

cabecalhos = {
    "X-ELS-APIKey": chave,
    "Accept": "application/json",
}

try:
    resposta = requests.get(
        url,
        params=parametros,
        headers=cabecalhos,
        timeout=30,
    )

    print("Status HTTP:", resposta.status_code)

    if resposta.ok:
        dados = resposta.json()
        print(json.dumps(dados, indent=2, ensure_ascii=False))
    else:
        print("Resposta:", resposta.text[:1000])

except requests.RequestException as erro:
    print("Erro na conexão:", erro)
