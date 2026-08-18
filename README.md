# VoiceNote CLI

> Grave a ideia. Receba o texto. Volte a criar. — a versão **terminal**, grátis e aberta.

## O que é

O **VoiceNote CLI** é o flagship em linha de comando do VoiceNote: você **grava uma nota de voz no terminal**, ela é **transcrita pela OpenAI** e o **texto volta no próprio terminal** (e é salvo em `.md`). Pensado pra quem pensa melhor falando — capturar ideia, briefing, bug ou rascunho sem parar pra digitar.

É **grátis e aberto**: roda na **sua máquina** com a **sua própria chave de API** da OpenAI. Você paga só o uso da transcrição direto pra OpenAI (hoje ~**US$ 0,003/min** no `gpt-4o-mini-transcribe`) — sem mensalidade.

> Prefere não instalar nada? A **versão web** (assinatura) resolve toda a infra pra você: **[voicenote.com.br](https://voicenote.com.br)**. CLI e web seguem o mesmo roadmap.

---

## Compatibilidade

Funciona em **macOS, Linux e Windows**. A gravação detecta o sistema e usa o backend de áudio do `ffmpeg` adequado:

| SO | Backend de gravação | Clipboard automático |
|---|---|---|
| macOS | `avfoundation` | `pbcopy` |
| Linux | `pulse` (PulseAudio/PipeWire) · `alsa` opcional | `wl-copy` / `xclip` / `xsel` |
| Windows | `dshow` | `clip` |

- **Testado em macOS.** Os caminhos de Linux/Windows foram portados e devem funcionar, mas ainda precisam de validação em campo — abra uma issue se algo travar.
- Forçar um backend: `export VOICENOTE_AUDIO_BACKEND=alsa` (ou `pulse`/`dshow`/`avfoundation`).
- Sem clipboard automático no sistema? O texto aparece no terminal e fica salvo no `.md` do mesmo jeito.
- O modo **"transcrever fila de arquivos"** (opção 2) independe disso e roda em qualquer SO.

---

## Requisitos

- **Python 3**
- **ffmpeg** (inclui `ffprobe`)
- **Chave de API da OpenAI**

---

## Instalação

**1. Pegue o código**
```bash
git clone https://github.com/GianValente/voicenote-cli.git ~/voicenote-cli
cd ~/voicenote-cli
```
> O programa usa `~/voicenote-cli` como base (cria `fila/`, `transcripts/`, `processados/`, `temp/`).

**2. Dependência Python** — use um **ambiente virtual** (`venv`)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # ou: .venv/bin/pip install openai
```
> **Por que venv e não `pip3 install` global:** o pacote `openai` traz extensões compiladas
> (`pydantic_core`, `jiter`) que são específicas da arquitetura da máquina. Instalado no Python
> global, ele se mistura com o que já estiver lá — e se aquele Python um dia passar a rodar em
> outra arquitetura, o import quebra com `incompatible architecture`, sem você ter mexido em nada.
>
> **Mac com Apple Silicon (M1/M2/M3…):** é onde isso mais morde. Se o seu Python foi usado
> **sob Rosetta** em algum momento, os pacotes ficaram em `x86_64`; ao rodar nativo (`arm64`) o
> programa morre em `ImportError: dlopen(... _pydantic_core ...) incompatible architecture`.
> O `venv` isola o CLI desse histórico. Pra conferir qual arquitetura você tem:
> ```bash
> .venv/bin/python -c "import platform; print(platform.machine())"   # arm64 ou x86_64
> ```

**3. ffmpeg**
- macOS: `brew install ffmpeg`
- Linux (Debian/Ubuntu): `sudo apt install ffmpeg`
- Windows: `winget install Gyan.FFmpeg` (ou choco/baixar do site)

Teste: `ffmpeg -version`

**4. Chave da OpenAI** (ver seção abaixo)

**5. Alias pra chamar fácil** (ver seção abaixo)

---

## Chave da API da OpenAI

1. Crie em **platform.openai.com → API keys**.
2. Salve num arquivo protegido na sua home (não fica dentro do projeto, não é versionado):
   ```bash
   printf "Cole sua OPENAI_API_KEY e aperte ENTER: "; stty -echo; read OPENAI_API_KEY; stty echo; echo; printf "%s" "$OPENAI_API_KEY" > ~/.openai_api_key; chmod 600 ~/.openai_api_key; unset OPENAI_API_KEY
   ```
3. Confira a permissão (`-rw-------`):
   ```bash
   ls -l ~/.openai_api_key
   ```

> ⚠️ O arquivo **não é criptografado** — só protegido por permissão. **Nunca** versione nem cole a chave em chat/print/documento.

O programa lê a chave da variável de ambiente `OPENAI_API_KEY` (o alias abaixo carrega do arquivo).

---

## Comando curto (alias)

Adicione no `~/.zshrc` (ou `~/.bashrc`):
```bash
# VoiceNote CLI
function voicenote() {
  export OPENAI_API_KEY="$(cat ~/.openai_api_key)"
  cd ~/voicenote-cli || return
  .venv/bin/python voicenote.py
}
```
Recarregue: `source ~/.zshrc`

Rodar:
```bash
voicenote
```

Sem alias:
```bash
cd ~/voicenote-cli && export OPENAI_API_KEY="$(cat ~/.openai_api_key)" && .venv/bin/python voicenote.py
```
> Use sempre o `.venv/bin/python` (não o `python3` do sistema) — é ele que enxerga o `openai`
> instalado no passo 2. Se o seu alias usa `python3`, troque por `.venv/bin/python`.

---

## Usando

O menu abre assim:
```txt
VOICENOTE CLI

1. Gravar novo áudio e transcrever
2. Transcrever todos os áudios da fila
3. Listar dispositivos de áudio
4. Sair
```
- **Gravar:** escolhe o microfone, grava, **ENTER** encerra; transcreve e devolve o texto (copiado pro clipboard no macOS + salvo em `transcripts/*.md`).
- **Fila:** joga arquivos de áudio em `fila/` e transcreve todos de uma vez.
- **Áudio longo:** é fatiado em pedaços de ~6 min, transcrito em partes e juntado na ordem — não perde o final. **O corte cai numa pausa da fala, não no relógio:** o programa procura o silêncio mais próximo do alvo (dentro de ±45 s) e corta ali. Cortar em 6:00 cravados parte uma palavra ao meio, e o modelo "completa" o fragmento — sai palavra inventada, trecho perdido ou frase repetida, uma vez por emenda. O limiar de silêncio é **medido no seu áudio**, não fixo (mic e ambiente mudam o que é "silêncio"). Se não houver pausa alguma na janela, aquele corte cai no tempo — e o programa **avisa** quantos cortes foram em pausa e quantos no relógio.

Formatos aceitos: `.m4a .mp3 .wav .mp4 .mpeg .mpga .webm`.

---

## Custo

Você paga a OpenAI direto pelo uso (sem intermediário). Hoje, `gpt-4o-mini-transcribe` ≈ **US$ 0,003/min** (~US$ 0,18/h). Sem mensalidade, sem limite de tempo de áudio.

---

## Customizar (opcional)

É um único `voicenote.py` — dá pra ajustar no topo: `MODEL`, o `TRANSCRIPTION_PROMPT`, `CHUNK_SECONDS`, etc. Quem topa, pode plugar outros modelos/IA. Dica: abrir o projeto no **Claude Code** (ou outro agente de CLI) facilita estender — peça melhorias em linguagem natural.

---

## Segurança

- A chave fica **fora** do código e **fora** do git (`~/.openai_api_key`, `chmod 600`).
- Não versione nem compartilhe esse arquivo. Não cole a chave em lugar nenhum.
- Nada é enviado a servidores além da própria OpenAI (pra transcrever).

---

## Licença

[GNU GPL v3.0](LICENSE).
