import os
import requests
from dotenv import load_dotenv

load_dotenv()
chave = os.getenv("ELSEVIER_API_KEY")

url = "https://api.elsevier.com/content/serial/title/4700152225"

resposta = requests.get(
    url,
    params={"view": "CITESCORE"},
    headers={
        "X-ELS-APIKey": chave,
        "Accept": "application/json",
    },
    timeout=30,
)

print("HTTP:", resposta.status_code)

if resposta.ok:
    dados = resposta.json()
    entrada = dados.get("serial-metadata-response", {}).get("entry", [])
    if isinstance(entrada, dict):
        entrada = [entrada]

    for fonte in entrada:
        print("Título:", fonte.get("dc:title"))
        print("Source ID:", fonte.get("source-id"))
        print("Anos/status:", [
            (ano.get("year"), ano.get("status"))
            for ano in fonte.get("citeScoreYearInfo", [])
        ])
else:
    print("A consulta por identificador não foi bem-sucedida.")
