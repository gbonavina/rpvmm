from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

from bs4 import BeautifulSoup

import pandas as pd

import json
import re
import sys
import time
from pathlib import Path

SOPHIA_URL = "http://www.sophia.bibl.ita.br/biblioteca/index.html"

FIREFOX_BIN = "/snap/firefox/current/usr/lib/firefox/firefox"  # localização do meu navegador, de outra pessoa pode ser diferente

def make_driver(*, headless: bool = True) -> webdriver.Firefox:
    options = Options()
    if headless:
        options.add_argument("-headless")
    options.binary_location = FIREFOX_BIN
    return webdriver.Firefox(options=options)

def get_html(url: str) -> str:
    driver = make_driver()
    driver.get(url)
    return driver.page_source

def clean_students(students: Path) -> pd.DataFrame:
    students = pd.read_csv(students)

    # Drop 'Desligado' 
    students = students[students['Situação'] != 'Desligado']
    
    # Drop duplicates
    # students = students.drop_duplicates(subset=['Name'])

    return students

def _quick_search_box(driver: webdriver.Firefox):
    # index.html is a frameset; the form is injected into mainFrame after load.
    try:
        alert = driver.switch_to.alert
        alert.accept()
        return False
    except Exception:
        pass
    try:
        driver.switch_to.default_content()
        driver.switch_to.frame("mainFrame")
        box = driver.find_element(
            By.CSS_SELECTOR, "form[name='frm_rapida'] input[name='rapida_campo']"
        )
        return box if box.is_displayed() else False
    except Exception:
        return False

OUTPUT_CSV = Path(__file__).resolve().parents[2] / "data" / "materiais.csv"
COLUMNS = ["autor", "material", "titulo", "ano", "assunto"]

def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())

def _results_marker(driver: webdriver.Firefox):
    try:
        driver.switch_to.alert.accept()
        return "alert"
    except Exception:
        pass
    try:
        driver.switch_to.default_content()
        driver.switch_to.frame("mainFrame")
        html = driver.page_source
    except Exception:
        return False
    if (
        "Nenhum registro encontrado" in html
        or "registros encontrados" in html
        or "registro encontrado" in html
    ):
        return html
    return False

def _extract_results(html: str, autor: str) -> list[dict]:
    # autor: nome do aluno
    # material: <td class="td_ficha_dir esquerda">Dissertação de mestrado acadêmico</td>
    # titulo: <td class="td_ficha_dir esquerda"><a class="link_custom_negrito" title="Abrir detalhes..." href="javascript:LinkDetalhes(parent.hiddenFrame.modo_busca,1,0,80346,1,'resultado',1);">A data-centric approach to missing data imputation</a> : addressing noise, adversarial and fairness challenges</td>
    # ano: <td class="td_ficha_dir esquerda">2025</td>
    # assunto: <a class="link_custom" title="Buscar todos os registros deste assunto..." href="javascript:LinkBuscaAssunto(parent.hiddenFrame.modo_busca,108973,'Aprendizado_(inteligência_artificial)',1);">Aprendizado (inteligência artificial)</a>
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.select("#p_div_aba1_resultado table"):
        fields = {}
        body = table.find("tbody", recursive=False) or table
        for tr in body.find_all("tr", recursive=False):
            label = tr.find("td", class_="td_ficha_esq", recursive=False)
            value = tr.find("td", class_="td_ficha_dir", recursive=False)
            if label and value:
                fields[label.get_text(strip=True)] = value
        if "Título" not in fields:
            continue
        assunto_cell = fields.get("Assuntos")
        if assunto_cell is None:
            assunto = ""
        else:
            links = assunto_cell.select("a.link_custom")
            assunto = "; ".join(_text(link) for link in links) if links else _text(assunto_cell)
        rows.append({
            "autor": autor,
            "material": _text(fields["Material"]) if "Material" in fields else "",
            "titulo": _text(fields["Título"]),
            "ano": _text(fields["Ano"]) if "Ano" in fields else "",
            "assunto": assunto,
        })
    return rows

def _page_number(driver: webdriver.Firefox) -> str:
    try:
        driver.switch_to.default_content()
        driver.switch_to.frame("mainFrame")
        marks = driver.find_elements(By.CSS_SELECTOR, "span.paginacao_link_atual")
    except Exception:
        return ""
    return marks[0].text.strip() if marks else ""

def _click_next(driver: webdriver.Firefox) -> bool:
    driver.switch_to.default_content()
    driver.switch_to.frame("mainFrame")
    buttons = [
        button
        for button in driver.find_elements(By.CSS_SELECTOR, "span[title='Próxima']")
        if button.is_displayed() and "proximo" in (button.get_attribute("onclick") or "")
    ]
    if not buttons:
        return False
    current = _page_number(driver)
    buttons[0].click()
    WebDriverWait(driver, 30).until(lambda d: _page_number(d) not in ("", current))
    return True

def search_student(driver: webdriver.Firefox, *, student: pd.Series) -> list[dict]:
    # Search for the student in Sophia
    driver.get(SOPHIA_URL)
    search = WebDriverWait(driver, 30).until(_quick_search_box)
    search.clear()
    search.send_keys(student["Name"])
    driver.find_element(
        By.CSS_SELECTOR, "form[name='frm_rapida'] input.button_busca[value='Buscar']"
    ).click()

    rows = []
    for _ in range(100):
        marker = WebDriverWait(driver, 30).until(_results_marker)
        if marker == "alert":
            break
        rows.extend(_extract_results(marker, student["Name"]))
        try:
            if not _click_next(driver):
                break
        except TimeoutException:
            break
    return rows

def write_materials(rows: list[dict]) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLUMNS).to_csv(OUTPUT_CSV, index=False)

if __name__ == "__main__":
    print("Cleaning students...")
    students = clean_students(Path("src/thesis/ALUNOS-Completo.csv"))
    print("Searching students...")
    driver = make_driver(headless=False)
    rows = []
    try:
        for number, (_, student) in enumerate(students.iterrows(), start=1):
            print(f"Searching student {number} of {len(students)}: {student['Name']}...")
            found = search_student(driver, student=student)
            print(f"  {len(found)} material(s)")
            rows.extend(found)
            write_materials(rows)
    finally:
        driver.quit()
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")