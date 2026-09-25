# SETAPI

**Seu banco. Sua API. Seu controle.**

Backend self-hosted com PostgreSQL, Redis, API automática, WebSocket, painel administrativo, tokens por tabela e storage integrado. O painel usa a mesma API que suas aplicações. Código próprio sob licença MIT, sem limite de faturamento.

> **v0.1.0 — primeira versão funcional.** Não é um substituto completo do Directus nem foi validado para cargas de produção. Consulte os limites abaixo antes de migrar dados reais.

## Instalação com três serviços

| Serviço | Conteúdo |
|---|---|
| `setapi_app` | API, painel, WebSocket e worker de eventos/backups |
| `setapi_db` | PostgreSQL 17 com volume persistente |
| `setapi_redis` | Redis 8 com senha e volume persistente |

API e worker são processos supervisionados no mesmo contêiner. Se um deles falhar, o contêiner encerra para que o Docker o reinicie. O healthcheck verifica API, banco, Redis e heartbeat do worker.

### Docker

Pré-requisitos: Docker Engine com Compose v2 e Python 3. O script instala a stack; não instala o Docker no sistema.

```bash
git clone https://github.com/daset-net/setapi.git
cd setapi
bash install.sh
```

O instalador gera `.env` com senhas aleatórias e chave de criptografia, constrói a imagem e aguarda os três serviços ficarem saudáveis. Execuções seguintes preservam as credenciais existentes.

- Painel: **http://localhost:8055**; documentação: **http://localhost:8055/docs**.
- Login inicial: `admin@setapi.local`; senha em `SETAPI_ADMIN_PASSWORD` no `.env`.
- A porta é publicada somente em `127.0.0.1`. PostgreSQL e Redis não publicam portas externas.
- Os nomes de contêiner são fixos: uma instalação desta stack por host Docker.

Para personalizar a primeira instalação:

```bash
bash install.sh --admin-email admin@empresa.com --public-url https://api.empresa.com
# Configure o proxy HTTPS para encaminhar HTTP e WebSocket à porta 8055.
```

Para preparar as ENVs antes de instalar:

```bash
bash install.sh --generate-only
# Revise .env antes da primeira inicialização.
bash install.sh
docker compose logs -f setapi_app
```

`--port 8055` e `--bind-host 127.0.0.1` controlam a publicação inicial no Docker. Use `--bind-host 0.0.0.0` quando precisar publicar a porta nas interfaces do host. Após gerar `.env`, altere essas opções no próprio arquivo. O administrador só é criado quando não há usuários; trocar a ENV não redefine a senha de um usuário existente. Não altere senhas do banco nem a chave de criptografia de uma instalação existente sem uma migração.

O instalador recusa migrar automaticamente a stack antiga com quatro serviços. Pare e revise essa instalação antes de migrar, preservando seus volumes e `.env`. **`docker compose down -v` apaga os volumes.**

### EasyPanel — template Custom

Na cópia local do repositório, execute:

```bash
bash install.sh --target easypanel --admin-email admin@empresa.com
```

O comando gera os seguintes arquivos privados em `conexao/instalacao/`:

- `easypanel-template.json`: template completo com os três serviços e suas ENVs.
- `setapi_app.env`, `setapi_db.env`, `setapi_redis.env`: cópias das configurações e credenciais de cada serviço.

No EasyPanel, abra o projeto, escolha **+ Serviço → Templates → Custom**, cole o conteúdo de `easypanel-template.json` e crie os serviços. O template usa o Dockerfile do repositório público, conecta os serviços pela rede interna e configura a porta 8055 com HTTPS. O nome do projeto e o domínio automático são resolvidos pelos placeholders oficiais do EasyPanel. O script apenas gera o template: a instalação no servidor ocorre ao importá-lo no painel.

Para usar seu domínio, acrescente `--public-url https://api.empresa.com` e configure o DNS para o servidor. Consulte o login e a senha em `setapi_app.env`. Para outro ambiente, use `--output /caminho/privado/nova-instalacao`; uma pasta já preenchida não será sobrescrita.

Esses arquivos contêm segredos, têm permissão de leitura/escrita apenas para o proprietário e são ignorados pelo Git. A geração Docker e a geração EasyPanel criam credenciais independentes; use os arquivos correspondentes ao ambiente instalado. O schema segue a [API oficial de templates do EasyPanel](https://easypanel.io/docs/api/templates/createFromSchema).

Em produção, configure `SETAPI_PUBLIC_URL`, HTTPS e encaminhamento de WebSocket no proxy. Configure o limite de corpo do proxy igual ou menor que o limite de upload. Aceite `X-Forwarded-For` apenas dos proxies conhecidos (`FORWARDED_ALLOW_IPS`).

A estrutura e as configurações persistem no PostgreSQL. Reserve espaço temporário em `setapi_app` para pelo menos três vezes o tamanho do dump, além de margem, durante backups.

## Configuração

| Variável | Finalidade |
|---|---|
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` | Senhas dos serviços da stack Docker |
| `SETAPI_BIND_HOST` / `SETAPI_PORT` | Interface e porta publicadas pelo Compose |
| `SETAPI_RUN_WORKER` | `true` na stack padrão; inicia o worker junto à API |
| `DATABASE_URL` | Conexão PostgreSQL; banco dedicado recomendado |
| `REDIS_URL` | Conexão Redis, incluindo senha quando configurada |
| `SETAPI_ENCRYPTION_KEY` | Chave Fernet de 32 bytes em base64 URL-safe; protege credenciais e backups |
| `SETAPI_ADMIN_EMAIL` / `SETAPI_ADMIN_PASSWORD` | Administrador inicial; senha de pelo menos 12 caracteres |
| `SETAPI_PUBLIC_URL` | Origem exata do painel, incluindo esquema e porta |
| `SETAPI_COOKIE_SECURE` | `true` para HTTPS; `false` somente no ambiente HTTP local |
| `SETAPI_CORS_ORIGINS` | Origens de aplicações separadas por vírgula; sem liberação por padrão |
| `SETAPI_MAX_UPLOAD_MB` | Limite de arquivo, padrão 50 MiB |

Guarde a chave de criptografia separadamente do servidor e dos backups. Perder essa chave impede recuperar credenciais e abrir backups. A v0.1 não tem rotação automática da chave.

PostgreSQL e Redis são configurados por variáveis de ambiente para permitir bootstrap e recuperação. O painel mostra seu estado; conexões S3/R2/Drive são configuradas pelo painel. Alterar a conexão principal requer reiniciar API e worker.

## O que está implementado

- Login do painel com cookie HttpOnly/SameSite, verificação de origem e proteção CSRF.
- Senhas Argon2id; tokens aleatórios armazenados somente como SHA-256, com validade, revogação e escopos por tabela.
- Usuários administradores e membros; ativação/desativação pelo painel.
- Tabelas no schema `data`, CRUD imediato, criação/renomeação/exclusão de campos e exclusão de tabelas com confirmação explícita.
- Tipos `text`, `integer`, `decimal`, `boolean`, `datetime`, `date`, `uuid`, `json`; campos obrigatórios, unicidade e chaves estrangeiras UUID com exclusão restrita.
- `id` UUID, `created_at` e `updated_at` automáticos e protegidos contra escrita do cliente.
- Filtros de igualdade em JSON, ordenação e paginação (até 200 registros por chamada).
- Eventos transacionais via outbox PostgreSQL → worker → Redis → WebSocket.
- S3 e R2 via API S3; Google Drive via OAuth refresh token e upload resumível em blocos.
- Upload administrativo e download autorizado, sem tornar o bucket público.
- Backups manuais e periódicos, criptografia autenticada em blocos, SHA-256 e retenção por agendamento.
- Restauração offline para banco vazio e histórico de operações sem segredos.

## API em uso

Crie uma tabela pelo painel ou pela API administrativa:

```json
POST /api/tables
{
  "name": "clientes",
  "columns": [
    {"name": "nome", "type": "text", "nullable": false},
    {"name": "email", "type": "text", "unique": true},
    {"name": "ativo", "type": "boolean"}
  ]
}
```

No painel, crie um token com os escopos necessários:

```json
{"clientes": ["read", "create", "update", "delete"]}
```

O segredo aparece uma única vez. Use-o **no backend da sua aplicação**. Um token de serviço incluído em JavaScript público pode ser copiado por qualquer visitante.

```bash
# Defina SETAPI_TOKEN no ambiente sem incluí-lo em arquivos versionados.
curl http://localhost:8055/api/data/clientes \
  -H "Authorization: Bearer $SETAPI_TOKEN"

curl -X POST http://localhost:8055/api/data/clientes \
  -H "Authorization: Bearer $SETAPI_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"nome":"Maria","email":"maria@example.com","ativo":true}'

curl --get http://localhost:8055/api/data/clientes \
  -H "Authorization: Bearer $SETAPI_TOKEN" \
  --data-urlencode 'filter={"ativo":true}' \
  --data-urlencode 'sort=-created_at' \
  --data-urlencode 'limit=25'
```

O token administrativo pode gerenciar o ambiente inteiro. O token comum exige escopos e, se seu proprietário for membro, as permissões atuais desse usuário também devem permitir a operação. A revogação e a desativação do proprietário são verificadas em cada chamada.

### Tempo real

Conecte a `/ws` e envie a autenticação no primeiro frame, em até cinco segundos. O token não é passado na URL:

```json
{"token":"SEU_TOKEN","tables":["clientes"]}
```

O servidor confirma com `{"type":"ready","tables":["clientes"],"resync":true}` e envia notificações:

```json
{"type":"change","table":"clientes","operation":"updated","id":"UUID","event_id":42}
```

Notificações contêm identificadores, não o conteúdo dos registros. Faça uma nova consulta à API para obter os dados. Reconsulte também ao reconectar. A entrega é **pelo menos uma vez até o Redis**, com possíveis duplicatas: deduplique por `event_id`. Redis Pub/Sub não guarda mensagens para clientes desconectados e não há replay de histórico na v0.1. O servidor verifica novamente a autorização antes de enviar eventos e verifica a sessão nos heartbeats.

Alterações externas feitas diretamente no PostgreSQL não geram eventos; todo acesso de escrita deve passar pela API. Alterações de estrutura ficam disponíveis sem restart. Ao criar tabelas, clientes externos precisam reabrir a assinatura com a nova lista.

## Storage

Configuração S3:

```json
{"bucket":"meu-bucket","region":"us-east-1","access_key_id":"...","secret_access_key":"..."}
```

Para R2, use provedor `r2`, região `auto` e `endpoint_url` como `https://ACCOUNT_ID.r2.cloudflarestorage.com`.

Configuração Google Drive:

```json
{"client_id":"...","client_secret":"...","refresh_token":"...","folder_id":"..."}
```

O consentimento OAuth e a emissão inicial do refresh token são feitos previamente no Google. A pasta precisa estar acessível ao usuário autorizado e ao escopo escolhido. O painel não tem um assistente OAuth nesta versão. Para outros provedores S3 compatíveis, use `endpoint_url` HTTPS. Outros protocolos exigem um novo adaptador em `app/storage.py`.

As conexões são cadastradas antes do teste; o botão **Testar conexão** confirma leitura do bucket/pasta. O teste completo de escrita é um upload real. Credenciais nunca são devolvidas pela API de listagem. Uma conexão existente pode ter suas credenciais atualizadas por `PUT /api/storages/{id}`; mudar bucket ou pasta exige nova conexão para preservar referências antigas.

## Backup e restauração

Cada backup contém:

1. `database.dump`: dump PostgreSQL em formato custom, incluindo dados, usuários e configurações persistidas.
2. `config.json`: inventário auxiliar de storages e agendamentos; credenciais continuam criptografadas.

O conjunto é criptografado antes do envio ao provedor. O checksum do arquivo criptografado aparece na API de backups. **Conteúdo dos arquivos externos, configuração do Docker, variáveis de ambiente e papéis globais do cluster PostgreSQL não estão incluídos.** O dump é transacionalmente consistente; o inventário auxiliar é capturado separadamente e não deve substituir o dump como fonte de recuperação.

O agendamento roda no worker. A retenção remove somente backups concluídos do mesmo agendamento, preservando a quantidade configurada. Cópias manuais não expiram automaticamente. Remover o agendamento não apaga backups anteriores. O painel mostra o estado running enquanto o worker processa a cópia. Se o processo for interrompido, o próximo worker marca esse trabalho como failed; solicite uma nova cópia.

### Restaurar sem sobrescrever o ambiente em uso

1. Baixe o arquivo `.setapi` pelo provedor e verifique seu SHA-256 contra o registro do backup.
2. Crie um banco PostgreSQL **vazio**, dedicado à recuperação, com permissões de criação de schema/tabelas.
3. Disponibilize a chave original e a URL do banco de destino no ambiente do comando:

```bash
# SETAPI_ENCRYPTION_KEY e SETAPI_RESTORE_DATABASE_URL devem estar definidos no ambiente.
docker compose run --rm --no-deps \
  -v "$PWD/backup.setapi:/restore/backup.setapi:ro" \
  -e SETAPI_ENCRYPTION_KEY -e SETAPI_RESTORE_DATABASE_URL \
  setapi_app python -m app.restore /restore/backup.setapi --confirm-database setapi_recuperado
```

O comando recusa banco não vazio, valida a integridade criptográfica e restaura em uma transação. Depois revoga tokens antigos, limpa eventos pendentes e pausa agendamentos. Faça login novamente e crie novos tokens. Valide o ambiente recuperado antes de apontar a API para ele. Use a mesma chave original para ler as credenciais restauradas.

O Dockerfile inclui cliente PostgreSQL **17**. Para usar PostgreSQL de versão principal mais recente, ajuste e teste o cliente `pg_dump/pg_restore` correspondente antes de agendar backups.

## Arquitetura

```mermaid
flowchart LR
  Panel[Painel] --> API[FastAPI]
  App[Aplicações] --> API
  API --> PG[(PostgreSQL)]
  PG --> Worker[Worker: outbox e backups]
  Worker --> Redis[(Redis)]
  Redis --> WS[WebSocket autenticado]
  WS --> App
  API --> Storage[S3 / R2 / Google Drive]
  Worker --> Storage
```

Schemas: `setapi` guarda metadados privados e `data` guarda tabelas gerenciadas. O token de uma aplicação não dá acesso SQL ao banco. A API usa parâmetros para valores e identificadores SQL validados e escapados para operações dinâmicas. Exclusões estruturais usam `RESTRICT`, sem apagar relações em cascata automaticamente.

## Limites desta primeira versão

- Uma conexão PostgreSQL e uma Redis por ambiente; não é um gerenciador multi-banco.
- API automática somente para tabelas criadas no schema `data` com o contrato de colunas do SETAPI; não publica automaticamente tabelas arbitrárias de bancos existentes.
- Permissões por tabela/operação; **não há isolamento por linha, tenant ou campo**. Não compartilhe uma mesma tabela de dados sensíveis entre clientes que devem ver linhas diferentes.
- Não há migração automática do Directus, GraphQL, editor visual de relacionamentos, alteração arbitrária de tipos de coluna ou editor genérico de índices. Unicidade cria índices no PostgreSQL.
- Formulários de registros e definições de campos usam JSON no painel inicial.
- Login é para usuários do painel; autenticação pública de usuários finais, MFA, recuperação por e-mail e rotação de senha ainda não foram implementados.
- Login tem limite de tentativas por IP e conta. Quotas/rate limits gerais devem ser configurados no proxy nesta versão.
- Arquivos: upload administrativo, listagem/download por proprietário ou administrador; não há biblioteca pública nem exclusão de arquivos pelo painel.
- Backups longos precisam de espaço temporário e banda; não há PITR/WAL, cópia dos objetos externos, restauração online ou retomada persistente de um upload Drive interrompido.
- Alterações diretas no banco ficam fora da outbox. Não há garantia de ordenação global ao usar múltiplos workers; comece com um worker.
- Requer homologação de carga, revisão de segurança e testes com seus provedores reais antes de produção.

## Testes

Use um banco dedicado chamado **`setapi_test`**. Os testes apagam seus schemas `data` e `setapi`, e criam/substituem um banco de recuperação chamado `setapi_restore_test`. Nunca use banco de produção.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/setapi_test
export REDIS_URL=redis://localhost:6379/15
.venv/bin/python -m pytest -q
```

O usuário de teste precisa poder criar o banco de restauração. `pg_dump` e `pg_restore` 17 devem estar no PATH. Os testes usam PostgreSQL e Redis reais, e substituem apenas o transporte externo do storage por uma cópia local no teste de backup. Credenciais reais de S3/R2/Drive não são necessárias.

## Licença

[MIT](LICENSE) para o código do SETAPI. Sem condição de faturamento ou número de funcionários. Dependências e serviços utilizados mantêm suas próprias licenças e condições. Nenhum código do Directus foi utilizado.
