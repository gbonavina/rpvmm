import os
import requests
from dotenv import load_dotenv

load_dotenv()
chave = os.getenv("ELSEVIER_API_KEY")
url = "https://api.elsevier.com/content/serial/title"

for nome in ["Gestao E Producao", "Mathematics"]:
    resposta = requests.get(
        url,
        params={"title": nome, "view": "CITESCORE", "count": 5},
        headers={
            "X-ELS-APIKey": chave,
            "Accept": "application/json",
        },
        timeout=30,
    )

    print(f"\n{nome} — HTTP {resposta.status_code}")

    if resposta.ok:
        entradas = resposta.json().get(
            "serial-metadata-response", {}
        ).get("entry", [])

        for entrada in entradas:
            print("Título:", entrada.get("dc:title"))
            print("Source ID:", entrada.get("source-id"))
            print(
                "Anos/status:",
                [
                    (ano.get("year"), ano.get("status"))
                    for ano in entrada.get("citeScoreYearInfo", [])
                ],
            )
    else:
        print("A API não retornou uma resposta bem-sucedida.")
