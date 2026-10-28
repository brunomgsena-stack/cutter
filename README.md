# Cutter

App para macOS que baixa vídeos do YouTube, Instagram, TikTok, X/Twitter, Facebook, Vimeo e de [qualquer site que o yt-dlp suporte](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), recorta trechos e grava lives do YouTube — inclusive voltando alguns minutos no tempo e seguindo ao vivo, sem emenda.

> **English summary:** Cutter is a small macOS app (Swift + WKWebView shell around a pure-Python local server) built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org). It downloads full videos from YouTube, Instagram, TikTok, X and any other site yt-dlp supports, cuts clips from videos and live streams, and records live streams starting up to 1 hour in the past (using YouTube's DVR window) and continuing live until you stop.

<p align="center"><img src="icon.svg" width="128" alt="Ícone do Cutter"></p>

## O que faz

- **Vídeo inteiro** — baixa o vídeo completo (ou a live já encerrada). Posts com vários vídeos (carrossel do Instagram) salvam todos, numerados.
- **Trecho** — informe início e fim (`HH:MM:SS`) e baixa só aquele pedaço; funciona em vídeos, lives encerradas e lives em andamento.
- **Gravar ao vivo** (só YouTube) — grava a live a partir de agora **ou voltando 5, 10, 30 ou 60 minutos**, e continua até você parar ou a live acabar. Útil para quando você só percebe que precisava gravar depois que a parte importante já começou (sessões legislativas, palestras, eventos).
- **Transcrição (.txt)** — marque "Gerar transcrição" e o Cutter salva um `.txt` de texto puro ao lado do vídeo, com o mesmo nome. Usa a legenda do próprio site (manual, depois automática, no idioma original); sem legenda (comum no Instagram e no TikTok), transcreve no Mac com o [whisper.cpp](https://github.com/ggml-org/whisper.cpp), detectando o idioma. Funciona também para trechos.
- **Legenda (.srt)** — marque "Gerar legenda" e o Cutter salva um `.srt` com tempos ao lado do arquivo, com o mesmo nome (abre junto no VLC/IINA e importa em editores de vídeo). Usa a legenda do site, ajustada ao trecho recortado; sem legenda, gera com o Whisper. Funciona junto com a transcrição `.txt` e com os formatos de áudio.
- Escolha de qualidade (360p a 4K), com preferência por H.264/AAC para abrir em qualquer player/editor.
- **Formato: vídeo, só áudio ou vídeo sem som** — em "Só áudio", salva MP3, M4A, Opus, WAV ou o áudio original (sem converter), com bitrate de 64 a 320 kbps; MP3 e M4A já vêm com título, artista e capa. "Vídeo sem som" tira a faixa de áudio. Vale para vídeo inteiro, trecho e lives (ideia inspirada no [cobalt](https://github.com/imputnet/cobalt)).
- Arraste um link do navegador para a janela do Cutter para colar e buscar o vídeo.
- Tenta outros clientes do YouTube automaticamente quando um falha, e mostra mensagens de erro compreensíveis (post privado, exige login, sem vídeo, bloqueado na região...). Quando o site pede login, aparece um botão que leva direto à escolha dos cookies do navegador.

## Requisitos

- macOS 11 ou mais novo
- [Homebrew](https://brew.sh) com `yt-dlp` e `ffmpeg`
- `python3` (já vem com as Command Line Tools do Xcode)
- Para compilar: Command Line Tools do Xcode (`xcode-select --install`)

```bash
brew install yt-dlp ffmpeg
```

Opcional, para transcrever vídeos sem legenda: `brew install whisper-cpp`. Na primeira transcrição o Cutter baixa o modelo `large-v3-turbo` (~550 MB) para `~/Library/Application Support/Cutter/models`.

Opcional, para o ícone ser gerado a partir do `icon.svg` no build: `brew install librsvg`.

## Instalação

```bash
git clone https://github.com/brunomgsena-stack/cutter.git
cd cutter
mac/build.sh --install
```

O app é instalado em `~/Applications/Cutter.app`. Sem `--install`, ele fica em `build/Cutter.app`.

Na primeira abertura, o macOS pode avisar que o app não é de um desenvolvedor identificado (ele é assinado localmente). Clique com o botão direito no app → **Abrir**.

## Ajustes

Pelo link **Ajustes** no rodapé:

- **Salvar em** — pasta de destino (padrão: `~/Downloads`).
- **Cookies do navegador** — opcional. Necessário para stories do Instagram, posts privados (de contas que você segue), vídeos com restrição de idade, quando o YouTube pedir "confirme que você não é um robô" ou quando o site limitar o acesso anônimo. Escolha um navegador em que você esteja logado no site. Para o Safari, o macOS exige dar Acesso Total ao Disco ao Cutter. Os cookies são lidos localmente pelo yt-dlp e nunca saem da sua máquina.

As preferências ficam em `~/Library/Application Support/Cutter/config.json`.

## Como funciona

- `app.py` — servidor HTTP local (só `127.0.0.1`, sem dependências além da biblioteca padrão) que chama `yt-dlp` e `ffmpeg`.
- `index.html` — interface.
- `mac/main.swift` — app nativo mínimo: inicia o `app.py` e mostra a interface numa janela própria. Fechar a janela salva gravações em andamento.

Para desenvolver sem recompilar, rode o servidor direto e abra o endereço que ele imprimir no navegador:

```bash
python3 app.py --open
```

## Aviso legal

O Cutter é uma ferramenta para uso pessoal. Baixar conteúdo pode violar os Termos de Serviço dos sites (YouTube, Instagram, TikTok etc.) e direitos autorais de terceiros. Use apenas com conteúdo que você tem direito de baixar — por exemplo, conteúdo próprio, licenciado para isso, ou de domínio público. Você é responsável pelo uso que faz da ferramenta.

Este projeto não é afiliado ao YouTube, Google, Meta, TikTok, X nem a nenhum dos sites suportados.

## Licença

[MIT](LICENSE)
