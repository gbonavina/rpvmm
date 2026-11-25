# Aplicativo Desktop PPG-PO CAPES — Guia Rápido

Este aplicativo desktop unifica **todo o pipeline de coleta de dados, correlação semântica e otimização CAPES (Engenharias III)** do PPG-PO (UNIFESP/ITA), sem necessidade de servidor web, banco de dados externo ou edição de código.

---

## Como Abrir o Aplicativo

Basta dar **duplo-clique** no arquivo:

```text
Iniciar_Aplicativo_CAPES.bat
```

*(Na primeira execução em um computador novo, ele verifica e instala automaticamente qualquer biblioteca Python faltante e já abre a janela do programa).*

Se preferir abrir pelo terminal:
```powershell
python aplicativo_capes.py
```

---

## Passo a Passo para Qualquer Usuário (Zero Código)

1. **Configurar a API Key (`⚙️ API Key & Arquivos`):**
   * Abra a aba **⚙️ API Key & Arquivos** no menu lateral esquerdo.
   * Cole sua chave gratuita da Elsevier/Scopus (`X-ELS-APIKey`, obtida em [dev.elsevier.com](https://dev.elsevier.com/)).
   * Conecte-se ao Wi-Fi/VPN institucional da **UNIFESP** ou do **ITA** (ou preencha o campo opcional `InstToken`) para habilitar resumos completos e FWCI direto da API SciVal.
   * Clique em **🔌 Testar Conexão API Agora** para validar cada serviço em tempo real e depois em **💾 Salvar Configurações**.
   * Caso queira atualizar a planilha de alunos (`ALUNOS-Completo.csv`) ou a tabela do Qualis (`.xlsx` ou `.csv`), basta clicar em **Procurar...** na mesma tela.

2. **Executar a Coleta Automática (`🚀 Coleta Automática`):**
   * Escolha o intervalo de anos (ex.: botões rápidos **`2021–2024`** ou **`2025–2028`**, ou digite qualquer período).
   * Clique em **▶️ EXECUTAR PIPELINE COMPLETO** (ou rode apenas a etapa desejada individualmente):
     * **Passo 1 (Lattes):** Extrai artigos dos docentes no período e consolida orientandos elegíveis (excluindo pós-doutorandos). Possui também botão para abrir o navegador e baixar currículos Lattes novos com detecção automática de CAPTCHA.
     * **Passo 2 (Scopus + SciVal + CiteScore + Qualis):** Coleta citações, resumo, periódico, ISSN (com fallback automático na API pública do Crossref), **FWCI (`F1..F8`)**, **Percentil CiteScore (`P1..P8`)** e **Qualis (`A1..C`)**.
     * **Passo 3 (Discentes & Defesas):** Identifica coautorias com mestrandos, doutorandos e egressos do PPG-PO, posição de **1º autor**, **ano de defesa** e **orientador** (com opção de busca ampliada pelos Perfis Scopus).
     * **Passo 4 (Teses Sophia/ITA & Correlação Tese × Artigo):** Coleta teses/dissertações no acervo Sophia/ITA e calcula a similaridade semântica via IA (`all-MiniLM-L6-v2`) entre tese/defesa e artigo publicado.
     * **Passo 5 (Otimização CAPES):** Resolve o modelo matemático MILP (PuLP/CBC) para alocação ótima nos subitens `2.4.1`, `2.1.3` e `Índice D`.

3. **Consultar DOIs Avulsos (`🔎 Consulta Rápida DOI`):**
   * Cole um ou vários DOIs e clique em **🔍 Consultar DOIs Agora** para obter imediatamente todas as métricas CAPES e exportar para Excel.

4. **Visualizar, Filtrar e Exportar (`📊 Dados & Exportação`):**
   * Alterne entre todas as tabelas geradas, pesquise em tempo real por docente, discente, DOI, revista ou estrato, dê **duplo-clique** em qualquer linha para ler o resumo completo e abrir o link do DOI, ou clique em **📥 Exportar Tudo em Excel (.xlsx)** para gerar uma planilha consolidada com todas as abas.
