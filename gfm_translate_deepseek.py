"""
Tradutor automático de localização para Victoria 2 - Greater Flavor Mod (GFM) -> pt-BR
Motor de IA: DeepSeek (via SDK da OpenAI, API compatível).
Formato alvo: CSV do Clausewitz, 15 colunas separadas por ';', codificação Windows-1252 (cp1252).

Uso:
    python gfm_translate_deepseek.py                                # traduz tudo (só o delta, graças ao cache)
    python gfm_translate_deepseek.py --dry-run                      # só conta quantas linhas faltam traduzir
    python gfm_translate_deepseek.py --only text.csv                # processa exatamente o arquivo text.csv
    python gfm_translate_deepseek.py --only events                  # processa arquivos cujo nome contém "events"
    python gfm_translate_deepseek.py --only text.csv --clean-cache  # limpa traduções suspeitas do cache e retraduz
    python gfm_translate_deepseek.py --only countries --retranslate-names  # retraduz nomes próprios que ficaram com artigo

Configuração: copie .env.example para .env e preencha a chave da API e os caminhos (veja o README).
"""

import argparse
import codecs
import hashlib
import unicodedata
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from tqdm import tqdm

load_dotenv()

# ==============================================================================
# 1. CONFIGURAÇÕES GERAIS E DIRETÓRIOS
# ==============================================================================
def _env_path(name: str, default: Path | None = None) -> Path:
    value = os.getenv(name, "").strip().strip("'\"")
    if value:
        return Path(value).expanduser()
    if default is not None:
        return default
    sys.exit(f"Variável {name} não definida. Copie .env.example para .env e preencha os caminhos.")


SOURCE_DIR = _env_path("GFM_SOURCE_DIR")          # pasta 'localisation' do GFM original (só leitura)
OUTPUT_DIR = _env_path("GFM_OUTPUT_DIR")          # pasta 'localisation' do mod de tradução (saída)
BASE_DIR = _env_path("GFM_WORK_DIR", Path.cwd())  # onde ficam cache e log de falhas (padrão: pasta atual)
CACHE_FILE = BASE_DIR / "gfm_deepseek_cache_v2.json"
FAILURES_LOG = BASE_DIR / "gfm_falhas.log"

OUTPUT_PREFIX = os.getenv("GFM_OUTPUT_PREFIX", "")  # prefixo opcional dos arquivos de saída (ex.: "z_gfm_")
ENGLISH_COL = 1      # índice da coluna do inglês (é ela que recebe o texto em pt-BR)
TOTAL_COLS = 15      # KEY + 13 idiomas + "x"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")  # se a API recusar, tente "deepseek-flash" (veja o README)
BASE_URL = "https://api.deepseek.com"
TEMPERATURE = 0.2         # mais baixo para evitar criatividade excessiva em menus/nomes
BATCH_SIZE = 25           # linhas por chamada
MAX_RETRIES = 3

TARGET_LANG = "Português do Brasil formal, diplomático e histórico do século XIX para Victoria 2"
ENCODING = "cp1252"       # codificação estrita do motor Clausewitz

# Glossário técnico: vai no prompt (a IA flexiona gênero/número ao redor do termo)
GLOSSARY = {
    "Sphere of Influence": "Esfera de Influência",
    "Great Power": "Grande Potência",
    "Secondary Power": "Potência Secundária",
    "Civilized Nation": "Nação Civilizada",
    "Uncivilized Nation": "Nação Não-Civilizada",
    "Consciousness": "Consciência",
    "Militancy": "Militância",
    "Infamy": "Infâmia",
    "War Exhaustion": "Exaustão de Guerra",
    "National Focus": "Foco Nacional",
    "Life Rating": "Qualidade de Vida",
    "RGO": "RGO",
    "Casus Belli": "Casus Belli",
    "Laissez Faire": "Laissez-Faire",
    "State Capitalism": "Capitalismo de Estado",
    "Planned Economy": "Economia Planejada",
    "Jingoism": "Jingoísmo",
}

# Ajustes automáticos de pós-processamento para textos que vazam das caixinhas da UI
UI_EXACT_FITS = {
    "Voting System": "Sist. Eleitoral",
    "Press Rights": "Imprensa",
    "Public Meetings": "Reuniões Públicas",
    "Max. Work Hours": "Jornada Máxima",
    "Safety Regulations": "Seg. do Trabalho",
    "Unemployment Subsidies": "Auxílio-Desemprego",
    "Cannot pass §Rsocial§! reforms": "Sem reforma §Rsocial§!",
    "Cannot pass §Rpolitical§! reforms": "Sem reforma §Rpolítica§!",
}

UI_PREFIX_FITS = [
    ("Jornada de Trabalho Ilimitada", "Jornada Ilimitada"),
    ("Jornada de Trabalho de ", "Jornada de "),
]

# ==============================================================================
# 2. BLINDAGEM DE TOKENS, ANTI-CRASH C++ E SANITIZAÇÃO
# ==============================================================================
# Captura: $VAR$ (incluindo $VAL|=+2%$), \n \r \t, §Y / §!, %d %s %.1f, [GetCountryName], £ícones£
TOKEN_REGEX = re.compile(
    r"(\$[^$\s]+\$"
    r"|\\[nrt]"
    r"|§[A-Za-z!0-9]"
    r"|%[+0]*\d*(?:\.\d+)?[hlL]?[diouxXfFeEgGsc%]"
    r"|\[[^\]]+\]"
    r"|£[A-Za-z0-9_]+£)"
)
PLACEHOLDER_RE = re.compile(r"__\s*V\s*(\d+)\s*__", re.IGNORECASE)

# Regexes do consertar_crash.py integradas:
FMT_REGEX = re.compile(r"%[+0]*\d*(?:\.\d+)?[hlL]?[diouxXfFeEgGsc]")
VAR_REGEX = re.compile(r"\$[^$\s]+\$")

ENCODING_WARNINGS: list[str] = []


def _cp1252_passthru(err):
    """Lê/escreve os 5 bytes indefinidos do cp1252 sem perder o resto do arquivo (e sem cair em latin-1)."""
    if isinstance(err, UnicodeDecodeError):
        return "".join(chr(b) for b in err.object[err.start:err.end]), err.end
    if isinstance(err, UnicodeEncodeError):
        out = bytearray()
        for ch in err.object[err.start:err.end]:
            out += bytes([ord(ch)]) if ord(ch) in (0x81, 0x8D, 0x8F, 0x90, 0x9D) else b"?"
        return bytes(out), err.end
    raise err


codecs.register_error("cp1252_passthru", _cp1252_passthru)


def protect_tokens(text: str):
    """Troca tokens do motor por âncoras __V0__, __V1__... e devolve (texto, {id: token})."""
    placeholders: dict[int, str] = {}

    def replacer(match):
        idx = len(placeholders)
        placeholders[idx] = match.group(0)
        return f"__V{idx}__"

    return TOKEN_REGEX.sub(replacer, text), placeholders


def make_anchor_id(index: int, english_protected: str) -> str:
    """
    Cria uma chave semântica (ex: '0_Portugal', '1_Scotland') em vez de '0', '1'.
    Isso trava a atenção do LLM na palavra certa e impede deslocamento de linhas (off-by-one).
    """
    clean_text = PLACEHOLDER_RE.sub("", english_protected)
    slug = re.sub(r"[^A-Za-z0-9]", "", clean_text)[:10]
    return f"{index}_{slug}" if slug else str(index)


def is_crash_safe(en_raw: str, pt_restored: str) -> bool:
    """
    Regras do consertar_crash.py:
    1. Especificadores C++ (%s, %d...) precisam ter a exata mesma quantidade e ORDEM!
    2. Variáveis $VAR$ precisam ser idênticas.
    3. Símbolos $, [ e ] não podem estar quebrados/ímpares.
    """
    if FMT_REGEX.findall(en_raw) != FMT_REGEX.findall(pt_restored):
        return False
    if sorted(VAR_REGEX.findall(en_raw)) != sorted(VAR_REGEX.findall(pt_restored)):
        return False
    # '$' soltos (ex.: "$25 million") podem sumir ("25 milhões de dólares"), mas nunca surgir novos
    if VAR_REGEX.sub("", pt_restored).count("$") > VAR_REGEX.sub("", en_raw).count("$"):
        return False
    if (
        (en_raw.count("[") != pt_restored.count("["))
        or (en_raw.count("]") != pt_restored.count("]"))
    ):
        return False
    return True


FAIL_REASONS: dict[str, str] = {}
FAIL_HINTS: dict[str, str] = {}   # feedback enviado ao modelo na retentativa do mesmo item


def build_hint(reason: str, translated: str, placeholders: dict) -> str:
    if reason.startswith("âncoras divergentes"):
        found = Counter(int(n) for n in PLACEHOLDER_RE.findall(translated))
        missing = [f"__V{i}__" for i in placeholders if found[i] == 0]
        extra = [f"__V{i}__" for i, n in found.items() if i not in placeholders or n > 1]
        return ("Rejected. " + (f"Missing tokens: {missing}. " if missing else "")
                + (f"Repeated or unknown tokens: {extra}. " if extra else "")
                + "Every __V#__ token of the input must appear exactly once in the output, "
                  "even if it looks meaningless (for example right after a % sign).")
    return f"Rejected: {reason[:300]}. Keep '$', '[' and ']' exactly as in the source."


def check_translation(english_raw: str, english_protected: str, translated: str, placeholders: dict):
    """Devolve None se a tradução é válida; senão, o motivo da rejeição (vai para o log de falhas)."""
    if not isinstance(translated, str):
        return "resposta não é string"

    found = Counter(int(n) for n in PLACEHOLDER_RE.findall(translated))
    expected = Counter(placeholders.keys())
    if found != expected:
        return (f"âncoras divergentes: esperado {sorted(expected.elements())}, "
                f"voltou {sorted(found.elements())}")

    restored = restore_tokens(translated, placeholders)
    if not is_crash_safe(english_raw, restored):
        return (f"is_crash_safe falhou: formatos en={FMT_REGEX.findall(english_raw)} "
                f"pt={FMT_REGEX.findall(restored)}; vars en={VAR_REGEX.findall(english_raw)} "
                f"pt={VAR_REGEX.findall(restored)}; "
                f"$ {english_raw.count('$')}/{restored.count('$')}, "
                f"[ {english_raw.count('[')}/{restored.count('[')}, "
                f"] {english_raw.count(']')}/{restored.count(']')}")

    clean_en = PLACEHOLDER_RE.sub("", english_protected).strip()
    clean_pt = PLACEHOLDER_RE.sub("", translated).strip()

    if clean_en and not clean_pt:
        return "tradução vazia"

    # Trava de proporção de tamanho (impede que uma palavra curta receba uma frase inteira)
    len_en, len_pt = len(clean_en), len(clean_pt)
    if len_en > 0:
        ratio = (len_pt + 4) / (len_en + 4)
        if ratio > 2.35 or ratio < 0.40:
            return f"proporção de tamanho {ratio:.2f} fora do intervalo 0.40-2.35"

    return None


def sanity_check(english_raw: str, english_protected: str, translated: str, placeholders: dict) -> bool:
    return check_translation(english_raw, english_protected, translated, placeholders) is None


def restore_tokens(translated: str, placeholders: dict) -> str:
    """Restaura os tokens originais (tolerante a espaços dentro da âncora)."""
    return PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], translated)


# --- Artigo inicial em nomes próprios ("The United Kingdom" -> "Reino Unido", não "O Reino Unido") ---
STRIP_NAME_ARTICLES = True
NAME_MAX_WORDS = 5
NAME_SMALL_WORDS = {
    "of", "the", "and", "in", "on", "upon", "for", "to", "de", "del", "la", "le", "les", "du", "des",
    "von", "van", "der", "den", "y", "e", "al", "el", "da", "do", "di",
}
PT_LEADING_ARTICLE_RE = re.compile(r"^(?:Os|As|O|A)\s+(?=\S)")


def looks_like_proper_name(en: str) -> bool:
    """Texto curto, sem pontuação de frase, sem variáveis, com as palavras em maiúscula (ex.: 'The Republic of Texas')."""
    s = en.strip()
    words = s.split()
    if not 1 <= len(words) <= NAME_MAX_WORDS:
        return False
    if re.search(r'[.,:;!?$§%\[\]\\()"]|__V', s):
        return False
    return all(w.lower() in NAME_SMALL_WORDS or w[0].isupper() or w[0].isdigit() for w in words)


def strip_name_article(en: str, pt: str) -> str:
    """Tira o artigo maiúsculo (O/A/Os/As) do início de nomes próprios: ficam bem em qualquer frase e em caixinhas pequenas."""
    if not STRIP_NAME_ARTICLES or pt == en or not looks_like_proper_name(en):
        return pt
    m = PT_LEADING_ARTICLE_RE.match(pt)
    if not m or en.split()[0].lower() == m.group().strip().lower():  # ex.: "A Coruña" mantém o "A"
        return pt
    rest = pt[m.end():]
    return rest[:1].upper() + rest[1:]


def apply_ui_fits(en: str, pt: str) -> str:
    """Encurta traduções que sabidamente estouram caixinhas pequenas da interface do Victoria 2."""
    if en in UI_EXACT_FITS:
        return UI_EXACT_FITS[en]
    for bad_prefix, good_prefix in UI_PREFIX_FITS:
        if pt.startswith(bad_prefix):
            pt = good_prefix + pt[len(bad_prefix):]
    return pt


def sanitize_for_vic2(text: str, context: str = "") -> str:
    """
    Roda SOMENTE sobre o texto traduzido (campo isolado), nunca sobre a linha inteira.
    - ';' dentro do texto quebraria a contagem de colunas -> vira ','
    - quebras de linha reais viram o literal \\n
    - aspas/travessões tipográficos viram ASCII
    - caracteres fora do cp1252 são trocados por '?' e registrados em log
    """
    text = text.replace(";", ",")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "")
    replacements = {
        "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
        "\u2014": "-", "\u2013": "-", "\u2026": "...",
        "\u0152": "OE", "\u0153": "oe", "\x8c": "OE", "\x9c": "oe",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    try:
        text.encode(ENCODING)
    except UnicodeEncodeError:
        bad_chars = sorted({c for c in text if not _encodable(c)})
        ENCODING_WARNINGS.append(f"{context}: fora do cp1252 (transliterado quando possível) {bad_chars}")
        text = "".join(c if _encodable(c) else _fold_char(c) for c in text)
    return text


def _encodable(ch: str) -> bool:
    try:
        ch.encode(ENCODING)
        return True
    except UnicodeEncodeError:
        return False


def _fold_char(ch: str) -> str:
    """Último recurso p/ caractere fora do cp1252: acha o equivalente cp1252 (C1 mal decodificado) ou tira o acento."""
    if 0x80 <= ord(ch) <= 0x9F:
        try:
            fixed = bytes([ord(ch)]).decode(ENCODING)
            if _encodable(fixed):
                return fixed
        except UnicodeDecodeError:
            pass
    special = {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "ı": "i", "Ħ": "H", "ħ": "h"}
    if ch in special:
        return special[ch]
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return base if base and all(_encodable(c) for c in base) else "?"


# ==============================================================================
# 3. CACHE PERSISTENTE E LIMPEZA CIRÚRGICA
# ==============================================================================
def text_hash(english: str) -> str:
    return hashlib.sha1(english.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"[AVISO] Cache ilegível ({exc}). Fazendo backup e começando um novo.")
            CACHE_FILE.replace(CACHE_FILE.with_suffix(".json.bak"))
    return {}


def save_cache(cache: dict):
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(CACHE_FILE)  # troca atômica: Ctrl+C no meio não corrompe o cache


def clean_corrupted_cache(files: list[Path], cache: dict) -> int:
    """
    Remove do cache dos arquivos selecionados:
    1. Qualquer tradução que viole o is_crash_safe (%s/%d fora de ordem ou $VAR$ quebrado).
    2. Textos curtos (< 45 chars) sem variáveis ou com proporção de tamanho inválida.
    """
    removed = 0
    for src in files:
        rows = parse_rows(read_lines(src))
        for kind, payload in rows:
            if kind != "row":
                continue
            en = payload[ENGLISH_COL]
            h = text_hash(en)
            if h not in cache:
                continue

            pt = cache[h]
            protected_en, ph = protect_tokens(en)
            protected_pt, _ = protect_tokens(pt)

            is_short_no_tokens = (len(ph) == 0 and len(en) < 45)
            ratio = (len(protected_pt) + 4) / (len(protected_en) + 4)
            bad_ratio = (ratio > 2.35 or ratio < 0.40)

            if not is_crash_safe(en, pt) or is_short_no_tokens or bad_ratio:
                del cache[h]
                removed += 1

    if removed > 0:
        backup_path = CACHE_FILE.with_suffix(".json.before_clean.bak")
        if CACHE_FILE.exists():
            CACHE_FILE.replace(backup_path)
            print(f"[BACKUP] Cache antigo salvo em: {backup_path.name}")
        save_cache(cache)

    return removed


# ==============================================================================
# 4. INTEGRAÇÃO COM DEEPSEEK
# ==============================================================================
def build_system_prompt() -> str:
    glossary = "\n".join(f'- "{en}" -> "{pt}"' for en, pt in GLOSSARY.items())
    return (
        "You are a professional game localizer translating the Victoria 2 mod "
        "'Greater Flavor Mod' from English into " + TARGET_LANG + ".\n\n"
        "INPUT: a JSON object mapping anchored ids (like '0_Portugal') to English strings.\n"
        "OUTPUT: ONLY a valid JSON object with the EXACT SAME ids mapped to their exact Portuguese "
        "translations. No comments, no markdown.\n\n"
        "RULES:\n"
        "1. NEVER shift, swap or misalign translations between keys. Each key MUST contain the exact "
        "translation of its own English string.\n"
        "2. Short strings (1 to 4 words) are country names, provinces, reforms or UI buttons: "
        "translate them literally, accurately and VERY concisely so they fit inside small UI boxes.\n"
        "3. Tokens like __V0__, __V1__ are engine variables. Copy each one_in_the_exact_same_order, once, "
        "with the exact same spelling. Never translate, delete, duplicate or invent them.\n"
        "4. Never use the semicolon character (;). Use a comma or a period instead.\n"
        "5. Use only straight ASCII quotes (\" and '). No curly quotes, no em/en dashes, no ellipsis character.\n"
        "6. Do not add line breaks. Line breaks are already encoded as tokens.\n"
        "7. Keep proper nouns of people, places and historical events in their standard Portuguese "
        "form when one exists (e.g. 'Portugal' -> 'Portugal', 'Scotland' -> 'Escócia'), otherwise leave unchanged.\n"
        "8. Mandatory glossary (inflect gender and number as the sentence requires, "
        "e.g. 'Great Powers' -> 'Grandes Potências'):\n" + glossary + "\n"
        "9. If a string has nothing to translate, return it unchanged.\n"
        "10. When the WHOLE English string is just a proper name (country, state, province, culture, "
        "nationality, party), never start the Portuguese with an article: 'The United Kingdom' -> "
        "'Reino Unido' (not 'O Reino Unido'), 'The Netherlands' -> 'Países Baixos'.\n"
    )


def call_model(client: OpenAI, items: dict, hints: dict | None = None) -> dict:
    """Uma chamada: {id_ancorado: texto_protegido} -> {id_ancorado: tradução_crua}."""
    user_content = json.dumps(items, ensure_ascii=False)
    if hints:
        user_content += ("\n\nNOTES: these ids were rejected in a previous attempt. Fix the problem described, "
                        "and answer ONLY with the JSON object:\n" + json.dumps(hints, ensure_ascii=False))
    response = client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
        messages=[
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": user_content},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def translate_batch(client: OpenAI, batch: dict, source_by_id: dict, placeholders_by_id: dict,
                    max_retries: int = MAX_RETRIES) -> dict:
    """
    Traduz um lote com retries. Só devolve itens VÁLIDOS (âncoras intactas + is_crash_safe + tamanho coerente).
    """
    pending = dict(batch)
    results: dict[str, str] = {}

    for attempt in range(1, max_retries + 1):
        try:
            data = call_model(client, pending, {k: FAIL_HINTS[k] for k in pending if k in FAIL_HINTS})
        except Exception as exc:
            wait = 2 ** attempt
            tqdm.write(f"[tentativa {attempt}/{max_retries}] erro da API: {exc} (aguardando {wait}s)")
            time.sleep(wait)
            continue

        for item_id in list(pending):
            translated = data.get(item_id)
            if translated is None:
                FAIL_REASONS[item_id] = "id ausente (ou valor não-string) na resposta do modelo"
                FAIL_HINTS[item_id] = "Your answer lacked this id. Return every id exactly as given."
                continue
            reason = check_translation(
                source_by_id[item_id], pending[item_id], translated, placeholders_by_id[item_id]
            )
            if reason is None:
                results[item_id] = restore_tokens(translated, placeholders_by_id[item_id])
                del pending[item_id]
            else:
                FAIL_REASONS[item_id] = f"{reason} | resposta do modelo: {translated!r}"
                FAIL_HINTS[item_id] = build_hint(reason, translated, placeholders_by_id[item_id])

        if not pending:
            break
        tqdm.write(f"[tentativa {attempt}/{max_retries}] {len(pending)} item(ns) inválido(s), repetindo")
        time.sleep(1)

    return results


# ==============================================================================
# 5. PROCESSAMENTO DOS ARQUIVOS DE LOCALIZAÇÃO
# ==============================================================================
def read_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    text = raw.decode(ENCODING, errors="cp1252_passthru")
    # Remove \r soltos no meio das frases e divide APENAS em \n real
    text = text.replace("\r\n", "\n").replace("\r", " ")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def needs_translation(protected: str) -> bool:
    """Só vale traduzir se sobrar alguma palavra além das âncoras (evita gastar token com '$X$' ou números)."""
    return bool(re.search(r"[A-Za-z]{2,}", re.sub(r"__V\d+__", "", protected)))


def parse_rows(lines: list[str]) -> list:
    """Cada linha vira ('raw', texto) [comentário/vazia/sem coluna de inglês] ou ('row', campos)."""
    rows = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            rows.append(("raw", line))
            continue
        fields = line.split(";")
        if len(fields) <= ENGLISH_COL or not fields[ENGLISH_COL].strip():
            rows.append(("raw", line))
            continue
        rows.append(("row", fields))
    return rows


def translate_pending(client, pending_texts: list, cache: dict, file_label: str):
    """Traduz textos em inglês ainda fora do cache, em lotes, salvando o cache a cada lote."""
    for start in tqdm(range(0, len(pending_texts), BATCH_SIZE), desc=file_label, unit="lote"):
        chunk = pending_texts[start:start + BATCH_SIZE]
        FAIL_REASONS.clear()
        FAIL_HINTS.clear()

        batch, ph_by_id, source_by_id = {}, {}, {}
        for i, english in enumerate(chunk):
            protected, placeholders = protect_tokens(english)
            item_id = make_anchor_id(i, protected)
            batch[item_id] = protected
            ph_by_id[item_id] = placeholders
            source_by_id[item_id] = english

        results = translate_batch(client, batch, source_by_id, ph_by_id)

        # Itens que falharam no lote: última chance, um por um
        for item_id in [k for k in batch if k not in results]:
            single = translate_batch(
                client,
                {item_id: batch[item_id]},
                {item_id: source_by_id[item_id]},
                {item_id: ph_by_id[item_id]},
                max_retries=2,
            )
            results.update(single)

        for item_id, english in source_by_id.items():
            if item_id in results:
                cache[text_hash(english)] = sanitize_for_vic2(results[item_id], context=file_label)
            else:
                reason = FAIL_REASONS.get(item_id, "motivo desconhecido")
                with open(FAILURES_LOG, "a", encoding="utf-8") as log:
                    log.write(f"{file_label}\t{english}\n\t-> {reason}\n")
                tqdm.write(f"[FALHA] mantido em inglês: {english[:70]!r}")
                tqdm.write(f"        motivo: {reason[:400]}")

        save_cache(cache)


def process_file(client, src: Path, cache: dict, dry_run: bool) -> int:
    rows = parse_rows(read_lines(src))

    # 1. Auditoria automática anti-crash no cache antes de verificar pendências:
    # Se o cache tinha alguma linha antiga que quebrava o C++ (detectada pelo consertar_crash.py),
    # ela já é removida daqui para ser retraduzida corretamente ou mantida segura!
    cache_dirty = False
    for kind, payload in rows:
        if kind == "row":
            en = payload[ENGLISH_COL]
            h = text_hash(en)
            if h in cache and not is_crash_safe(en, cache[h]):
                del cache[h]
                cache_dirty = True
    if cache_dirty and not dry_run:
        save_cache(cache)

    # 2. Textos únicos ainda não traduzidos (ordem preservada)
    pending, seen = [], set()
    for kind, payload in rows:
        if kind != "row":
            continue
        english = payload[ENGLISH_COL]
        h = text_hash(english)
        if h in cache or h in seen:
            continue
        if not needs_translation(protect_tokens(english)[0]):
            continue
        seen.add(h)
        pending.append(english)

    if dry_run:
        return len(pending)

    if pending:
        translate_pending(client, pending, cache, src.name)

    # 3. Monta o arquivo de saída com proteção final do consertar_crash.py + ajuste de caixinha UI
    out_name = src.name if src.name.startswith(OUTPUT_PREFIX) else OUTPUT_PREFIX + src.name
    out_lines = []
    crash_prevented = 0

    for kind, payload in rows:
        if kind == "raw":
            out_lines.append(payload)
            continue
        fields = list(payload)
        en = fields[ENGLISH_COL]
        pt = cache.get(text_hash(en), en)
        pt = strip_name_article(en, pt)
        pt = apply_ui_fits(en, pt)

        # Barreira final (consertar_crash): nunca escreve no CSV uma linha que feche o jogo
        if not is_crash_safe(en, pt):
            pt = en
            crash_prevented += 1

        fields[ENGLISH_COL] = pt
        if len(fields) < TOTAL_COLS:
            fields += [""] * (TOTAL_COLS - len(fields))
            fields[TOTAL_COLS - 1] = "x"
        out_lines.append(";".join(fields))

    if crash_prevented > 0:
        print(f"[ANTI-CRASH] {crash_prevented} linha(s) perigosa(s) neutralizada(s) em {src.name}.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / out_name
    tmp_path = out_path.with_suffix(".csv.tmp")

    # Escrita atômica com errors="replace" para proteger contra bytes latin-1 nas outras colunas
    with open(tmp_path, "w", encoding=ENCODING, errors="cp1252_passthru", newline="") as f:
        f.write("\r\n".join(out_lines) + "\r\n")
    tmp_path.replace(out_path)

    return len(pending)


def drop_article_names_from_cache(files: list[Path], cache: dict) -> int:
    """Remove do cache nomes próprios traduzidos COM artigo, para serem retraduzidos com a regra nova do prompt."""
    removed = 0
    for src in files:
        for kind, payload in parse_rows(read_lines(src)):
            if kind != "row":
                continue
            en = payload[ENGLISH_COL]
            h = text_hash(en)
            pt = cache.get(h)
            if pt and strip_name_article(en, pt) != pt:
                del cache[h]
                removed += 1
    if removed:
        if CACHE_FILE.exists():
            CACHE_FILE.with_suffix(".json.before_names.bak").write_bytes(CACHE_FILE.read_bytes())
        save_cache(cache)
    return removed


def select_files(only_arg: str) -> list[Path]:
    """
    Se o usuário passar um nome terminado em .csv (ex: --only text.csv), busca o arquivo EXATO.
    Caso contrário (ex: --only events), busca arquivos que contenham o termo no nome.
    """
    target = only_arg.strip().lower()
    all_csvs = sorted(SOURCE_DIR.glob("*.csv"))
    if not target:
        return all_csvs
    if target.endswith(".csv"):
        return [p for p in all_csvs if p.name.lower() == target]
    return [p for p in all_csvs if target in p.name.lower()]


# ==============================================================================
# 6. EXECUÇÃO
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Tradutor GFM (Victoria 2) com DeepSeek")
    parser.add_argument("--dry-run", action="store_true", help="só conta linhas pendentes, sem chamar a API")
    parser.add_argument("--only", default="", help="nome exato do arquivo (ex: text.csv) ou trecho do nome")
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help="remove do cache traduções curtas/deslocadas dos arquivos selecionados antes de traduzir",
    )
    parser.add_argument(
        "--retranslate-names",
        action="store_true",
        help="retraduz (via API, com a regra de 'sem artigo' do prompt) os nomes próprios que estão no cache com O/A/Os/As",
    )
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("Defina DEEPSEEK_API_KEY no arquivo .env")

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    files = select_files(args.only)
    if not files:
        sys.exit(f"Nenhum .csv correspondente a '{args.only}' encontrado em {SOURCE_DIR}")

    cache = load_cache()

    if args.clean_cache:
        removed = clean_corrupted_cache(files, cache)
        print(f"[LIMPEZA] {removed} entrada(s) curta(s)/deslocada(s) removida(s) do cache.")

    if args.retranslate_names:
        removed = drop_article_names_from_cache(files, cache)
        print(f"[NOMES] {removed} nome(s) com artigo removido(s) do cache, serão retraduzidos agora.")

    client = OpenAI(api_key=api_key or "dry-run", base_url=BASE_URL)
    total = 0

    try:
        for src in files:
            total += process_file(client, src, cache, args.dry_run)
    except KeyboardInterrupt:
        save_cache(cache)
        print("\nInterrompido. Cache salvo, é só rodar de novo para continuar.")
        return

    print(f"\n{'Pendentes' if args.dry_run else 'Traduzidos agora'}: {total} textos únicos em {len(files)} arquivo(s).")
    if ENCODING_WARNINGS:
        print(f"[AVISO] {len(ENCODING_WARNINGS)} caso(s) com caractere fora do cp1252 (viraram '?'):")
        for warning in ENCODING_WARNINGS[:20]:
            print("  -", warning)
    if FAILURES_LOG.exists():
        print(f"[AVISO] Linhas que falharam e ficaram em inglês: {FAILURES_LOG}")


if __name__ == "__main__":
    main()