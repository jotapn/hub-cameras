# Vision Hub

Sistema em Python para gestao de cameras com:

- cadastro de conexoes DVR/RTSP
- suporte a Hikvision ISAPI para snapshot e consulta de informacoes
- usuarios com autenticacao local
- permissoes por camera para visualizacao live, snapshot e gerenciamento

## Requisitos

- Python 3.11+

## Instalacao

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Execucao

```bash
uvicorn app.main:app --reload
```

Abra `http://127.0.0.1:8000`.

Primeiro acesso:

- usuario: `admin`
- senha: `admin123`

## go2rtc

O projeto gera automaticamente um `go2rtc.yaml` na raiz com as cameras cadastradas.

O binario foi preparado em:

```text
tools/go2rtc/go2rtc.exe
```

Para iniciar o proxy local:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\start-go2rtc.ps1
```

Interface do go2rtc:

```text
http://127.0.0.1:1984
```

Painel administrativo no sistema:

```text
http://127.0.0.1:8000/go2rtc
```

O sistema ja sincroniza cameras novas direto na API do go2rtc quando ele estiver online, sem precisar reiniciar manualmente.

Para acesso de outras maquinas na rede, o navegador remoto precisa conseguir abrir:

```text
http://SEU_IP:1984
```

Entao libere no firewall as portas:

- `8000` para a aplicacao
- `1984` para a interface/player do go2rtc
- `8555` para WebRTC do go2rtc, se necessario

## Subir tudo junto no Windows

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\start-stack.ps1
```

## Subir em servidor com Docker

```bash
docker compose up -d --build
```

Isso sobe:

- app web em `8000`
- go2rtc em `1984`
- RTSP local do go2rtc em `8554`
- WebRTC do go2rtc em `8555`

## Como funciona a conexao

Ao cadastrar a camera, o sistema monta a URL RTSP usando:

```text
rtsp://usuario:senha@host:554/cam/realmonitor?channel={channel}&subtype={subtype}
```

Esse formato cobre o exemplo informado para DVR.

Para Hikvision ISAPI, o sistema consulta por padrao:

- `/ISAPI/System/deviceInfo`
- `/ISAPI/Streaming/channels`
- `/ISAPI/Streaming/channels/{stream_id}/picture`

O `stream_id` segue o padrao `101`, `102`, `201`, etc.

## Observacoes

- O navegador nao reproduz RTSP puro nativamente. Este sistema entrega a URL RTSP para uso em players, NVRs, Home Assistant ou proxies como go2rtc.
- A visualizacao web nativa usa snapshot via ISAPI.
- Troque a `secret_key` da sessao antes de colocar em producao.
