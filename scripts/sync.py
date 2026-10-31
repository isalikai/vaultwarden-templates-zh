#!/usr/bin/env python3
"""Sync Vaultwarden's email templates from an official release and translate
them into Simplified Chinese.

Design (why it is safe to run unattended):

* Only the *text* of a template is ever sent to a translation service.
  Handlebars expressions ({{...}}), HTML tags, comments, <style> blocks, URLs
  and HTML entities are cut out of the text and replaced by numbered tokens
  such as ⟦1⟧. After translation every token must come back exactly once,
  otherwise that sentence is rejected and stays in English.
* The translated text is spliced back into the *original* file, so the markup
  and template logic of the official file are kept byte for byte.
* A final whole-file check compares all Handlebars expressions, tags and URLs
  of the original and the result. Any difference -> the English original is used.
* Translations are stored per sentence in translations.json (a translation
  memory). Re-runs only translate new or changed sentences, and you can fix a
  translation by editing that file.

Standard library only, so the GitHub Actions job needs no `pip install`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_REPO = "dani-garcia/vaultwarden"
UPSTREAM_SUBDIR = "src/static/templates/email"

OPEN, CLOSE = "⟦", "⟧"  # ⟦ ⟧
PH_RE = re.compile(OPEN + r"(\d+)" + CLOSE)

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

# Order matters: comments / style+script blocks / doctype first, then Handlebars
# (triple braces before double braces), then ordinary tags.
HTML_TOKEN_RE = re.compile(
    r"(?P<comment><!--[\s\S]*?-->)"
    r"|(?P<block><(?P<bt>style|script)\b[\s\S]*?</(?P=bt)\s*>)"
    r"|(?P<doctype><![^>]*>)"
    r"|(?P<hbs3>\{\{\{[\s\S]*?\}\}\})"
    r"|(?P<hbs>\{\{[\s\S]*?\}\})"
    r"|(?P<tag></?[A-Za-z][^<>]*>)",
    re.I,
)
HBS_RE = re.compile(r"\{\{\{[\s\S]*?\}\}\}|\{\{[\s\S]*?\}\}")
# Things inside running text that must also be protected.
INLINE_RE = re.compile(
    r"https?://[^\s<>\"'⟦]+"
    r"|&(?:[A-Za-z][A-Za-z0-9]*|#\d+|#[xX][0-9A-Fa-f]+);"
    r"|[^\s<>⟦⟧]*[/?=#][^\s<>⟦⟧]*"  # glued URL pieces such as /#/verify-email/?userId=
)
URL_RE = re.compile(r"https?://[^\s<>\"'{}]+")
TAG_NAME_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)")
SINGLE_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)[^<>]*>")
INLINE_TAGS = {"a", "b", "i", "u", "em", "strong", "span", "code", "small", "sub", "sup", "s", "mark", "font"}

LEAD_HTML = re.compile(r"^(\s*)(.*?)(\s*)$", re.S)
LEAD_TEXT = re.compile(r"^(\s*(?:(?:[*+\-]|\d+[.)])\s+)?)(.*?)(\s*)$", re.S)


def is_html_file(name: str) -> bool:
    return name.endswith(".html.hbs") or name in ("email_header.hbs", "email_footer.hbs")


def classify(tok: str) -> str:
    """'sep' = structural, never part of a sentence; 'inline' = kept inside a sentence."""
    if tok.startswith("{{{"):
        return "inline"
    if tok.startswith("{{"):
        inner = tok[2:-2].lstrip("~").lstrip()
        if inner and (inner[0] in "#/>!^" or inner.startswith("else")):
            return "sep"
        return "inline"
    if tok.startswith("<!"):
        return "sep"
    m = TAG_NAME_RE.match(tok)
    if m and m.group(2).lower() in INLINE_TAGS and not tok.endswith("/>"):
        return "inline"
    return "sep"


def tokenize(src: str, html: bool) -> list[list[str]]:
    parts: list[list[str]] = []
    pos = 0
    rx = HTML_TOKEN_RE if html else HBS_RE
    for m in rx.finditer(src):
        if m.start() > pos:
            parts.append(["text", src[pos:m.start()]])
        parts.append([classify(m.group(0)), m.group(0)])
        pos = m.end()
    if pos < len(src):
        parts.append(["text", src[pos:]])
    if not html:  # plain-text templates: every line is its own sentence
        split: list[list[str]] = []
        for kind, text in parts:
            if kind != "text":
                split.append([kind, text])
                continue
            for piece in re.split(r"(\n)", text):
                if piece:
                    split.append(["sep" if piece == "\n" else "text", piece])
        parts = split
    assert "".join(t for _, t in parts) == src, "tokenizer must be lossless"
    return parts


@dataclass
class Segment:
    i0: int
    i1: int
    lead: str
    trail: str
    core: str  # text with ⟦n⟧ tokens, whitespace-normalised: this is the cache key
    toks: list[str]
    pairs: list[tuple[int, int]]  # (open_id, close_id) of inline tag pairs


def find_segments(parts: list[list[str]], html: bool) -> list[Segment]:
    segs: list[Segment] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        atoms: list[tuple[bool, str]] = []  # (is_protected, text)
        for idx in run:
            kind, t = parts[idx]
            if kind == "text":
                pos = 0
                for m in INLINE_RE.finditer(t):
                    if m.start() > pos:
                        atoms.append((False, t[pos:m.start()]))
                    atoms.append((True, m.group(0)))
                    pos = m.end()
                if pos < len(t):
                    atoms.append((False, t[pos:]))
            else:
                atoms.append((True, t))
        # Protected pieces that touch each other (<b>{{name}}</b>, {{url}}/#/path?x={{id}}, ...)
        # become ONE token, so the translator can neither reorder nor split them.
        merged: list[tuple[bool, str]] = []
        for protected, t in atoms:
            if protected and merged and merged[-1][0]:
                merged[-1] = (True, merged[-1][1] + t)
            else:
                merged.append((protected, t))
        toks: list[str] = []
        text = ""
        for protected, t in merged:
            if protected:
                toks.append(t)
                text += f"{OPEN}{len(toks)}{CLOSE}"
            else:
                text += t
        lead, core, trail = (LEAD_HTML if html else LEAD_TEXT).match(text).groups()
        if html:
            core = re.sub(r"\s+", " ", core)
        if re.search(r"[A-Za-z]", PH_RE.sub("", core)):
            pairs: list[tuple[int, int]] = []
            stack: list[tuple[str, int]] = []
            for n, tok in enumerate(toks, 1):
                m = SINGLE_TAG_RE.fullmatch(tok)
                if not m or tok.endswith("/>"):
                    continue
                name = m.group(2).lower()
                if m.group(1) == "":
                    stack.append((name, n))
                elif stack and stack[-1][0] == name:
                    pairs.append((stack.pop()[1], n))
            segs.append(Segment(run[0], run[-1], lead, trail, core, toks, pairs))

    for idx, (kind, _) in enumerate(parts):
        if kind == "sep":
            flush()
            run = []
        else:
            run.append(idx)
    flush()
    return segs


def validate(core: str, pairs: list[tuple[int, int]], out: str) -> str | None:
    """Return None if `out` is an acceptable translation of `core`, else the reason."""
    n = len(PH_RE.findall(core))
    ids = sorted(int(x) for x in PH_RE.findall(out))
    if ids != list(range(1, n + 1)):
        return f"each of the {n} tokens {OPEN}1{CLOSE}..{OPEN}{n}{CLOSE} must appear exactly once"
    for o, c in pairs:
        if out.index(f"{OPEN}{o}{CLOSE}") > out.index(f"{OPEN}{c}{CLOSE}"):
            return f"{OPEN}{o}{CLOSE} must come before {OPEN}{c}{CLOSE}"
    rest = PH_RE.sub("", out)
    if not rest.strip():
        return "empty translation"
    if OPEN in rest or CLOSE in rest:
        return "malformed token"
    if "\n" in out or "\r" in out:
        return "must be a single line"
    if re.search(r"\{\{|\}\}|<[A-Za-z/!]|https?://", out):
        return "must not contain HTML tags, braces or URLs"
    return None


def render(src: str, html: bool, lookup) -> tuple[str, list[Segment]]:
    """Splice translations into `src`. Returns (new text, segments left in English)."""
    parts = tokenize(src, html)
    left: list[Segment] = []
    for s in find_segments(parts, html):
        out = lookup(s.core)
        if out is None or validate(s.core, s.pairs, out):
            left.append(s)
            continue
        restored = PH_RE.sub(lambda m: s.toks[int(m.group(1)) - 1], out.strip())
        parts[s.i0][1] = s.lead + restored + s.trail
        for k in range(s.i0 + 1, s.i1 + 1):
            parts[k][1] = ""
    return "".join(t for _, t in parts), left


def skeleton(text: str, html: bool) -> tuple[list[str], list[str]]:
    rx = HTML_TOKEN_RE if html else HBS_RE
    return sorted(m.group(0) for m in rx.finditer(text)), sorted(URL_RE.findall(text))


def final_check(src: str, out: str, html: bool) -> bool:
    return skeleton(src, html) == skeleton(out, html)


# --------------------------------------------------------------------------
# Translation providers
# --------------------------------------------------------------------------

class RateLimited(Exception):
    pass


class ProviderError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


MAX_WAIT = 120  # a longer Retry-After means a daily quota is gone: give up on this provider


def http_json(url: str, headers: dict, payload: dict, timeout: int = 120, retries: int = 4) -> dict:
    data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After", "")
                wait = float(ra) if re.fullmatch(r"\d+(\.\d+)?", ra) else min(5 * 2 ** attempt, 60)
                if e.code == 429 and wait > MAX_WAIT:
                    raise RateLimited(f"rate limit, retry-after {wait:.0f}s")
                if attempt == retries:
                    raise (RateLimited if e.code == 429 else ProviderError)(f"HTTP {e.code}: {body}")
                log(f"  HTTP {e.code}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            raise ProviderError(f"HTTP {e.code}: {body}", e.code)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                raise ProviderError(f"network error: {e}")
            time.sleep(min(5 * 2 ** attempt, 60))
    raise ProviderError("unreachable")


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a < 0 or b <= a:
            raise ProviderError("model did not return JSON")
        obj = json.loads(text[a:b + 1])
    if not isinstance(obj, dict):
        raise ProviderError("model did not return a JSON object")
    return obj


def load_glossary() -> dict:
    p = ROOT / "glossary.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def system_prompt() -> str:
    g = load_glossary()
    keep = ", ".join(g.get("keep", []))
    terms = "\n".join(f"- {k} => {v}" for k, v in g.get("terms", {}).items())
    return f"""You are a localization engineer translating the e-mail templates of Vaultwarden (a self-hosted, Bitwarden-compatible password manager) from English into Simplified Chinese (zh-CN).

You receive a JSON object that maps ids to English strings (sentence fragments from e-mails). Return ONLY a JSON object with exactly the same keys and the Chinese translations as values.

Rules:
1. Tokens such as {OPEN}1{CLOSE}, {OPEN}2{CLOSE} stand for variables, links, HTML tags, URLs or entities. Copy every token that appears in a string into its translation EXACTLY ONCE, unchanged. You may move tokens to wherever they read naturally in Chinese. When two tokens wrap some text, like {OPEN}1{CLOSE}text{OPEN}2{CLOSE}, keep the first before the second and translate the text between them.
2. Never invent, delete, renumber or translate tokens. Never write HTML tags, curly braces or URLs yourself.
3. Keep product and technical names unchanged: {keep}.
4. Tone: polite, concise and neutral, like a security notification. Use full-width Chinese punctuation. Do not add spaces around tokens unless the English has them.
5. The strings are fragments: keep leading/trailing punctuation such as colons, keep each translation on a single line, add no explanations.
6. Use this glossary consistently:
{terms}
"""


class LLMProvider:
    name = "llm"

    def available(self) -> bool:
        raise NotImplementedError

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def translate(self, batch: dict[str, str], feedback: dict[str, str]) -> dict[str, str]:
        user = "Translate the values of this JSON object into Simplified Chinese. Return only a JSON object with the same keys.\n"
        if feedback:
            user += "A previous attempt was rejected for these keys, fix them:\n"
            user += "\n".join(f"- {k}: {why}" for k, why in feedback.items()) + "\n"
        user += json.dumps(batch, ensure_ascii=False, indent=1)
        obj = extract_json(self.complete(system_prompt(), user))
        return {k: v for k, v in obj.items() if isinstance(v, str)}


class GitHubModels(LLMProvider):
    name = "github"
    URL = "https://models.github.ai/inference/chat/completions"

    def __init__(self) -> None:
        self.model = os.environ.get("GH_MODELS_MODEL") or "openai/gpt-4.1-mini"

    def available(self) -> bool:
        return bool(os.environ.get("GITHUB_TOKEN"))

    def complete(self, system: str, user: str) -> str:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        try:
            res = http_json(self.URL, headers, payload)
        except ProviderError as e:
            if e.code != 400:
                raise
            payload.pop("response_format")  # some models reject JSON mode
            res = http_json(self.URL, headers, payload)
        try:
            return res["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response shape")


class Gemini(LLMProvider):
    name = "gemini"
    URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self) -> None:
        self.model = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def complete(self, system: str, user: str) -> str:
        url = self.URL_TMPL.format(model=self.model)
        headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        res = http_json(url, headers, payload)
        try:
            return res["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response shape")


class Mock:
    """Offline stand-in for tests: prefixes the text. MOCK_BREAK_EVERY=N makes ~1/N
    of the sentences come back with a token missing, to exercise the safety net."""
    name = "mock"

    def available(self) -> bool:
        return True

    def translate(self, batch: dict[str, str], feedback: dict[str, str]) -> dict[str, str]:
        n = int(os.environ.get("MOCK_BREAK_EVERY", "0") or 0)
        out = {}
        for k, v in batch.items():
            t = "【译】" + v
            if n and zlib.crc32(v.encode()) % n == 0 and PH_RE.search(t):
                t = PH_RE.sub("", t, count=1)
            out[k] = t
        return out


PROVIDERS = {"github": GitHubModels, "gemini": Gemini, "mock": Mock}


class AllProvidersFailed(Exception):
    pass


MAX_ITEMS, MAX_CHARS, MAX_ATTEMPTS = 25, 3000, 3


def make_batches(keys: list[str]) -> list[list[str]]:
    batches, cur, size = [], [], 0
    for k in keys:
        cur.append(k)
        size += len(k)
        if len(cur) >= MAX_ITEMS or size >= MAX_CHARS:
            batches.append(cur)
            cur, size = [], 0
    if cur:
        batches.append(cur)
    return batches


def translate_missing(needed: dict[str, Segment], cache: dict[str, str], providers: list, delay: float,
                      save) -> int:
    """Translate every key in `needed` that the cache cannot serve. Returns #new translations."""
    done = 0
    alive = list(providers)
    for batch in make_batches(list(needed)):
        todo = {str(i): k for i, k in enumerate(batch, 1)}
        feedback: dict[str, str] = {}
        for _ in range(MAX_ATTEMPTS):
            if not todo:
                break
            payload = {i: k for i, k in todo.items()}
            result = None
            while alive and result is None:
                p = alive[0]
                try:
                    result = p.translate(payload, {i: feedback[i] for i in todo if i in feedback})
                except RateLimited as e:
                    log(f"  provider {p.name}: {e}; switching")
                    alive.pop(0)
                except ProviderError as e:
                    log(f"  provider {p.name}: {e}; switching")
                    alive.pop(0)
                if delay and result is not None:
                    time.sleep(delay)
            if result is None:
                raise AllProvidersFailed()
            for i, core in list(todo.items()):
                seg = needed[core]
                out = result.get(i)
                why = "missing from the response" if out is None else validate(core, seg.pairs, out.strip())
                if why is None:
                    cache[core] = out.strip()
                    done += 1
                    del todo[i]
                    feedback.pop(i, None)
                else:
                    feedback[i] = why
        for i, core in todo.items():
            log(f"  rejected, stays English: {core[:70]!r} ({feedback.get(i)})")
        save()
    return done


# --------------------------------------------------------------------------
# Sync driver
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){1,3})$")


def parse_ver(tag: str) -> tuple[int, ...]:
    m = VERSION_RE.match(tag.strip())
    if not m:
        raise SystemExit(f"unrecognised version: {tag!r}")
    return tuple(int(x) for x in m.group(1).split("."))


def latest_release_tag() -> str:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "vw-zh-sync"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["tag_name"]


def fetch_upstream(tag: str, workdir: Path) -> Path:
    log(f"cloning {UPSTREAM_REPO} @ {tag}")
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--no-tags", "--branch", tag,
         f"https://github.com/{UPSTREAM_REPO}.git", str(workdir)],
        check=True,
    )
    d = workdir / UPSTREAM_SUBDIR
    if not d.is_dir():
        raise SystemExit(f"{UPSTREAM_SUBDIR} not found in {tag}")
    return d


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", help="official version tag (default: latest release)")
    ap.add_argument("--retry", action="store_true", help="re-run even if VERSION is already up to date")
    ap.add_argument("--provider", default=os.environ.get("TRANSLATE_PROVIDER") or "github,gemini",
                    help="comma separated, tried in order: github, gemini, mock")
    ap.add_argument("--delay", type=float, default=5.0, help="seconds to wait after each API call")
    ap.add_argument("--upstream-dir", help="use a local copy of the email templates instead of cloning")
    ap.add_argument("--report", help="write a Markdown report (used as the PR body)")
    ap.add_argument("--stats", action="store_true", help="only print statistics, change nothing")
    ap.add_argument("--retranslate", action="store_true", help="ignore translations.json and translate everything again")
    ap.add_argument("--out", default=str(ROOT), help="repository root to write into (default: this repo)")
    args = ap.parse_args()

    out_root = Path(args.out)
    version_file = out_root / "VERSION"
    current = version_file.read_text().strip() if version_file.exists() else ""

    if args.version:
        if not VERSION_RE.match(args.version):
            raise SystemExit("--version must look like 1.37.4")
        target = args.version
    else:
        target = latest_release_tag()
        if not args.retry and current and parse_ver(target) <= parse_ver(current):
            log(f"up to date (translated: {current}, latest official: {target})")
            set_output("uptodate", "true")
            set_output("version", current)
            return 0
    set_output("uptodate", "false")
    set_output("version", target)

    tmp = tempfile.mkdtemp(prefix="vw-upstream-")
    try:
        src_dir = Path(args.upstream_dir) if args.upstream_dir else fetch_upstream(target, Path(tmp) / "repo")
        files = {p.name: p.read_bytes().decode("utf-8") for p in sorted(src_dir.glob("*.hbs"))}
        if not files:
            raise SystemExit("no .hbs files found upstream")

        cache_path = out_root / "translations.json"
        cache: dict[str, str] = {}
        if cache_path.exists() and not args.retranslate:
            cache = json.loads(cache_path.read_text("utf-8"))

        def save_cache() -> None:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")

        # 1. find every sentence and decide what the cache cannot serve
        all_segs: dict[str, list[Segment]] = {}
        needed: dict[str, Segment] = {}
        total = 0
        for name, text in files.items():
            segs = find_segments(tokenize(text, is_html_file(name)), is_html_file(name))
            all_segs[name] = segs
            total += len(segs)
            for s in segs:
                c = cache.get(s.core)
                if (c is None or validate(s.core, s.pairs, c)) and s.core not in needed:
                    needed[s.core] = s
        unique = len({s.core for segs in all_segs.values() for s in segs})
        log(f"{len(files)} files, {total} sentences ({unique} unique), {len(needed)} to translate, "
            f"{sum(len(k) for k in needed)} characters")
        if args.stats:
            return 0

        # 2. translate what is missing
        providers = []
        for pname in [x.strip() for x in args.provider.split(",") if x.strip()]:
            if pname not in PROVIDERS:
                raise SystemExit(f"unknown provider {pname!r}")
            p = PROVIDERS[pname]()
            if p.available():
                providers.append(p)
            else:
                log(f"provider {pname}: not configured, skipped")
        new = 0
        incomplete = False
        if needed:
            if not providers:
                log("no translation provider available; untranslated sentences stay in English")
                incomplete = True
            else:
                log("providers: " + ", ".join(f"{p.name}" + (f" ({p.model})" if hasattr(p, "model") else "") for p in providers))
                delay = 0 if all(p.name == "mock" for p in providers) else args.delay
                try:
                    new = translate_missing(needed, cache, providers, delay, save_cache)
                except AllProvidersFailed:
                    log("all providers failed or ran out of quota; the rest stays in English (re-run later to resume)")
                    incomplete = True
                save_cache()

        # 3. build the output files
        old_up = out_root / "upstream" / "email"
        old_files = {p.name: p.read_bytes().decode("utf-8") for p in old_up.glob("*.hbs")} if old_up.is_dir() else {}
        added = sorted(set(files) - set(old_files))
        removed = sorted(set(old_files) - set(files))
        modified = sorted(n for n in files if n in old_files and files[n] != old_files[n])

        results: dict[str, str] = {}
        english_left: list[tuple[str, str]] = []
        file_fallbacks: list[str] = []
        for name, text in files.items():
            html = is_html_file(name)
            out, left = render(text, html, cache.get)
            if not final_check(text, out, html):
                file_fallbacks.append(name)
                out, left = text, find_segments(tokenize(text, html), html)
            results[name] = out
            english_left += [(name, s.core) for s in left]

        for d, content in (("templates", results), ("upstream", files)):
            target_dir = out_root / d / "email"
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True)
            for name, text in content.items():
                (target_dir / name).write_bytes(text.encode("utf-8"))
        version_file.write_text(target + "\n")

        translated = total - len(english_left)
        log(f"done: {translated}/{total} sentences translated, {len(english_left)} left in English, "
            f"{new} new translations this run")
        for name in file_fallbacks:
            log(f"WARNING: {name} failed the whole-file check and was kept in English")

        # 4. report (becomes the pull-request description)
        if args.report:
            lines = [
                f"## 同步 Vaultwarden 官方邮件模板到 {target}",
                "",
                f"- 官方文件变化（对比上一次快照）：新增 {len(added)}，修改 {len(modified)}，删除 {len(removed)}",
                f"- 句子：共 {total} 条，已翻译 {translated} 条，保留英文 {len(english_left)} 条，本次新译 {new} 条",
                f"- 翻译服务：{', '.join(p.name for p in providers) or '无'}",
            ]
            if added or removed or modified:
                lines += ["", "<details><summary>官方变化的文件</summary>", ""]
                lines += [f"- 新增 `{n}`" for n in added] + [f"- 修改 `{n}`" for n in modified] + [f"- 删除 `{n}`" for n in removed]
                lines += ["", "</details>"]
            if incomplete or english_left or file_fallbacks:
                lines += ["", "### 需要关注", ""]
                if incomplete:
                    lines.append("- 翻译服务额度用尽或不可用，剩余句子保留英文。稍后在 Actions 里勾选 `retry` 手动运行即可补译。")
                lines += [f"- `{n}` 整文件校验失败，已保留英文原版" for n in file_fallbacks]
                lines += [f"- `{n}`：{c[:80]}" for n, c in english_left[:40]]
                if len(english_left) > 40:
                    lines.append(f"- …另有 {len(english_left) - 40} 条")
            lines += [
                "",
                "### 合并前建议看一眼",
                "- `upstream/` 目录的 diff 就是官方这次改了什么",
                "- `translations.json` 的新增条目就是这次新译的句子，不满意可以直接改",
                "- 译文只替换了文字，变量、链接和 HTML 结构已通过自动校验，与官方原文一致",
            ]
            Path(args.report).write_text("\n".join(lines) + "\n", "utf-8")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
