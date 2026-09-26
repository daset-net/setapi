# SETAPI

**Seu banco. Sua API. Seu controle.**

Backend self-hosted com PostgreSQL, Redis, API automática, WebSocket, painel administrativo, tokens por tabela e storage integrado. O painel usa a mesma API que suas aplicações. Código próprio sob licença MIT, sem limite de faturamento.

> **v0.3.0 — PostgREST privado e RLS nas consultas.** Não é um substituto completo do Directus nem foi validado para cargas de produção. Consulte os limites abaixo antes de migrar dados reais.

As consultas de dados usam **PostgREST 16.4**, com RLS por organização/proprietário, sem mudar as rotas públicas. Escritas continuam transacionais no SETAPI. Veja [arquitetura, ENVs e limites](docs/POSTGREST.md).

## Instalação com três serviços

| Serviço | Conteúdo |
|---|---|
| `setapi_app` | API, painel, WebSocket, PostgREST e worker de eventos/backups |
| `setapi_db` | PostgreSQL 17 com volume persistente |
| `setapi_redis` | Redis 8 com senha e volume persistente |

API, PostgREST e worker são processos supervisionados no mesmo contêiner. Se um deles falhar, o contêiner encerra para que o Docker o reinicie. O healthcheck verifica API, PostgREST, banco, Redis e heartbeat do worker.

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
| `SETAPI_DB_POOL_MAX` | Máximo de conexões por processo, padrão 20 |
| `SETAPI_WS_MAX` / `SETAPI_HTTP_CONCURRENCY` | Limites por processo, padrão 1000 |
| `SETAPI_ALLOW_REGISTRATION` | Cadastro público de alunos; desativado por padrão |
| `SETAPI_SMTP_HOST/PORT/FROM/USER/PASSWORD` | Recuperação/verificação de e-mail; TLS obrigatório |
| `SETAPI_STORAGE_HOSTS` | Allowlist de hosts HTTPS para S3 compatível, além de AWS/R2 |

Guarde a chave de criptografia separadamente do servidor e dos backups. Perder essa chave impede recuperar credenciais e abrir backups. A chave ainda não tem rotação automática.

PostgreSQL e Redis são configurados por variáveis de ambiente para permitir bootstrap e recuperação. O painel mostra seu estado; conexões S3/R2/Drive são configuradas pelo painel. Alterar a conexão principal requer reiniciar API e worker.

## Atualizar instalações anteriores

Antes de atualizar, faça backup e preserve a chave de criptografia. A inicialização adiciona tabelas/colunas internas e instala triggers nas tabelas compatíveis, sem excluir registros. **Membros agora precisam de política explícita na tabela**, além dos escopos do usuário/token; configure proprietário, tenant e campos pelo painel antes de liberar aplicações. Tokens administrativos continuam com acesso amplo.

Os novos limites são: 64 KiB por gravação de registro, 2 MiB por página de leitura, offset máximo 10.000, 1.200 requisições/minuto por usuário, 10.000/minuto por IP e dez WebSockets por usuário. Arquivos grandes devem ir ao storage. Para listas frequentes, use `include_total=false` e limites pequenos. Links de recuperação usam fragmento do navegador; não coloque tokens de serviço nas URLs.

Consulte [segurança e configuração](docs/SECURITY.md) e [testes de carga](docs/PERFORMANCE.md). Os testes não certificam ausência de vulnerabilidades nem capacidade do servidor de produção.

## O que está implementado

- Login do painel com cookie HttpOnly/SameSite=Lax, verificação de origem e proteção CSRF. Lax permite retornar do Google; o callback exige state vinculado à sessão e de uso único.
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

## Organizações isoladas

No painel, use **Organizações → Nova organização**. Em **Usuários**, escolha a organização de cada conta. Ao criar uma tabela, mantenha **Isolar dados por organização** marcado: o SETAPI cria o campo, o índice e a política necessários. Depois libere os campos e operações que os membros poderão usar.

- Uma organização por usuário; o mesmo banco é compartilhado com isolamento lógico das linhas.
- O cliente não escolhe nem altera `organization_id` nas gravações: a API usa a organização do usuário autenticado.
- Sem política de organização na tabela, uma conta de organização é bloqueada.
- Desativar uma organização encerra seus acessos e preserva registros. Reativar exige novo login.
- O administrador global gerencia todas as organizações. Contas de organização não recebem essa permissão.
- Em **Tokens**, escolha um usuário responsável da organização; o token herda sua organização e não amplia suas permissões.
- Backups/storages/configuração são administrativos da instância. Não são compartilhados com as contas das organizações.

A API aceita `POST /api/organizations` com `{name}`, `PATCH /api/organizations/{id}` com `{name?,active?}` e `POST /api/tables` com `organization_isolated:true`. Tabelas existentes precisam de campo/política de organização e associação correta dos registros antes da liberação; dados não são distribuídos automaticamente.

## Autenticação dos alunos

- `POST /api/app-auth/register`: `{email,password}`; desativado até configurar SMTP e `SETAPI_ALLOW_REGISTRATION=true`.
- `POST /api/app-auth/verify`: confirma o token enviado por e-mail; não concede acesso a tabelas.
- `POST /api/app-auth/login`: `{email,password,otp?}`; retorna token individual com validade de 12 horas. Envie como `Authorization: Bearer ...`.
- `GET /api/auth/me`, `POST /api/auth/logout`: identidade e encerramento da sessão.
- `POST /api/auth/password`: senha atual, nova senha e código MFA quando habilitado.
- `POST /api/auth/forgot-password` e `/api/auth/reset-password`: recuperação; MFA continua obrigatório se ativado.
- `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`: revogação das próprias sessões.

No painel, crie o usuário como **Aplicativo (aluno)** e atribua permissões e organização. Na tabela, crie campos UUID para proprietário/tenant e configure **Permissões da tabela**. A API preenche esses campos e impede sua substituição pelo aluno. As regras de proprietário e tenant se somam quando ambas estão configuradas. Um aluno não recebe credenciais do PostgreSQL/Redis nem o token administrativo.

A documentação `/docs` e `/openapi.json` agora exige sessão administrativa. A aplicação do aluno deve usar seu próprio frontend; usuários de aplicativo não fazem login no console administrativo.

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

Notificações contêm identificadores, não o conteúdo dos registros. Faça uma nova consulta à API para obter os dados. Reconsulte também ao reconectar. A entrega é **pelo menos uma vez até o Redis**, com possíveis duplicatas: deduplique por `event_id`. Redis Pub/Sub não guarda mensagens para clientes desconectados e não há replay de histórico nesta versão. O servidor verifica novamente a autorização antes de enviar eventos e verifica a sessão nos heartbeats.

Tabelas gerenciadas possuem trigger transacional: INSERT/UPDATE/DELETE feitos diretamente no PostgreSQL também geram eventos. Escritas SQL externas exigem uma conta de banco confiável; as políticas da API não se aplicam a quem recebe credenciais SQL. Alterações de estrutura ficam disponíveis sem restart. Ao criar tabelas, clientes externos precisam reabrir a assinatura com a nova lista.

## Storage

Configuração S3:

```json
{"bucket":"meu-bucket","region":"us-east-1","access_key_id":"...","secret_access_key":"..."}
```

Para R2, use provedor `r2`, região `auto` e `endpoint_url` como `https://ACCOUNT_ID.r2.cloudflarestorage.com`.

### Google Drive: conectar com Google

No painel, abra **Storage e arquivos → Conectar storage → Google Drive → Conectar com Google**. Escolha a conta, autorize e volte ao painel: o SETAPI cria uma pasta exclusiva, salva o refresh token criptografado e deixa a conexão disponível para arquivos e backups. Não é necessário preencher JSON nem gerar tokens manualmente.

O administrador configura o aplicativo Google **uma única vez**, em **Configurar Google**:

1. No Google Cloud, crie um projeto e ative a API Google Drive.
2. Configure a tela de consentimento OAuth. Em modo Testing, inclua a conta desejada nos usuários de teste.
3. Crie credenciais OAuth do tipo **Aplicativo da Web** e cadastre exatamente a URI de redirecionamento exibida no painel: `https://SEU_DOMINIO/api/integrations/google/callback`.
4. Cole Client ID e Client Secret nos campos do painel. O segredo é criptografado no banco e não é devolvido pela API.

`SETAPI_PUBLIC_URL` precisa corresponder ao domínio HTTPS utilizado. Esse cadastro identifica o SETAPI perante o Google; o repositório não inclui credenciais de um aplicativo Google compartilhado. Consulte a [documentação oficial do OAuth](https://developers.google.com/identity/protocols/oauth2/web-server).

A integração usa o escopo `drive.file`, limitado aos arquivos autorizados/criados pelo aplicativo, e cria uma pasta nova em cada conexão. Não solicita acesso irrestrito ao Drive nem oferece seleção de pastas existentes. O botão **Reconectar com Google** preserva a pasta e exige acesso à pasta original. Conexões antigas criadas manualmente podem precisar de uma nova conexão se a pasta anterior não estiver acessível a esse escopo.

O Google pode expirar refresh tokens de aplicativos externos em modo Testing após sete dias. Para uso contínuo, ajuste o status de publicação e os requisitos de consentimento do seu projeto conforme a documentação Google. O fluxo usa state de uso único com validade de dez minutos, vinculado à sessão administrativa, além de PKCE. Tokens e códigos não são exibidos pelo painel; os logs de acesso do Uvicorn estão desativados para não registrar o código no callback. Configure também seu proxy para não registrar parâmetros desse endpoint.

S3/R2 usam campos individuais no painel: bucket, região, endpoint e chaves de acesso. Para outros provedores S3 compatíveis, use `endpoint_url` HTTPS. Outros protocolos exigem um adaptador em `app/storage.py`.

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
- A API opera no schema `data`. Tabelas preexistentes podem ser preparadas pelo painel: adiciona UUID e datas quando faltarem. IDs preexistentes de outro tipo e relacionamentos precisam de migração explícita. Outros schemas não são expostos automaticamente.
- Políticas por tabela, proprietário, tenant e campos. Configure-as explicitamente: membros sem política não recebem acesso. Administradores têm acesso total; nunca compartilhe tokens administrativos com alunos.
- Não há migração automática do Directus ou GraphQL. O painel oferece campos relacionais UUID, conversão de tipos PostgreSQL com confirmação e criação de índices; tabelas grandes exigem manutenção planejada.
- Registros, campos, permissões e provedores têm formulários. Campos cujo próprio tipo é JSON continuam usando um editor JSON.
- Usuários de aplicativo possuem login separado; cadastro público exige ativação explícita e SMTP com TLS. Novos cadastros começam sem permissões e sem tenant. Administradores atribuem acesso. MFA TOTP e códigos de recuperação estão disponíveis; troca/reset de senha revogam todos os tokens e sessões.
- Existem limites de login, recuperação, requisições e WebSockets. Proteção volumétrica/DDoS, limite de conexões e egress de rede também precisam ser configurados na infraestrutura.
- Arquivos: upload e exclusão administrativos, listagem/download por proprietário ou administrador; sem biblioteca pública por padrão.
- Backups longos precisam de espaço temporário e banda; não há PITR/WAL, cópia dos objetos externos, restauração online ou retomada persistente de um upload Drive interrompido.
- Triggers capturam alterações de registros. Não há garantia de ordenação global ao usar múltiplos workers; comece com um worker. Clientes devem reconsultar ao reconectar ou receber resync.
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
