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

from pathlib import Path
from datetime import datetime
from openai import OpenAI
import subprocess
import platform
import shutil
import os
import sys
import threading
import time


# =========================
# CONFIGURAÇÕES
# =========================

BASE_DIR = Path.home() / "voicenote-cli"

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


# =========================
# DISPOSITIVOS E GRAVAÇÃO
# =========================

def list_audio_devices():
    """Lista (best-effort) os dispositivos de entrada conforme o backend do SO."""
    backend = audio_backend()
    print("\nDispositivos de áudio disponíveis:\n")

    if backend == "avfoundation":
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        show_audio = False
        for line in result.stderr.splitlines():
            if "AVFoundation audio devices:" in line:
                show_audio = True
                continue
            if show_audio:
                if "Error opening input" in line:
                    break
                if "] [" in line:
                    print(line.split("] ", 1)[-1])

    elif backend == "dshow":
        result = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for line in result.stderr.splitlines():
            if "(audio)" in line and '"' in line:
                name = line.split('"')[1]
                print(f'  {name}')
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


def choose_audio_device() -> str:
    """Pergunta o dispositivo conforme o backend. Retorna o TOKEN cru (índice/nome/source)."""
    backend = audio_backend()
    list_audio_devices()

    if backend == "avfoundation":
        return input("Digite o número do microfone que deseja usar [0]: ").strip() or "0"
    if backend == "dshow":
        name = input("Digite o NOME EXATO do dispositivo de áudio: ").strip()
        while not name:
            name = input("O Windows (dshow) exige o nome exato. Digite o dispositivo: ").strip()
        return name
    # pulse / alsa
    return input("Digite a fonte/dispositivo [default]: ").strip() or "default"


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

def split_audio(audio_path: Path) -> list[Path]:
    print("\nÁudio longo detectado.")
    print("Fatiando em partes menores para não perder o final...")

    chunk_folder = TEMP_DIR / safe_stem(audio_path)
    chunk_folder.mkdir(parents=True, exist_ok=True)

    output_pattern = chunk_folder / f"{safe_stem(audio_path)}_parte_%03d.m4a"

    command = [
        "ffmpeg",
        "-y",
        "-i", str(audio_path),
        "-f", "segment",
        "-segment_time", str(CHUNK_SECONDS),
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
        return True

    except Exception as error:
        print(f"\nErro durante a transcrição: {type(error).__name__}: {error}")
        if hasattr(error, "body") and error.body:
            print(f"Detalhes da API: {error.body}")
        print("\nO áudio não foi movido para processados.")
        print(f"Arquivo preservado em: {audio_path}")
        return False


# =========================
# OPÇÃO 1 — GRAVAR
# =========================

def record_and_transcribe_flow(client: OpenAI):
    audio_device = choose_audio_device()

    while True:
        audio_path = record_audio(audio_device)

        answer = input("\nDeseja transcrever agora? [s/n]: ").strip().lower()

        if answer in ["s", "sim", ""]:
            process_single_audio(client, audio_path, show_transcript=True)
        else:
            print("\nBeleza. O áudio ficou salvo em:")
            print(audio_path)
            print("Você pode transcrever depois pela opção 2 do menu.")

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
    answer = input("Deseja continuar? [s/n]: ").strip().lower()

    if answer not in ["s", "sim", ""]:
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
