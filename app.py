#!/usr/bin/env python3
"""Cutter - servidor local em Python puro (sem dependencias)."""

import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Cutter" / "config.json"
COOKIE_BROWSERS = ["", "firefox", "chrome", "safari", "brave", "edge", "chromium", "opera", "vivaldi"]
DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads"),
    "cookies_browser": "",  # vazio = sem cookies
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    if cfg.get("cookies_browser") not in COOKIE_BROWSERS:
        cfg["cookies_browser"] = ""
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def download_dir():
    d = Path(os.path.expanduser(load_config()["download_dir"]))
    d.mkdir(parents=True, exist_ok=True)
    return d


def missing_dependencies():
    return [tool for tool in ("yt-dlp", "ffmpeg") if not shutil.which(tool)]


def whisper_bin():
    """whisper.cpp (brew install whisper-cpp). O app aberto pelo Finder nao herda o PATH do shell."""
    return shutil.which("whisper-cli") or next(
        (p for p in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli") if os.path.exists(p)), None)
PORT_START = 8765

jobs = {}
jobs_lock = threading.Lock()
recordings = {}  # job_id -> True enquanto a gravacao de live esta ativa


def set_job(job_id, **fields):
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {}
        jobs[job_id].update(fields)


def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))


def hhmmss_to_seconds(s):
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 3:
        h, m, sec = parts
    elif len(parts) == 2:
        h = "0"
        m, sec = parts
    else:
        raise ValueError("Use o formato HH:MM:SS")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def seconds_to_hhmmss(n):
    n = int(n)
    return f"{n // 3600:02d}:{(n % 3600) // 60:02d}:{n % 60:02d}"


def sanitize_filename(name):
    name = re.sub(r"[^\w\s\-\.]", "", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    return name[:80] or "video"


# Nao amarrar num cliente InnerTube so: Tenta o padrao do yt-dlp e, se falhar, cai pros alternativos.
# (So vale pro YouTube; nos outros sites o yt-dlp roda uma vez, sem extractor-args.)
CLIENTS = ["default", "web_embedded", "tv"]
QUALITIES = {0, 360, 480, 720, 1080, 1440, 2160}

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
SITE_NAMES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "instagram.com": "Instagram",
    "tiktok.com": "TikTok", "x.com": "X", "twitter.com": "X", "facebook.com": "Facebook",
    "fb.watch": "Facebook", "vimeo.com": "Vimeo", "twitch.tv": "Twitch", "reddit.com": "Reddit",
    "threads.net": "Threads", "threads.com": "Threads", "kwai.com": "Kwai", "dailymotion.com": "Dailymotion",
}


def local_path(text):
    """Caminho de arquivo colado ou arrastado: aceita file://, ~ e aspas."""
    text = str(text).strip().strip("'\"")
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
    return Path(os.path.expanduser(text))


def normalize_url(url):
    url = url.strip()
    if url and not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def url_host(url):
    host = (urlparse(normalize_url(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_youtube(url):
    host = url_host(url)
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def site_name(url):
    host = url_host(url)
    for domain, name in SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "o site"


# Erros que nao mudam trocando de cliente - nao adianta tentar de novo.
FATAL_PATTERNS = [
    "Private video", "members-only", "Join this channel", "This live event will begin",
    "has been removed", "account associated with this video has been terminated",
    "copyright claim",
]

LOGIN_HINT = ("Em Ajustes > Cookies do navegador, escolha um navegador em que voce esteja logado no {site} "
              "e tente de novo. Os cookies sao lidos so no seu Mac.")

# stderr do yt-dlp -> mensagem que da pra entender. (padrao, mensagem, e_caso_de_login)
# A ordem importa: falhas ao ler cookies vem antes do "pediu login" generico.
FRIENDLY_ERRORS = [
    (r"cookies are no longer valid|rotated in the browser",
     "Os cookies do navegador venceram. Abra o {site} nesse navegador, confira o login e tente de novo.", True),
    (r"could not find \w+ cookies|failed to load cookies|cookies database|failed to decrypt",
     "Nao consegui ler os cookies do navegador escolhido em Ajustes. Ele esta instalado e ja abriu o {site}?", True),
    (r"Operation not permitted.*[Cc]ookies|[Cc]ookies.*Operation not permitted|binarycookies",
     "O macOS bloqueou a leitura dos cookies do Safari. De Acesso Total ao Disco para o Cutter "
     "(Ajustes do Sistema > Privacidade e Seguranca) ou escolha outro navegador em Ajustes.", False),
    (r"Sign in to confirm (you.re not a bot|your age)",
     "O YouTube pediu login (\"confirme que voce nao e um robo\" ou restricao de idade).\n" + LOGIN_HINT, True),
    (r"Private video", "Video privado.", False),
    (r"members-only|Join this channel", "Video exclusivo pra membros do canal.", False),
    (r"This live event will begin", "A live ainda nao comecou.", False),
    (r"IP address is blocked",
     "O {site} bloqueou este post para o seu IP ou regiao. As vezes funciona usando os cookies de um "
     "navegador logado no {site} (Ajustes); senao, tente outra rede.", True),
    (r"Cannot parse data",
     "O {site} nao liberou este video sem login (comum no Facebook e em posts privados).\n" + LOGIN_HINT, True),
    (r"log ?in|logged.in|sign.?in|authenticat|account credentials|--cookies|private|"
     r"age.restricted|confirm your age|empty media response|not available.*rate.limit",
     "O {site} exige login para este conteudo (post privado, story, restricao de idade, ou o site "
     "limitou o acesso anonimo).\n" + LOGIN_HINT, True),
    (r"No video could be found|no video in this|There.s no video|No video formats found",
     "Nao achei video neste link (pode ser so foto ou texto).", False),
    (r"[Ss]uspended", "A conta foi suspensa.", False),
    (r"HTTP Error 403", "O {site} recusou o download (403). Tente de novo em alguns minutos.", False),
    (r"HTTP Error 429|Too Many Requests", "Muitas requisicoes - o {site} limitou. Espere uns minutos.", False),
    (r"HTTP Error 404|not found|has been removed|deleted",
     "Conteudo nao encontrado (removido, apagado ou link errado).", False),
    (r"Video unavailable|video is unavailable|not available",
     "Video indisponivel (removido, bloqueado na regiao ou sem permissao).", False),
    (r"Unsupported URL|is not a valid URL",
     "Link nao suportado: o yt-dlp nao reconhece video neste endereco.", False),
    (r"timed out|Connection (refused|reset)|Temporary failure in name resolution|nodename nor servname",
     "O {site} nao respondeu (rede lenta ou fora do ar). Tente de novo.", False),
]


class YtdlpError(RuntimeError):
    def __init__(self, raw):
        super().__init__(raw)
        self.raw = raw
        self.fatal = any(p.lower() in raw.lower() for p in FATAL_PATTERNS)


def explain_error(raw, url=""):
    """-> (mensagem, precisa_login). Olha primeiro as linhas de ERROR (WARNINGs falam de cookies a toa)."""
    lines = raw.splitlines()
    focus = "\n".join([l for l in lines if "ERROR" in l] or lines)
    site = site_name(url) if url else "site"
    for pattern, msg, login in FRIENDLY_ERRORS:
        if re.search(pattern, focus, re.IGNORECASE):
            msg = msg.format(site=site)
            browser = load_config()["cookies_browser"]
            if login and browser and "Ajustes > Cookies" in msg:
                msg = (msg.split("\n")[0] + f"\nVoce ja esta usando os cookies do {browser.title()}: abra o {site} "
                       "nele, confira se esta logado (e se sua conta tem acesso a esse post) e tente de novo.")
            return f"{msg}\n\nDetalhe tecnico:\n{raw}", login
    return raw, False


def fail_job(job_id, raw, url):
    msg, login = explain_error(raw, url)
    set_job(job_id, status="error", error=msg, login=login)


def ytdlp_cmd(client, *args):
    cookies = load_config()["cookies_browser"]
    return [
        "yt-dlp", "--no-update",
        *(["--cookies-from-browser", cookies] if cookies else []),
        *(["--extractor-args", f"youtube:player_client={client}"] if client != "default" else []),
        "--no-playlist",  # watch?v=...&list=... baixa so o video (carrossel do Instagram continua vindo inteiro)
        *args,
    ]


def client_order(preferred, url):
    if not is_youtube(url):
        return ["default"]
    if preferred in CLIENTS:
        return [preferred] + [c for c in CLIENTS if c != preferred]
    return list(CLIENTS)


def format_args(height):
    """Prefere H.264/AAC (abre no QuickTime e em qualquer editor) dentro do limite de altura."""
    sort = "vcodec:h264,acodec:aac"
    if height:
        sort = f"res:{height}," + sort
    return ["-f", "bv*+ba/b", "-S", sort]


def tail_of(lines, n=5):
    return "\n".join(l for l in lines if l.strip())[-2000:].splitlines()[-n:]


def run_ytdlp_info(url):
    """Busca info do video via yt-dlp. Retorna dict com title, duration, is_live, etc."""
    errors = []
    for client in client_order(None, url):
        cmd = ytdlp_cmd(
            client, "--no-download",
            "--print", "title:::%(title)s",
            "--print", "duration:::%(duration)s",
            "--print", "is_live:::%(is_live)s",
            "--print", "live_status:::%(live_status)s",
            "--print", "release_timestamp:::%(release_timestamp)s",
            "--print", "height:::%(height)s",
            "--print", "thumbnail:::%(thumbnail)s",
            "--print", "channel:::%(channel,uploader)s",
            "--print", "id:::%(id)s",
            "--print", "site:::%(extractor_key)s",
            url,
        )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"O {site_name(url)} demorou demais pra responder (timed out). Tente de novo.")
        if result.returncode == 0:
            # Carrossel/playlist imprime um bloco por video: fica com o primeiro e conta quantos sao.
            info = {"client": client, "count": 0}
            for line in result.stdout.strip().splitlines():
                if ":::" in line:
                    k, v = line.split(":::", 1)
                    if k == "title":
                        info["count"] += 1
                    if info["count"] <= 1:
                        info[k] = v
            return info
        err = YtdlpError("\n".join(tail_of((result.stderr or result.stdout).splitlines())) or "Falha ao buscar info")
        errors.append(err)
        if err.fatal:
            break
    raise errors[-1]


def parse_percent(line):
    m = re.search(r"(\d+(?:\.\d+)?)%", line)
    return float(m.group(1)) if m else None


def run_ytdlp_streaming(cmd, on_percent):
    """Roda yt-dlp mostrando progresso; levanta YtdlpError com o fim do log se falhar."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last = []
    for line in proc.stdout:
        last.append(line.rstrip())
        last = last[-15:]
        pct = parse_percent(line)
        if pct is not None:
            on_percent(pct)
    proc.wait()
    if proc.returncode != 0:
        errs = [l for l in last if "ERROR" in l or "WARNING" in l] or last
        raise YtdlpError("\n".join(errs[-5:]))


def clip_basename(title, start_s, end_s, tag=""):
    # Corta so o titulo, pra nunca perder o horario do trecho no nome.
    span = f"{seconds_to_hhmmss(start_s)}_a_{seconds_to_hhmmss(end_s)}".replace(":", "-")
    return f"{sanitize_filename(title)[:60].rstrip('_-')}{tag}_{span}"


def finish_download(job_id, files, message="Pronto!"):
    """Registra o(s) arquivo(s) baixado(s). Se vier transcricao depois, o job continua 'running'."""
    fields = dict(progress=100, message=message, file_path=str(files[0]), files=[str(f) for f in files])
    if not get_job(job_id).get("transcribe"):
        fields["status"] = "done"
    set_job(job_id, **fields)


def download_vod_section(job_id, client, url, start_s, end_s, title, height):
    """Caso VOD comum: --download-sections funciona nativamente."""
    set_job(job_id, status="running", message="Baixando trecho...", progress=0)
    out_path = download_dir() / f"{clip_basename(title, start_s, end_s)}.mp4"
    cmd = ytdlp_cmd(
        client, "--newline",
        "--download-sections", f"*{start_s}-{end_s}",
        *format_args(height),
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        url,
    )
    run_ytdlp_streaming(cmd, lambda pct: set_job(
        job_id, progress=pct, message=f"Baixando trecho... {pct:.0f}%"))
    finish_download(job_id, [out_path])


def download_full(job_id, client, url, title, height, count=1):
    """Video inteiro (VOD ou live encerrada). count > 1: carrossel/post com varios videos."""
    set_job(job_id, status="running", message="Baixando video...", progress=0)
    base = sanitize_filename(title)[:80].rstrip('_-')
    out_path = download_dir() / f"{base}.mp4"
    out_tmpl = str(download_dir() / f"{base}_%(playlist_index)02d.mp4") if count > 1 else str(out_path)
    cmd = ytdlp_cmd(
        client, "--newline",
        *format_args(height),
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "5",
        "-o", out_tmpl,
        url,
    )
    run_ytdlp_streaming(cmd, lambda pct: set_job(
        job_id, progress=pct, message=f"Baixando video... {pct:.0f}%"))
    if count > 1:
        files = sorted(download_dir().glob(f"{base}_[0-9][0-9].mp4"))
        if files:
            finish_download(job_id, files, f"Pronto! {len(files)} videos salvos")
            return
    finish_download(job_id, [out_path])



def download_post_live(job_id, client, url, start_s, end_s, title, height):
    """Caso post-live: baixa completo + corta com ffmpeg + remove o temp."""
    set_job(job_id, status="running", message="Baixando video completo...", progress=0)
    tmp_full = download_dir() / f".tmp_full_{job_id}.mp4"
    cmd = ytdlp_cmd(
        client, "--newline",
        *format_args(height),
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "5",
        "-o", str(tmp_full),
        url,
    )
    try:
        run_ytdlp_streaming(cmd, lambda pct: set_job(
            job_id, progress=pct * 0.85, message=f"Baixando video completo... {pct:.0f}%"))
    except Exception:
        tmp_full.unlink(missing_ok=True)
        raise

    set_job(job_id, progress=85, message="Cortando trecho...")
    out_path = download_dir() / f"{clip_basename(title, start_s, end_s)}.mp4"
    cmd2 = [
        "ffmpeg", "-y",
        "-ss", seconds_to_hhmmss(start_s),
        "-to", seconds_to_hhmmss(end_s),
        "-i", str(tmp_full),
        "-c", "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd2, capture_output=True, text=True)
    tmp_full.unlink(missing_ok=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError("\n".join(tail) or "ffmpeg falhou")
    finish_download(job_id, [out_path])


def hls_segment_duration(m3u8):
    """Duracao real dos segmentos (YouTube usa 5s, 2s ou 1s conforme a latencia da live)."""
    durs = [float(m.group(1)) for m in re.finditer(r"#EXTINF:([\d.]+)", m3u8)]
    if durs:
        durs.sort()
        return max(1, round(durs[len(durs) // 2]))
    m = re.search(r"#EXT-X-TARGETDURATION:(\d+)", m3u8)
    return int(m.group(1)) if m else 5


def live_manifest_url(client, url, height):
    """URL do manifesto HLS (muxado) da live via yt-dlp."""
    sort = f"res:{height}" if height else "res"
    r = subprocess.run(ytdlp_cmd(client, "-f", "b", "-S", sort, "-g", url),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        raise YtdlpError("\n".join(tail_of((r.stderr or "").splitlines()))
                         or "Nao obteve URL HLS. Tente novamente em alguns segundos.")
    return r.stdout.strip().splitlines()[-1]


def read_manifest(manifest_url):
    """-> (seg_duration, template_url, edge_sq, ended). edge_sq = segmento mais novo publicado."""
    m3u8 = fetch_url(manifest_url).decode("utf-8", "replace")
    segment_lines = [l for l in m3u8.splitlines() if l.startswith("http")]
    if not segment_lines:
        raise RuntimeError("Manifesto HLS sem segmentos.")
    template = segment_lines[-1]
    m = re.search(r"/sq/(\d+)", template)
    if not m:
        raise RuntimeError("Nao achei numero de sequencia (sq) no segmento.")
    return hls_segment_duration(m3u8), template, int(m.group(1)), "#EXT-X-ENDLIST" in m3u8


def segment_url(template, sq):
    return re.sub(r"/sq/\d+/", f"/sq/{sq}/", template)


def fetch_url(url, tries=4, timeout=20):
    import urllib.request
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Falhou depois de {tries} tentativas: {last}")


def download_live_hls(job_id, client, url, start_s, end_s, title, height):
    """Caso ao vivo: pega manifesto HLS, troca sq, monta m3u8 local, ffmpeg concat."""
    set_job(job_id, status="running", message="Preparando download da live...", progress=5)
    manifest_url = live_manifest_url(client, url, height)
    set_job(job_id, progress=10, message="Lendo manifesto HLS...")
    seg_duration, template, template_sq, _ = read_manifest(manifest_url)
    start_sq = start_s // seg_duration
    end_sq = (end_s + seg_duration - 1) // seg_duration

    # O release_timestamp e o horario agendado; a live real costuma comecar depois
    # (e em lives 24/7 a numeracao nem parte dele). Se o fim pedido passou da borda
    # da live, desloca a janela inteira pra tras mantendo a duracao.
    live_edge = template_sq - 2
    if end_sq > live_edge:
        shift = end_sq - live_edge
        start_sq -= shift
        end_sq = live_edge
    if start_sq < 0:
        start_sq = 0
    if start_sq >= end_sq:
        raise RuntimeError("Janela vazia. Verifique os horarios.")
    num_segs = end_sq - start_sq + 1

    urls = [segment_url(template, sq) for sq in range(start_sq, end_sq + 1)]
    out_path = download_dir() / f"{clip_basename(title, start_s, end_s, '_LIVE')}.mp4"
    tmp_ts = download_dir() / f".tmp_live_{job_id}.ts"
    try:
        fetch_segments(job_id, urls, tmp_ts, seg_duration)
        set_job(job_id, progress=95, message="Juntando...")
        result = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(tmp_ts),
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            str(out_path),
        ], capture_output=True, text=True)
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-5:]
            raise RuntimeError("\n".join(tail) or "ffmpeg falhou")
    finally:
        tmp_ts.unlink(missing_ok=True)
    finish_download(job_id, [out_path])


def fetch_segments(job_id, urls, dest, seg_duration):
    """Baixa os segmentos HLS em paralelo, com timeout e retry, e concatena em ordem.
    (O ffmpeg baixando sozinho travava pra sempre quando uma conexao engasgava.)"""
    import concurrent.futures
    get = fetch_url

    total = len(urls)
    parts = [dest.with_name(f"{dest.stem}_{i:05d}.part") for i in range(total)]

    def save(i):
        parts[i].write_bytes(get(urls[i]))

    done = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(save, i) for i in range(total)]):
                fut.result()
                done += 1
                set_job(job_id, progress=15 + 80 * done / total,
                        message=f"Baixando... {done * seg_duration}s / {total * seg_duration}s")
        # MPEG-TS pode ser concatenado byte a byte
        with open(dest, "wb") as f:
            for p in parts:
                f.write(p.read_bytes())
    finally:
        for p in parts:
            p.unlink(missing_ok=True)


def record_live(job_id, client, url, title, height, back_min=0):
    """Grava a live: opcionalmente volta `back_min` minutos (DVR do YouTube) e segue ao vivo
    ate o usuario parar ou a live acabar. Baixa segmento por segmento (com retry) num .ts
    que ja e assistivel durante a gravacao; no fim converte pra .mp4."""
    import concurrent.futures

    set_job(job_id, status="running", message="Conectando na live...", progress=0, recording=True)
    with jobs_lock:
        recordings[job_id] = True
    try:
        manifest_url = live_manifest_url(client, url, height)
        seg, template, edge, ended = read_manifest(manifest_url)
        next_sq = max(0, edge - (back_min * 60) // seg) if back_min else edge

        stamp = time.strftime("%Y-%m-%d_%H-%M")
        base = f"{sanitize_filename(title)[:60].rstrip('_-')}_AO_VIVO_{stamp}"
        ts_path = download_dir() / f"{base}_gravando.ts"
        out_path = download_dir() / f"{base}.mp4"
        written = skipped = size = 0
        last_poll = time.time()

        def fetch_one(sq):
            nonlocal template, manifest_url
            try:
                return fetch_url(segment_url(template, sq), tries=3)
            except Exception:
                # URL expirada (sessoes longas)? Renova o manifesto e tenta de novo.
                try:
                    manifest_url = live_manifest_url(client, url, height)
                    template = read_manifest(manifest_url)[1]
                    return fetch_url(segment_url(template, sq), tries=2)
                except Exception:
                    return None  # fora da janela do DVR ou perdido: pula

        with open(ts_path, "ab") as out, concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            while not get_job(job_id).get("stopping"):
                if next_sq > edge:
                    if ended:
                        break
                    time.sleep(max(1, seg / 2))
                    try:
                        seg, template, edge, ended = read_manifest(manifest_url)
                        last_poll = time.time()
                    except Exception:
                        try:
                            manifest_url = live_manifest_url(client, url, height)
                        except Exception:
                            if time.time() - last_poll > 120:
                                break  # live sumiu
                    continue
                batch = list(range(next_sq, min(edge, next_sq + 7) + 1))
                for data in pool.map(fetch_one, batch):
                    if data is None:
                        skipped += 1
                        continue
                    out.write(data)
                    written += 1
                    size += len(data)
                out.flush()
                next_sq = batch[-1] + 1
                behind = max(0, edge - next_sq + 1) * seg
                extra = f" · alcancando o ao vivo ({seconds_to_hhmmss(behind)} atras)" if behind > seg * 2 else ""
                set_job(job_id, message=f"Gravando... {seconds_to_hhmmss(written * seg)} · {size / 1e6:.0f} MB{extra}")

        why = "Gravacao parada" if get_job(job_id).get("stopping") else "A live terminou"
        if written == 0:
            ts_path.unlink(missing_ok=True)
            raise RuntimeError("Nada foi gravado.")
        set_job(job_id, message="Finalizando arquivo...")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(ts_path),
                            "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path)],
                           capture_output=True, text=True)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            ts_path.unlink(missing_ok=True)
            final = out_path
        else:
            out_path.unlink(missing_ok=True)
            final = ts_path  # o .ts continua assistivel
        note = f" ({skipped} segmentos indisponiveis)" if skipped else ""
        set_job(job_id, status="done", progress=100, recording=False,
                message=f"{why} - {seconds_to_hhmmss(written * seg)} gravados{note}", file_path=str(final))
    finally:
        with jobs_lock:
            recordings.pop(job_id, None)


def stop_recording(job_id):
    with jobs_lock:
        active = job_id in recordings
    if active:
        set_job(job_id, stopping=True, message="Finalizando arquivo...")
    return active


def stop_all_recordings():
    with jobs_lock:
        ids = list(recordings)
    for job_id in ids:
        stop_recording(job_id)
    deadline = time.time() + 55
    while time.time() < deadline:
        with jobs_lock:
            if not recordings:
                return
        time.sleep(0.2)

# ---------- Transcricao ----------
# 1) legenda do proprio site (manual, depois automatica, no idioma original);
# 2) sem legenda: Whisper local (whisper.cpp, acelerado pela GPU no Apple Silicon).
WHISPER_MODEL = "ggml-large-v3-turbo-q5_0.bin"
WHISPER_MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{WHISPER_MODEL}"
MODELS_DIR = CONFIG_PATH.parent / "models"


def vtt_time(t):
    parts = t.replace(",", ".").split(":")
    return sum(float(x) * 60 ** i for i, x in enumerate(reversed(parts)))


def vtt_to_text(vtt, start_s=None, end_s=None):
    """WebVTT -> texto puro. Remove tempos/tags e as linhas repetidas das legendas automaticas
    (que vao "rolando": cada bloco repete a linha anterior). Quebra paragrafo em pausas longas."""
    paragraphs, current, recent, last_end = [], [], [], None
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "")):
        m = re.search(r"([\d:.,]+)\s+-->\s+([\d:.,]+)", block)
        if not m:
            continue
        b, e = vtt_time(m.group(1)), vtt_time(m.group(2))
        if (start_s is not None and e <= start_s) or (end_s is not None and b >= end_s):
            continue
        if last_end is not None and b - last_end > 2.5 and current:
            paragraphs.append(" ".join(current))
            current = []
        last_end = e
        for line in block[m.end():].splitlines():
            line = re.sub(r"<[^>]+>", "", line)
            line = re.sub(r"\s+", " ", line.replace("&nbsp;", " ").replace("&amp;", "&")
                          .replace("&lt;", "<").replace("&gt;", ">")).strip()
            if not line or line in recent:
                continue
            current.append(line)
            recent = (recent + [line])[-3:]
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs).strip()


def pick_subtitle(info):
    """-> (idioma, automatica?) da legenda no idioma original, ou None."""
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    orig = [k for k in auto if k.endswith("-orig")]
    lang = info.get("language") or (orig[0][:-5] if orig else (next(iter(auto)) if len(auto) == 1 else None))
    if lang:
        for k in subs:
            if k == lang or k.split("-")[0] == lang.split("-")[0]:
                return k, False
    if subs and not lang:  # idioma desconhecido: a primeira costuma ser a original do autor
        return next(iter(subs)), False
    if orig:
        return orig[0], True
    if lang and lang in auto:
        return lang, True
    return None


def site_transcript(client, url, start_s=None, end_s=None):
    """Texto das legendas do site, ou None se nao houver."""
    r = subprocess.run(ytdlp_cmd(client, "-J", "--skip-download", url), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    try:
        info = json.loads(r.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    choice = pick_subtitle(info)
    if not choice:
        return None
    lang, automatic = choice
    tmp = Path(tempfile.mkdtemp(prefix="cutter-subs-"))
    try:
        r = subprocess.run(ytdlp_cmd(
            client, "--skip-download", "--write-auto-subs" if automatic else "--write-subs",
            "--sub-langs", lang, "--sub-format", "vtt/best", "--convert-subs", "vtt",
            "-o", str(tmp / "sub"), url), capture_output=True, text=True, timeout=180)
        files = sorted(tmp.glob("sub*.vtt"))
        if r.returncode != 0 or not files:
            return None
        return vtt_to_text(files[0].read_text(errors="replace"), start_s, end_s) or None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_whisper_model(job_id):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    model = MODELS_DIR / WHISPER_MODEL
    if model.exists():
        return model
    import urllib.request
    part = model.with_suffix(".part")
    req = urllib.request.Request(WHISPER_MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if total:
                set_job(job_id, progress=100 * got / total,
                        message=f"Baixando o modelo de transcricao (so na primeira vez)... "
                                f"{got / 1e6:.0f} de {total / 1e6:.0f} MB")
    part.rename(model)
    return model


def whisper_transcript(job_id, video, label=""):
    exe = whisper_bin()
    if not exe:
        raise RuntimeError("Sem legenda no site, e o Whisper nao esta instalado. "
                           "No Terminal: brew install whisper-cpp")
    model = ensure_whisper_model(job_id)
    tmp = Path(tempfile.mkdtemp(prefix="cutter-whisper-"))
    try:
        set_job(job_id, progress=0, message=f"Extraindo o audio{label}...")
        wav = tmp / "audio.wav"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn",
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
            raise RuntimeError("O video nao tem audio - nao ha o que transcrever.")
        cmd = [exe, "-m", str(model), "-f", str(wav), "-l", "auto", "-otxt", "-of", str(tmp / "out"),
               "-pp", "-t", str(max(4, min(8, os.cpu_count() or 4)))]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last = []
        for line in proc.stdout:
            last = (last + [line.rstrip()])[-10:]
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                set_job(job_id, progress=float(m.group(1)), message=f"Transcrevendo com Whisper{label}... {m.group(1)}%")
            m = re.search(r"auto-detected language: (\w+)", line)
            if m:
                set_job(job_id, message=f"Transcrevendo com Whisper{label} (idioma: {m.group(1)})...")
        proc.wait()
        out = tmp / "out.txt"
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError("O Whisper falhou:\n" + "\n".join(l for l in last if l.strip())[-800:])
        lines = [l.strip() for l in out.read_text(errors="replace").splitlines()]
        return "\n".join(l for l in lines if l and l != "[BLANK_AUDIO]").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_job(job_id, client, url, files, start_s=None, end_s=None, use_subs=True):
    """Gera <video>.txt ao lado de cada video. Falhar aqui nao apaga o download."""
    txts, notes = [], []
    done_msg = get_job(job_id).get("message") or "Pronto!"  # mensagem do download, antes das etapas da transcricao
    for i, video in enumerate(files):
        video = Path(video)
        label = f" ({i + 1}/{len(files)})" if len(files) > 1 else ""
        try:
            text, source = None, "Whisper"
            if use_subs and len(files) == 1:
                set_job(job_id, progress=0, message="Procurando legendas do site...")
                text, source = site_transcript(client, url, start_s, end_s), "legendas do site"
            if not text:
                text, source = whisper_transcript(job_id, video, label), "Whisper"
            if not text:
                raise RuntimeError("Nenhuma fala reconhecida no audio.")
            txt = video.with_suffix(".txt")
            txt.write_text(text + "\n", encoding="utf-8")
            txts.append(txt)
            notes.append(source)
        except Exception as e:
            notes.append(f"erro: {e}")
    if txts:
        how = ", ".join(sorted(set(n for n in notes if not n.startswith("erro"))))
        msg = f"{done_msg} · transcricao via {how}"
    else:
        msg = f"{done_msg} · transcricao nao gerada"
    errors = [n[6:] for n in notes if n.startswith("erro")]
    set_job(job_id, status="done", progress=100, message=msg, txt_path=str(txts[0]) if txts else "",
            transcript_error="\n".join(errors))


def run_download_job(job_id, url, start_s, end_s, info, height, mode="clip", back_min=0, transcribe=False):
    # is_live chega como bool (via /api/info -> pagina) ou "True"/"False" (direto do yt-dlp)
    is_live = str(info.get("is_live", "")).lower() == "true"
    live_status = info.get("live_status", "")
    title = info.get("title") or "video"
    youtube = is_youtube(url)
    if not youtube:
        # Instagram/TikTok repetem titulos ("Video by fulano"): o id evita sobrescrever outro download.
        vid = str(info.get("id") or "")
        if vid and vid != "NA" and vid not in title:
            title = f"{title[:40]} {vid}"
        if is_live or live_status == "is_live":
            set_job(job_id, status="error",
                    error=f"Gravar ou recortar live em andamento so funciona no YouTube por enquanto "
                          f"({site_name(url)} nao suportado). Quando a live terminar, da pra baixar o video.")
            return
    if (is_live or live_status == "is_live") and mode == "record":
        fn = lambda job_id, client, url, _s, _e, title, height: record_live(job_id, client, url, title, height, back_min)
        info = {**info, "client": "web_embedded"}
    elif is_live or live_status == "is_live":
        if start_s is None:
            set_job(job_id, status="error", error="Live em andamento: escolha um trecho ou grave ao vivo.")
            return
        fn = download_live_hls
        # So o web_embedded entrega HLS muxado (itags 91-96) em live.
        info = {**info, "client": "web_embedded"}
    elif live_status == "post_live":
        fn = download_post_live
    else:
        fn = download_vod_section
    if start_s is None and mode == "full":
        try:
            count = int(info.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        fn = lambda job_id, client, url, _s, _e, title, height: download_full(job_id, client, url, title, height, count)

    clients = client_order(info.get("client"), url)
    transcribe = transcribe and mode != "record"
    set_job(job_id, transcribe=transcribe)
    for i, client in enumerate(clients):
        try:
            fn(job_id, client, url, start_s, end_s, title, height)
            if transcribe:
                # Live em andamento: a numeracao de tempo da legenda nao bate com o trecho; vai de Whisper.
                live_now = is_live or live_status == "is_live"
                transcribe_job(job_id, client, url, get_job(job_id).get("files") or [],
                               start_s, end_s, use_subs=not live_now)
            return
        except YtdlpError as e:
            if e.fatal or i == len(clients) - 1:
                fail_job(job_id, e.raw, url)
                return
            set_job(job_id, progress=0, message=f"Falhou com o cliente {client}, tentando {clients[i + 1]}...")
        except Exception as e:
            fail_job(job_id, str(e), url)
            return


# --- Formatos de saida (ideia do cobalt: modos "audio" e "mute") ---
# Codigo novo e separado: o caminho de video normal (run_download_job) segue intocado.
AUDIO_FORMATS = {"mp3", "m4a", "opus", "wav", "best"}
AUDIO_BITRATES = {320, 256, 192, 128, 96, 64}
AUDIO_CODECS = {"mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"], "opus": ["-c:a", "libopus"],
                "wav": ["-c:a", "pcm_s16le"], "best": ["-c:a", "copy"]}
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac", ".webm", ".mka")


def audio_ytdlp_args(fmt, bitrate):
    """Extrai so o audio, com titulo/artista e capa embutidos quando o formato aceita."""
    args = ["-f", "ba/b", "-x", "--audio-format", fmt, "--embed-metadata"]
    if fmt not in ("best", "wav"):
        args += ["--audio-quality", f"{bitrate}K"]
    if fmt in ("mp3", "m4a"):
        args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    return args


def download_audio(job_id, client, url, start_s, end_s, title, fmt, bitrate, count=1):
    """So audio direto pelo yt-dlp (VOD, inteiro ou trecho): nao baixa a faixa de video."""
    clip = start_s is not None
    label = "audio do trecho" if clip else "audio"
    set_job(job_id, status="running", message=f"Baixando {label}...", progress=0)
    base = clip_basename(title, start_s, end_s) if clip else sanitize_filename(title)[:80].rstrip('_-')
    many = count > 1 and not clip
    out_tmpl = str(download_dir() / (f"{base}_%(playlist_index)02d.%(ext)s" if many else f"{base}.%(ext)s"))
    started = time.time() - 2
    cmd = ytdlp_cmd(
        client, "--newline", "--no-mtime",
        *(["--download-sections", f"*{start_s}-{end_s}"] if clip else []),
        *audio_ytdlp_args(fmt, bitrate),
        "-o", out_tmpl,
        url,
    )
    run_ytdlp_streaming(cmd, lambda pct: set_job(
        job_id, progress=pct, message=f"Baixando {label}... {pct:.0f}%"))
    pattern = f"{base}_[0-9][0-9].*" if many else f"{base}.*"
    files = sorted(f for f in download_dir().glob(pattern) if f.suffix.lower() in AUDIO_EXTS)
    # A pasta pode ter um audio antigo com o mesmo nome em outro formato: fica com o que acabou de sair.
    files = [f for f in files if f.stat().st_mtime >= started] or files
    if not files:
        raise RuntimeError("O yt-dlp terminou mas o arquivo de audio nao apareceu na pasta.")
    finish_download(job_id, files, f"Pronto! {len(files)} audios salvos" if len(files) > 1 else "Pronto!")


def ffmpeg_convert(src, output, fmt, bitrate, title="", artist=""):
    """Transforma um video ja baixado em audio (ou video sem som). Devolve o arquivo novo."""
    src = Path(src)
    if output == "mute":
        tmp = src.with_name(f".tmp_mudo_{src.name}")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v", "-c", "copy", "-an",
               "-movflags", "+faststart", str(tmp)]
        dest = src
    else:
        ext = "m4a" if fmt == "best" else fmt
        dest = src.with_suffix("." + ext)
        tmp = src.with_name(f".tmp_audio_{dest.name}")
        rate = [] if fmt in ("best", "wav") else ["-b:a", f"{bitrate}k"]
        meta = [x for k, v in (("title", title), ("artist", artist)) if v for x in ("-metadata", f"{k}={v}")]
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", *AUDIO_CODECS[fmt], *rate, *meta, str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Falha ao converter com ffmpeg:\n" + "\n".join(tail_of(r.stderr.splitlines())))
    os.replace(tmp, dest)
    if dest != src:
        src.unlink(missing_ok=True)
    return dest


def run_then_convert(job_id, url, start_s, end_s, info, height, mode, output, fmt, bitrate, transcribe):
    """Roda o download de video normal num job interno e depois converte o resultado.
    Cobre live em andamento, live encerrada e "sem som" sem tocar no fluxo existente."""
    inner = job_id + "_v"
    t = threading.Thread(target=run_download_job, daemon=True,
                         args=(inner, url, start_s, end_s, info, height, mode, 0, transcribe))
    t.start()
    while t.is_alive():
        j = get_job(inner)
        set_job(job_id, status="running", progress=j.get("progress", 0), message=j.get("message", "Baixando..."))
        t.join(0.5)
    j = get_job(inner)
    with jobs_lock:
        jobs.pop(inner, None)
    if j.get("status") != "done":
        set_job(job_id, status="error", error=j.get("error") or "Falha no download", login=j.get("login", False))
        return
    set_job(job_id, progress=0, message="Removendo o audio..." if output == "mute" else "Extraindo o audio...")
    try:
        files = [ffmpeg_convert(f, output, fmt, bitrate, info.get("title") or "", info.get("channel") or "")
                 for f in j.get("files") or [j["file_path"]]]
    except Exception as e:
        fail_job(job_id, str(e), url)
        return
    set_job(job_id, status="done", progress=100, message=j.get("message") or "Pronto!",
            file_path=str(files[0]), files=[str(f) for f in files], output=output,
            txt_path=j.get("txt_path", ""), transcript_error=j.get("transcript_error", ""))


def run_media_job(job_id, url, start_s, end_s, info, height, mode, output, fmt, bitrate, transcribe):
    """Entrada dos formatos novos (output = "audio" ou "mute")."""
    set_job(job_id, output=output)
    live_status = info.get("live_status", "")
    live_or_post = str(info.get("is_live", "")).lower() == "true" or live_status in ("is_live", "post_live")
    if output == "mute" or live_or_post:
        return run_then_convert(job_id, url, start_s, end_s, info, height, mode, output, fmt, bitrate, transcribe)
    title = info.get("title") or "audio"
    if not is_youtube(url):
        vid = str(info.get("id") or "")
        if vid and vid != "NA" and vid not in title:
            title = f"{title[:40]} {vid}"
    try:
        count = int(info.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    clients = client_order(info.get("client"), url)
    set_job(job_id, transcribe=transcribe)
    for i, client in enumerate(clients):
        try:
            download_audio(job_id, client, url, start_s, end_s, title, fmt, bitrate, count)
            if transcribe:
                transcribe_job(job_id, client, url, get_job(job_id).get("files") or [], start_s, end_s)
            return
        except YtdlpError as e:
            if e.fatal or i == len(clients) - 1:
                fail_job(job_id, e.raw, url)
                return
            set_job(job_id, progress=0, message=f"Falhou com o cliente {client}, tentando {clients[i + 1]}...")
        except Exception as e:
            fail_job(job_id, str(e), url)
            return


# --- Legenda .srt (codigo novo; a transcricao .txt original continua igual quando o .srt nao e pedido) ---
def srt_time(t):
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def clean_sub_line(line):
    line = re.sub(r"<[^>]+>", "", line)
    return re.sub(r"\s+", " ", line.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">")).strip()


def vtt_to_srt(vtt, start_s=None, end_s=None, offset=0.0):
    """WebVTT -> SRT. Legendas automaticas do YouTube "rolam" (cada bloco repete a linha anterior):
    fica so a linha nova de cada bloco. offset = segundo do video original que vira 0 no arquivo."""
    cues, recent = [], []
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "")):
        m = re.search(r"([\d:.,]+)\s+-->\s+([\d:.,]+)", block)
        if not m:
            continue
        b, e = vtt_time(m.group(1)), vtt_time(m.group(2))
        if (start_s is not None and e <= start_s) or (end_s is not None and b >= end_s):
            continue
        new = []
        for line in block[m.end():].splitlines():
            line = clean_sub_line(line)
            if line and line not in recent:
                new.append(line)
                recent = (recent + [line])[-3:]
        if not new or e - b < 0.05:
            continue
        if end_s is not None:
            e = min(e, end_s)
        cues.append((max(0.0, b - offset), max(0.0, e - offset), "\n".join(new)))
    # Blocos que se sobrepoem (rolagem) atrapalham alguns players: corta o fim no inicio do proximo.
    out = []
    for i, (b, e, text) in enumerate(cues):
        if i + 1 < len(cues):
            e = min(e, cues[i + 1][0]) if cues[i + 1][0] > b else e
        out.append(f"{len(out) + 1}\n{srt_time(b)} --> {srt_time(e)}\n{text}\n")
    return "\n".join(out)


def site_vtt(client, url):
    """VTT bruto das legendas do site (mesma escolha de idioma da transcricao), ou None."""
    r = subprocess.run(ytdlp_cmd(client, "-J", "--skip-download", url), capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    try:
        info = json.loads(r.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    choice = pick_subtitle(info)
    if not choice:
        return None
    lang, automatic = choice
    tmp = Path(tempfile.mkdtemp(prefix="cutter-subs-"))
    try:
        r = subprocess.run(ytdlp_cmd(
            client, "--skip-download", "--write-auto-subs" if automatic else "--write-subs",
            "--sub-langs", lang, "--sub-format", "vtt/best", "--convert-subs", "vtt",
            "-o", str(tmp / "sub"), url), capture_output=True, text=True, timeout=180)
        files = sorted(tmp.glob("sub*.vtt"))
        if r.returncode != 0 or not files:
            return None
        return files[0].read_text(errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def media_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def clip_offset(path, start_s, end_s):
    """Recorte sem reencode comeca no keyframe anterior ao inicio pedido: estima o inicio real pelo fim."""
    if start_s is None:
        return 0.0
    dur = media_duration(path)
    if not dur or end_s is None:
        return float(start_s)
    return min(float(start_s), max(float(start_s) - 10, end_s - dur))


def whisper_srt(job_id, media, label=""):
    """Whisper uma vez so, saindo .srt e .txt. -> (srt, txt)"""
    exe = whisper_bin()
    if not exe:
        raise RuntimeError("Sem legenda no site, e o Whisper nao esta instalado. "
                           "No Terminal: brew install whisper-cpp")
    model = ensure_whisper_model(job_id)
    tmp = Path(tempfile.mkdtemp(prefix="cutter-whisper-"))
    try:
        set_job(job_id, progress=0, message=f"Extraindo o audio{label}...")
        wav = tmp / "audio.wav"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(media), "-vn",
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
            raise RuntimeError("O arquivo nao tem audio - nao ha o que transcrever.")
        # -ml/-sow: frases curtas, do tamanho de uma legenda (~2 linhas)
        cmd = [exe, "-m", str(model), "-f", str(wav), "-l", "auto", "-osrt", "-otxt", "-ml", "84", "-sow",
               "-of", str(tmp / "out"), "-pp", "-t", str(max(4, min(8, os.cpu_count() or 4)))]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last = []
        for line in proc.stdout:
            last = (last + [line.rstrip()])[-10:]
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                set_job(job_id, progress=float(m.group(1)), message=f"Gerando legenda com Whisper{label}... {m.group(1)}%")
            m = re.search(r"auto-detected language: (\w+)", line)
            if m:
                set_job(job_id, message=f"Gerando legenda com Whisper{label} (idioma: {m.group(1)})...")
        proc.wait()
        srt, txt = tmp / "out.srt", tmp / "out.txt"
        if proc.returncode != 0 or not srt.exists():
            raise RuntimeError("O Whisper falhou:\n" + "\n".join(l for l in last if l.strip())[-800:])
        cues = []
        for block in re.split(r"\n\s*\n", srt.read_text(errors="replace").strip()):
            lines = block.splitlines()
            if len(lines) >= 3 and "-->" in lines[1]:
                text = "\n".join(l.strip() for l in lines[2:] if l.strip())
                if text and text != "[BLANK_AUDIO]":
                    cues.append(f"{len(cues) + 1}\n{lines[1].strip()}\n{text}\n")
        txt_lines = [l.strip() for l in txt.read_text(errors="replace").splitlines()] if txt.exists() else []
        return "\n".join(cues), "\n".join(l for l in txt_lines if l and l != "[BLANK_AUDIO]").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_files_job(job_id, paths):
    """Arquivo ja baixado (arrastado pro app ou escolhido): gera <arquivo>.srt e <arquivo>.txt ao lado dele."""
    if not whisper_bin():
        set_job(job_id, status="error", error="Para gerar legenda de um arquivo, instale o Whisper. "
                                              "No Terminal: brew install whisper-cpp")
        return
    done, errors = [], []
    for i, media in enumerate(paths):
        media = Path(media)
        label = f" ({i + 1}/{len(paths)}: {media.name})" if len(paths) > 1 else ""
        try:
            srt, txt = whisper_srt(job_id, media, label)
            if not srt.strip():
                raise RuntimeError("Nenhuma fala reconhecida no audio.")
            srt_path, txt_path = media.with_suffix(".srt"), media.with_suffix(".txt")
            srt_path.write_text(srt, encoding="utf-8")
            txt_path.write_text(txt + "\n", encoding="utf-8")
            done.append((media, txt_path, srt_path))
        except Exception as e:
            errors.append(f"{media.name}: {e}" if len(paths) > 1 else str(e))
    if not done:
        set_job(job_id, status="error", error="\n\n".join(errors) or "Falha ao gerar a legenda")
        return
    media, txt_path, srt_path = done[0]
    msg = "Legenda pronta" if len(done) == 1 else f"Legendas prontas ({len(done)} arquivos)"
    set_job(job_id, status="done", progress=100, message=msg, file_path=str(media), txt_path=str(txt_path),
            srt_path=str(srt_path), transcript_error="\n".join(errors), local=True)


def subtitles_job(job_id, client, url, files, start_s, end_s, use_subs, want_txt, done_msg):
    """Gera <arquivo>.srt (e o .txt, se pedido) ao lado de cada arquivo baixado."""
    srts, txts, notes = [], [], []
    for i, media in enumerate(files):
        media = Path(media)
        label = f" ({i + 1}/{len(files)})" if len(files) > 1 else ""
        try:
            srt = txt = None
            source = "Whisper"
            if use_subs and len(files) == 1:
                set_job(job_id, progress=0, message="Procurando legendas do site...")
                vtt = site_vtt(client, url)
                if vtt:
                    srt = vtt_to_srt(vtt, start_s, end_s, clip_offset(media, start_s, end_s)) or None
                    txt = vtt_to_text(vtt, start_s, end_s) or None
                    source = "legendas do site"
            if not srt:
                srt, txt = whisper_srt(job_id, media, label)
                source = "Whisper"
            if not srt:
                raise RuntimeError("Nenhuma fala reconhecida no audio.")
            out = media.with_suffix(".srt")
            out.write_text(srt + "\n", encoding="utf-8")
            srts.append(out)
            if want_txt and txt:
                t = media.with_suffix(".txt")
                t.write_text(txt + "\n", encoding="utf-8")
                txts.append(t)
            notes.append(source)
        except Exception as e:
            notes.append(f"erro: {e}")
    how = ", ".join(sorted(set(n for n in notes if not n.startswith("erro"))))
    what = "legenda e transcricao" if want_txt else "legenda"
    msg = f"{done_msg} · {what} via {how}" if srts else f"{done_msg} · legenda nao gerada"
    errors = [n[6:] for n in notes if n.startswith("erro")]
    set_job(job_id, status="done", progress=100, message=msg, srt_path=str(srts[0]) if srts else "",
            txt_path=str(txts[0]) if txts else "", transcript_error="\n".join(errors))


def run_with_subtitles(job_id, url, start_s, end_s, info, height, mode, output, fmt, bitrate, want_txt):
    """Roda o download normal (video, audio ou sem som) num job interno e depois gera o .srt."""
    inner = job_id + "_s"
    if output in ("audio", "mute"):
        target, args = run_media_job, (inner, url, start_s, end_s, info, height, mode, output, fmt, bitrate, False)
    else:
        target, args = run_download_job, (inner, url, start_s, end_s, info, height, mode, 0, False)
    t = threading.Thread(target=target, daemon=True, args=args)
    t.start()
    while t.is_alive():
        j = get_job(inner)
        set_job(job_id, status="running", progress=j.get("progress", 0), message=j.get("message", "Baixando..."))
        t.join(0.5)
    j = get_job(inner)
    with jobs_lock:
        jobs.pop(inner, None)
    if j.get("status") != "done":
        set_job(job_id, status="error", error=j.get("error") or "Falha no download", login=j.get("login", False))
        return
    set_job(job_id, status="running", file_path=j.get("file_path"), files=j.get("files"), output=j.get("output", output))
    live_now = str(info.get("is_live", "")).lower() == "true" or info.get("live_status") == "is_live"
    client = "web_embedded" if live_now else (info.get("client") or "default")
    subtitles_job(job_id, client, url, j.get("files") or [j["file_path"]], start_s, end_s,
                  use_subs=not live_now, want_txt=want_txt, done_msg=j.get("message") or "Pronto!")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._serve_file(APP_DIR / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/api/progress/"):
            job_id = path.rsplit("/", 1)[-1]
            self._json(200, get_job(job_id) or {"status": "unknown"})
        elif path == "/api/config":
            self._json(200, {**load_config(), "browsers": COOKIE_BROWSERS,
                             "missing": missing_dependencies(), "whisper": bool(whisper_bin())})
        elif path.startswith("/api/stop/"):
            job_id = path.rsplit("/", 1)[-1]
            self._json(200, {"ok": stop_recording(job_id)})
        elif path == "/api/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), stop_all_recordings(), os._exit(0))).start()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/info":
                data = self._read_json()
                url = normalize_url(data.get("url", ""))
                if not url:
                    return self._json(400, {"error": "URL vazia"})
                try:
                    info = run_ytdlp_info(url)
                except YtdlpError as e:
                    msg, login = explain_error(e.raw, url)
                    return self._json(422, {"error": msg, "login": login})
                try:
                    dur_s = int(float(info.get("duration") or 0))
                except ValueError:
                    dur_s = 0
                self._json(200, {
                    "title": info.get("title", ""),
                    "duration_seconds": dur_s,
                    "duration_hms": seconds_to_hhmmss(dur_s) if dur_s else "",
                    "is_live": info.get("is_live", "").lower() == "true",
                    "live_status": info.get("live_status", ""),
                    "release_timestamp": info.get("release_timestamp", ""),
                    "client": info.get("client", ""),
                    "thumbnail": info.get("thumbnail", "") if info.get("thumbnail", "").startswith("http") else "",
                    "channel": "" if info.get("channel") in (None, "NA") else info.get("channel", ""),
                    "max_height": int(info["height"]) if info.get("height", "").isdigit() else 0,
                    "id": info.get("id", ""),
                    "site": site_name(url),
                    "youtube": is_youtube(url),
                    "count": info.get("count", 1),
                })
            elif path == "/api/download":
                data = self._read_json()
                url = normalize_url(data.get("url", ""))
                start = data.get("start", "").strip()
                end = data.get("end", "").strip()
                info = data.get("info", {})
                try:
                    height = int(data.get("quality", 720))
                except (TypeError, ValueError):
                    height = 720
                if height not in QUALITIES:
                    height = 720
                mode = data.get("mode", "clip")
                try:
                    back_min = max(0, min(int(data.get("back_min", 0)), 240))
                except (TypeError, ValueError):
                    back_min = 0
                if mode in ("full", "record"):
                    start_s = end_s = None
                else:
                    try:
                        start_s = hhmmss_to_seconds(start)
                        end_s = hhmmss_to_seconds(end)
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    if end_s <= start_s:
                        return self._json(400, {"error": "O fim precisa ser maior que o inicio."})
                job_id = uuid.uuid4().hex[:12]
                set_job(job_id, status="queued", progress=0, message="Iniciando...")
                transcribe = bool(data.get("transcribe"))
                output = data.get("output", "video")
                fmt = data.get("audio_format") if data.get("audio_format") in AUDIO_FORMATS else "mp3"
                try:
                    bitrate = int(data.get("audio_bitrate", 192))
                except (TypeError, ValueError):
                    bitrate = 192
                if bitrate not in AUDIO_BITRATES:
                    bitrate = 192
                if data.get("srt") and mode != "record":
                    t = threading.Thread(target=run_with_subtitles, daemon=True,
                                         args=(job_id, url, start_s, end_s, info, height, mode,
                                               output if output in ("audio", "mute") else "video",
                                               fmt, bitrate, transcribe))
                elif output in ("audio", "mute") and mode != "record":
                    t = threading.Thread(target=run_media_job, daemon=True,
                                         args=(job_id, url, start_s, end_s, info, height, mode, output,
                                               fmt, bitrate, transcribe))
                else:
                    t = threading.Thread(target=run_download_job, daemon=True,
                                         args=(job_id, url, start_s, end_s, info, height, mode, back_min, transcribe))
                t.start()
                self._json(200, {"job_id": job_id})
            elif path == "/api/open":
                data = self._read_json()
                p = data.get("path", "")
                if p and Path(p).exists():
                    subprocess.Popen(["open", p])
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "Arquivo nao encontrado"})
            elif path == "/api/config":
                data = self._read_json()
                cfg = load_config()
                if "download_dir" in data:
                    d = os.path.expanduser(str(data["download_dir"]).strip())
                    if not d or not os.path.isabs(d):
                        return self._json(400, {"error": "Use um caminho completo, ex.: ~/Downloads"})
                    cfg["download_dir"] = d
                if data.get("cookies_browser", cfg["cookies_browser"]) in COOKIE_BROWSERS:
                    cfg["cookies_browser"] = data.get("cookies_browser", cfg["cookies_browser"])
                save_config(cfg)
                self._json(200, cfg)
            elif path == "/api/transcribe-file":
                # So a pagina do proprio Cutter pode pedir (outro site aberto no navegador nao).
                origin = self.headers.get("Origin", "")
                if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
                    return self._json(403, {"error": "Origem nao permitida"})
                paths = [local_path(p) for p in self._read_json().get("paths", []) if str(p).strip()]
                missing = [str(p) for p in paths if not p.is_file()]
                if not paths or missing:
                    return self._json(400, {"error": ("Arquivo nao encontrado: " + ", ".join(missing))
                                            if missing else "Nenhum arquivo"})
                job_id = uuid.uuid4().hex[:12]
                set_job(job_id, status="queued", progress=0, message="Iniciando...")
                threading.Thread(target=transcribe_files_job, args=(job_id, [str(p) for p in paths]),
                                 daemon=True).start()
                self._json(200, {"job_id": job_id})
            elif path == "/api/reveal":
                p = self._read_json().get("path", "")
                if p and Path(p).exists():
                    subprocess.Popen(["open", "-R", p])
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "Arquivo nao encontrado"})
            elif path == "/api/open-folder":
                subprocess.Popen(["open", str(download_dir())])
                self._json(200, {"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def find_free_port(start):
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("Nao encontrei porta livre.")


def main():
    port = find_free_port(PORT_START)
    print(f"Cutter rodando em http://localhost:{port}")
    print("Deixe esta janela aberta ate terminar de usar.")
    print("Pra encerrar, feche esta janela ou aperte Ctrl+C.")
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        if "--open" in sys.argv:
            threading.Thread(target=lambda: (time.sleep(0.8), subprocess.Popen(["open", f"http://localhost:{port}"]))).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nEncerrando.")
            stop_all_recordings()


if __name__ == "__main__":
    main()
