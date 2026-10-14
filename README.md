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
