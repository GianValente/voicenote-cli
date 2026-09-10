# Mudancas

Versionado em [SemVer](https://semver.org/lang/pt-BR/).

⭐ **A regra de leitura, e e a unica que voce precisa:**
**MAIOR = voce tem trabalho a fazer.** MENOR = recurso novo, nada quebra.
CORRECAO = conserto.

⚠️ **Atualizar e sempre DUAS coisas.** `git pull` sozinho nao basta: quando uma
versao traz dependencia nova, o programa quebra com `ImportError` e parece
defeito. O comando inteiro esta no README, secao *Atualizar*.

---

## 0.2.0 — 2026-09-10

Primeira versao com numero. As anteriores existiram, mas nao eram
identificaveis: nao havia `__version__`, nem tag, nem este arquivo — e sem isso
ninguem consegue dizer o que esta rodando.

### Novo
- **Segunda camada de formatacao da transcricao.** A transcricao crua passa por
  um passo de formatacao com intencao declarada (nota, email, lista). O prompt
  foi partido em **duas chamadas** de proposito: um prompt nao pode aplicar um
  formato e decidir nao aplica-lo ao mesmo tempo — misturar as duas tarefas
  degrada as duas.
- **`--version`** (e `-V`), respondido antes do menu.

### Mudou — e vale ler
- 🔴 **Os seus dados sairam de dentro do clone do git.** Antes, `fila/`,
  `transcripts/`, `processados/` e `temp/` eram criados dentro da pasta clonada.
  Agora vao para `~/.voicenote/` (ou o que estiver em `$VOICENOTE_HOME`).

  **Por que:** `git clean -fdx` apaga arquivo ignorado junto — e e o que se
  digita quando alguem diz "limpa tudo e clona de novo". Ser ignorado pelo git
  **nao e protecao**: e o que faz a limpeza levar sem perguntar e o `status` nao
  avisar. Suas transcricoes estavam nessa posicao.

  **Voce precisa fazer algo?** Provavelmente nao. Se ja havia dados na pasta
  antiga, o programa **continua usando ela** e avisa como mover — nada some, e a
  mudanca so vale para instalacao nova. O aviso so aparece se a sua pasta de
  dados for mesmo um clone de git.

### Ainda nao
- **Diarizacao** ("quem falou quando") esta em construcao e sera **opcional**,
  com chave de API propria. Nao esta nesta versao.
