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
CLIENTS = ["default", "web_embedded", "tv"]
QUALITIES = {0, 360, 480, 720, 1080, 1440, 2160}

# Erros que nao mudam trocando de cliente - nao adianta tentar de novo.
FATAL_PATTERNS = [
    "Private video", "members-only", "Join this channel", "This live event will begin",
    "has been removed", "account associated with this video has been terminated",
    "copyright claim",
]

# stderr do yt-dlp -> mensagem que da pra entender. (padrao, mensagem)
FRIENDLY_ERRORS = [
    (r"cookies are no longer valid|rotated in the browser|Sign in to confirm (you.re not a bot|your age)",
     "O YouTube pediu login ou os cookies do navegador venceram.\n"
     "Em Ajustes, escolha um navegador em que voce esteja logado no YouTube "
     "(ou abra youtube.com nele e confira o login) e tente de novo."),
    (r"could not find \w+ cookies|failed to load cookies|cookies database",
     "Nao consegui ler os cookies do navegador escolhido em Ajustes. "
     "Ele esta instalado e ja abriu o YouTube?"),
    (r"Private video", "Video privado."),
    (r"members-only|Join this channel", "Video exclusivo pra membros do canal."),
    (r"This live event will begin", "A live ainda nao comecou."),
    (r"HTTP Error 403", "O YouTube recusou o download (403). Tente de novo em alguns minutos."),
    (r"HTTP Error 429|Too Many Requests", "Muitas requisicoes - o YouTube limitou. Espere uns minutos."),
    (r"Video unavailable|video is unavailable", "Video indisponivel (removido, bloqueado na regiao ou sem permissao)."),
    (r"Unsupported URL|is not a valid URL", "Link invalido ou nao suportado."),
]


class YtdlpError(RuntimeError):
    def __init__(self, raw):
        super().__init__(raw)
        self.raw = raw
        self.fatal = any(p.lower() in raw.lower() for p in FATAL_PATTERNS)


def friendly_error(raw):
    for pattern, msg in FRIENDLY_ERRORS:
        if re.search(pattern, raw, re.IGNORECASE):
            return f"{msg}\n\nDetalhe tecnico:\n{raw}"
    return raw


def ytdlp_cmd(client, *args):
    cookies = load_config()["cookies_browser"]
    return [
        "yt-dlp", "--no-update",
        *(["--cookies-from-browser", cookies] if cookies else []),
        "--extractor-args", f"youtube:player_client={client}",
        *args,
    ]


def client_order(preferred):
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
    for client in CLIENTS:
        cmd = ytdlp_cmd(
            client, "--no-download",
            "--print", "title:::%(title)s",
            "--print", "duration:::%(duration)s",
            "--print", "is_live:::%(is_live)s",
            "--print", "live_status:::%(live_status)s",
            "--print", "release_timestamp:::%(release_timestamp)s",
            "--print", "height:::%(height)s",
            "--print", "thumbnail:::%(thumbnail)s",
            "--print", "channel:::%(channel)s",
            url,
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            info = {"client": client}
            for line in result.stdout.strip().splitlines():
                if ":::" in line:
                    k, v = line.split(":::", 1)
                    info[k] = v
            return info
        err = YtdlpError("\n".join(tail_of((result.stderr or result.stdout).splitlines())) or "Falha ao buscar info")
        errors.append(err)
        if err.fatal:
            break
    raise RuntimeError(friendly_error(errors[-1].raw))


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
    set_job(job_id, status="done", progress=100, message="Pronto!", file_path=str(out_path))


def download_full(job_id, client, url, title, height):
    """Video inteiro (VOD ou live encerrada)."""
    set_job(job_id, status="running", message="Baixando video...", progress=0)
    out_path = download_dir() / f"{sanitize_filename(title)[:80].rstrip('_-')}.mp4"
    cmd = ytdlp_cmd(
        client, "--newline",
        *format_args(height),
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "5",
        "-o", str(out_path),
        url,
    )
    run_ytdlp_streaming(cmd, lambda pct: set_job(
        job_id, progress=pct, message=f"Baixando video... {pct:.0f}%"))
    set_job(job_id, status="done", progress=100, message="Pronto!", file_path=str(out_path))


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
    set_job(job_id, status="done", progress=100, message="Pronto!", file_path=str(out_path))


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
    set_job(job_id, status="done", progress=100, message="Pronto!", file_path=str(out_path))


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


def run_download_job(job_id, url, start_s, end_s, info, height, mode="clip", back_min=0):
    # is_live chega como bool (via /api/info -> pagina) ou "True"/"False" (direto do yt-dlp)
    is_live = str(info.get("is_live", "")).lower() == "true"
    live_status = info.get("live_status", "")
    title = info.get("title") or "video"
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
        full = download_full
        fn = lambda job_id, client, url, _s, _e, title, height: full(job_id, client, url, title, height)

    clients = client_order(info.get("client"))
    for i, client in enumerate(clients):
        try:
            fn(job_id, client, url, start_s, end_s, title, height)
            return
        except YtdlpError as e:
            if e.fatal or i == len(clients) - 1:
                set_job(job_id, status="error", error=friendly_error(e.raw))
                return
            set_job(job_id, progress=0, message=f"Falhou com o cliente {client}, tentando {clients[i + 1]}...")
        except Exception as e:
            set_job(job_id, status="error", error=friendly_error(str(e)))
            return


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
                             "missing": missing_dependencies()})
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
                url = data.get("url", "").strip()
                if not url:
                    return self._json(400, {"error": "URL vazia"})
                info = run_ytdlp_info(url)
                try:
                    dur_s = int(info.get("duration") or 0)
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
                })
            elif path == "/api/download":
                data = self._read_json()
                url = data.get("url", "").strip()
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
                t = threading.Thread(target=run_download_job, args=(job_id, url, start_s, end_s, info, height, mode, back_min), daemon=True)
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
