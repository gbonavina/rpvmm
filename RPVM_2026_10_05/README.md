# RPVM — artigos de alunos com docentes (2025+)

Pacote reprodutível para identificar artigos publicados desde 2025 por alunos
do programa que tenham coautoria com um docente do programa. A coleta não
inclui alunos que publicaram sem docente, conforme a regra definida para a
plataforma de inscrição.

## Estrutura

```text
data/
  alunos.csv                    cadastro institucional de alunos
  artigos_docentes_2025.csv     artigos de docentes, com DOI e FWCI
  professors.json               docentes e seus IDs Lattes
  lattes/                       HTMLs Lattes dos docentes
scripts/
  testar_acesso_scopus.py
  extrair_orientacoes_lattes.py
  resumir_orientandos_lattes.py
  extrair_alunos_scival.py
outputs/                        resultados reproduzidos pela execução
run_pipeline.bat                execução completa no Prompt de Comando
```

Os scripts dependem apenas da biblioteca padrão do Python 3.10+.

## Procedimento reproduzível

1. Abra o Prompt de Comando na raiz deste pacote:

```bat
cd "C:\Users\lfeli\OneDrive\Área de Trabalho\RPVM\RPVM_2026_10_05"
```

2. Conecte-se à rede institucional/VPN que dá acesso ao Scopus.

3. Defina a chave somente na sessão atual. Nunca a salve em arquivos do
repositório:

```bat
set "SCOPUS_API_KEY=SUA_CHAVE"
```

4. Verifique acesso ao endpoint Scopus usado no pipeline:

```bat
python scripts\testar_acesso_scopus.py
```

5. Extraia as orientações e as supervisões de pós-doutorado dos HTMLs Lattes:

```bat
python scripts\extrair_orientacoes_lattes.py --lattes-dir ".\data\lattes" --professors ".\data\professors.json" --students ".\data\alunos.csv" --output ".\outputs\orientacoes_lattes.csv" --unmapped-output ".\outputs\lattes_sem_docente.csv" --postdocs-output ".\outputs\posdocs_lattes.csv"
```

6. Gere a lista de alunos orientados, excluindo toda pessoa identificada como
pós-doutoranda:

```bat
python scripts\resumir_orientandos_lattes.py --orientations ".\outputs\orientacoes_lattes.csv" --postdocs ".\outputs\posdocs_lattes.csv" --output ".\outputs\orientandos_lattes_consolidados.csv" --excluded-postdocs-output ".\outputs\posdocs_excluidos_lattes.csv"
```

7. Para cada DOI do artigo de docente, recupere autoria no Scopus e cruze apenas
com os alunos orientados pelo mesmo docente:

```bat
python scripts\extrair_alunos_scival.py --articles ".\data\artigos_docentes_2025.csv" --students ".\outputs\orientandos_lattes_consolidados.csv" --start-year 2025 --output ".\outputs\alunos_artigos_scival.csv" --unmatched-output ".\outputs\artigos_scival_sem_aluno.csv" --errors-output ".\outputs\artigos_scival_erros.csv"
```

Também é possível executar as etapas 4 a 7 com:

```bat
run_pipeline.bat
```

## Dataset final

O arquivo `outputs/alunos_artigos_scival.csv` tem uma linha por `aluno × artigo`
e contém DOI, EID, FWCI, citações, Author ID Scopus, posição de autoria,
primeira autoria e a evidência do vínculo de orientação Lattes.

Uma linha só entra quando as condições abaixo são atendidas:

1. O nome está no cadastro institucional.
2. O aluno é orientando do docente nos HTMLs Lattes fornecidos.
3. O aluno não consta em supervisão de pós-doutorado.
4. O nome do aluno coincide com um autor Scopus do artigo daquele docente.

`artigos_scival_sem_aluno.csv` e `artigos_scival_erros.csv` devem ser mantidos
como evidência e auditoria da execução.

## Versionamento Git

Antes de enviar ao GitHub, leia `docs/PRIVACIDADE.md`. O cadastro contém dados
pessoais; use repositório privado ou remova/anonomize campos identificáveis
antes de publicar em um repositório público.

```bat
git init
git add .
git commit -m "Pipeline reprodutivel RPVM 2026-10-05"
```
