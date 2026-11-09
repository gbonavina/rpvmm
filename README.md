# ETL RPVMM

## Identificação de artigos com coautoria de alunos

O projeto possui:

- `data/artigos.csv`: artigos já extraídos dos Lattes, com professor e DOI;
- `ALUNOS-Completo.csv`: cadastro dos alunos;
- `data/lattes/*.html`: currículos Lattes salvos localmente.

O script `src/alunos_artigos.py` cruza essas três fontes.

### O que ele faz

1. lê os artigos e seus DOIs em `data/artigos.csv`;
2. abre os HTMLs dos Lattes já salvos em `data/lattes/`;
3. extrai os autores que aparecem em cada referência bibliográfica;
4. compara os autores com a coluna `Name` de `ALUNOS-Completo.csv`;
5. mantém somente os artigos que possuem pelo menos um aluno como coautor;
6. gera `data/artigos_com_alunos.csv`.

O resultado contém:

- `professor`
- `artigo`
- `doi`
- `aluno`
- `autor_no_lattes`
- `metodo_correspondencia`

A correspondência por iniciais só é aceita quando há um único aluno compatível, para reduzir falsos positivos.

### Executar

Na raiz do projeto:

```bash
uv run python src/alunos_artigos.py
```

ou:

```bash
uv run python main.py
```

O arquivo final será:

```text
data/artigos_com_alunos.csv
```

## Similaridade tese × artigo

`src/similaridade_teses.py` (usado por `src/measure_similarity.ipynb`) procura o
aluno de cada artigo na tabela completa de teses (`data/materiais.csv`, todos os
anos, só dissertações e teses) e calcula a similaridade de cossenos entre o
título da tese e **título + resumo** do artigo (`resumo_scopus`). A coluna
`usa_resumo` mostra se o resumo existia; `cosine_similarity_titulo` guarda a
versão só com título.

```bash
uv run python src/similaridade_teses.py \
  --artigos resultados_2021_2024/alunos_artigos_scival_2021_2024_com_fwci.csv \
  --saida resultados_2021_2024/thesis_articles_similarity_2021_2024.csv
```

## Quadriênio 2021–2024: completar os dados

`src/enriquecer_scopus.py` preenche `source`, `citations`, `fwci` (SciVal),
`qualis`, `percentil` e `resumo_scopus` a partir do DOI, gerando um novo CSV.
Requer `SCOPUS_API_KEY` (ou `ELSEVIER_API_KEY` no `.env`) e acesso à rede
institucional/VPN ou `SCOPUS_INSTTOKEN`.

```bash
uv run python src/enriquecer_scopus.py \
  --entrada resultados_2021_2024/artigos_docentes_2021_2024.csv \
  --saida resultados_2021_2024/artigos_docentes_2021_2024_enriquecido.csv
uv run python src/enriquecer_scopus.py \
  --entrada resultados_2021_2024/alunos_artigos_scival_2021_2024_com_fwci.csv \
  --saida resultados_2021_2024/alunos_artigos_scival_2021_2024_completo.csv
# opcional: resumos para a similaridade de 2025
uv run python src/enriquecer_scopus.py --sem-fwci \
  --entrada RPVM_2026_10_05/outputs/alunos_artigos_scival.csv \
  --saida RPVM_2026_10_05/outputs/alunos_artigos_scival_completo.csv
```

## Quadriênio 2021–2024: coautoria discente e perfis Scopus

Seguindo a `Eng3-Ficha.pdf` (coautoria com "discentes ou egressos do Programa";
levantamento pelo Scopus ID de titulados e docentes):

```bash
# discentes/egressos entre os autores dos artigos dos docentes (qualquer orientador)
uv run python src/coautores_discentes.py        # -> alunos_artigos_2021_2024_ampliado.csv
# produção pelos perfis Scopus de docentes e titulados (inclui eventos)
uv run python src/producao_scopus_perfis.py     # -> alunos_artigos_2021_2024_final.csv
# backtest com as faixas do Relatório CAPES 2021-2024
uv run python src/backtest_capes_2021_2024.py
```

Perfis aceitos/rejeitados ficam em `resultados_2021_2024/perfis_scopus_2021_2024.csv`
para conferência manual (homônimos).

## Modelo de otimização da nota CAPES (Engenharias III)

`src/otimizacao_capes.py` é um modelo de programação inteira mista (PuLP/CBC)
baseado em `Eng3-Ficha.pdf` e aplicado a 2021–2024 como backtest:

- **2.4.1 (IBAD/IBPD)**: atribui artigos com coautoria discente aos docentes
  (uma vaga por ano como permanente, artigo em coautoria entre docentes conta
  para só um deles, só os 60% melhores da lista por F1..F8/P1..P8 pontuam);
- **2.1.3 (IBV/IBA)**: escolhe até 7 teses e 3 dissertações com um artigo em
  que o titulado é primeiro autor e cuja similaridade com a tese é `>= --sim-min`;
- **2.1.1 (índice D)**: penaliza repetição de orientador nessas indicações;
- **2.3.1 / 2.3.2**: calculados (não há decisão do programa).

Compara o ótimo com uma linha de base sem coordenação e grava as listas em
`resultados_2021_2024/otimizacao/`. Use os arquivos `*_completo.csv` /
`*_enriquecido.csv` (detectados automaticamente) para ter percentil e IBPD/IBV.

```bash
uv run python src/otimizacao_capes.py                     # padrão
uv run python src/otimizacao_capes.py --docentes docentes_permanentes.csv --um-por-ano --fwci-max 2
```

`--docentes` recebe `docente,anos` (quantidade de anos como permanente ou lista
`2021;2022`); sem ele, todos os docentes contam 4 anos.
