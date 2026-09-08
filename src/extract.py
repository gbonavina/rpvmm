from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

from bs4 import BeautifulSoup

import json
import re
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_PATH = DATA_DIR / "professors.json"
LATTES_DIR = DATA_DIR / "lattes"

NAME_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<institution>[^)]+)\)\s*$")
LATTES_ID_RE = re.compile(r"lattes\.cnpq\.br/(\d+)")

FIREFOX_BIN = "/snap/firefox/current/usr/lib/firefox/firefox"  # localização do meu navegador, de outra pessoa pode ser diferente
URL = "https://ppgpo.unifesp.br/in%C3%ADcio/docentes"
CAPTCHA_TIMEOUT = 300
PAUSE_BETWEEN_CVS = 2
STABLE_SECONDS = 5
POLL_SECONDS = 0.5
# âncoras que o menu do Lattes promete; se estão no menu, têm que existir no corpo
CRITICAL_SECTIONS = (
    "ProducaoBibliografica",
    "ProducaoTecnica",
    "ArtigosCompletos",
    "ProjetosPesquisa",
    "AtuacaoProfissional",
    "FormacaoAcademicaTitulacao",
    "LinhaPesquisa",
)


def make_driver(*, headless: bool = True) -> webdriver.Firefox:
    options = Options()
    if headless:
        options.add_argument("-headless")
    options.binary_location = FIREFOX_BIN
    return webdriver.Firefox(options=options)


def get_html(url: str) -> str:
    driver = make_driver(headless=False)
    try:
        driver.get(url)
        return driver.page_source
    finally:
        driver.quit()


def parse_professor(box) -> dict:
    block = box.select_one("div.tyJCtd") or box
    heading = block.find("p")
    raw_name = heading.get_text(strip=True) if heading else None

    name, institution = raw_name, None
    if raw_name and (m := NAME_RE.match(raw_name)):
        name = m.group("name")
        institution = m.group("institution")

    area = email = None
    for li in block.select("li"):
        text = " ".join(li.get_text(" ", strip=True).split())
        if text.startswith("Área:"):
            area = text.split(":", 1)[1].strip()
        elif text.startswith("Email:"):
            email = text.split(":", 1)[1].strip()

    lattes_a = block.find("a", href=re.compile(r"lattes\.cnpq\.br"))
    lattes = lattes_a["href"] if lattes_a else None
    lattes_id = lattes_id_from_url(lattes)

    return {
        "name": name,
        "institution": institution,
        "area": area,
        "email": email,
        "lattes": lattes,
        "lattes_id": lattes_id,
        "lattes_html": None,
    }


def get_professor_data() -> list[dict]:
    soup = BeautifulSoup(get_html(URL), "html.parser")
    boxes = soup.select("div.oKdM2c.ZZyype")
    filled = [b for b in boxes if "Área:" in b.get_text() or "Email:" in b.get_text()]
    return [parse_professor(box) for box in filled]


def save_professors(professors: list[dict]) -> Path:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(professors, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return DATA_PATH


def lattes_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    stripped = url.strip()
    if stripped.isdigit() and len(stripped) >= 16:
        return stripped
    m = LATTES_ID_RE.search(stripped)
    return m.group(1) if m else None


def lattes_url(url_or_id: str) -> str:
    lid = lattes_id_from_url(url_or_id)
    if not lid:
        raise ValueError(f"URL/ID Lattes inválido: {url_or_id!r}")
    return f"http://lattes.cnpq.br/{lid}"


def download_lattes(
    url_or_id: str,
    driver: webdriver.Firefox | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """Abre o Lattes, espera o captcha ser resolvido e salva o HTML em data/lattes/{id}.html."""
    url = lattes_url(url_or_id)
    lid = lattes_id_from_url(url)
    assert lid is not None

    LATTES_DIR.mkdir(parents=True, exist_ok=True)
    out = LATTES_DIR / f"{lid}.html"
    if (
        not force
        and out.exists()
        and is_complete_cv(out.read_text(encoding="utf-8", errors="ignore"))
    ):
        print(f"skip {lid} (já baixado)")
        return out

    own_driver = driver is None
    if own_driver:
        driver = make_driver(headless=False)
    assert driver is not None

    try:
        print(f"open {url}")
        driver.get(url)
        html = wait_until_cv(driver)

        if looks_like_captcha(html) or not looks_like_cv(html):
            print(f"ainda captcha/sem CV para {lid}, não salvando")
            return None

        missing = missing_promised_sections(html)
        if missing:
            print(f"CV pode estar incompleto para {lid} (faltando {missing})")
            if input("Salvar mesmo assim? [s/N] ").strip().lower() not in {"s", "sim", "y", "yes"}:
                return None

        out.write_text(html, encoding="utf-8")
        print(f"saved {out} ({len(html)} chars)")
        return out
    finally:
        if own_driver:
            driver.quit()


def looks_like_cv(html: str) -> bool:
    """Title in <head> appears before the body streams in; require real CV content."""
    text = html.lower()
    if "<body" not in text:
        return False
    return (
        'class="infpessoa"' in text
        or "última atualização do currículo" in text
        or "ultima atualizacao do curriculo" in text
    )


def looks_like_captcha(html: str) -> bool:
    if looks_like_cv(html):
        return False
    text = html.lower()
    return "recaptcha" in text or "g-recaptcha" in text or "captcha" in text


def missing_promised_sections(html: str) -> list[str]:
    missing = []
    for section in CRITICAL_SECTIONS:
        if f'href="#{section}"' not in html:
            continue
        if f'name="{section}"' not in html and f'id="{section}"' not in html:
            missing.append(section)
    return missing


def is_complete_cv(html: str) -> bool:
    return looks_like_cv(html) and not missing_promised_sections(html)


def cv_html(driver: webdriver.Firefox) -> str:
    html = driver.page_source
    if looks_like_cv(html):
        return html

    for frame in driver.find_elements("css selector", "iframe"):
        try:
            driver.switch_to.frame(frame)
            inner = driver.page_source
        except Exception:
            inner = ""
        finally:
            driver.switch_to.default_content()
        if looks_like_cv(inner):
            return inner
    return html


def wait_until_cv(driver: webdriver.Firefox) -> str:
    try:
        WebDriverWait(driver, CAPTCHA_TIMEOUT).until(
            lambda d: looks_like_cv(cv_html(d))
        )
    except TimeoutException:
        input("Captcha ainda visível? Resolva no navegador e pressione Enter...")

    try:
        WebDriverWait(driver, CAPTCHA_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass

    last_len = -1
    stable_since: float | None = None
    deadline = time.monotonic() + CAPTCHA_TIMEOUT
    html = cv_html(driver)
    while time.monotonic() < deadline:
        html = cv_html(driver)
        n = len(html)
        complete = is_complete_cv(html)
        if n != last_len:
            last_len = n
            stable_since = time.monotonic()
            print(f"  carregando: {n} chars", flush=True)
        elif (
            complete
            and stable_since is not None
            and time.monotonic() - stable_since >= STABLE_SECONDS
        ):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(POLL_SECONDS)
            return cv_html(driver)
        time.sleep(POLL_SECONDS)

    input("A página ainda está carregando? Espere o currículo inteiro e pressione Enter...")
    return cv_html(driver)


def unique_lattes_urls(professors: list[dict]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for professor in professors:
        lid = professor.get("lattes_id") or lattes_id_from_url(professor.get("lattes"))
        url = professor.get("lattes")
        if lid and url:
            urls[lid] = url
    return urls


def attach_lattes_paths(professors: list[dict]) -> list[dict]:
    for professor in professors:
        lid = professor.get("lattes_id") or lattes_id_from_url(professor.get("lattes"))
        professor["lattes_id"] = lid
        html_path = LATTES_DIR / f"{lid}.html" if lid else None
        professor["lattes_html"] = (
            str(html_path) if html_path and html_path.exists() else None
        )
    return professors


def download_lattes_htmls(professors: list[dict]) -> int:
    urls = unique_lattes_urls(professors)
    saved = 0
    driver = make_driver(headless=False)
    try:
        for i, (lid, url) in enumerate(urls.items()):
            path = download_lattes(url, driver)
            if path is not None:
                saved += 1
            if i < len(urls) - 1:
                time.sleep(PAUSE_BETWEEN_CVS)
    finally:
        driver.quit()
    return saved


def run_pipeline() -> None:
    professors = get_professor_data()
    path = save_professors(professors)
    print(f"Wrote {len(professors)} professors to {path}")

    n = download_lattes_htmls(professors)
    attach_lattes_paths(professors)
    save_professors(professors)
    print(f"Wrote {n} Lattes CVs to {LATTES_DIR}")
    print(f"Updated {path} with lattes_html paths")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        driver = make_driver(headless=False)
        try:
            for link in sys.argv[1:]:
                download_lattes(link, driver)
        finally:
            driver.quit()
    else:
        run_pipeline()
