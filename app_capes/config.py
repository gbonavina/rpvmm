"""Gerenciamento de configurações persistentes e caminhos padrão do aplicativo."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parent
MAIN_BRANCH_DIR = WORKSPACE_ROOT / "branches" / "main"
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "dados_coletados"


def _first_existing(*candidates: Path) -> str:
    for path in candidates:
        if path and path.exists():
            return str(path.resolve())
    return str(candidates[0].resolve()) if candidates else ""


@dataclass
class AppConfig:
    scopus_api_key: str = ""
    scopus_inst_token: str = ""
    ppgpo_docentes_url: str = "https://ppgpo.unifesp.br/in%C3%ADcio/docentes"
    sophia_url: str = "http://www.sophia.bibl.ita.br/biblioteca/index.html"
    ano_inicio: int = 2021
    ano_fim: int = 2024
    fwci_year_range: str = "10yrs"
    sim_min: float = 0.40
    fwci_max: float = 3.0
    max_teses: int = 7
    max_dissertacoes: int = 3
    veiculo_estrato: str = "percentil"  # "percentil" ou "qualis"
    um_por_ano: bool = False
    incluir_perfis_scopus: bool = False
    sobrescrever_metricas: bool = False
    browser: str = "auto"  # "auto", "edge", "chrome", "firefox"
    api_delay: float = 0.20
    theme: str = "dark"

    alunos_csv: str = ""
    qualis_path: str = ""
    lattes_dir: str = ""
    professors_json: str = ""
    materiais_csv: str = ""
    traducoes_csv: str = ""
    percentis_csv: str = ""
    output_dir: str = ""

    def populate_defaults(self) -> None:
        """Preenche automaticamente caminhos existentes no repositório caso vazios."""
        load_dotenv(WORKSPACE_ROOT / ".env")
        load_dotenv(MAIN_BRANCH_DIR / ".env")
        load_dotenv(APP_DIR / ".env")

        if not self.scopus_api_key:
            self.scopus_api_key = (
                os.getenv("SCOPUS_API_KEY")
                or os.getenv("ELSEVIER_API_KEY")
                or ""
            )
        if not self.scopus_inst_token:
            self.scopus_inst_token = os.getenv("SCOPUS_INSTTOKEN") or ""

        if not self.alunos_csv or not Path(self.alunos_csv).exists():
            self.alunos_csv = _first_existing(
                MAIN_BRANCH_DIR / "ALUNOS-Completo.csv",
                WORKSPACE_ROOT / "ALUNOS-Completo.csv",
            )

        if not self.qualis_path or not Path(self.qualis_path).exists():
            self.qualis_path = _first_existing(
                MAIN_BRANCH_DIR / "qualis_21-24.xlsx",
                WORKSPACE_ROOT / "QUALIS 2021-2024.csv",
                WORKSPACE_ROOT / "qualis_21-24.xlsx",
            )

        if not self.lattes_dir or not Path(self.lattes_dir).exists():
            self.lattes_dir = _first_existing(
                MAIN_BRANCH_DIR / "data" / "lattes",
                WORKSPACE_ROOT / "data" / "lattes",
            )

        if not self.professors_json or not Path(self.professors_json).exists():
            self.professors_json = _first_existing(
                MAIN_BRANCH_DIR / "data" / "professors.json",
                WORKSPACE_ROOT / "data" / "professors.json",
            )

        if not self.materiais_csv or not Path(self.materiais_csv).exists():
            self.materiais_csv = _first_existing(
                MAIN_BRANCH_DIR / "data" / "materiais.csv",
                WORKSPACE_ROOT / "data" / "materiais.csv",
            )

        if not self.traducoes_csv or not Path(self.traducoes_csv).exists():
            self.traducoes_csv = _first_existing(
                MAIN_BRANCH_DIR / "data" / "traducoes_teses.csv",
                WORKSPACE_ROOT / "data" / "traducoes_teses.csv",
            )

        if not self.percentis_csv or not Path(self.percentis_csv).exists():
            self.percentis_csv = _first_existing(
                MAIN_BRANCH_DIR / "percentis_periodicos.csv",
                WORKSPACE_ROOT / "percentis_periodicos.csv",
            )

        if not self.output_dir:
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            self.output_dir = str(DEFAULT_OUTPUT_DIR.resolve())

    @classmethod
    def load(cls) -> AppConfig:
        cfg = cls()
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for key, value in raw.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except Exception:
                pass
        cfg.populate_defaults()
        return cfg

    def save(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Mantém também um arquivo .env local sincronizado para scripts auxiliares
        env_lines = [
            f"SCOPUS_API_KEY={self.scopus_api_key}",
            f"ELSEVIER_API_KEY={self.scopus_api_key}",
            f"SCOPUS_INSTTOKEN={self.scopus_inst_token}",
        ]
        (APP_DIR / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
