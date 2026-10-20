# Cutter

App para macOS que baixa vídeos do YouTube inteiros, recorta trechos e grava lives — inclusive voltando alguns minutos no tempo e seguindo ao vivo, sem emenda.

> **English summary:** Cutter is a small macOS app (Swift + WKWebView shell around a pure-Python local server) built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org). It downloads full videos, cuts clips from videos and live streams, and records live streams starting up to 1 hour in the past (using YouTube's DVR window) and continuing live until you stop.

<p align="center"><img src="icon.svg" width="128" alt="Ícone do Cutter"></p>

## O que faz

- **Vídeo inteiro** — baixa o vídeo completo (ou a live já encerrada).
- **Trecho** — informe início e fim (`HH:MM:SS`) e baixa só aquele pedaço; funciona em vídeos, lives encerradas e lives em andamento.
- **Gravar ao vivo** — grava a live a partir de agora **ou voltando 5, 10, 30 ou 60 minutos**, e continua até você parar ou a live acabar. Útil para quando você só percebe que precisava gravar depois que a parte importante já começou (sessões legislativas, palestras, eventos).
- Escolha de qualidade (360p a 4K), com preferência por H.264/AAC para abrir em qualquer player/editor.
- Tenta outros clientes do YouTube automaticamente quando um falha, e mostra mensagens de erro compreensíveis.

## Requisitos

- macOS 11 ou mais novo
- [Homebrew](https://brew.sh) com `yt-dlp` e `ffmpeg`
- `python3` (já vem com as Command Line Tools do Xcode)
- Para compilar: Command Line Tools do Xcode (`xcode-select --install`)

```bash
brew install yt-dlp ffmpeg
```

Opcional, para o ícone ser gerado a partir do `icon.svg` no build: `brew install librsvg`.

## Instalação

```bash
git clone <url-deste-repositorio> cutter
cd cutter
mac/build.sh --install
```

O app é instalado em `~/Applications/Cutter.app`. Sem `--install`, ele fica em `build/Cutter.app`.

Na primeira abertura, o macOS pode avisar que o app não é de um desenvolvedor identificado (ele é assinado localmente). Clique com o botão direito no app → **Abrir**.

## Ajustes

Pelo link **Ajustes** no rodapé:

- **Salvar em** — pasta de destino (padrão: `~/Downloads`).
- **Cookies do navegador** — opcional. Se o YouTube pedir login ("confirme que você não é um robô") ou o vídeo tiver restrição de idade, escolha um navegador em que você esteja logado no YouTube. Os cookies são lidos localmente pelo yt-dlp e nunca saem da sua máquina.

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

O Cutter é uma ferramenta para uso pessoal. Baixar conteúdo pode violar os Termos de Serviço do YouTube e direitos autorais de terceiros. Use apenas com conteúdo que você tem direito de baixar — por exemplo, conteúdo próprio, licenciado para isso, ou de domínio público. Você é responsável pelo uso que faz da ferramenta.

Este projeto não é afiliado ao YouTube nem ao Google.

## Licença

[MIT](LICENSE)
