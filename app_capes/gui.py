"""Interface Gráfica Desktop (CustomTkinter) para Coleta Bibliométrica e Otimização CAPES — PPG-PO."""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import customtkinter as ctk
import pandas as pd

from app_capes.collectors import (
    collect_student_coauthorship,
    compute_thesis_article_correlation,
    enrich_articles_full,
    extract_lattes_articles_and_orientations,
    faixa_fwci_str,
    faixa_percentil_str,
    load_students_registry,
    lookup_dois_batch,
    scrape_ppgpo_professors_and_lattes,
    scrape_sophia_theses,
    test_elsevier_connection,
    to_float,
)
from app_capes.config import MAIN_BRANCH_DIR, WORKSPACE_ROOT, AppConfig
from app_capes.optimizer import run_capes_optimization


class PPGPOCapesApp(ctk.CTk):
    """Aplicativo Desktop Completo de Coleta de Dados e Otimização CAPES (PPG-PO)."""

    def __init__(self) -> None:
        super().__init__()
        self.cfg = AppConfig.load()

        ctk.set_appearance_mode(self.cfg.theme if self.cfg.theme in ("dark", "light") else "dark")
        ctk.set_default_color_theme("blue")

        self.title("PPG-PO CAPES — Coletor Bibliométrico & Otimizador (Scopus • SciVal • Qualis • Lattes • Sophia)")
        self.geometry("1380x840")
        self.minsize(1120, 700)

        self.ui_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.datasets: dict[str, pd.DataFrame] = {}
        self.current_dataset_key: str = "docentes"
        self.current_filtered_df: pd.DataFrame = pd.DataFrame()
        self.quick_doi_df: pd.DataFrame = pd.DataFrame()
        self.last_opt_result: dict | None = None

        self._configure_treeview_style()
        self._build_layout()
        self._bootstrap_existing_data()
        self._refresh_dashboard_and_table()
        self.after(120, self._poll_ui_queue)

    # ==========================================================================
    # Estilo Visual das Tabelas
    # ==========================================================================

    def _configure_treeview_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Capes.Treeview",
            background="#1e222a",
            foreground="#e8ecf2",
            fieldbackground="#1e222a",
            rowheight=28,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Capes.Treeview.Heading",
            background="#2b313c",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 10),
            padding=(6, 6),
        )
        style.map(
            "Capes.Treeview",
            background=[("selected", "#2563eb")],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "Capes.Treeview.Heading",
            background=[("active", "#374151")],
        )

    # ==========================================================================
    # Layout Principal (Sidebar + Páginas)
    # ==========================================================================

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar Esquerda ---
        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color=("#e5e7eb", "#14171d"))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(8, weight=1)

        title_lbl = ctk.CTkLabel(
            self.sidebar,
            text="PPG-PO CAPES",
            font=ctk.CTkFont(family="Segoe UI", size=21, weight="bold"),
        )
        title_lbl.grid(row=0, column=0, padx=20, pady=(22, 2), sticky="w")

        sub_lbl = ctk.CTkLabel(
            self.sidebar,
            text="Coleta Scopus • FWCI • Qualis\nDefesas & Correlação Tese×Artigo",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
            justify="left",
        )
        sub_lbl.grid(row=1, column=0, padx=20, pady=(0, 18), sticky="w")

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("explorer", "📊  Dados & Exportação"),
            ("pipeline", "🚀  Coleta Automática"),
            ("quick_doi", "🔎  Consulta Rápida DOI"),
            ("optimizer", "🏆  Otimizador CAPES"),
            ("settings", "⚙️  API Key & Arquivos"),
        ]
        for idx, (key, label) in enumerate(nav_items, start=2):
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                anchor="w",
                height=42,
                corner_radius=8,
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="transparent",
                text_color=("gray15", "gray90"),
                hover_color=("gray75", "#262c36"),
                command=lambda k=key: self._select_page(k),
            )
            btn.grid(row=idx, column=0, padx=14, pady=4, sticky="ew")
            self.nav_buttons[key] = btn

        # Badge de Status da API Key na Sidebar
        self.status_card = ctk.CTkFrame(self.sidebar, corner_radius=10, fg_color=("#d1d5db", "#1f242d"))
        self.status_card.grid(row=9, column=0, padx=14, pady=10, sticky="ew")

        self.api_badge_lbl = ctk.CTkLabel(
            self.status_card,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            justify="left",
        )
        self.api_badge_lbl.pack(padx=12, pady=(10, 4), anchor="w")

        self.period_badge_lbl = ctk.CTkLabel(
            self.status_card,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "gray70"),
            justify="left",
        )
        self.period_badge_lbl.pack(padx=12, pady=(0, 10), anchor="w")

        open_out_btn = ctk.CTkButton(
            self.sidebar,
            text="📂 Abrir Pasta de Saída",
            height=34,
            fg_color=("#3b82f6", "#2563eb"),
            command=self._open_output_folder,
        )
        open_out_btn.grid(row=10, column=0, padx=14, pady=(2, 16), sticky="ew")

        # --- Container Principal ---
        self.container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.container.grid(row=0, column=1, sticky="nsew", padx=18, pady=14)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(0, weight=1)

        self.pages: dict[str, ctk.CTkFrame] = {}
        self._build_explorer_page()
        self._build_pipeline_page()
        self._build_quick_doi_page()
        self._build_optimizer_page()
        self._build_settings_page()

        self._update_sidebar_badges()
        self._select_page("explorer")

    def _select_page(self, page_key: str) -> None:
        for k, frame in self.pages.items():
            if k == page_key:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_forget()
        for k, btn in self.nav_buttons.items():
            btn.configure(
                fg_color=("#2563eb", "#2563eb") if k == page_key else "transparent",
                text_color="#ffffff" if k == page_key else ("gray15", "gray90"),
            )

    def _update_sidebar_badges(self) -> None:
        has_key = bool(self.cfg.scopus_api_key.strip())
        self.api_badge_lbl.configure(
            text="🟢 API Key Configurada" if has_key else "🟡 Sem API Key (Configure em ⚙️)",
            text_color=("#15803d", "#4ade80") if has_key else ("#b45309", "#facc15"),
        )
        self.period_badge_lbl.configure(
            text=f"Período ativo: {self.cfg.ano_inicio}–{self.cfg.ano_fim}\nVeículo: {self.cfg.veiculo_estrato.capitalize()}"
        )

    # ==========================================================================
    # Página 1: Explorador de Dados & Exportação
    # ==========================================================================

    def _build_explorer_page(self) -> None:
        page = ctk.CTkFrame(self.container, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)
        self.pages["explorer"] = page

        # Cabeçalho + Botões de Exportação
        hdr = ctk.CTkFrame(page, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr,
            text="Painel de Dados Coletados & Exportação",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        btn_box = ctk.CTkFrame(hdr, fg_color="transparent")
        btn_box.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            btn_box,
            text="🔄 Recarregar Tabelas",
            width=150,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._reload_all_from_disk,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            btn_box,
            text="📄 Exportar Aba (.csv)",
            width=155,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._export_current_csv,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            btn_box,
            text="📥 Exportar Tudo em Excel (.xlsx)",
            width=220,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._export_all_excel,
        ).pack(side="left", padx=5)

        # Cards KPI
        kpi_frame = ctk.CTkFrame(page, fg_color="transparent")
        kpi_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for i in range(6):
            kpi_frame.grid_columnconfigure(i, weight=1)

        self.kpi_labels: dict[str, ctk.CTkLabel] = {}
        kpi_defs = [
            ("docentes", "Artigos Docentes"),
            ("a1_p1", "Estrato A1 / P1"),
            ("fwci", "Média FWCI"),
            ("discentes", "Vínculos Discentes"),
            ("correlacao", "Pares Tese × Artigo"),
            ("nota_opt", "Quesito 2 (Ótimo)"),
        ]
        for idx, (k_id, k_title) in enumerate(kpi_defs):
            card = ctk.CTkFrame(kpi_frame, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
            card.grid(row=0, column=idx, padx=4, sticky="ew")
            ctk.CTkLabel(
                card, text=k_title, font=ctk.CTkFont(size=11), text_color=("gray40", "gray70")
            ).pack(padx=10, pady=(8, 0), anchor="w")
            val_lbl = ctk.CTkLabel(
                card, text="-", font=ctk.CTkFont(size=19, weight="bold")
            )
            val_lbl.pack(padx=10, pady=(2, 8), anchor="w")
            self.kpi_labels[k_id] = val_lbl

        # Seletor de Tabela + Busca Instantânea
        toolbar = ctk.CTkFrame(page, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        toolbar.grid_columnconfigure(1, weight=1)

        self.dataset_options_map = {
            "Artigos Docentes (Scopus + FWCI + Qualis + Percentil)": "docentes",
            "Coautoria Discente/Egresso & Defesas": "discentes",
            "Correlação Tese/Defesa × Artigo (NLP)": "correlacao",
            "Teses & Dissertações (Sophia/ITA)": "teses",
            "Periódicos & Percentis CiteScore": "periodicos",
            "Alocação Ótima CAPES (2.4.1)": "opt_241",
            "Indicações Teses/Dissertações (2.1.3)": "opt_213",
        }
        self.dataset_menu = ctk.CTkOptionMenu(
            toolbar,
            values=list(self.dataset_options_map.keys()),
            width=370,
            height=34,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_dataset_selected,
        )
        self.dataset_menu.grid(row=0, column=0, padx=12, pady=10, sticky="w")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_table_filter())
        search_entry = ctk.CTkEntry(
            toolbar,
            textvariable=self.search_var,
            placeholder_text="🔎 Filtrar por professor, aluno, título, DOI, periódico, Qualis, ano de defesa...",
            height=34,
        )
        search_entry.grid(row=0, column=1, padx=8, pady=10, sticky="ew")

        self.row_count_lbl = ctk.CTkLabel(
            toolbar,
            text="0 registros  (Duplo-clique na linha para ver resumo completo)",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
        )
        self.row_count_lbl.grid(row=0, column=2, padx=14, pady=10, sticky="e")

        # Tabela Treeview com Scrollbars
        table_container = ctk.CTkFrame(page, corner_radius=10, fg_color="#1e222a")
        table_container.grid(row=3, column=0, sticky="nsew")
        table_container.grid_columnconfigure(0, weight=1)
        table_container.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_container, style="Capes.Treeview", show="headings")
        vsb = ttk.Scrollbar(table_container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=(4, 0))
        vsb.grid(row=0, column=1, sticky="ns", pady=(4, 0))
        hsb.grid(row=1, column=0, sticky="ew", padx=(4, 0))
        self.tree.bind("<Double-1>", self._on_row_double_click)

    # ==========================================================================
    # Página 2: Central de Coleta Automática (Pipeline Completo ou por Etapa)
    # ==========================================================================

    def _build_pipeline_page(self) -> None:
        page = ctk.CTkFrame(self.container, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)
        self.pages["pipeline"] = page

        # Topo: Período + Execução Completa
        top_bar = ctk.CTkFrame(page, corner_radius=12, fg_color=("#f3f4f6", "#1e222a"))
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top_bar.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(
            top_bar, text="Período da Coleta:", font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=0, column=0, padx=(16, 6), pady=14)

        self.pipe_ini_entry = ctk.CTkEntry(top_bar, width=70, justify="center")
        self.pipe_ini_entry.insert(0, str(self.cfg.ano_inicio))
        self.pipe_ini_entry.grid(row=0, column=1, padx=4, pady=14)

        ctk.CTkLabel(top_bar, text="até").grid(row=0, column=2, padx=4, pady=14)

        self.pipe_fim_entry = ctk.CTkEntry(top_bar, width=70, justify="center")
        self.pipe_fim_entry.insert(0, str(self.cfg.ano_fim))
        self.pipe_fim_entry.grid(row=0, column=3, padx=4, pady=14)

        presets = ctk.CTkFrame(top_bar, fg_color="transparent")
        presets.grid(row=0, column=4, padx=10, pady=14)
        ctk.CTkButton(
            presets, text="2021–2024", width=86, height=30, fg_color="#374151",
            command=lambda: self._set_period_preset(2021, 2024),
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            presets, text="2025–2028", width=86, height=30, fg_color="#374151",
            command=lambda: self._set_period_preset(2025, 2028),
        ).pack(side="left", padx=3)

        self.chk_profiles_var = tk.BooleanVar(value=self.cfg.incluir_perfis_scopus)
        ctk.CTkCheckBox(
            top_bar,
            text="Incluir busca ampliada por Perfis Scopus (eventos/egressos)",
            variable=self.chk_profiles_var,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=5, padx=10, pady=14, sticky="w")

        run_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        run_box.grid(row=0, column=6, padx=14, pady=14, sticky="e")

        self.btn_run_all = ctk.CTkButton(
            run_box,
            text="▶️ EXECUTAR PIPELINE COMPLETO",
            height=40,
            width=255,
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_full_pipeline,
        )
        self.btn_run_all.pack(side="left", padx=5)

        self.btn_stop = ctk.CTkButton(
            run_box,
            text="⏹ Parar",
            height=40,
            width=90,
            fg_color="#dc2626",
            hover_color="#b91c1c",
            state="disabled",
            command=self._request_stop,
        )
        self.btn_stop.pack(side="left", padx=4)

        # Grade de Etapas Individuais
        steps_grid = ctk.CTkFrame(page, fg_color="transparent")
        steps_grid.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for c in range(5):
            steps_grid.grid_columnconfigure(c, weight=1)

        stages = [
            (
                "1. Currículos Lattes",
                "Extrai artigos e orientações (excluindo pós-docs) dos HTMLs do Lattes.",
                "Extrair dos HTMLs",
                self._start_stage_lattes_parse,
                "Baixar Novos (Web)",
                self._start_stage_lattes_web,
            ),
            (
                "2. Scopus + FWCI + Qualis",
                "Coleta citações, resumo, FWCI SciVal (F1..F8), Percentil (P1..P8) e Qualis.",
                "Coletar Métricas",
                self._start_stage_enrich,
                None,
                None,
            ),
            (
                "3. Discentes & Defesas",
                "Cruza coautores com cadastro de alunos/egressos, 1º autor e ano de defesa.",
                "Mapear Coautorias",
                self._start_stage_students,
                None,
                None,
            ),
            (
                "4. Teses & Correlação",
                "Cruza teses Sophia/ITA com artigos via IA (MiniLM) gerando correlação.",
                "Calcular Correlação",
                self._start_stage_correlation,
                "Raspar Sophia/ITA",
                self._start_stage_sophia_web,
            ),
            (
                "5. Otimização CAPES",
                "Resolve o modelo MILP para maximizar os itens 2.4.1, 2.1.3 e Índice D.",
                "Otimizar Nota",
                self._start_stage_optimize,
                None,
                None,
            ),
        ]

        for col_i, (st_title, st_desc, b1_txt, b1_cmd, b2_txt, b2_cmd) in enumerate(stages):
            card = ctk.CTkFrame(steps_grid, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
            card.grid(row=0, column=col_i, padx=4, sticky="nsew")
            ctk.CTkLabel(
                card, text=st_title, font=ctk.CTkFont(size=13, weight="bold")
            ).pack(padx=12, pady=(10, 4), anchor="w")
            ctk.CTkLabel(
                card,
                text=st_desc,
                font=ctk.CTkFont(size=11),
                text_color=("gray35", "gray70"),
                wraplength=215,
                justify="left",
            ).pack(padx=12, pady=(0, 10), anchor="w")
            ctk.CTkButton(
                card, text=b1_txt, height=32, command=b1_cmd
            ).pack(padx=12, pady=(0, 6), fill="x")
            if b2_txt and b2_cmd:
                ctk.CTkButton(
                    card, text=b2_txt, height=28, fg_color="#374151", hover_color="#4b5563", command=b2_cmd
                ).pack(padx=12, pady=(0, 10), fill="x")
            else:
                ctk.CTkLabel(card, text="", height=28).pack(pady=(0, 10))

        # Barra de Progresso
        prog_frame = ctk.CTkFrame(page, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
        prog_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        prog_frame.grid_columnconfigure(0, weight=1)

        self.prog_status_lbl = ctk.CTkLabel(
            prog_frame,
            text="Pronto para iniciar coleta.",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.prog_status_lbl.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")

        self.progress_bar = ctk.CTkProgressBar(prog_frame, height=12)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="ew")

        # Console de Logs em Tempo Real
        log_frame = ctk.CTkFrame(page, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            log_frame, text="Console de Execução em Tempo Real", font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")

        self.log_box = ctk.CTkTextbox(
            log_frame, font=ctk.CTkFont(family="Consolas", size=12), wrap="word"
        )
        self.log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

    # ==========================================================================
    # Página 3: Consulta Rápida Avulsa por DOI
    # ==========================================================================

    def _build_quick_doi_page(self) -> None:
        page = ctk.CTkFrame(self.container, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        self.pages["quick_doi"] = page

        ctk.CTkLabel(
            page,
            text="Consulta Rápida Avulsa por DOI (Scopus + FWCI + Percentil + Qualis)",
            font=ctk.CTkFont(size=21, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        inp_card = ctk.CTkFrame(page, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
        inp_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        inp_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            inp_card,
            text="Cole abaixo um ou mais DOIs (um por linha, vírgula ou espaço) e clique em Consultar:",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 4), sticky="w")

        self.doi_input_box = ctk.CTkTextbox(inp_card, height=95, font=ctk.CTkFont(family="Consolas", size=12))
        self.doi_input_box.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="ew")
        self.doi_input_box.insert(
            "1.0",
            "10.1016/j.ejor.2024.03.030\n10.1016/j.cor.2025.107006\n10.1109/access.2025.3559772",
        )

        act_col = ctk.CTkFrame(inp_card, fg_color="transparent")
        act_col.grid(row=1, column=1, padx=(0, 14), pady=(0, 12), sticky="ns")

        ctk.CTkButton(
            act_col,
            text="🔍 Consultar DOIs Agora",
            width=200,
            height=38,
            fg_color="#2563eb",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._run_quick_doi_lookup,
        ).pack(pady=(0, 8))

        ctk.CTkButton(
            act_col,
            text="📥 Exportar Resultado (.xlsx)",
            width=200,
            height=34,
            fg_color="#16a34a",
            command=self._export_quick_doi_excel,
        ).pack()

        res_card = ctk.CTkFrame(page, corner_radius=10, fg_color="#1e222a")
        res_card.grid(row=2, column=0, sticky="nsew")
        res_card.grid_columnconfigure(0, weight=1)
        res_card.grid_rowconfigure(0, weight=1)

        self.quick_tree = ttk.Treeview(res_card, style="Capes.Treeview", show="headings")
        q_vsb = ttk.Scrollbar(res_card, orient="vertical", command=self.quick_tree.yview)
        q_hsb = ttk.Scrollbar(res_card, orient="horizontal", command=self.quick_tree.xview)
        self.quick_tree.configure(yscrollcommand=q_vsb.set, xscrollcommand=q_hsb.set)
        self.quick_tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=(4, 0))
        q_vsb.grid(row=0, column=1, sticky="ns", pady=(4, 0))
        q_hsb.grid(row=1, column=0, sticky="ew", padx=(4, 0))
        self.quick_tree.bind("<Double-1>", self._on_quick_row_double_click)

    # ==========================================================================
    # Página 4: Otimizador Interativo CAPES (Engenharias III)
    # ==========================================================================

    def _build_optimizer_page(self) -> None:
        page = ctk.CTkFrame(self.container, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        self.pages["optimizer"] = page

        ctk.CTkLabel(
            page,
            text="Modelo de Otimização Matemática da Nota CAPES (Quesito 2 — Engenharias III)",
            font=ctk.CTkFont(size=21, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        ctrl = ctk.CTkFrame(page, corner_radius=10, fg_color=("#f3f4f6", "#1e222a"))
        ctrl.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        ctk.CTkLabel(ctrl, text="Similaridade Mín. Tese×Artigo:").grid(row=0, column=0, padx=(14, 4), pady=12, sticky="w")
        self.opt_sim_entry = ctk.CTkEntry(ctrl, width=65, justify="center")
        self.opt_sim_entry.insert(0, f"{self.cfg.sim_min:.2f}")
        self.opt_sim_entry.grid(row=0, column=1, padx=4, pady=12)

        ctk.CTkLabel(ctrl, text="Teto FWCI (IBA):").grid(row=0, column=2, padx=(12, 4), pady=12, sticky="w")
        self.opt_fwci_entry = ctk.CTkEntry(ctrl, width=65, justify="center")
        self.opt_fwci_entry.insert(0, f"{self.cfg.fwci_max:.1f}")
        self.opt_fwci_entry.grid(row=0, column=3, padx=4, pady=12)

        ctk.CTkLabel(ctrl, text="Estrato do Periódico:").grid(row=0, column=4, padx=(12, 4), pady=12, sticky="w")
        self.opt_veic_menu = ctk.CTkOptionMenu(
            ctrl, values=["percentil", "qualis"], width=120
        )
        self.opt_veic_menu.set(self.cfg.veiculo_estrato)
        self.opt_veic_menu.grid(row=0, column=5, padx=4, pady=12)

        self.opt_um_ano_var = tk.BooleanVar(value=self.cfg.um_por_ano)
        ctk.CTkCheckBox(
            ctrl, text="1 artigo/ano por docente", variable=self.opt_um_ano_var
        ).grid(row=0, column=6, padx=12, pady=12)

        ctk.CTkButton(
            ctrl,
            text="⚡ Calcular Alocação Ótima",
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=36,
            command=self._run_interactive_optimizer,
        ).grid(row=0, column=7, padx=14, pady=12)

        # Painel de Resultado Comparativo
        self.opt_summary_box = ctk.CTkTextbox(
            page, font=ctk.CTkFont(family="Consolas", size=13), wrap="none"
        )
        self.opt_summary_box.grid(row=2, column=0, sticky="nsew")

    # ==========================================================================
    # Página 5: Configurações, API Key & Arquivos
    # ==========================================================================

    def _build_settings_page(self) -> None:
        page = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        self.pages["settings"] = page

        ctk.CTkLabel(
            page,
            text="Configurações de API Key, Parâmetros e Caminhos",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        # Card 1: Credenciais Elsevier
        api_card = ctk.CTkFrame(page, corner_radius=12, fg_color=("#f3f4f6", "#1e222a"))
        api_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        api_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            api_card,
            text="🔑 Credenciais Elsevier (Scopus / SciVal / CiteScore)",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(14, 4), sticky="w")

        ctk.CTkLabel(
            api_card,
            text="Insira sua API Key gratuita do portal Elsevier Developer. Conectado à rede/VPN da UNIFESP ou ITA, o acesso completo é liberado automaticamente.",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
        ).grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 10), sticky="w")

        ctk.CTkLabel(api_card, text="Scopus / Elsevier API Key:").grid(row=2, column=0, padx=16, pady=8, sticky="w")
        self.api_key_entry = ctk.CTkEntry(api_card, show="•", placeholder_text="Cole aqui sua chave X-ELS-APIKey...")
        self.api_key_entry.insert(0, self.cfg.scopus_api_key)
        self.api_key_entry.grid(row=2, column=1, padx=8, pady=8, sticky="ew")

        self.show_key_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            api_card,
            text="Mostrar",
            variable=self.show_key_var,
            width=80,
            command=lambda: self.api_key_entry.configure(show="" if self.show_key_var.get() else "•"),
        ).grid(row=2, column=2, padx=14, pady=8)

        ctk.CTkLabel(api_card, text="InstToken (Opcional fora da VPN):").grid(row=3, column=0, padx=16, pady=8, sticky="w")
        self.inst_token_entry = ctk.CTkEntry(api_card, show="•", placeholder_text="Deixe em branco se estiver na rede/VPN UNIFESP ou ITA")
        self.inst_token_entry.insert(0, self.cfg.scopus_inst_token)
        self.inst_token_entry.grid(row=3, column=1, padx=8, pady=8, sticky="ew")

        api_btns = ctk.CTkFrame(api_card, fg_color="transparent")
        api_btns.grid(row=4, column=0, columnspan=3, padx=16, pady=(6, 10), sticky="w")

        ctk.CTkButton(
            api_btns,
            text="💾 Salvar Configurações",
            fg_color="#16a34a",
            hover_color="#15803d",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._save_settings_from_ui,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            api_btns,
            text="🔌 Testar Conexão API Agora",
            fg_color="#2563eb",
            command=self._test_api_clicked,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            api_btns,
            text="🌐 Gerar Chave no Portal Elsevier (dev.elsevier.com)",
            fg_color="#374151",
            hover_color="#4b5563",
            command=lambda: webbrowser.open("https://dev.elsevier.com/"),
        ).pack(side="left")

        self.api_diag_lbl = ctk.CTkLabel(
            api_card,
            text="",
            font=ctk.CTkFont(family="Consolas", size=12),
            justify="left",
        )
        self.api_diag_lbl.grid(row=5, column=0, columnspan=3, padx=16, pady=(2, 14), sticky="w")

        # Card 2: Arquivos de Entrada e Pasta de Saída
        files_card = ctk.CTkFrame(page, corner_radius=12, fg_color=("#f3f4f6", "#1e222a"))
        files_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        files_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            files_card,
            text="📁 Bases de Entrada & Pasta de Saída (Sem precisar mexer em código)",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(14, 10), sticky="w")

        self.path_entries: dict[str, ctk.CTkEntry] = {}
        file_rows = [
            ("alunos_csv", "Cadastro de Alunos e Defesas (CSV):", False),
            ("qualis_path", "Planilha Qualis CAPES (.xlsx ou .csv):", False),
            ("lattes_dir", "Pasta de Currículos Lattes (.html):", True),
            ("materiais_csv", "Base de Teses Sophia/ITA (CSV):", False),
            ("output_dir", "Pasta de Saída dos Dados Coletados:", True),
        ]
        for idx, (attr_name, label_txt, is_dir) in enumerate(file_rows, start=1):
            ctk.CTkLabel(files_card, text=label_txt).grid(row=idx, column=0, padx=16, pady=6, sticky="w")
            ent = ctk.CTkEntry(files_card)
            ent.insert(0, getattr(self.cfg, attr_name, ""))
            ent.grid(row=idx, column=1, padx=8, pady=6, sticky="ew")
            self.path_entries[attr_name] = ent
            ctk.CTkButton(
                files_card,
                text="Procurar...",
                width=105,
                fg_color="#374151",
                command=lambda a=attr_name, d=is_dir: self._browse_path(a, d),
            ).grid(row=idx, column=2, padx=14, pady=6)

        # Navegador e URL PPG-PO
        ctk.CTkLabel(files_card, text="URL Docentes PPG-PO:").grid(row=6, column=0, padx=16, pady=6, sticky="w")
        self.url_ppgpo_entry = ctk.CTkEntry(files_card)
        self.url_ppgpo_entry.insert(0, self.cfg.ppgpo_docentes_url)
        self.url_ppgpo_entry.grid(row=6, column=1, padx=8, pady=6, sticky="ew")

        ctk.CTkLabel(files_card, text="Navegador Web Scraping:").grid(row=7, column=0, padx=16, pady=(6, 14), sticky="w")
        self.browser_menu = ctk.CTkOptionMenu(
            files_card, values=["auto", "edge", "chrome", "firefox"], width=160
        )
        self.browser_menu.set(self.cfg.browser)
        self.browser_menu.grid(row=7, column=1, padx=8, pady=(6, 14), sticky="w")

    # ==========================================================================
    # Inicialização Inteligente das Bases Existentes
    # ==========================================================================

    def _bootstrap_existing_data(self) -> None:
        """Garante que o aplicativo já abra populado usando as bases existentes no projeto."""
        out_dir = Path(self.cfg.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Copia percentis_periodicos.csv inicial se ainda não existir na pasta de saída
        perc_dest = out_dir / "percentis_periodicos.csv"
        if not perc_dest.exists() and self.cfg.percentis_csv and Path(self.cfg.percentis_csv).exists():
            shutil.copy2(self.cfg.percentis_csv, perc_dest)

        # Copia teses_dissertacoes_sophia.csv inicial se ainda não existir
        teses_dest = out_dir / "teses_dissertacoes_sophia.csv"
        if not teses_dest.exists() and self.cfg.materiais_csv and Path(self.cfg.materiais_csv).exists():
            shutil.copy2(self.cfg.materiais_csv, teses_dest)

        # Semeia arquivos de 2021-2024 da branch main caso o usuário abra nesse período pela 1ª vez
        doc_2124 = out_dir / "artigos_docentes_2021_2024_enriquecido.csv"
        src_doc_2124 = MAIN_BRANCH_DIR / "resultados_2021_2024" / "artigos_docentes_2021_2024_enriquecido.csv"
        if not doc_2124.exists() and src_doc_2124.exists():
            df_d = pd.read_csv(src_doc_2124, encoding="utf-8-sig", dtype=str).fillna("")
            df_d["faixa_F"] = df_d.get("fwci", pd.Series([""] * len(df_d))).map(faixa_fwci_str)
            df_d["faixa_P"] = df_d.get("percentil", pd.Series([""] * len(df_d))).map(faixa_percentil_str)
            df_d.to_csv(doc_2124, index=False, encoding="utf-8-sig")

        al_2124 = out_dir / "alunos_artigos_2021_2024.csv"
        src_al_2124 = MAIN_BRANCH_DIR / "resultados_2021_2024" / "alunos_artigos_2021_2024_final.csv"
        if not al_2124.exists() and src_al_2124.exists():
            df_a = pd.read_csv(src_al_2124, encoding="utf-8-sig", dtype=str).fillna("")
            df_a["faixa_F"] = df_a.get("fwci", pd.Series([""] * len(df_a))).map(faixa_fwci_str)
            df_a["faixa_P"] = df_a.get("percentil", pd.Series([""] * len(df_a))).map(faixa_percentil_str)
            if self.cfg.alunos_csv and Path(self.cfg.alunos_csv).exists():
                reg = load_students_registry(self.cfg.alunos_csv).set_index("nome")
                df_a["ano_defesa"] = df_a["aluno"].map(
                    lambda n: str(int(reg.loc[n, "ano_defesa"])) if n in reg.index and pd.notna(reg.loc[n, "ano_defesa"]) else ""
                )
                df_a["orientador"] = df_a["aluno"].map(
                    lambda n: str(reg.loc[n, "orientador"]) if n in reg.index else ""
                )
            df_a.to_csv(al_2124, index=False, encoding="utf-8-sig")

        corr_2124 = out_dir / "correlacao_teses_artigos_2021_2024.csv"
        src_corr_2124 = MAIN_BRANCH_DIR / "resultados_2021_2024" / "thesis_articles_similarity_2021_2024.csv"
        if not corr_2124.exists() and src_corr_2124.exists():
            df_c = pd.read_csv(src_corr_2124, encoding="utf-8-sig", dtype=str).fillna("")
            df_c["associado_tese"] = df_c.get("cosine_similarity", pd.Series(["0"] * len(df_c))).map(
                lambda v: (to_float(v) or 0.0) >= self.cfg.sim_min
            )
            if self.cfg.alunos_csv and Path(self.cfg.alunos_csv).exists():
                reg = load_students_registry(self.cfg.alunos_csv).set_index("nome")
                df_c["ano_defesa"] = df_c["autor"].map(
                    lambda n: str(int(reg.loc[n, "ano_defesa"])) if n in reg.index and pd.notna(reg.loc[n, "ano_defesa"]) else ""
                )
                df_c["orientador"] = df_c["autor"].map(
                    lambda n: str(reg.loc[n, "orientador"]) if n in reg.index else ""
                )
            df_c.to_csv(corr_2124, index=False, encoding="utf-8-sig")

        # Semeia também 2025–2028 com os arquivos já coletados na main (data/artigos_percentil_final.csv)
        doc_2528 = out_dir / "artigos_docentes_2025_2028_enriquecido.csv"
        src_doc_25 = MAIN_BRANCH_DIR / "data" / "artigos_percentil_final.csv"
        if not doc_2528.exists() and src_doc_25.exists():
            df_25 = pd.read_csv(src_doc_25, encoding="utf-8-sig", dtype=str).fillna("")
            df_25["faixa_F"] = df_25.get("fwci", pd.Series([""] * len(df_25))).map(faixa_fwci_str)
            df_25["faixa_P"] = df_25.get("percentil", pd.Series([""] * len(df_25))).map(faixa_percentil_str)
            df_25.to_csv(doc_2528, index=False, encoding="utf-8-sig")

        al_2528 = out_dir / "alunos_artigos_2025_2028.csv"
        src_al_25 = MAIN_BRANCH_DIR / "RPVM_2026_10_05" / "outputs" / "alunos_artigos_scival_completo.csv"
        if not al_2528.exists() and src_al_25.exists():
            df_a25 = pd.read_csv(src_al_25, encoding="utf-8-sig", dtype=str).fillna("")
            df_a25["faixa_F"] = df_a25.get("fwci", pd.Series([""] * len(df_a25))).map(faixa_fwci_str)
            df_a25["faixa_P"] = df_a25.get("percentil", pd.Series([""] * len(df_a25))).map(faixa_percentil_str)
            df_a25.to_csv(al_2528, index=False, encoding="utf-8-sig")

        corr_2528 = out_dir / "correlacao_teses_artigos_2025_2028.csv"
        src_corr_25 = MAIN_BRANCH_DIR / "data" / "thesis_articles_similarity.csv"
        if not corr_2528.exists() and src_corr_25.exists():
            shutil.copy2(src_corr_25, corr_2528)

        self._load_datasets_into_memory()

    def _load_datasets_into_memory(self) -> None:
        out_dir = Path(self.cfg.output_dir)
        ini, fim = self.cfg.ano_inicio, self.cfg.ano_fim

        def read_safe(*paths: Path) -> pd.DataFrame:
            for p in paths:
                if p and p.exists():
                    try:
                        return pd.read_csv(p, encoding="utf-8-sig", dtype=str).fillna("")
                    except Exception:
                        continue
            return pd.DataFrame()

        self.datasets["docentes"] = read_safe(
            out_dir / f"artigos_docentes_{ini}_{fim}_enriquecido.csv",
            out_dir / f"artigos_docentes_{ini}_{fim}.csv",
        )
        self.datasets["discentes"] = read_safe(
            out_dir / f"alunos_artigos_{ini}_{fim}.csv",
        )
        self.datasets["correlacao"] = read_safe(
            out_dir / f"correlacao_teses_artigos_{ini}_{fim}.csv",
        )
        self.datasets["teses"] = read_safe(
            out_dir / "teses_dissertacoes_sophia.csv",
            Path(self.cfg.materiais_csv) if self.cfg.materiais_csv else Path(),
        )
        self.datasets["periodicos"] = read_safe(
            out_dir / "percentis_periodicos.csv",
            Path(self.cfg.percentis_csv) if self.cfg.percentis_csv else Path(),
        )
        if not self.datasets["periodicos"].empty and "faixa_P" not in self.datasets["periodicos"].columns:
            self.datasets["periodicos"]["faixa_P"] = self.datasets["periodicos"]["percentil"].map(faixa_percentil_str)

        opt_folder = out_dir / f"otimizacao_{ini}_{fim}_{self.cfg.veiculo_estrato}"
        fallback_opt = MAIN_BRANCH_DIR / "resultados_2021_2024" / "otimizacao"
        self.datasets["opt_241"] = read_safe(
            opt_folder / "lista_2_4_1.csv",
            fallback_opt / "lista_2_4_1.csv" if (ini, fim) == (2021, 2024) else Path(),
        )
        self.datasets["opt_213"] = read_safe(
            opt_folder / "indicacoes_2_1_3.csv",
            fallback_opt / "indicacoes_2_1_3.csv" if (ini, fim) == (2021, 2024) else Path(),
        )
        self.datasets["opt_resumo"] = read_safe(
            opt_folder / "resumo.csv",
            fallback_opt / "resumo.csv" if (ini, fim) == (2021, 2024) else Path(),
        )

    # ==========================================================================
    # Atualização do Painel e da Tabela Interativa
    # ==========================================================================

    def _refresh_dashboard_and_table(self) -> None:
        df_doc = self.datasets.get("docentes", pd.DataFrame())
        df_dis = self.datasets.get("discentes", pd.DataFrame())
        df_cor = self.datasets.get("correlacao", pd.DataFrame())
        df_res = self.datasets.get("opt_resumo", pd.DataFrame())

        # KPI 1: Artigos Docentes
        n_doc = len(df_doc)
        n_dois = df_doc["doi"].replace("", pd.NA).dropna().nunique() if "doi" in df_doc.columns else 0
        self.kpi_labels["docentes"].configure(text=f"{n_doc} ({n_dois} únicos)")

        # KPI 2: A1 / P1
        if not df_doc.empty and "qualis" in df_doc.columns:
            n_a1 = int(df_doc["qualis"].str.upper().eq("A1").sum())
            pct_a1 = (100.0 * n_a1 / max(n_doc, 1))
            self.kpi_labels["a1_p1"].configure(text=f"{n_a1} A1 ({pct_a1:.0f}%)")
        else:
            self.kpi_labels["a1_p1"].configure(text="-")

        # KPI 3: Média FWCI
        if not df_doc.empty and "fwci" in df_doc.columns:
            fwcis = pd.to_numeric(df_doc["fwci"], errors="coerce").dropna()
            self.kpi_labels["fwci"].configure(text=f"{fwcis.mean():.2f}" if not fwcis.empty else "-")
        else:
            self.kpi_labels["fwci"].configure(text="-")

        # KPI 4: Discentes
        if not df_dis.empty:
            n_al = df_dis["aluno"].nunique() if "aluno" in df_dis.columns else 0
            self.kpi_labels["discentes"].configure(text=f"{len(df_dis)} ({n_al} alunos)")
        else:
            self.kpi_labels["discentes"].configure(text="0")

        # KPI 5: Correlação Tese × Artigo
        if not df_cor.empty:
            sims = pd.to_numeric(df_cor.get("cosine_similarity"), errors="coerce").fillna(0)
            n_ok = int((sims >= self.cfg.sim_min).sum())
            self.kpi_labels["correlacao"].configure(text=f"{n_ok} / {len(df_cor)} pares")
        else:
            self.kpi_labels["correlacao"].configure(text="0")

        # KPI 6: Nota Otimizada
        if not df_res.empty:
            col_q = "total_quesito_2" if "total_quesito_2" in df_res.columns else "objetivo"
            if col_q in df_res.columns:
                v_q = to_float(df_res.iloc[0][col_q])
                self.kpi_labels["nota_opt"].configure(text=f"{v_q:.4f}" if v_q is not None else "-")
            else:
                self.kpi_labels["nota_opt"].configure(text="-")
        else:
            self.kpi_labels["nota_opt"].configure(text="-")

        self._apply_table_filter()

    def _on_dataset_selected(self, label: str) -> None:
        self.current_dataset_key = self.dataset_options_map.get(label, "docentes")
        self._apply_table_filter()

    def _apply_table_filter(self) -> None:
        df = self.datasets.get(self.current_dataset_key, pd.DataFrame())
        query = self.search_var.get().strip().casefold()
        if not df.empty and query:
            mask = df.apply(lambda row: query in " ".join(str(v).casefold() for v in row), axis=1)
            filtered = df[mask].reset_index(drop=True)
        else:
            filtered = df.copy()

        self.current_filtered_df = filtered
        self.row_count_lbl.configure(
            text=f"{len(filtered)} de {len(df)} registros  (Duplo-clique para detalhes)"
        )
        self._populate_treeview(self.tree, filtered)

    def _populate_treeview(self, tree_widget: ttk.Treeview, df: pd.DataFrame) -> None:
        tree_widget.delete(*tree_widget.get_children())
        if df.empty:
            tree_widget["columns"] = ("vazio",)
            tree_widget.heading("vazio", text="Nenhum dado carregado para esta visualização.")
            tree_widget.column("vazio", width=650, anchor="w")
            return

        # Oculta colunas muito longas na listagem resumida (disponíveis no duplo-clique)
        hidden_cols = {"resumo_scopus", "resumo", "autores_do_artigo", "eid", "scopus_id"}
        visible_cols = [c for c in df.columns if c not in hidden_cols] or list(df.columns)
        tree_widget["columns"] = visible_cols

        width_hints = {
            "professor": 190, "docente": 190, "docente_alocado": 190, "aluno": 190,
            "autor": 190, "titulado": 190, "orientador": 170,
            "artigo": 330, "titulo": 310, "source": 210, "doi": 185,
            "citations": 75, "fwci": 70, "faixa_F": 65, "percentil": 75,
            "faixa_P": 65, "qualis": 65, "ano": 65, "ano_defesa": 85,
            "ano_publicacao_scopus": 85, "primeiro_autor": 85,
            "cosine_similarity": 110, "similaridade_tese": 110,
        }

        for col in visible_cols:
            tree_widget.heading(
                col,
                text=col,
                command=lambda c=col, tw=tree_widget: self._sort_treeview_column(tw, c, False),
            )
            w = width_hints.get(col, 120)
            anchor = "center" if w <= 95 else "w"
            tree_widget.column(col, width=w, minwidth=55, anchor=anchor)

        for idx, (_, row) in enumerate(df.head(1500).iterrows()):
            vals = [str(row[c])[:140] for c in visible_cols]
            tree_widget.insert("", "end", iid=str(idx), values=vals)

    def _sort_treeview_column(self, tw: ttk.Treeview, col: str, reverse: bool) -> None:
        items = [(tw.set(k, col), k) for k in tw.get_children("")]

        def sort_key(pair):
            num = to_float(pair[0])
            return (0, num) if num is not None else (1, pair[0].casefold())

        items.sort(key=sort_key, reverse=reverse)
        for index, (_, k) in enumerate(items):
            tw.move(k, "", index)
        tw.heading(col, command=lambda: self._sort_treeview_column(tw, col, not reverse))

    # ==========================================================================
    # Modal de Inspeção Detalhada (Duplo-Clique na Linha)
    # ==========================================================================

    def _on_row_double_click(self, _event) -> None:
        sel = self.tree.selection()
        if not sel or self.current_filtered_df.empty:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.current_filtered_df):
            self._open_record_inspector(self.current_filtered_df.iloc[idx])

    def _on_quick_row_double_click(self, _event) -> None:
        sel = self.quick_tree.selection()
        if not sel or self.quick_doi_df.empty:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.quick_doi_df):
            self._open_record_inspector(self.quick_doi_df.iloc[idx])

    def _open_record_inspector(self, row: pd.Series) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Detalhes Completos do Registro")
        win.geometry("780x580")
        win.transient(self)
        win.grab_set()

        txt = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        txt.pack(fill="both", expand=True, padx=16, pady=(16, 10))

        lines = []
        for col, val in row.items():
            lines.append(f"▸ {col.upper()}:\n  {val}\n")
        full_str = "\n".join(lines)
        txt.insert("1.0", full_str)
        txt.configure(state="disabled")

        btn_bar = ctk.CTkFrame(win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=16, pady=(0, 14))

        doi_val = str(row.get("doi") or "").strip()
        if doi_val:
            ctk.CTkButton(
                btn_bar,
                text=f"🌐 Abrir DOI ({doi_val})",
                fg_color="#2563eb",
                command=lambda: webbrowser.open(f"https://doi.org/{doi_val}"),
            ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_bar,
            text="📋 Copiar Tudo",
            fg_color="#374151",
            command=lambda: (self.clipboard_clear(), self.clipboard_append(full_str)),
        ).pack(side="left")

        ctk.CTkButton(
            btn_bar, text="Fechar", width=90, command=win.destroy
        ).pack(side="right")

    # ==========================================================================
    # Execução em Background Thread (Sem Travar a Interface)
    # ==========================================================================

    def _log(self, msg: str) -> None:
        self.ui_queue.put(("log", msg))

    def _progress(self, cur: int, tot: int, label: str) -> None:
        frac = min(max(cur / max(tot, 1), 0.0), 1.0)
        self.ui_queue.put(("progress", (frac, f"{label} ({cur}/{tot})")))

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self.log_box.insert("end", str(payload) + "\n")
                    self.log_box.see("end")
                elif kind == "progress":
                    frac, txt = payload
                    self.progress_bar.set(frac)
                    self.prog_status_lbl.configure(text=txt)
                elif kind == "done":
                    self.btn_run_all.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self._load_datasets_into_memory()
                    self._refresh_dashboard_and_table()
                elif kind == "quick_doi_done":
                    self._populate_treeview(self.quick_tree, self.quick_doi_df)
                elif kind == "opt_done":
                    self._render_optimization_report(payload)
                    self._load_datasets_into_memory()
                    self._refresh_dashboard_and_table()
                elif kind == "api_test_done":
                    self.api_diag_lbl.configure(text=str(payload))
                elif kind == "error":
                    self.btn_run_all.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    messagebox.showerror("Erro na Operação", str(payload))
        except queue.Empty:
            pass
        self.after(120, self._poll_ui_queue)

    def _sync_period_from_pipeline_bar(self) -> bool:
        try:
            ini = int(self.pipe_ini_entry.get().strip())
            fim = int(self.pipe_fim_entry.get().strip())
            if ini > fim or ini < 1990 or fim > 2040:
                raise ValueError
            self.cfg.ano_inicio = ini
            self.cfg.ano_fim = fim
            self.cfg.incluir_perfis_scopus = bool(self.chk_profiles_var.get())
            self.cfg.save()
            self._update_sidebar_badges()
            return True
        except ValueError:
            messagebox.showwarning("Período Inválido", "Informe anos válidos (ex.: 2021 até 2024 ou 2025 até 2028).")
            return False

    def _set_period_preset(self, ini: int, fim: int) -> None:
        self.pipe_ini_entry.delete(0, "end")
        self.pipe_ini_entry.insert(0, str(ini))
        self.pipe_fim_entry.delete(0, "end")
        self.pipe_fim_entry.insert(0, str(fim))
        self._sync_period_from_pipeline_bar()
        self._load_datasets_into_memory()
        self._refresh_dashboard_and_table()

    def _launch_task(self, target_fn) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Em Execução", "Já existe uma tarefa em andamento. Aguarde ou clique em Parar.")
            return
        if not self._sync_period_from_pipeline_bar():
            return
        self.stop_event.clear()
        self.btn_run_all.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress_bar.set(0)

        def wrapper():
            try:
                target_fn()
                self.ui_queue.put(("done", None))
            except Exception as exc:
                self._log(f"[ERRO] {exc}")
                self.ui_queue.put(("error", str(exc)))

        self.worker_thread = threading.Thread(target=wrapper, daemon=True)
        self.worker_thread.start()

    def _request_stop(self) -> None:
        self.stop_event.set()
        self._log("Solicitação de parada enviada... finalizando requisição atual.")

    # ==========================================================================
    # Comandos das Etapas do Pipeline
    # ==========================================================================

    def _start_full_pipeline(self) -> None:
        def job():
            self._log("=" * 72)
            self._log(f"INICIANDO PIPELINE COMPLETO ({self.cfg.ano_inicio}–{self.cfg.ano_fim})")
            self._log("=" * 72)

            art_raw, _ = extract_lattes_articles_and_orientations(
                self.cfg, log=self._log, progress=self._progress
            )
            if self.stop_event.is_set():
                return

            out_dir = Path(self.cfg.output_dir)
            art_enriq = out_dir / f"artigos_docentes_{self.cfg.ano_inicio}_{self.cfg.ano_fim}_enriquecido.csv"
            _, authors_map = enrich_articles_full(
                art_raw, art_enriq, self.cfg, log=self._log, progress=self._progress, stop_event=self.stop_event
            )
            if self.stop_event.is_set():
                return

            al_csv = collect_student_coauthorship(
                art_enriq, self.cfg, authors_by_doi=authors_map,
                log=self._log, progress=self._progress, stop_event=self.stop_event
            )
            if self.stop_event.is_set():
                return

            teses_csv = out_dir / "teses_dissertacoes_sophia.csv"
            if not teses_csv.exists() and self.cfg.materiais_csv:
                teses_csv = Path(self.cfg.materiais_csv)
            corr_csv = compute_thesis_article_correlation(
                al_csv, teses_csv, self.cfg, log=self._log, progress=self._progress
            )

            try:
                opt_res = run_capes_optimization(art_enriq, al_csv, corr_csv, self.cfg, log=self._log)
                self.last_opt_result = opt_res
                self.ui_queue.put(("opt_done", opt_res))
            except Exception as exc_opt:
                self._log(f"Aviso na otimização: {exc_opt}")

            self._progress(1, 1, "Pipeline Completo Concluído com Sucesso!")
            self._log("TODOS OS DADOS FORAM COLETADOS E ATUALIZADOS NA ABA 'Dados & Exportação'!")

        self._launch_task(job)

    def _start_stage_lattes_parse(self) -> None:
        self._launch_task(
            lambda: extract_lattes_articles_and_orientations(self.cfg, log=self._log, progress=self._progress)
        )

    def _start_stage_lattes_web(self) -> None:
        def job():
            scrape_ppgpo_professors_and_lattes(
                self.cfg, only_missing_htmls=True, log=self._log, progress=self._progress, stop_event=self.stop_event
            )
            extract_lattes_articles_and_orientations(self.cfg, log=self._log, progress=self._progress)

        self._launch_task(job)

    def _start_stage_enrich(self) -> None:
        def job():
            out_dir = Path(self.cfg.output_dir)
            raw_csv = out_dir / f"artigos_docentes_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.csv"
            if not raw_csv.exists():
                raw_csv, _ = extract_lattes_articles_and_orientations(self.cfg, log=self._log, progress=self._progress)
            enriq_csv = out_dir / f"artigos_docentes_{self.cfg.ano_inicio}_{self.cfg.ano_fim}_enriquecido.csv"
            enrich_articles_full(
                raw_csv, enriq_csv, self.cfg, log=self._log, progress=self._progress, stop_event=self.stop_event
            )

        self._launch_task(job)

    def _start_stage_students(self) -> None:
        def job():
            out_dir = Path(self.cfg.output_dir)
            enriq_csv = out_dir / f"artigos_docentes_{self.cfg.ano_inicio}_{self.cfg.ano_fim}_enriquecido.csv"
            if not enriq_csv.exists():
                raise FileNotFoundError("Execute primeiro o Passo 2 (Scopus + FWCI + Qualis) para este período.")
            collect_student_coauthorship(
                enriq_csv, self.cfg, log=self._log, progress=self._progress, stop_event=self.stop_event
            )

        self._launch_task(job)

    def _start_stage_sophia_web(self) -> None:
        self._launch_task(
            lambda: scrape_sophia_theses(
                self.cfg, only_new_students=True, log=self._log, progress=self._progress, stop_event=self.stop_event
            )
        )

    def _start_stage_correlation(self) -> None:
        def job():
            out_dir = Path(self.cfg.output_dir)
            al_csv = out_dir / f"alunos_artigos_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.csv"
            if not al_csv.exists():
                raise FileNotFoundError("Execute primeiro o Passo 3 (Discentes & Defesas) para este período.")
            teses_csv = out_dir / "teses_dissertacoes_sophia.csv"
            if not teses_csv.exists() and self.cfg.materiais_csv:
                teses_csv = Path(self.cfg.materiais_csv)
            compute_thesis_article_correlation(
                al_csv, teses_csv, self.cfg, log=self._log, progress=self._progress
            )

        self._launch_task(job)

    def _start_stage_optimize(self) -> None:
        def job():
            out_dir = Path(self.cfg.output_dir)
            enriq_csv = out_dir / f"artigos_docentes_{self.cfg.ano_inicio}_{self.cfg.ano_fim}_enriquecido.csv"
            al_csv = out_dir / f"alunos_artigos_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.csv"
            corr_csv = out_dir / f"correlacao_teses_artigos_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.csv"
            res = run_capes_optimization(enriq_csv, al_csv, corr_csv, self.cfg, log=self._log)
            self.last_opt_result = res
            self.ui_queue.put(("opt_done", res))

        self._launch_task(job)

    # ==========================================================================
    # Consulta Rápida Avulsa & Otimizador Interativo
    # ==========================================================================

    def _run_quick_doi_lookup(self) -> None:
        raw_txt = self.doi_input_box.get("1.0", "end").strip()
        tokens = [t.strip() for t in re.split(r"[\s,;]+", raw_txt) if t.strip()]
        if not tokens:
            messagebox.showwarning("Sem DOIs", "Cole ao menos um DOI para consultar.")
            return

        def job():
            self._log(f"Consultando {len(tokens)} DOI(s) na Consulta Rápida...")
            self.quick_doi_df = lookup_dois_batch(tokens, self.cfg, log=self._log, progress=self._progress)
            self.ui_queue.put(("quick_doi_done", None))

        self._launch_task(job)

    def _export_quick_doi_excel(self) -> None:
        if self.quick_doi_df.empty:
            messagebox.showinfo("Vazio", "Faça uma consulta rápida primeiro.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialfile="consulta_dois_capes.xlsx",
            filetypes=[("Planilha Excel", "*.xlsx"), ("CSV", "*.csv")],
        )
        if not dest:
            return
        if dest.lower().endswith(".csv"):
            self.quick_doi_df.to_csv(dest, index=False, encoding="utf-8-sig")
        else:
            self.quick_doi_df.to_excel(dest, index=False)
        messagebox.showinfo("Exportado", f"Arquivo salvo com sucesso em:\n{dest}")

    def _run_interactive_optimizer(self) -> None:
        try:
            self.cfg.sim_min = float(self.opt_sim_entry.get().replace(",", "."))
            self.cfg.fwci_max = float(self.opt_fwci_entry.get().replace(",", "."))
            self.cfg.veiculo_estrato = self.opt_veic_menu.get()
            self.cfg.um_por_ano = bool(self.opt_um_ano_var.get())
            self.cfg.save()
            self._update_sidebar_badges()
        except ValueError:
            messagebox.showwarning("Parâmetros Inválidos", "Verifique os valores numéricos de similaridade e teto FWCI.")
            return

        self._start_stage_optimize()

    def _render_optimization_report(self, res: dict) -> None:
        self.opt_summary_box.delete("1.0", "end")
        r_opt = res["metrics_opt"]
        r_bas = res["metrics_base"]
        ganho = r_opt["objetivo_decisoes"] - r_bas["objetivo_decisoes"]

        lines = [
            f"══════════════════════════════════════════════════════════════════════════════════",
            f"  RESULTADO DA OTIMIZAÇÃO CAPES (ENGENHARIAS III) — PERÍODO {self.cfg.ano_inicio}–{self.cfg.ano_fim}",
            f"  Critério de Periódico: {self.cfg.veiculo_estrato.upper()} | Sim. Mínima: {self.cfg.sim_min:.2f} | Teto FWCI: {self.cfg.fwci_max:.1f}",
            f"══════════════════════════════════════════════════════════════════════════════════",
            f"",
            f"  Indicador                      Otimizado (MILP)     Linha de Base (Sem Coord.)   Diferença",
            f"  ──────────────────────────────────────────────────────────────────────────────────────────",
            f"  Artigos Alocados (2.4.1)       {r_opt['artigos_na_lista']:>16}     {r_bas['artigos_na_lista']:>26}",
            f"  IBAD (Impacto FWCI F1..F8)     {r_opt['IBAD']:>16.4f}     {r_bas['IBAD']:>26.4f}   {r_opt['IBAD']-r_bas['IBAD']:>+9.4f}",
            f"  IBPD (Veículo P1..P8/Qualis)   {r_opt['IBPD']:>16.4f}     {r_bas['IBPD']:>26.4f}   {r_opt['IBPD']-r_bas['IBPD']:>+9.4f}",
            f"  ► SUBITEM 2.4.1 (Docentes)     {r_opt['IND_2.4.1']:>16.4f}     {r_bas['IND_2.4.1']:>26.4f}   {r_opt['IND_2.4.1']-r_bas['IND_2.4.1']:>+9.4f}",
            f"",
            f"  Indicações Teses/Diss (2.1.3)  {r_opt['indicacoes_2.1.3']:>16}     {r_bas['indicacoes_2.1.3']:>26}",
            f"  IBV (Veículo Indicações)       {r_opt['IBV']:>16.4f}     {r_bas['IBV']:>26.4f}   {r_opt['IBV']-r_bas['IBV']:>+9.4f}",
            f"  IBA (FWCI Indicações)          {r_opt['IBA']:>16.4f}     {r_bas['IBA']:>26.4f}   {r_opt['IBA']-r_bas['IBA']:>+9.4f}",
            f"  ► SUBITEM 2.1.3 (Egressos)     {r_opt['IND_2.1.3']:>16.4f}     {r_bas['IND_2.1.3']:>26.4f}   {r_opt['IND_2.1.3']-r_bas['IND_2.1.3']:>+9.4f}",
            f"  ► ÍNDICE D (Diversidade Ori.)  {r_opt['D']:>16.4f}     {r_bas['D']:>26.4f}   {r_opt['D']-r_bas['D']:>+9.4f}",
            f"",
            f"  ► SUBITEM 2.3.1 (Doutores)     {r_opt['IND_2.3.1']:>16.4f}",
            f"  ► SUBITEM 2.3.2 (Mestres)      {r_opt['IND_2.3.2']:>16.4f}",
            f"  ──────────────────────────────────────────────────────────────────────────────────────────",
            f"  ★ NOTA QUANTITATIVA QUESITO 2  {r_opt['total_quesito_2']:>16.4f}     {r_bas['total_quesito_2']:>26.4f}   {ganho:>+9.4f}",
            f"══════════════════════════════════════════════════════════════════════════════════",
            f"",
            f"  As listas completas de artigos selecionados (2.4.1 e 2.1.3) já estão disponíveis",
            f"  na aba '📊 Dados & Exportação' e salvas em: {res['out_dir']}",
        ]
        self.opt_summary_box.insert("1.0", "\n".join(lines))

    # ==========================================================================
    # Configurações, Diagnóstico de API e Exportação Excel/CSV
    # ==========================================================================

    def _browse_path(self, attr_name: str, is_dir: bool) -> None:
        if is_dir:
            chosen = filedialog.askdirectory(initialdir=self.path_entries[attr_name].get() or str(WORKSPACE_ROOT))
        else:
            chosen = filedialog.askopenfilename(initialdir=str(WORKSPACE_ROOT))
        if chosen:
            self.path_entries[attr_name].delete(0, "end")
            self.path_entries[attr_name].insert(0, chosen)

    def _save_settings_from_ui(self) -> None:
        self.cfg.scopus_api_key = self.api_key_entry.get().strip()
        self.cfg.scopus_inst_token = self.inst_token_entry.get().strip()
        self.cfg.ppgpo_docentes_url = self.url_ppgpo_entry.get().strip()
        self.cfg.browser = self.browser_menu.get().strip()
        for attr, ent in self.path_entries.items():
            setattr(self.cfg, attr, ent.get().strip())
        Path(self.cfg.output_dir).mkdir(parents=True, exist_ok=True)
        self.cfg.save()
        self._update_sidebar_badges()
        self._load_datasets_into_memory()
        self._refresh_dashboard_and_table()
        messagebox.showinfo("Configurações Salvas", "Suas credenciais e caminhos foram salvos localmente com sucesso!")

    def _test_api_clicked(self) -> None:
        key = self.api_key_entry.get().strip()
        tok = self.inst_token_entry.get().strip()
        self.api_diag_lbl.configure(text="⏳ Testando conexão com Scopus, Serial Title, SciVal e Crossref...")

        def run_diag():
            diag = test_elsevier_connection(key, tok)
            out_lines = []
            for srv, (ok, detail) in diag.items():
                icon = "✅" if ok else "⚠️"
                out_lines.append(f"{icon} {srv}: {detail}")
            self.ui_queue.put(("api_test_done", "\n".join(out_lines)))

        threading.Thread(target=run_diag, daemon=True).start()

    def _reload_all_from_disk(self) -> None:
        self._load_datasets_into_memory()
        self._refresh_dashboard_and_table()

    def _export_current_csv(self) -> None:
        if self.current_filtered_df.empty:
            messagebox.showinfo("Tabela Vazia", "Não há dados na tabela atual para exportar.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"{self.current_dataset_key}_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.csv",
            filetypes=[("Arquivo CSV (Excel UTF-8)", "*.csv")],
        )
        if dest:
            self.current_filtered_df.to_csv(dest, index=False, encoding="utf-8-sig")
            messagebox.showinfo("Exportado", f"CSV exportado com sucesso:\n{dest}")

    def _export_all_excel(self) -> None:
        dest = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialfile=f"Base_Completa_CAPES_PPGPO_{self.cfg.ano_inicio}_{self.cfg.ano_fim}.xlsx",
            filetypes=[("Planilha Excel (.xlsx)", "*.xlsx")],
        )
        if not dest:
            return
        sheet_names = {
            "docentes": "Artigos_Docentes",
            "discentes": "Coautoria_Discentes_Defesas",
            "correlacao": "Correlacao_Tese_Artigo",
            "teses": "Teses_Sophia_ITA",
            "periodicos": "Periodicos_Percentis",
            "opt_resumo": "Otimizacao_Resumo",
            "opt_241": "Otimizacao_2_4_1",
            "opt_213": "Otimizacao_2_1_3",
        }
        wrote_any = False
        with pd.ExcelWriter(dest, engine="openpyxl") as writer:
            for k, sh_name in sheet_names.items():
                df_k = self.datasets.get(k, pd.DataFrame())
                if not df_k.empty:
                    df_k.to_excel(writer, sheet_name=sh_name[:31], index=False)
                    wrote_any = True
        if wrote_any:
            messagebox.showinfo("Planilha Completa Gerada", f"Todas as abas foram exportadas para:\n{dest}")
        else:
            messagebox.showwarning("Sem Dados", "Nenhuma tabela possui registros para exportar ainda.")

    def _open_output_folder(self) -> None:
        out = Path(self.cfg.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(out))  # type: ignore[attr-defined]
        except Exception:
            webbrowser.open(out.as_uri())


def launch() -> None:
    app = PPGPOCapesApp()
    app.mainloop()


if __name__ == "__main__":
    launch()
