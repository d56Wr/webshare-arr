"""Webshare.cz bridge for the *arr stack.

Exposes two faces on one port:
  /api            Newznab indexer (search results are Webshare files, the "nzb" is a tiny pointer)
  /sabnzbd/api    SABnzbd-compatible download client that fetches the pointed-to file from Webshare
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, Response, jsonify, request
from passlib.hash import md5_crypt

WS = "https://webshare.cz/api"
WS_USER = os.environ["WS_USER"]
WS_PASS = os.environ["WS_PASS"]
API_KEY = os.environ["API_KEY"]
PORT = int(os.environ.get("PORT", "9710"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MIN_MB = int(os.environ.get("MIN_MB", "150"))
MAX_GB = int(os.environ.get("MAX_GB", "40"))
MAX_MBPS = float(os.environ.get("MAX_MBPS", "0"))  # download speed cap in MB/s, 0 = unlimited
VIDEO_EXT = {"mkv", "mp4", "avi", "m4v", "ts", "wmv", "mov", "mpg", "mpeg"}
INCOMPLETE = os.path.join(DOWNLOAD_DIR, ".incomplete")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

app = Flask(__name__)
http = requests.Session()
http.headers["User-Agent"] = "webshare-arr/1.0"


# --------------------------------------------------------------------------- Webshare client

_token = {"value": None}
_token_lock = threading.Lock()


def ws_post(endpoint, **data):
    r = http.post(f"{WS}/{endpoint}/", data=data, timeout=30)
    r.raise_for_status()
    return r.text


def xml_field(xml, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1) if m else None


def ws_login():
    salt = xml_field(ws_post("salt", username_or_email=WS_USER), "salt")
    if not salt:
        raise RuntimeError("Webshare: no salt returned")
    pw = hashlib.sha1(md5_crypt.using(salt=salt).hash(WS_PASS).encode()).hexdigest()
    xml = ws_post("login", username_or_email=WS_USER, password=pw, keep_logged_in=1)
    if xml_field(xml, "status") != "OK":
        raise RuntimeError(f"Webshare login failed: {xml_field(xml, 'message') or xml[:200]}")
    return xml_field(xml, "token")


def ws_token(force=False):
    with _token_lock:
        if force or not _token["value"]:
            _token["value"] = ws_login()
        return _token["value"]


def ws_file_link(ident):
    for attempt in (0, 1):
        xml = ws_post("file_link", ident=ident, wst=ws_token(force=bool(attempt)), download_type="file_download")
        if xml_field(xml, "status") == "OK":
            return xml_field(xml, "link").replace("&amp;", "&")
        code = xml_field(xml, "code")
        if code != "LOGIN_FATAL_1" and attempt == 0 and "LOGIN" not in (code or ""):
            raise RuntimeError(f"Webshare file_link: {xml_field(xml, 'message') or code}")
    raise RuntimeError("Webshare file_link failed after relogin")


def ws_search(query, limit=100):
    xml = ws_post("search", what=query, category="video", limit=limit, offset=0, sort="")
    out = []
    for block in re.findall(r"<file>(.*?)</file>", xml, re.S):
        f = {k: xml_field(block, k) for k in ("ident", "name", "type", "size", "positive_votes", "negative_votes", "password")}
        try:
            f["size"] = int(f["size"])
            f["pos"] = int(f["positive_votes"] or 0)
            f["neg"] = int(f["negative_votes"] or 0)
        except (TypeError, ValueError):
            continue
        f["name"] = unescape(f["name"] or "")
        out.append(f)
    return out


def unescape(s):
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


# --------------------------------------------------------------------------- release naming

QUALITY_RE = re.compile(r"(2160p|4k|uhd|1080p|1080i|720p|576p|480p|blu-?ray|brrip|bdrip|web-?dl|web-?rip|hdtv|dvd-?rip|dvd|hdrip|remux|x264|x265|hevc)", re.I)
EP_RE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})|(\d{1,2})x(\d{2,3})")


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def guess_quality(size):
    mb = size / 1048576
    if mb < 1500:
        return "DVDRip"
    if mb < 5000:
        return "720p WEBRip"
    if mb < 20000:
        return "1080p WEBRip"
    return "2160p WEBRip"


YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


CZ_RE = re.compile(r"\b(cz|cze|czech|cesky|ceske|cesk\w*|dabing\w*|dab|cs)\b", re.I)
SK_RE = re.compile(r"\b(sk|slovak|slovensky|slovensk\w*|svk)\b", re.I)
SUB_RE = re.compile(r"\b(tit|titulky|titulek|sub|subs|subtitles|hardsub)\b", re.I)


def lang_tag(name):
    """Token Radarr/Sonarr understand (CZ / SK) when the file name claims Czech/Slovak audio; subtitles alone do not count."""
    n = fold(name).replace("_", " ").replace(".", " ")
    if SUB_RE.search(n) and not re.search(r"\b(dab|dabing\w*)\b", n, re.I):
        return ""
    if SK_RE.search(n) and not CZ_RE.search(n):
        return "SK"
    return "CZ" if CZ_RE.search(n) else ""


def release_title(name, size, year=None):
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name).strip()
    base = re.sub(r"[\\/:*?\"<>|]", " ", base)
    base = re.sub(r"\s+", " ", base)
    if year and not YEAR_RE.search(base):
        base += f" {year}"  # Radarr cannot parse a movie release (and so cannot import it) without a year
    if not QUALITY_RE.search(base):
        base += " " + guess_quality(size)
    tag = lang_tag(name)
    if tag and not re.search(rf"\b{tag}\b", base, re.I):
        base += " " + tag
    return base


def usable(f):
    if f["type"] and f["type"].lower() not in VIDEO_EXT:
        return False
    if f["password"] == "1":
        return False
    mb = f["size"] / 1048576
    if mb < MIN_MB or mb > MAX_GB * 1024:
        return False
    return f["neg"] <= f["pos"] + 2


MIN_SCORE = int(os.environ.get("MIN_SCORE", "55"))
BAD_TAGS = {"cam", "hdcam", "ts", "hdts", "telesync", "tc", "telecine", "scr", "screener", "workprint", "sample", "trailer", "teaser", "ukazka"}
NOISE = {
    "cz", "czech", "sk", "slovak", "dabing", "dab", "titulky", "tit", "eng", "en", "multi", "film", "movie", "top", "kvalita",
    "hd", "uhd", "4k", "hdr", "hdr10", "dv", "web", "dl", "webdl", "webrip", "bluray", "blu", "ray", "brrip", "bdrip", "hdtv",
    "dvdrip", "dvd", "remux", "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "ddp", "atmos", "mkv", "mp4", "avi",
    "1080p", "720p", "2160p", "480p", "576p", "internal", "extended", "remastered", "directors", "cut", "full", "fullhd",
}
CONTAINER_BONUS = {"mkv": 10, "mp4": 7, "m4v": 5, "avi": 3}


def tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", fold(s).lower()) if t]


def score(f, q):
    """0-100 how well a Webshare file fits the search; negative-ish results are clamped to 0."""
    qt = tokens(q)
    year_m = re.search(r"\b(19|20)\d{2}\b", q)
    year = year_m.group(0) if year_m else None
    qt = [t for t in qt if t != year]
    nt = tokens(f["name"])
    if not qt or any(t in BAD_TAGS for t in nt):
        return 0
    nset = set(nt)
    coverage = sum(t in nset for t in qt) / len(qt)
    if coverage < 0.8:
        return 0
    s = 50 * coverage
    # words that are neither in the query nor in the usual release noise suggest a different title ("Kolja 2", "Making of ...")
    extra = [t for t in nt if t not in qt and t not in NOISE and not t.isdigit() and not EP_RE.fullmatch(t)]
    s -= min(25, 5 * len(extra))
    # title tokens should come first in the filename, not buried after other words
    if qt[0] in nt and nt.index(qt[0]) > 2:
        s -= 10
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", fold(f["name"])))
    if year:
        s += 20 if year in years else (-25 if years else 0)
    else:
        s += 10
    s += CONTAINER_BONUS.get((f["type"] or "").lower(), 0)
    gb = f["size"] / 1073741824
    s += 10 if 0.7 <= gb <= 15 else 5 if 0.4 <= gb <= 30 else 0
    s += min(10, f["pos"]) - min(15, 3 * f["neg"])
    return max(0, min(100, round(s)))


def find_releases(q, season=None, ep=None, titles=None, year=None):
    """Search Webshare for one or more titles of the same work and score every hit against its best-matching title."""
    if titles is None:
        ym = YEAR_RE.search(q)
        year = year or (ym.group(0) if ym else None)
        titles = [re.sub(r"[\s+]+(?:19|20)\d{2}$", "", q).strip()]
    queries = []
    for t in titles:
        for v in ((f"{t} {year}" if year else None), t):
            for w in (v, fold(v) if v else None):
                if w and w not in queries:
                    queries.append(w)
    with ThreadPoolExecutor(max_workers=6) as pool:
        def run(query):
            try:
                return ws_search(query)
            except requests.RequestException as e:
                print("search error", query, e, flush=True)
                return []
        batches = list(pool.map(run, queries))
    seen, results = set(), []
    for files in batches:
        for f in files:
            if f["ident"] in seen or not usable(f):
                continue
            if season and ep:
                m = EP_RE.search(f["name"])
                if m and (int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))) != (int(season), int(ep)):
                    continue
            seen.add(f["ident"])
            best = max(((score(f, f"{t} {year}" if year else t), t) for t in titles), key=lambda x: x[0])
            f["score"], f["matched_title"] = best
            if f["score"] >= MIN_SCORE:
                results.append(f)
    results.sort(key=lambda f: (f["score"], f["size"]), reverse=True)
    print(f"search {titles!r} {year}: {len(results)} results from {len(queries)} queries, "
          f"best {[(f['score'], f['name']) for f in results[:3]]}", flush=True)
    return results[:30]


# --------------------------------------------------------------------------- movie metadata (Radarr's public metadata server)

_meta_cache = {}
META_URL = "https://api.radarr.video/v1/movie"
TITLE_LANGS = ("cs-CZ", "sk-SK")


def movie_meta(tmdbid=None, imdbid=None):
    key = tmdbid or imdbid
    hit = _meta_cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    url = f"{META_URL}/{tmdbid}" if tmdbid else f"{META_URL}/imdb/{imdbid}"
    try:
        r = http.get(url, timeout=20)
        r.raise_for_status()
        meta = r.json()
        if isinstance(meta, list):
            meta = meta[0] if meta else None
    except (requests.RequestException, ValueError) as e:
        print("metadata error", url, e, flush=True)
        return hit[1] if hit else None
    _meta_cache[key] = (time.time(), meta)
    return meta


def meta_titles(meta):
    """Main title first, then Czech/Slovak translations, original title and alternative titles; each also without the ': subtitle'."""
    cand = [meta.get("Title"), meta.get("OriginalTitle")]
    cand += [t.get("Title") for t in meta.get("Translations", []) if t.get("Language") in TITLE_LANGS]
    cand += [a.get("Title") for a in meta.get("AlternativeTitles", [])]
    out = []
    for t in cand:
        if not t or not re.search(r"[A-Za-z\u00C0-\u024F]", t):
            continue  # skip titles in non-latin scripts, Webshare names are latin
        for v in (t, re.split(r"\s*[:\u2013\u2014]\s+", t)[0]):
            v = v.strip()
            if len(v) > 2 and fold(v).lower() not in {fold(o).lower() for o in out}:
                out.append(v)
    return out[:14]


# --------------------------------------------------------------------------- Newznab

def need_key():
    return request.values.get("apikey") == API_KEY


def nzb_pointer(f_ident, name, size):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
        f"<meta type=\"ws-ident\">{escape(f_ident)}</meta>"
        f"<meta type=\"ws-name\">{escape(name)}</meta>"
        f"<meta type=\"ws-size\">{size}</meta></head>"
        # Radarr/Sonarr reject an NZB without a <file>; this one is only a placeholder
        f"<file poster=\"webshare\" date=\"0\" subject={quoteattr(name)}><groups><group>alt.binaries.webshare</group></groups>"
        f"<segments><segment bytes=\"{size}\" number=\"1\">{escape(f_ident)}@webshare.cz</segment></segments></file></nzb>"
    )


def err(code, desc):
    return Response(f'<?xml version="1.0"?><error code="{code}" description={quoteattr(desc)}/>', mimetype="text/xml")


CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps><server title="Webshare"/><limits max="100" default="100"/>
<searching><search available="yes" supportedParams="q"/>
<tv-search available="yes" supportedParams="q,season,ep"/>
<movie-search available="yes" supportedParams="q,imdbid,tmdbid"/></searching>
<categories>
<category id="2000" name="Movies"><subcat id="2030" name="Movies/SD"/><subcat id="2040" name="Movies/HD"/><subcat id="2045" name="Movies/UHD"/></category>
<category id="5000" name="TV"><subcat id="5030" name="TV/SD"/><subcat id="5040" name="TV/HD"/><subcat id="5045" name="TV/UHD"/></category>
</categories></caps>"""


def category_for(f, is_tv):
    mb = f["size"] / 1048576
    if is_tv:
        return 5030 if mb < 1500 else 5040 if mb < 20000 else 5045
    return 2030 if mb < 1500 else 2040 if mb < 20000 else 2045


@app.route("/api")
def newznab():
    if not need_key():
        return err(100, "Incorrect user credentials")
    t = request.args.get("t", "")
    base = request.host_url.rstrip("/")
    if t == "caps":
        return Response(CAPS, mimetype="text/xml")
    if t == "get":
        ident, name = request.args.get("id", ""), request.args.get("name", "")
        return Response(nzb_pointer(ident, name, request.args.get("size", "0")), mimetype="application/x-nzb")
    if t not in ("search", "movie", "tvsearch"):
        return err(202, "No such function")
    q = (request.args.get("q") or "").replace("+", " ").strip()
    is_tv = t == "tvsearch" or request.args.get("cat", "").startswith("5")
    ym = YEAR_RE.search(q)
    year = ym.group(0) if ym else None
    tmdbid, imdbid = request.args.get("tmdbid"), request.args.get("imdbid")
    meta = None
    if not is_tv and (tmdbid or imdbid):
        meta = movie_meta(tmdbid if tmdbid and tmdbid != "0" else None, ("tt" + imdbid.lstrip("t")) if imdbid else None)
    items = []
    if meta:
        year = str(meta.get("Year") or year or "") or None
        found = find_releases(q, titles=meta_titles(meta), year=year)
    elif q:
        found = find_releases(q, request.args.get("season"), request.args.get("ep"))
    else:  # RSS-style probe (indexer test): a few generic recent-looking files
        found = [dict(f, score=0) for f in ws_search("cz 1080p") if usable(f)][:20]
    if True:
        for f in found:
            title = release_title(f["name"], f["size"], None if is_tv else year)
            if meta and f.get("matched_title") and f["matched_title"] != meta.get("Title"):
                # found under a translated title: name the release after Radarr's own title so it parses and matches the movie
                tags = " ".join(dict.fromkeys(m.group(0) for m in QUALITY_RE.finditer(f["name"]))) or guess_quality(f["size"])
                title = f"{safe(meta['Title'])} {year} {tags} {lang_tag(f['name'])}".strip()
            link = f"{base}/api?t=get&apikey={API_KEY}&id={f['ident']}&size={f['size']}&name={requests.utils.quote(f['name'])}"
            cat = category_for(f, is_tv)
            items.append(
                f"<item><title>{escape(title)}</title><guid isPermaLink=\"false\">ws-{f['ident']}</guid>"
                f"<link>{escape(link)}</link><pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate>"
                f"<enclosure url={quoteattr(link)} length=\"{f['size']}\" type=\"application/x-nzb\"/>"
                f"<newznab:attr name=\"category\" value=\"{cat}\"/>"
                f"<newznab:attr name=\"size\" value=\"{f['size']}\"/>"
                f"<newznab:attr name=\"grabs\" value=\"{f['pos']}\"/>"
                f"<description>match {f['score']}%</description></item>"
            )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        f"<channel><title>Webshare</title>{''.join(items)}</channel></rss>"
    )
    return Response(body, mimetype="application/rss+xml")


# --------------------------------------------------------------------------- download manager

jobs = {}
jobs_lock = threading.RLock()
wake = threading.Event()
cancelled = set()


def save_state():
    with jobs_lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(jobs, fh)
        os.replace(tmp, STATE_FILE)


def load_state():
    try:
        with open(STATE_FILE) as fh:
            jobs.update(json.load(fh))
    except (OSError, ValueError):
        pass
    for j in jobs.values():
        if j["status"] == "Downloading":
            j["status"] = "Queued"


def safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .") or "download"


def run_job(j):
    ext = os.path.splitext(j["filename"])[1] or ".mkv"
    final_dir = os.path.join(DOWNLOAD_DIR, safe(j["category"]), safe(j["name"]))
    final_path = os.path.join(final_dir, safe(j["name"]) + ext)
    part = os.path.join(INCOMPLETE, f"{j['id']}.part")
    os.makedirs(INCOMPLETE, exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    link = ws_file_link(j["ident"])
    headers = {"Range": f"bytes={have}-"} if have else {}
    with http.get(link, headers=headers, stream=True, timeout=(15, 60)) as r:
        if r.status_code == 416:
            have = 0
            r.close()
            os.remove(part)
            raise RuntimeError("range rejected, restarting")
        r.raise_for_status()
        if have and r.status_code != 206:
            have = 0
        total = have + int(r.headers.get("Content-Length", 0))
        with jobs_lock:
            j["size"] = total or j["size"]
        mode = "ab" if have else "wb"
        done, last_save, t0, base = have, time.time(), time.time(), have
        with open(part, mode) as fh:
            for chunk in r.iter_content(1 << 20):
                if j["id"] in cancelled:
                    return False
                fh.write(chunk)
                done += len(chunk)
                j["done"] = done
                if MAX_MBPS:
                    ahead = (done - base) / (MAX_MBPS * 1048576) - (time.time() - t0)
                    if ahead > 0:
                        time.sleep(ahead)
                j["speed"] = (done - base) / max(time.time() - t0, 1)
                if time.time() - last_save > 15:
                    save_state()
                    last_save = time.time()
    if j["size"] and done < j["size"] * 0.999:
        raise RuntimeError(f"incomplete download {done}/{j['size']}")
    os.makedirs(final_dir, exist_ok=True)
    shutil.move(part, final_path)
    j["storage"] = final_dir
    return True


def worker():
    while True:
        with jobs_lock:
            queued = sorted((j for j in jobs.values() if j["status"] == "Queued"), key=lambda j: j["added"])
            j = queued[0] if queued else None
            if j:
                j["status"] = "Downloading"
        if not j:
            wake.wait(10)
            wake.clear()
            continue
        save_state()
        print("start", j["name"], flush=True)
        try:
            ok = run_job(j)
            with jobs_lock:
                if ok:
                    j["status"], j["completed"], j["done"] = "Completed", time.time(), j["size"]
                    print("done", j["name"], flush=True)
        except Exception as e:  # noqa: BLE001 - keep the worker alive whatever happens
            print("job failed", j["name"], e, flush=True)
            with jobs_lock:
                j["retries"] = j.get("retries", 0) + 1
                j["error"] = str(e)
                if j["retries"] >= 5:
                    j["status"], j["completed"] = "Failed", time.time()
                else:
                    j["status"] = "Queued"
            time.sleep(min(60, 5 * j["retries"]))
        cancelled.discard(j["id"])
        save_state()


def add_job(ident, filename, size, nzb_name, category):
    jid = f"ws_{ident}"
    with jobs_lock:
        if jid in jobs and jobs[jid]["status"] in ("Queued", "Downloading"):
            return jid
        jobs[jid] = {
            "id": jid, "ident": ident, "filename": filename, "name": safe(nzb_name), "category": category or "default",
            "size": size, "done": 0, "speed": 0, "status": "Queued", "added": time.time(), "retries": 0,
        }
    save_state()
    wake.set()
    return jid


# --------------------------------------------------------------------------- SABnzbd emulation

def mb(n):
    return f"{n / 1048576:.2f}"


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def timeleft(j):
    if not j.get("speed"):
        return "0:00:00"
    s = int(max(j["size"] - j["done"], 0) / j["speed"])
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def queue_slot(i, j):
    left = max(j["size"] - j["done"], 0)
    return {
        "status": j["status"], "index": i, "nzo_id": j["id"], "filename": j["name"], "cat": j["category"],
        "mb": mb(j["size"]), "mbleft": mb(left), "size": human(j["size"]), "sizeleft": human(left),
        "percentage": str(int(100 * j["done"] / j["size"])) if j["size"] else "0",
        "timeleft": timeleft(j), "priority": "Normal", "labels": [],
    }


def history_slot(j):
    return {
        "status": j["status"], "nzo_id": j["id"], "name": j["name"], "nzb_name": j["name"] + ".nzb",
        "category": j["category"], "storage": j.get("storage", ""), "path": j.get("storage", ""),
        "bytes": j["size"], "size": human(j["size"]), "fail_message": j.get("error", "") if j["status"] == "Failed" else "",
        "completed": int(j.get("completed", 0)), "download_time": 0, "script": "None", "report": "",
    }


@app.route("/sabnzbd/api", methods=["GET", "POST"])
def sab():
    mode = request.values.get("mode", "")
    if mode == "version":
        return jsonify(version="4.3.3")
    if not need_key():
        return jsonify(status=False, error="API Key Incorrect")
    name = request.values.get("name", "")

    if mode == "get_config":
        cats = [{"name": c, "dir": c, "pp": "", "script": "Default", "priority": -100}
                for c in ("*", "radarr", "sonarr", "movies", "tv")]
        return jsonify(config={"misc": {"complete_dir": DOWNLOAD_DIR, "enable_tv_sorting": False,
                                        "enable_movie_sorting": False, "enable_date_sorting": False,
                                        "pre_check": False, "history_retention": ""},
                               "categories": cats})
    if mode == "get_cats":
        return jsonify(categories=["*", "radarr", "sonarr"])
    if mode == "fullstatus":
        return jsonify(status={"completedir": DOWNLOAD_DIR, "version": "4.3.3"})

    if mode in ("addfile", "addurl"):
        ident = fname = size = None
        if mode == "addfile":
            up = next(iter(request.files.values()), None)
            body = up.read().decode("utf-8", "replace") if up else ""
            def meta(k):
                m = re.search(rf'<meta type="ws-{k}">(.*?)</meta>', body, re.S)
                return m.group(1) if m else None
            ident, fname, size = meta("ident"), meta("name"), meta("size")
            nzb_name = request.values.get("nzbname") or (os.path.splitext(up.filename or "")[0] if up else "")
        else:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(name).query)
            ident, fname, size = (q.get(k, [None])[0] for k in ("id", "name", "size"))
            nzb_name = request.values.get("nzbname") or fname
        if not ident:
            return jsonify(status=False, error="no webshare ident in nzb")
        fname = unescape(fname or ident)
        jid = add_job(ident, fname, int(size or 0), nzb_name or fname, request.values.get("cat"))
        return jsonify(status=True, nzo_ids=[jid])

    if mode == "queue":
        if name == "delete":
            for jid in request.values.get("value", "").split(","):
                with jobs_lock:
                    if jid in jobs and jobs[jid]["status"] in ("Queued", "Downloading"):
                        cancelled.add(jid)
                        jobs.pop(jid)
                        try:
                            os.remove(os.path.join(INCOMPLETE, f"{jid}.part"))
                        except OSError:
                            pass
            save_state()
            return jsonify(status=True)
        with jobs_lock:
            active = sorted((j for j in jobs.values() if j["status"] in ("Queued", "Downloading")), key=lambda j: j["added"])
            slots = [queue_slot(i, j) for i, j in enumerate(active)]
        return jsonify(queue={"status": "Downloading" if slots else "Idle", "paused": False, "slots": slots,
                              "noofslots": len(slots), "noofslots_total": len(slots)})

    if mode == "history":
        if name == "delete":
            for jid in request.values.get("value", "").split(","):
                with jobs_lock:
                    j = jobs.pop(jid, None)
                if j and request.values.get("del_files") == "1" and j.get("storage"):
                    shutil.rmtree(j["storage"], ignore_errors=True)
            save_state()
            return jsonify(status=True)
        cat = request.values.get("category")
        with jobs_lock:
            done = sorted((j for j in jobs.values() if j["status"] in ("Completed", "Failed")),
                          key=lambda j: j.get("completed", 0), reverse=True)
            if cat and cat != "*":
                done = [j for j in done if j["category"] == cat]
            slots = [history_slot(j) for j in done]
        return jsonify(history={"slots": slots, "noofslots": len(slots)})

    return jsonify(status=True)


@app.route("/health")
def health():
    return jsonify(ok=True, jobs=len(jobs))


if __name__ == "__main__":
    os.umask(0o002)  # group-writable, matches the arr containers (UMASK=002)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(INCOMPLETE, exist_ok=True)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
