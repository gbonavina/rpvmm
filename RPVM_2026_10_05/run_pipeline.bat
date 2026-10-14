@echo off
setlocal
cd /d "%~dp0"

if not defined SCOPUS_API_KEY (
  echo Defina SCOPUS_API_KEY nesta sessao antes de executar.
  exit /b 1
)

python scripts\testar_acesso_scopus.py
if errorlevel 1 exit /b 1

python scripts\extrair_orientacoes_lattes.py --lattes-dir ".\data\lattes" --professors ".\data\professors.json" --students ".\data\alunos.csv" --output ".\outputs\orientacoes_lattes.csv" --unmapped-output ".\outputs\lattes_sem_docente.csv" --postdocs-output ".\outputs\posdocs_lattes.csv"
if errorlevel 1 exit /b 1

python scripts\resumir_orientandos_lattes.py --orientations ".\outputs\orientacoes_lattes.csv" --postdocs ".\outputs\posdocs_lattes.csv" --output ".\outputs\orientandos_lattes_consolidados.csv" --excluded-postdocs-output ".\outputs\posdocs_excluidos_lattes.csv"
if errorlevel 1 exit /b 1

python scripts\extrair_alunos_scival.py --articles ".\data\artigos_docentes_2025.csv" --students ".\outputs\orientandos_lattes_consolidados.csv" --start-year 2025 --output ".\outputs\alunos_artigos_scival.csv" --unmatched-output ".\outputs\artigos_scival_sem_aluno.csv" --errors-output ".\outputs\artigos_scival_erros.csv"
if errorlevel 1 exit /b 1

echo Pipeline concluido. Consulte a pasta outputs.
