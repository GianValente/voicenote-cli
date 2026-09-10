#!/usr/bin/env python3
"""
VoiceNote CLI (versão PÚBLICA, agnóstica de SO).

Esta é a versão pensada pro REPOSITÓRIO PÚBLICO: grava nota de voz no terminal,
transcreve via OpenAI e devolve o texto (+ salva .md). A lógica de transcrição,
fatiamento e arquivos é idêntica à versão original; o que muda é que gravação e
clipboard agora detectam o sistema operacional (macOS / Linux / Windows).

⚠️ Testado em macOS. Gravação ao vivo em Linux (pulse/alsa) e Windows (dshow) foi
   portada mas ainda PRECISA de validação em campo — contribuições bem-vindas.
   O modo "transcrever fila de arquivos" (opção 2) roda em qualquer SO.

Backend de áudio pode ser forçado via env VOICENOTE_AUDIO_BACKEND
(avfoundation | pulse | alsa | dshow). Senão é deduzido do SO.
"""

__version__ = "0.2.0"

# 🔴 --version responde AQUI, antes de qualquer import pesado, e isso e a
# diferenca entre a flag servir e nao servir. Quem pergunta a versao quase
# sempre esta com um problema -- e o problema mais comum deste CLI e
# justamente uma dependencia que nao carrega:
#
#   ImportError: ... _pydantic_core ... incompatible architecture
#   (have 'x86_64', need 'arm64')
#
# Se a checagem morasse depois do `from openai import OpenAI`, a flag falharia
# exatamente na hora em que e mais necessaria. Medido em 2026-09-10, tentando
# rodar `--version` nesta maquina.
import sys as _sys
if "--version" in _sys.argv or "-V" in _sys.argv:
    print(f"VoiceNote CLI {__version__}")
    raise SystemExit(0)

from pathlib import Path
from datetime import datetime
from openai import OpenAI
import subprocess
import platform
import shutil
import os
import re
import sys
import threading
import time


# =========================
# CONFIGURAÇÕES
# =========================

# Onde ficam os dados do usuário (fila, transcrições, áudios processados).
#
# ⚠️ POR QUE ISSO NÃO É `voicenote-cli`: a instrução de instalação clona o repositório
# nessa pasta, então os dados nasceriam DENTRO da árvore de trabalho do git. O `.gitignore`
# já evita que apareçam no `git status`, mas isso não cobre o pior caso: um `git clean -fdx`
# — que é exatamente o que se digita quando alguém manda "limpa tudo e clona de novo" —
# apaga arquivo IGNORADO junto, e aí vão as transcrições da pessoa. Fora isso, um arquivo
# novo no repo com nome colidindo trava o `git pull` sem a pessoa entender por quê.
#
# Separar agora é uma linha. Depois de existir usuário, é migração com transcrição no meio.
#
# A ordem de resolução nunca surpreende quem já usa:
#   1. $VOICENOTE_HOME, se definido
#   2. ~/.voicenote, se já existir  → é pra onde quem migrou apontou
#   3. a pasta antiga, se tiver dado dentro → segue funcionando, com um aviso de como mover
#   4. ~/.voicenote (cria)
LEGACY_BASE_DIR = Path.home() / "voicenote-cli"
DEFAULT_BASE_DIR = Path.home() / ".voicenote"


def resolve_base_dir() -> Path:
    override = os.getenv("VOICENOTE_HOME")
    if override:
        return Path(override).expanduser()

    if DEFAULT_BASE_DIR.exists():
        return DEFAULT_BASE_DIR

    tem_dado = any(
        (LEGACY_BASE_DIR / nome).exists()
        for nome in ("fila", "transcripts", "processados", "temp")
    )
    if tem_dado:
        # O aviso só aparece onde o risco EXISTE: quando a pasta de dados é mesmo uma
        # árvore de git (foi clonada ali). Numa instalação que só copiou o arquivo, não
        # há `git clean` nem `git pull` pra estragar nada — e um banner diário numa
        # ferramenta de uso constante vira ruído que a pessoa aprende a não ler.
        if (LEGACY_BASE_DIR / ".git").exists():
            print(f"⚠️  Seus dados estão dentro do clone do git ({LEGACY_BASE_DIR}).")
            print(f"   Um `git clean -fdx` aí apaga transcrição junto. Pra separar:")
            print(f"     mkdir -p {DEFAULT_BASE_DIR} && mv {LEGACY_BASE_DIR}/{{fila,transcripts,processados,temp}} {DEFAULT_BASE_DIR}/")
        return LEGACY_BASE_DIR

    return DEFAULT_BASE_DIR


BASE_DIR = resolve_base_dir()

FILA_DIR = BASE_DIR / "fila"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
PROCESSADOS_DIR = BASE_DIR / "processados"
TEMP_DIR = BASE_DIR / "temp"

MODEL = "gpt-4o-mini-transcribe"

SUPPORTED_EXTENSIONS = [".m4a", ".mp3", ".wav", ".mp4", ".mpeg", ".mpga", ".webm"]

MAX_FILE_MB = 24
# O modelo de transcrição trunca a saída por volta de 8–11 min de áudio (limite do modelo,
# independente do tamanho do arquivo). Por isso fatiamos por DURAÇÃO, com margem abaixo
# desse teto — não só por tamanho. Um chunk de 6 min em AAC 128k dá ~5,6 MB (folga no upload).
CHUNK_SECONDS = 6 * 60
SILENCE_THRESHOLD_DB = -45
SILENCE_WARNING_SECONDS = 10

# Corte no silêncio (emendas limpas). Cortar em 6:00 cravados cai no meio de uma palavra:
# o modelo recebe meia palavra em cada ponta e "completa" o fragmento — sai palavra inventada,
# palavra perdida ou frase duplicada, uma vez por emenda. Aqui o ponto de corte é empurrado
# pro silêncio mais próximo do alvo, dentro de uma janela.
# O limiar de silêncio NÃO é fixo (um valor cravado ou não acha pausa nenhuma ou marca o áudio
# inteiro) e também NÃO dá pra deduzir do volume médio: medindo 12 gravações reais, o piso de
# ruído variou de -50 a -35 dB SEM acompanhar a média — a gravação mais alta (média -25 dB) tinha
# o piso mais baixo. Por isso o limiar é PROCURADO: começa estrito e afrouxa até as pausas
# aparecerem. Cada passe é só análise (~1 s num áudio de 15 min), então a busca é barata.
CUT_SILENCE_BELOW_MEAN_DB = 20   # ponto de PARTIDA da busca: 20 dB abaixo do volume médio
CUT_SILENCE_DB_RANGE = (-50, -20)  # ...limitado a esta faixa, pra não degenerar nos extremos
CUT_SILENCE_DB_STEP = 5          # de quanto em quanto afrouxa quando não aparece pausa
CUT_SILENCE_DB_CEILING = -30     # teto da busca: acima disso, fala baixa já conta como silêncio
CUT_SILENCE_MIN_SECONDS = 0.2    # a pausa entre duas palavras já basta pra emendar limpo
CUT_SEARCH_WINDOW = 45           # o quanto o corte pode andar pra achar silêncio (±s)
CUT_MIN_CHUNK_SECONDS = 60       # nenhuma parte menor que isso (evita rabinho inútil)

TRANSCRIPTION_PROMPT = """
Transcrição em português brasileiro.
A fala pode ser espontânea, com vícios de linguagem como "né", "tipo", "enfim".
Preserve o conteúdo original da fala.
Não resuma.
Não transforme em texto formal.
Mantenha nomes próprios, termos técnicos e palavras em inglês quando forem usados.
"""


# =========================
# SISTEMA OPERACIONAL / BACKEND DE ÁUDIO
# =========================

SYSTEM = platform.system()  # "Darwin" (macOS) | "Linux" | "Windows"


def audio_backend() -> str:
    """Backend de entrada do ffmpeg pra gravar. Env VOICENOTE_AUDIO_BACKEND sobrepõe."""
    forced = os.getenv("VOICENOTE_AUDIO_BACKEND", "").strip().lower()
    if forced in {"avfoundation", "pulse", "alsa", "dshow"}:
        return forced
    if SYSTEM == "Darwin":
        return "avfoundation"
    if SYSTEM == "Windows":
        return "dshow"
    return "pulse"  # Linux: PulseAudio/PipeWire é o mais comum (use VOICENOTE_AUDIO_BACKEND=alsa se preferir)


def ffmpeg_install_hint() -> str:
    if SYSTEM == "Darwin":
        return "Instale com: brew install ffmpeg"
    if SYSTEM == "Windows":
        return "Instale com: winget install Gyan.FFmpeg  (ou choco install ffmpeg)"
    return "Instale com: sudo apt install ffmpeg  (Debian/Ubuntu) — ou o gerenciador da sua distro"


def ffmpeg_input_args(device: str) -> list[str]:
    """Monta os args de ENTRADA do ffmpeg conforme o backend do SO."""
    backend = audio_backend()
    if backend == "avfoundation":
        # macOS: áudio-only é ":<índice>"
        return ["-f", "avfoundation", "-i", f":{device}"]
    if backend == "dshow":
        # Windows: nome EXATO do dispositivo
        return ["-f", "dshow", "-i", f"audio={device}"]
    if backend == "alsa":
        return ["-f", "alsa", "-i", device or "default"]
    # pulse
    return ["-f", "pulse", "-i", device or "default"]


# =========================
# PREPARAÇÃO
# =========================

def ensure_directories():
    for directory in [FILA_DIR, TRANSCRIPTS_DIR, PROCESSADOS_DIR, TEMP_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def check_ffmpeg():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        print(f"Erro: {', '.join(missing)} não encontrado(s).")
        print(ffmpeg_install_hint())
        sys.exit(1)


def check_api_key():
    if not os.getenv("OPENAI_API_KEY"):
        print("Erro: variável OPENAI_API_KEY não configurada.")
        if SYSTEM == "Windows":
            print('Configure (PowerShell): $env:OPENAI_API_KEY="sua_chave_aqui"')
        else:
            print('Configure com: export OPENAI_API_KEY="sua_chave_aqui"')
        sys.exit(1)


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def audio_duration_seconds(path: Path) -> float:
    """Duração do áudio em segundos via ffprobe. Retorna 0.0 se não conseguir ler
    (aí o fluxo cai no gatilho por tamanho como fallback)."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def safe_stem(path: Path) -> str:
    return path.stem.replace(" ", "_")


def get_audio_files_from_queue() -> list[Path]:
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(FILA_DIR.glob(f"*{ext}"))
    return sorted(files)


def flush_input_buffer():
    """Descarta o que ficou digitado no buffer do teclado.
    Tecla batida por engano enquanto o ffmpeg encerra o arquivo fica pendurada no
    buffer e é lida pela PRÓXIMA pergunta: o 's' de "transcrever agora" virava
    "\\s", que não bate com nada e caía no "não" sem avisar."""
    if not sys.stdin.isatty():
        return
    try:
        if SYSTEM == "Windows":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def ask_yes_no(prompt: str) -> bool:
    """Pergunta s/n limpando o buffer antes e PERGUNTANDO DE NOVO se não entender.
    Resposta desconhecida nunca vira "não" calado — isso já custou transcrição."""
    flush_input_buffer()

    while True:
        answer = input(prompt).strip().lower()

        if answer in ["", "s", "sim", "y", "yes"]:
            return True

        if answer in ["n", "nao", "não", "no"]:
            return False

        print(f'Não entendi "{answer}". Responda s (sim) ou n (não).')


# =========================
# DISPOSITIVOS E GRAVAÇÃO
# =========================

def enumerate_audio_devices() -> list[tuple[str, str]]:
    """Retorna [(token, nome)] dos dispositivos de entrada, conforme o backend.
    Re-executa a listagem a cada chamada: a lista muda em tempo real (ex.: no
    macOS o "Microsoft Teams Audio" aparece/some, reordenando os índices).
    - avfoundation: token = índice ('0'); nome = rótulo do device.
    - dshow: token = nome; nome = nome (o Windows já casa por nome).
    - pulse/alsa: [] (não dá pra enumerar de forma confiável, e o token já é o
      nome/'default', que não sofre reordenação)."""
    backend = audio_backend()

    if backend == "avfoundation":
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        devices = []
        show_audio = False
        for line in result.stderr.splitlines():
            if "AVFoundation audio devices:" in line:
                show_audio = True
                continue
            if show_audio:
                if "Error opening input" in line:
                    break
                # formato: "[AVFoundation indev @ 0x...] [0] Nome do device"
                match = re.search(r"\]\s*\[(\d+)\]\s*(.+)", line)
                if match:
                    devices.append((match.group(1), match.group(2).strip()))
        return devices

    if backend == "dshow":
        result = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        devices = []
        for line in result.stderr.splitlines():
            if "(audio)" in line and '"' in line:
                name = line.split('"')[1]
                devices.append((name, name))
        return devices

    # pulse / alsa
    return []


def list_audio_devices():
    """Lista (best-effort) os dispositivos de entrada conforme o backend do SO."""
    backend = audio_backend()
    print("\nDispositivos de áudio disponíveis:\n")

    if backend in ("avfoundation", "dshow"):
        # fonte única de parsing (ver enumerate_audio_devices)
        devices = enumerate_audio_devices()
        if devices:
            for token, name in devices:
                print(f"  [{token}] {name}" if backend == "avfoundation" else f"  {name}")
        else:
            print("  (não foi possível listar)")
        if backend == "dshow":
            print("\n(Use o nome EXATO entre aspas como dispositivo.)")

    elif backend in ("pulse", "alsa"):
        # Tenta listar via ffmpeg -sources; se não rolar, instrui o "default".
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-sources", backend],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        if out:
            print(out)
        else:
            print('  (não foi possível listar — use "default" ou o nome da fonte)')

    print()


def choose_audio_device() -> tuple[str, str]:
    """Pergunta o dispositivo conforme o backend. Retorna (token, nome_legível).
    O token vai pro ffmpeg; o NOME é o que guardamos pra re-achar o device se a
    lista de entrada reordenar entre gravações (ver resolve_audio_device)."""
    backend = audio_backend()

    if backend == "avfoundation":
        devices = enumerate_audio_devices()
        print("\nDispositivos de áudio disponíveis:\n")
        if devices:
            for index, name in devices:
                print(f"  [{index}] {name}")
        else:
            print("  (não foi possível listar — tente o índice 0)")
        print()
        index = input("Digite o número do microfone que deseja usar [0]: ").strip() or "0"
        name = next((n for i, n in devices if i == index), None) or f"índice {index}"
        return index, name

    if backend == "dshow":
        list_audio_devices()
        name = input("Digite o NOME EXATO do dispositivo de áudio: ").strip()
        while not name:
            name = input("O Windows (dshow) exige o nome exato. Digite o dispositivo: ").strip()
        return name, name

    # pulse / alsa
    list_audio_devices()
    token = input("Digite a fonte/dispositivo [default]: ").strip() or "default"
    return token, token


def resolve_audio_device(name: str, previous_token: str) -> str | None:
    """Re-enumera e devolve o token atual do dispositivo chamado `name`.
    - avfoundation/dshow: acha pelo nome (o índice do avfoundation muda em tempo real).
    - Lista vazia (pulse/alsa, ou falha ao listar): devolve o token anterior — não trava,
      pois nesses casos o token já é o nome/'default', que não sofre reordenação.
    - Enumerou mas o device sumiu: None (o chamador re-pergunta)."""
    devices = enumerate_audio_devices()
    if not devices:
        return previous_token
    for token, dev_name in devices:
        if dev_name == name:
            return token
    return None


def record_audio(audio_device: str) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    audio_path = FILA_DIR / f"voicenote_{timestamp}.m4a"

    command = [
        "ffmpeg",
        "-y",
        *ffmpeg_input_args(audio_device),
        "-af", "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level",
        "-acodec", "aac",
        "-b:a", "128k",
        str(audio_path),
    ]

    print("\n============================================================")
    print("GRAVAÇÃO INICIADA")
    print("============================================================")
    print(f"Arquivo: {audio_path}")
    print("Fale normalmente.")
    print("Quando quiser parar, aperte ENTER.")
    print("============================================================\n")

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    recording = True
    start_time = time.time()

    audio_state = {
        "last_db": None,
        "last_audio_time": time.time(),
        "last_warning_time": 0,
    }

    def db_to_bar(db_value):
        """Converte volume em dB pra uma barrinha simples."""
        if db_value is None:
            return "░░░░░░░░░░", "aguardando"
        if db_value < -55:
            level, status = 0, "baixo"
        elif db_value < -45:
            level, status = 2, "baixo"
        elif db_value < -35:
            level, status = 4, "ok"
        elif db_value < -25:
            level, status = 6, "ok"
        elif db_value < -15:
            level, status = 8, "alto"
        else:
            level, status = 10, "alto"
        bar = "█" * level + "░" * (10 - level)
        return bar, status

    def read_audio_levels():
        while recording and process.poll() is None:
            line = process.stderr.readline()
            if not line:
                continue
            marker = "lavfi.astats.Overall.RMS_level="
            if marker in line:
                try:
                    db_value = float(line.split(marker, 1)[1].strip())
                    audio_state["last_db"] = db_value
                    if db_value > SILENCE_THRESHOLD_DB:
                        audio_state["last_audio_time"] = time.time()
                except ValueError:
                    pass

    def show_timer_and_audio_feedback():
        while recording and process.poll() is None:
            elapsed = int(time.time() - start_time)
            minutes, seconds = elapsed // 60, elapsed % 60

            db_value = audio_state["last_db"]
            bar, status = db_to_bar(db_value)
            db_display = "--" if db_value is None else f"{db_value:.0f} dB"

            silence_seconds = int(time.time() - audio_state["last_audio_time"])

            print(
                f"\rGravando... {minutes:02d}:{seconds:02d} | áudio: {bar} {status} ({db_display})",
                end="", flush=True,
            )

            if silence_seconds >= SILENCE_WARNING_SECONDS:
                now = time.time()
                if now - audio_state["last_warning_time"] >= SILENCE_WARNING_SECONDS:
                    print(
                        f"\n⚠️  Pouco ou nenhum áudio relevante nos últimos "
                        f"{SILENCE_WARNING_SECONDS} segundos. "
                        "Verifique se o microfone correto está selecionado."
                    )
                    audio_state["last_warning_time"] = now

            time.sleep(1)

    level_thread = threading.Thread(target=read_audio_levels, daemon=True)
    level_thread.start()
    timer_thread = threading.Thread(target=show_timer_and_audio_feedback, daemon=True)
    timer_thread.start()

    # Tecla batida antes daqui não pode encerrar a gravação recém-começada.
    flush_input_buffer()
    input()

    recording = False
    print("\nEncerrando gravação...")

    if process.poll() is None:
        try:
            process.stdin.write("q\n")
            process.stdin.flush()
            process.wait(timeout=5)
        except Exception:
            process.terminate()
            process.wait()

    print("\nGravação finalizada.")
    print(f"Áudio salvo em: {audio_path}")

    return audio_path


# =========================
# FATIAMENTO
# =========================

def silence_threshold_db(audio_path: Path) -> float:
    """Limiar de silêncio em dB, derivado do volume médio da própria gravação.
    Cai no meio da faixa se o ffmpeg não reportar a média."""
    floor_db, ceil_db = CUT_SILENCE_DB_RANGE
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        for line in result.stderr.splitlines():
            if "mean_volume:" in line:
                mean = float(line.split("mean_volume:")[1].split()[0])
                return max(floor_db, min(ceil_db, mean - CUT_SILENCE_BELOW_MEAN_DB))
    except (OSError, ValueError, IndexError):
        pass
    return (floor_db + ceil_db) / 2


def detect_silences(audio_path: Path, threshold_db: float) -> list[tuple[float, float]]:
    """Trechos de silêncio (início, fim) em segundos, pelo filtro silencedetect do ffmpeg.

    É um passe de análise: não reencoda nada (saída vai pro /dev/null), só lê o áudio.
    Retorna [] em qualquer falha — aí o fatiamento cai no corte por tempo fixo."""
    command = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(audio_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={CUT_SILENCE_MIN_SECONDS}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return []

    # o filtro escreve no stderr: "... silence_start: 12.34" / "... silence_end: 13.02 | ..."
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in result.stderr.splitlines():
        try:
            if "silence_start:" in line:
                start = float(line.split("silence_start:")[1].split()[0])
            elif "silence_end:" in line and start is not None:
                silences.append((start, float(line.split("silence_end:")[1].split()[0])))
                start = None
        except (ValueError, IndexError):
            start = None  # linha estranha: ignora o par e segue
    return silences


def cut_points(duration: float, silences: list[tuple[float, float]]) -> tuple[list[float], int]:
    """Onde cortar: a cada ~CHUNK_SECONDS, deslocando pro MEIO do silêncio mais próximo.

    Cada alvo é medido a partir do corte anterior (e não do relógio do arquivo), pra um
    deslocamento não encurtar a parte seguinte. Sem silêncio na janela, corta no alvo —
    o comportamento antigo, que continua valendo pra áudio sem nenhuma pausa.

    Devolve (pontos, quantos caíram em pausa). O segundo número existe porque sem ele quem
    chama não distingue "3 cortes no silêncio" de "3 cortes no relógio": os dois voltam como
    uma lista de 3 pontos. Era o que fazia o CLI anunciar corte no silêncio sem ter achado um."""
    points: list[float] = []
    silence_hits = 0
    target = CHUNK_SECONDS
    while target < duration - CUT_MIN_CHUNK_SECONDS:
        previous = points[-1] if points else 0.0
        best: float | None = None
        for silence_start, silence_end in silences:
            middle = (silence_start + silence_end) / 2
            if abs(middle - target) > CUT_SEARCH_WINDOW:
                continue
            if middle - previous < CUT_MIN_CHUNK_SECONDS:
                continue
            if best is None or abs(middle - target) < abs(best - target):
                best = middle
        point = best if best is not None else target
        if best is not None:
            silence_hits += 1
        points.append(point)
        target = point + CHUNK_SECONDS
    return points, silence_hits


def plan_cuts(audio_path: Path, duration: float) -> tuple[list[float], int, float]:
    """Pontos de corte, afrouxando o limiar de silêncio até as pausas aparecerem.

    O limiar derivado do volume médio erra feio em gravação de microfone real: em 12 gravações
    longas do uso diário (ago/2026) ele achou pausa em UMA — nas outras 11 o fatiamento caía
    calado no corte por tempo, justamente o que o corte no silêncio existe pra evitar.
    Aqui o piso de ruído é procurado, não chutado: parte do limiar derivado (o mais estrito) e
    afrouxa de CUT_SILENCE_DB_STEP em CUT_SILENCE_DB_STEP até TODO corte cair numa pausa.
    Para no primeiro limiar que cobre tudo — o mais estrito que serve, o que menos arrisca
    confundir fala baixa com silêncio — e nunca passa de CUT_SILENCE_DB_CEILING.
    Devolve (pontos, quantos em pausa, limiar usado)."""
    threshold = silence_threshold_db(audio_path)
    best: tuple[list[float], int, float] = ([], -1, threshold)

    while True:
        points, hits = cut_points(duration, detect_silences(audio_path, threshold))

        if hits > best[1]:
            best = (points, hits, threshold)

        if hits == len(points) or threshold >= CUT_SILENCE_DB_CEILING:
            break

        threshold = min(threshold + CUT_SILENCE_DB_STEP, CUT_SILENCE_DB_CEILING)

    points, hits, threshold = best
    return points, max(hits, 0), threshold


def split_audio(audio_path: Path) -> list[Path]:
    print("\nÁudio longo detectado.")
    print("Fatiando em partes menores para não perder o final...")

    chunk_folder = TEMP_DIR / safe_stem(audio_path)
    chunk_folder.mkdir(parents=True, exist_ok=True)

    output_pattern = chunk_folder / f"{safe_stem(audio_path)}_parte_%03d.m4a"

    # Escolhe os pontos de corte no silêncio pra não partir palavra ao meio (ver constantes).
    duration = audio_duration_seconds(audio_path)
    points, silence_hits, threshold = plan_cuts(audio_path, duration) if duration else ([], 0, 0.0)

    if silence_hits:
        # A mensagem diz quantos cortes caíram em pausa DE VERDADE — o resto caiu no relógio.
        if silence_hits == len(points):
            print(f"Cortando em {silence_hits} pausa(s) da fala (silêncio abaixo de "
                  f"{threshold:.0f} dB), para não partir palavras.")
        else:
            print(f"Cortando em {len(points)} ponto(s): {silence_hits} em pausa da fala, "
                  f"{len(points) - silence_hits} no tempo (sem pausa por perto).")
        segment_args = ["-segment_times", ",".join(f"{p:.3f}" for p in points)]
    else:
        # sem duração legível ou sem nenhuma pausa detectada: corte por tempo fixo (como antes)
        print("Nenhuma pausa utilizável encontrada — cortando por tempo.")
        segment_args = ["-segment_time", str(CHUNK_SECONDS)]

    command = [
        "ffmpeg",
        "-y",
        "-i", str(audio_path),
        "-f", "segment",
        *segment_args,
        # -reset_timestamps 1: cada parte recomeça em 0 e reporta a SUA duração real.
        # Sem isso, os pedaços herdam a duração do arquivo inteiro e o modelo trunca igual.
        "-reset_timestamps", "1",
        "-c", "copy",
        str(output_pattern),
    ]

    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    chunks = sorted(chunk_folder.glob("*.m4a"))
    if not chunks:
        raise RuntimeError("Nenhuma parte foi gerada pelo ffmpeg.")

    print(f"{len(chunks)} parte(s) gerada(s).")
    return chunks


# =========================
# TRANSCRIÇÃO
# =========================

def transcribe_file(client: OpenAI, audio_path: Path) -> str:
    print(f"Transcrevendo: {audio_path.name}")
    with open(audio_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=MODEL,
            file=audio_file,
            prompt=TRANSCRIPTION_PROMPT.strip(),
        )
    return transcription.text.strip()


def transcribe_audio(client: OpenAI, audio_path: Path) -> str:
    size = file_size_mb(audio_path)
    duration = audio_duration_seconds(audio_path)

    print("\n============================================================")
    print("TRANSCRIÇÃO")
    print("============================================================")
    print(f"Arquivo: {audio_path.name}")
    print(f"Tamanho: {size:.2f} MB")
    if duration:
        print(f"Duração: {int(duration // 60):02d}:{int(duration % 60):02d}")
    print(f"Modelo: {MODEL}")
    print("============================================================\n")

    # Fatia se passar do limite de DURAÇÃO do modelo (gatilho principal — o modelo trunca
    # áudio longo) OU se o arquivo for grande demais pro upload. Se a duração não pôde ser
    # lida (duration == 0), só o tamanho decide.
    needs_split = duration > CHUNK_SECONDS or size > MAX_FILE_MB

    if not needs_split:
        return transcribe_file(client, audio_path)

    chunks = split_audio(audio_path)
    transcript_parts = []
    for index, chunk in enumerate(chunks, start=1):
        print(f"\nParte {index}/{len(chunks)}")
        transcript_parts.append(transcribe_file(client, chunk))

    return "\n\n".join(transcript_parts)


# =========================
# SAÍDA
# =========================

def get_unique_output_path(audio_path: Path) -> Path:
    base_output = TRANSCRIPTS_DIR / f"{audio_path.stem}_transcricao.md"
    if not base_output.exists():
        return base_output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return TRANSCRIPTS_DIR / f"{audio_path.stem}_transcricao_{timestamp}.md"


def save_markdown(audio_path: Path, transcript: str) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_path = get_unique_output_path(audio_path)

    # dois espaços no fim = quebra de linha "hard" do Markdown (metadados empilhados).
    markdown = (
        f"# Transcrição — {audio_path.stem}\n\n"
        f"**Data de processamento:** {timestamp}  \n"
        f"**Arquivo original:** {audio_path.name}  \n"
        f"**Modelo:** {MODEL}\n\n"
        f"---\n\n"
        f"{transcript}\n"
    )

    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def move_audio_to_processed(audio_path: Path):
    destination = PROCESSADOS_DIR / audio_path.name
    if destination.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = PROCESSADOS_DIR / f"{audio_path.stem}_{timestamp}{audio_path.suffix}"
    shutil.move(str(audio_path), str(destination))


def clean_temp(audio_path: Path):
    chunk_folder = TEMP_DIR / safe_stem(audio_path)
    if chunk_folder.exists():
        shutil.rmtree(chunk_folder)


def copy_to_clipboard(text: str):
    """Copia pro clipboard conforme o SO (best-effort). macOS: pbcopy; Windows: clip;
    Linux: wl-copy (Wayland) ou xclip/xsel (X11). Se nada existir, só avisa."""
    if SYSTEM == "Darwin":
        candidates = [["pbcopy"]]
    elif SYSTEM == "Windows":
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]

    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text, text=True, check=True)
            print("\nTranscrição copiada para a área de transferência.")
            return
        except Exception:
            continue

    print("\n(Clipboard automático indisponível neste sistema — o texto está acima e salvo no .md.)")
    if SYSTEM not in ("Darwin", "Windows"):
        print("Dica: instale wl-clipboard (Wayland) ou xclip/xsel (X11) pra copiar automático.")


def print_transcript(transcript: str):
    print("\n\n============================================================")
    print("RESULTADO DA TRANSCRIÇÃO")
    print("============================================================\n")
    print(transcript)
    print("\n============================================================")
    copy_to_clipboard(transcript)


def process_single_audio(client: OpenAI, audio_path: Path, show_transcript: bool = True) -> bool:
    try:
        transcript = transcribe_audio(client, audio_path)
        output_path = save_markdown(audio_path, transcript)

        if show_transcript:
            print_transcript(transcript)

        move_audio_to_processed(audio_path)
        clean_temp(audio_path)

        print(f"\nTranscrição salva em: {output_path}")
        print(f"Áudio movido para: {PROCESSADOS_DIR}")
        print("\nProcesso concluído.")

        # 2ª camada: só depois de tudo acima estar garantido. Falha aqui não custa
        # a transcrição, que já está salva, copiada e com o áudio arquivado.
        if show_transcript:
            offer_formatting(client, transcript, output_path)
        return True

    except Exception as error:
        print(f"\nErro durante a transcrição: {type(error).__name__}: {error}")
        if hasattr(error, "body") and error.body:
            print(f"Detalhes da API: {error.body}")
        print("\nO áudio não foi movido para processados.")
        print(f"Arquivo preservado em: {audio_path}")
        return False


# =========================
# 2ª CAMADA — FORMATAÇÃO (D26, portado da web)
# =========================
# A tese está em docs/FORMATACAO_INTENCAO_E_LIMITES.md: a 2ª camada é TRANSPORTE,
# NÃO AUTORIA. Ela muda a apresentação do que a pessoa disse pra caber num destino;
# não muda o conteúdo. Por isso o controle é por DESTINO ("pra onde isso vai?") e não
# por "formato": destino é um container com regras conhecidas, e molde aberto convida
# o modelo a preencher — foi exatamente assim que, na web, o preset de e-mail passou a
# inventar saudação e despedida.
#
# Três diferenças em relação à web, todas por causa do meio:
#  1. o CLI roda com a chave do PRÓPRIO usuário → não há franquia a debitar;
#  2. o clipboard É a entrega (o CLI existe pra colar em outro lugar), então o texto
#     formatado toma o lugar do literal no clipboard — e isso é dito em voz alta;
#  3. o literal continua no .md, intacto, e o formatado é ANEXADO embaixo — um arquivo
#     por gravação, o literal sempre em cima.

FORMAT_MODEL = "gpt-5.4-nano"

# ⚠️ MOLDURA v3 — e a v2 (a que passou na web) REPROVOU aqui. Vale o registro porque a
# causa é estrutural, não de redação:
#   v1: o bloco de e-mail mandava "saudação, corpo, despedida" — instruía a inventar.
#   v2: proibiu boilerplate e criou um "PASSO 1: se não couber, devolva limpo e PARE".
#       Passou na web. No CLI, com fala vaga de 15 s, o modelo devolveu o texto limpo
#       *com um "Assunto:" inventado em cima* — obedeceu os dois de uma vez.
#   Diagnóstico: um prompt só não pode mandar APLICAR um formato e AO MESMO TEMPO decidir
#   não aplicá-lo. O modelo racha a diferença, e o resultado é um híbrido que nenhum dos
#   dois ramos pedia.
#   v3: separa em DUAS chamadas — uma decide, a outra escreve. Cada uma com um trabalho só.
#   É a mesma lição que a bancada de locutores deu no mesmo dia: decidir e reescrever são
#   tarefas diferentes, e misturá-las degrada as duas. Custo: ~$0,0005 a mais por uso.

DECISION_PROMPT = """Below is a speech transcript and a target format.

Does the transcript have enough substance to become {rotulo} without you writing
material the person did not say?

Answer with one word: SIM or NAO.
- SIM if the content carries the format on its own.
- NAO if it is too short, too vague, or has nothing the format needs — in that case the
  honest output is the transcript merely cleaned up, not an empty shell of the format.

TRANSCRIPT:
{transcricao}
"""

CLEANUP_PROMPT = """Clean up this speech transcript and output nothing else.

- Remove filler words and false starts. Fix punctuation and capitalisation.
- Change NOTHING else: no reordering, no summarising, no added words.
- Do not add a title, a subject line, a greeting or a sign-off.
- Write in the SAME language as the transcript.
- Output only the cleaned text: no preamble, no explanation, no code fences.

TRANSCRIPT:
{transcricao}
"""

FORMAT_FRAME = """You rewrite a speech transcript. You never invent.

RULES — these override everything below:
- Use only what is in the transcript. Never add facts, names, numbers, dates or conclusions.
- Keep proper nouns, technical terms and foreign words exactly as spoken.
- Write in the SAME language as the transcript.
- Never supply boilerplate the person did not speak: greetings, sign-offs, salutations
  or closing formulas.
- Output only the resulting text: no preamble, no explanation, no code fences, no "here is".

TASK:
{bloco}

TRANSCRIPT:
{transcricao}
"""

# (id do menu, rótulo, operação, instrução ao modelo)
# "extrair" é recorte DECLARADO — e por isso a tela avisa que é recorte. Extração que
# não se declara vira indistinguível de resumo, e resumir está fora por princípio:
# resumir decide POR VOCÊ o que importou, e devolve esse juízo com a sua voz.
DESTINATIONS = [
    ("1", "E-mail", "enquadrar",
     "Rewrite as an email. A subject line (\"Assunto:\", translated into the output language) "
     "drawn from what was actually said, then the message itself. Include a greeting or a "
     "sign-off ONLY if the person spoke one — otherwise leave them out entirely. Professional "
     "register, spoken fillers gone, but keep the person's own wording where it works. "
     "Never invent a recipient or sender name."),
    ("2", "Mensagem (WhatsApp)", "enquadrar",
     "Rewrite as a short message for a chat app. Direct, warm, line breaks between ideas, no "
     "formal salutation. As short as the content allows without dropping anything that was said."),
    ("3", "Devolutiva (feedback)", "enquadrar",
     "Reorganise as feedback addressed to its recipient. Group into what works, what does not, "
     "and what to do next — dropping any group the transcript has nothing for. Keep the "
     "speaker's judgements exactly as strong or as soft as they were stated."),
    ("4", "Lista de tarefas", "extrair",
     "Extract what has to be done. Two lists, each omitted when empty: decisions taken, and open "
     "items. One line per item, starting with a verb; owner and deadline only if spoken. This is "
     "the ONLY destination allowed to drop conversational content that is neither a decision nor "
     "an action. If the transcript holds no decision and no action, fall back to STEP 1 and "
     "return it cleaned — never return an empty result."),
    ("5", "Prompt pra uma IA", "enquadrar",
     "Rewrite as an instruction for an AI: context, then task, then expected output format. Make "
     "a vague spoken reference explicit ONLY when the transcript itself makes it explicit "
     "somewhere; otherwise leave it as stated."),
]


def choose_destination() -> tuple | None:
    """Menu de destino. ENTER = não formatar — o fluxo de quem só quer o literal
    não pode ficar mais lento por causa desta feature."""
    print("\n------------------------------------------------------------")
    print("LEVAR ESSE TEXTO PRA ALGUM LUGAR?")
    print("------------------------------------------------------------")
    for key, label, operacao, _ in DESTINATIONS:
        marca = "  (recorte)" if operacao == "extrair" else ""
        print(f"  {key}. {label}{marca}")
    print("  ENTER. não, ficar só com a transcrição")

    while True:
        flush_input_buffer()  # D24: toda pergunta limpa o buffer antes
        answer = input("\nDestino: ").strip()

        if answer == "":
            return None

        for item in DESTINATIONS:
            if answer == item[0]:
                return item

        print(f'Não entendi "{answer}". Escolha um número de 1 a {len(DESTINATIONS)} ou ENTER.')


def _ask(client: OpenAI, prompt: str) -> str:
    response = client.chat.completions.create(
        model=FORMAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,  # aqui fidelidade vale mais que variedade
    )
    return (response.choices[0].message.content or "").strip()


def format_transcript(client: OpenAI, transcript: str, destino: tuple) -> tuple[str, bool]:
    """Devolve (texto, aplicou_formato). Duas chamadas de propósito — ver a nota da v3:
    um prompt só não consegue aplicar um formato e decidir não aplicá-lo ao mesmo tempo."""
    _, label, _, bloco = destino
    print(f"\nFormatando para: {label}...")

    veredito = _ask(client, DECISION_PROMPT.format(rotulo=label, transcricao=transcript))
    cabe = veredito.strip().upper().startswith("SIM")

    if not cabe:
        # devolve limpo, e DIZ que devolveu limpo — a v2 falhava calada, entregando
        # um híbrido que parecia o formato pedido.
        return _ask(client, CLEANUP_PROMPT.format(transcricao=transcript)), False

    return _ask(client, FORMAT_FRAME.format(bloco=bloco, transcricao=transcript)), True


def append_formatted(output_path: Path, label: str, texto: str):
    """Anexa embaixo do literal, no MESMO arquivo. O literal fica em cima, sempre."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bloco = f"\n\n---\n\n## {label}\n\n*Formatado em {timestamp} · modelo {FORMAT_MODEL}*\n\n{texto}\n"
    with open(output_path, "a", encoding="utf-8") as arquivo:
        arquivo.write(bloco)


def offer_formatting(client: OpenAI, transcript: str, output_path: Path):
    """Roda DEPOIS de a transcrição estar salva, copiada e o áudio movido — assim
    nenhuma falha aqui pode custar a transcrição, que é o produto principal."""
    while True:
        destino = choose_destination()
        if destino is None:
            return

        _, label, operacao, _ = destino

        try:
            formatado, aplicou = format_transcript(client, transcript, destino)
        except Exception as error:
            print(f"\nNão consegui formatar: {type(error).__name__}: {error}")
            print("A transcrição continua salva e intacta.")
            if ask_yes_no("Tentar outro destino? [s/n]: "):
                continue
            return

        if not formatado:
            print("\nO modelo devolveu vazio. A transcrição continua salva e intacta.")
            if ask_yes_no("Tentar outro destino? [s/n]: "):
                continue
            return

        titulo = label.upper() if aplicou else f"{label.upper()} — SÓ LIMPO"
        print("\n============================================================")
        print(titulo)
        print("============================================================\n")
        print(formatado)
        print("\n============================================================")
        if not aplicou:
            # "não coube" mentiria quando a fala JÁ servia como está (uma mensagem curta de
            # WhatsApp, por exemplo). A frase descreve o desfecho, não julga a fala.
            print("O formato não acrescentaria nada aqui — ou a fala já servia assim, ou não")
            print("tinha conteúdo pro formato. Nos dois casos eu limpei em vez de encenar.")
        if aplicou and operacao == "extrair":
            print("Isto é um RECORTE — só o que virou item. O texto completo está acima e no .md.")

        append_formatted(output_path, label if aplicou else f"{label} (só limpo)", formatado)
        print(f"Anexado ao arquivo: {output_path.name}")

        copy_to_clipboard(formatado)
        print("(o clipboard agora tem o texto formatado, não mais a transcrição literal)")

        if not ask_yes_no("\nQuer levar pra outro destino também? [s/n]: "):
            return


# =========================
# OPÇÃO 1 — GRAVAR
# =========================

def record_and_transcribe_flow(client: OpenAI):
    audio_device, device_name = choose_audio_device()

    while True:
        # A lista de dispositivos pode mudar em tempo real (no macOS o índice
        # reordena quando um device aparece/some). Antes de CADA gravação, re-acha
        # o device pelo nome pra não gravar da fonte errada (gravação sairia muda).
        resolved = resolve_audio_device(device_name, audio_device)
        if resolved is None:
            print(f'\n⚠️  O dispositivo "{device_name}" não está mais na lista.')
            print("A lista mudou ou ele foi desconectado. Escolha de novo:")
            audio_device, device_name = choose_audio_device()
        elif resolved != audio_device:
            print(f'\nℹ️  A lista mudou; "{device_name}" agora é [{resolved}]. Ajustado automaticamente.')
            audio_device = resolved

        audio_path = record_audio(audio_device)

        if ask_yes_no("\nDeseja transcrever agora? [s/n]: "):
            process_single_audio(client, audio_path, show_transcript=True)
        else:
            print("\nBeleza. O áudio ficou salvo em:")
            print(audio_path)
            print("Você pode transcrever depois pela opção 2 do menu.")

        flush_input_buffer()
        next_action = input(
            "\nO que deseja fazer agora?\n"
            "1. Gravar outro áudio com o mesmo microfone\n"
            "2. Voltar ao menu principal\n"
            "3. Sair\n"
            "Escolha uma opção [1]: "
        ).strip()

        if next_action == "" or next_action == "1":
            continue
        if next_action == "2":
            return
        if next_action == "3":
            print("\nAté a próxima.")
            sys.exit(0)

        print("\nOpção inválida. Voltando ao menu principal.")
        return


# =========================
# OPÇÃO 2 — TRANSCREVER FILA
# =========================

def transcribe_queue_flow(client: OpenAI):
    audio_files = get_audio_files_from_queue()

    if not audio_files:
        print("\nNenhum arquivo de áudio encontrado na fila.")
        print(f"Pasta da fila: {FILA_DIR}")
        return

    print("\nArquivos encontrados na fila:\n")
    for index, audio_path in enumerate(audio_files, start=1):
        print(f"{index}. {audio_path.name} ({file_size_mb(audio_path):.2f} MB)")

    print("\nO script vai transcrever todos os arquivos listados.")
    if not ask_yes_no("Deseja continuar? [s/n]: "):
        print("\nOperação cancelada. Voltando ao menu principal.")
        return

    print("\nIniciando transcrição da fila...")

    total = len(audio_files)
    success_count = 0

    for index, audio_path in enumerate(audio_files, start=1):
        print("\n============================================================")
        print(f"ARQUIVO {index}/{total}")
        print("============================================================")
        print(f"{audio_path.name}")

        if process_single_audio(client, audio_path, show_transcript=True):
            success_count += 1

    print("\n============================================================")
    print("RESUMO DA FILA")
    print("============================================================")
    print(f"Arquivos encontrados: {total}")
    print(f"Transcritos com sucesso: {success_count}")
    print(f"Com erro: {total - success_count}")
    print("============================================================")


# =========================
# MENU
# =========================

def show_main_menu():
    print("\n============================================================")
    print("VOICENOTE CLI")
    print("============================================================")
    print("1. Gravar novo áudio e transcrever")
    print("2. Transcrever todos os áudios da fila")
    print("3. Listar dispositivos de áudio")
    print("4. Sair")
    print("============================================================")


def main():
    ensure_directories()
    check_ffmpeg()
    check_api_key()

    client = OpenAI()

    while True:
        show_main_menu()
        choice = input("Escolha uma opção [1]: ").strip()

        if choice == "" or choice == "1":
            record_and_transcribe_flow(client)
        elif choice == "2":
            transcribe_queue_flow(client)
        elif choice == "3":
            list_audio_devices()
        elif choice == "4":
            print("\nAté a próxima.")
            break
        else:
            print("\nOpção inválida. Tente novamente.")


if __name__ == "__main__":
    main()
