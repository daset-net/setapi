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
| `SETAPI_SMTP_HOST/PORT/FROM/USER/PASSWORD` | Legado. Prefira configurar o e-mail por API no painel, que tem prioridade sobre estas variáveis |
| `SETAPI_GOOGLE_CLIENT_ID` / `SETAPI_GOOGLE_CLIENT_SECRET` | Opcional: aplicativo OAuth do Google, se não for informado em Configurações no painel |
| `SETAPI_STORAGE_HOSTS` | Allowlist de hosts HTTPS para S3 compatível, além de AWS/R2 |

`SETAPI_ADMIN_EMAIL` e `SETAPI_ADMIN_PASSWORD` só criam o primeiro administrador, quando o banco está vazio. Alterá-las depois não muda o login. Para aplicar os valores atuais (recuperar o acesso ou trocar e-mail/senha), abra o console do serviço e rode:

```bash
python -m app.reset_admin              # redefine a senha, ou cria o administrador com esse e-mail
python -m app.reset_admin --disable-mfa  # também desliga a verificação em duas etapas
```

As sessões abertas dessa conta são encerradas. O comando não promove membros de organização nem usuários do app; o administrador anterior, se tinha outro e-mail, continua existindo e pode ser desativado em Usuários.

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

## Organizações

Cada organização é um espaço de trabalho próprio, como no NocoDB. No painel, o seletor no topo da barra lateral troca entre **Plataforma** e as organizações; em **Organizações**, o botão **Abrir →** faz o mesmo. Dentro de uma organização, Tabelas, Usuários, Tokens, Storage e Atividade mostram e criam só o que é dela. Organizações, Backups e E-mail ficam em Plataforma.

**Tabelas próprias.** Cada organização tem as suas tabelas, com estrutura própria: duas organizações podem ter uma tabela `clientes` com campos diferentes, sem conflito. Para quem usa a API, o nome é só `clientes`; internamente o SETAPI guarda cada uma com o prefixo da organização (`o` + 10 caracteres hexadecimais + `_`, um padrão reservado).

- Na API, as rotas de tabelas, campos, índices, permissões e registros aceitam `organization_id`. O administrador global trabalha na Plataforma sem ele e dentro da organização com ele. Usuários e tokens de uma organização trabalham sempre na própria e recebem 404 se apontarem para outra.
- A estrutura (criar e alterar tabelas, campos, índices e permissões) é do administrador global e dos **administradores da organização**. O administrador global marca a conta como administrador da organização (`org_admin: true` em `POST /api/users` ou `PUT /api/users/{id}/access`). Essa conta trabalha só nas tabelas da própria organização, com acesso a todos os registros e campos delas; nunca altera tabelas da Plataforma nem de outra organização. Pela API ou pelo MCP, ela usa um token próprio, que vale só dentro da organização. Todo token de um administrador da organização tem os poderes dele, sem precisar marcar nada: crie o usuário como administrador com "Criar um token junto" e o token já cria tabelas, grava registros e envia arquivos. Promover um membro mantém os tokens dele funcionando, agora como administrador. Rebaixar a membro ou trocar a organização revoga todos os tokens. Para um acesso limitado, emita o token a partir de um membro. Os demais usuários da organização leem e gravam registros conforme as permissões.
- Uma tabela nova de organização recebe uma política com todos os campos liberados para os usuários dela. Adicionar, renomear, retipar ou remover um campo atualiza essa política sozinho; restrinja em **Permissões da tabela**.
- Relacionamentos (`references`) ficam dentro da mesma organização.
- O tempo real (`/ws`) aceita `organization_id` no primeiro envio, e os eventos chegam com o nome da tabela sem prefixo.

**Tabelas compartilhadas.** Na Plataforma continua existindo a tabela compartilhada entre organizações (`organization_isolated: true`): uma estrutura só, com o campo `organization_id` preenchido pela API e cada organização vendo só os próprios registros. Os usuários de organização continuam alcançando essas tabelas, a menos que a organização tenha uma tabela própria com o mesmo nome.

- Uma organização por usuário. Em **Tokens**, o token de um usuário herda a organização dele e não amplia suas permissões.
- Desativar uma organização encerra seus acessos e preserva os dados. Reativar exige novo login.
- Storage: cada organização conecta o próprio; o administrador global também cria conexões para uma organização (`organization_id` no corpo). Backups do banco só vão para storage da Plataforma.

A API aceita `POST /api/organizations` com `{name}` e `PATCH /api/organizations/{id}` com `{name?,active?}`.

## Autenticação dos alunos

- `POST /api/app-auth/register`: `{email,password}`; desativado até configurar SMTP e `SETAPI_ALLOW_REGISTRATION=true`.
- `POST /api/app-auth/verify`: confirma o token enviado por e-mail; não concede acesso a tabelas.
- `POST /api/app-auth/login`: `{email,password,otp?}`; retorna token individual com validade de 12 horas. Envie como `Authorization: Bearer ...`.
- `GET /api/auth/me`, `POST /api/auth/logout`: identidade e encerramento da sessão.
- `POST /api/auth/password`: senha atual, nova senha e código MFA quando habilitado.
- `POST /api/auth/forgot-password` e `/api/auth/reset-password`: recuperação; MFA continua obrigatório se ativado.
- `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`: revogação das próprias sessões.

No painel, crie o usuário como **Aplicativo (usuário final)** e atribua permissões e organização. Na tabela, crie campos UUID para proprietário/tenant e configure **Permissões da tabela**. A API preenche esses campos e impede sua substituição pelo aluno. As regras de proprietário e tenant se somam quando ambas estão configuradas. Um aluno não recebe credenciais do PostgreSQL/Redis nem o token administrativo.

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

No painel, escolha a organização no seletor da barra lateral e crie o token em **Usuários**, junto com o usuário (marque *Criar um token de API junto com este usuário*) ou pelo botão **Token** da linha de um usuário já existente. Também dá para criar em **Tokens de acesso**, escolhendo o usuário da organização que responde pelo token. Tokens pertencem sempre a uma organização; os tokens antigos da plataforma aparecem em **Configurações** até serem revogados.

Para cada tabela, escolha o acesso: **Somente leitura**, **Somente escrita** (criar, editar e excluir), **Leitura e escrita** ou **Personalizado**, que abre as quatro operações. O seletor *Aplicar a todas…* repete a mesma escolha em todas as tabelas. Os escopos resultantes são os mesmos da API:

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

## Servidor MCP

O SETAPI é também um servidor MCP (Streamable HTTP) em `/mcp`, para IAs e agentes usarem a API inteira. Conecte com a URL e um token do SETAPI:

```json
{"mcpServers": {"setapi": {"type": "http", "url": "https://SEU_DOMINIO/mcp",
  "headers": {"Authorization": "Bearer set_..."}}}}
```

- **Uma ferramenta por rota REST**, geradas da especificação OpenAPI: rota nova vira ferramenta sem mudar o MCP. Os nomes seguem a função da rota (`create_table`, `add_column`, `rename_column`, `edit_column`, `drop_column`, `drop_table`, `list_records`, `create_record`, `update_record`, `delete_record`, `upload_file` e as demais).
- **Mesmo comportamento da API**: cada chamada é repassada à própria API REST com o token recebido. Validação, permissões, políticas por tabela, limites de taxa e registro de atividade são os da REST.
- **Argumentos**: parâmetros de caminho e de consulta no nível de cima; o corpo JSON da rota em `body`. A resposta é `{"status", "body"}`, com o mesmo código HTTP e o mesmo corpo da REST; erros HTTP voltam com `isError`.
- **Arquivos** vão e voltam em base64 (`upload_file`, `download_file`), no limite de `SETAPI_MAX_UPLOAD_MB`.
- Use um **token administrativo** para criar e alterar tabelas e campos. Um token de organização só enxerga o que aquela organização pode acessar.

O servidor é sem sessão: responde JSON em POST, sem stream de eventos (GET e DELETE em `/mcp` devolvem 405). Requisições com `Origin` fora de `SETAPI_PUBLIC_URL` e `SETAPI_CORS_ORIGINS` são recusadas.

## Storage

Configuração S3:

```json
{"bucket":"meu-bucket","region":"us-east-1","access_key_id":"...","secret_access_key":"..."}
```

Para R2, use provedor `r2`, região `auto` e `endpoint_url` como `https://ACCOUNT_ID.r2.cloudflarestorage.com`.

### E-mail por API

O SETAPI envia e-mail só para quem acessa o painel: notificações e recuperação de senha. O administrador global configura em **E-mail**: escolhe o provedor, cola a API key, e o SETAPI lista as inboxes daquela chave para escolher a que envia.

| Provedor | API |
|---|---|
| AgentMail | `api.agentmail.to` |
| OpenMail | `api.openmail.sh` |
| AGMail | `api.agmail.ai` |

Quem recebe:

- **Administradores do painel**: avisos gerais (backup que falhou, organização que conectou o Google Drive) e recuperação de senha.
- **Usuários do painel**: avisos da própria organização e recuperação de senha. Organizações desativadas deixam de receber.

A inbox precisa pertencer à chave; o SETAPI confere com o provedor ao salvar. A chave fica criptografada no banco e não volta ao navegador. O botão **Enviar teste para mim** manda uma mensagem na hora para o administrador logado. As variáveis `SETAPI_SMTP_*` são legado: continuam funcionando para instalações antigas, e o provedor configurado no painel tem prioridade.

### Configurações da plataforma

Com **Plataforma** no seletor, o administrador global abre **Configurações**:

- **Personalização**: nome da plataforma, mensagem da tela de login e cor principal. O nome aparece no login, na barra lateral, no assunto dos e-mails e no aplicativo autenticador.
- **Avançado**: duração do login (1 a 720 horas), tamanho máximo de arquivo enviado ao storage (padrão: `SETAPI_MAX_UPLOAD_MB`) e validade sugerida para novos tokens.
- **E-mail** e **Aplicativo Google** (veja abaixo).
- **Sistema**: versão, PostgreSQL, Redis, worker, endereço público e origens CORS, que continuam nas variáveis do serviço.

Na API: `GET /api/platform/branding` (público) e `GET`/`PUT /api/platform/settings` (administrador global).

### Backups da plataforma e das organizações

- **Plataforma → Backups**: cópia criptografada do banco inteiro, enviada para um destino da plataforma. O cartão **Backups das organizações** mostra o agendamento e o último backup de cada organização, com Backup agora, Agendar e Remover.
- **Organização → Backups**: cópia só das tabelas dela (com os registros), das permissões das tabelas, das conexões de storage e da lista de usuários sem senhas, enviada para o storage da própria organização.
- Cada agendamento mantém de **1 a 7** cópias; as mais antigas são excluídas do provedor depois de cada backup concluído.
- O **administrador da organização** também vê a página Backups dela: faz backup, agenda e restaura. Ele nunca vê os backups da plataforma nem os de outra organização.
- **Pontos de restauração**: cada backup concluído da organização tem **↺ Restaurar**. O worker primeiro salva o estado atual como backup "Antes da restauração"; depois, numa única transação (`psql --single-transaction`), apaga as tabelas atuais da organização e recria as do ponto escolhido, com registros, campos, índices e permissões. Se algo falhar, nada muda. O arquivo precisa ser daquela organização e conter só tabelas dela.
- Na API: `organization_id` em `POST /api/backups` e `POST /api/backup-schedules`; `GET /api/backups?organization_id=…` ou `?all=true`; `POST /api/backups/{id}/restore` e `GET /api/restores`.

### Arquivos e pastas pela API

O administrador da organização gerencia o storage com o **token administrativo** dele, não só pelo painel (tokens de membros continuam sem acesso). O Google Drive é a exceção: conectar exige o navegador, porque o Google pede o login da conta.

| Ação | Rota |
|---|---|
| Conectar S3 / R2 | `POST /api/storages`, `POST /api/integrations/cloudflare/connect` |
| Definir o storage padrão | `POST /api/storages/{id}/default` |
| Listar arquivos | `GET /api/files?storage_id=…&folder_id=…` (ou `&root=true`; `storage_id=default` = o padrão) |
| Enviar arquivo | `POST /api/files` (vai para o padrão; `?storage_id=…` escolhe outro) ou `POST /api/files/{storage_id}`, com `?folder_id=…` (multipart, campo `file`) |
| Dados de um arquivo | `GET /api/files/{id}` (nome, tamanho, pasta, storage e link de download) |
| Renomear / mover arquivo | `PATCH /api/files/{id}` com `name` e/ou `folder_id` (`null` = início) |
| Apagar arquivo | `DELETE /api/files/{id}` |
| Listar pastas | `GET /api/folders?storage_id=…` (todas; ou `parent_id=…`, `root=true`) |
| Criar pasta | `POST /api/folders` com `storage_id`, `name`, `parent_id` |
| Renomear / mover pasta | `PATCH /api/folders/{id}` com `name` e/ou `parent_id` |
| Apagar pasta | `DELETE /api/folders/{id}` (vazia) ou `?recursive=true` (com tudo dentro, inclusive no provedor) |

**Storage padrão:** cada organização (e a plataforma) marca um storage como padrão em Storage e arquivos. Ele recebe os envios que não informam `storage_id` (ou informam `default`). Com um storage só, ele já é o padrão.

**Campos de arquivo:** nas tabelas, o tipo `file` guarda o id de um arquivo enviado. Cada campo pode ter o próprio storage (`storage_id` na criação do campo): por exemplo, `contrato` no Google Drive e `foto` no Cloudflare R2; sem storage, usa o padrão. O SETAPI recusa arquivos de outra organização e, se o campo tiver storage próprio, arquivos de outro storage. No painel, o formulário do registro envia o arquivo direto para o storage do campo. Pela API: envie o arquivo em `POST /api/files?storage_id=…`, depois grave o `id` devolvido no campo.

As pastas são do SETAPI, não do provedor: renomear e mover são instantâneos e iguais para Drive, R2 e S3.

### Mudar uma organização de servidor

Em **Plataforma → Organizações**, **↓ Exportar** gera um pacote `.setapi-org` protegido por uma senha que você escolhe (mínimo 12 caracteres; o SETAPI não a guarda). No servidor novo, **↑ Importar organização** com o pacote e a mesma senha.

- O pacote leva as tabelas com todos os registros, as permissões das tabelas, os usuários com as senhas atuais e a autenticação em duas etapas, as conexões de storage com as credenciais, as pastas, a lista de arquivos e os agendamentos de backup. Tokens de API não vão: crie novos no servidor novo.
- Os arquivos continuam no Drive, R2 ou S3; o servidor novo usa as mesmas conexões.
- Tudo mantém os mesmos ids, então registros, campos de arquivo e usuários continuam ligados. Se a organização, o prefixo das tabelas, algum usuário (id ou e-mail), storage, pasta ou arquivo já existir no destino, nada é importado.
- Diferente do backup, o pacote não depende da `SETAPI_ENCRYPTION_KEY`: abre em qualquer servidor com a senha.
- Importe só pacotes que você mesmo exportou: a importação cria tabelas no banco de dados. O limite de tamanho é `SETAPI_IMPORT_MAX_MB` (4096 por padrão; ajuste também o proxy).
- Na API (administrador global): `POST /api/organizations/{id}/package` (campo `passphrase`) e `POST /api/organizations/import` (campos `file` e `passphrase`).

### Exportar dados

**Exportar dados** (administrador da organização, ou o global dentro dela) baixa todos os registros de todas as tabelas da organização, ou de uma só: **XLSX** (uma aba por tabela), **CSV** (um `.csv` por tabela num `.zip`, UTF-8 com BOM para o Excel) ou **JSON** (um arquivo, tipos preservados). Na API: `GET /api/export?format=xlsx|csv|json&table=…`.

### Pesquisa nos registros

`GET /api/data/<tabela>?search=texto` procura o texto em todos os campos legíveis (ou só em `search_field`), sem diferenciar maiúsculas, acentos e símbolos: `sao-paulo!` encontra "São Paulo". O painel mostra a pesquisa, de 1 a 100 registros por página e a navegação acima e abaixo da lista.

### Google Drive: conectar com Google

No painel, escolha a organização no seletor e abra **Storage e arquivos → Conectar storage → Google Drive → Conectar Google** (para os backups da plataforma: **Plataforma → Backups → Conectar destino**). O Google abre na hora: quem não está logado informa e-mail e senha; quem já está logado vai direto para a tela de autorização. Ao voltar, o SETAPI cria uma pasta exclusiva, salva o refresh token criptografado e deixa a conexão disponível para arquivos e backups.

Cada organização conecta o **próprio** Drive: o usuário de painel da organização entra no SETAPI, abre **Storage e arquivos** e clica em Conectar Google com a conta dele. A conexão, a pasta e os arquivos ficam vinculados à organização; as outras não enxergam nem usam. O administrador global vê as conexões de cada organização ao selecioná-la. Backups do banco vão somente para os destinos da plataforma (página Backups, do administrador global), porque contêm os dados de todas as organizações.

Para o botão funcionar, o administrador global registra o aplicativo Google **uma única vez**, em **Plataforma → Configurações → Aplicativo Google**:

1. No Google Cloud, crie um projeto e ative a API Google Drive.
2. Configure a tela de consentimento OAuth.
3. Crie credenciais OAuth do tipo **Aplicativo da Web** com a URI de redirecionamento `https://SEU_DOMINIO/api/integrations/google/callback`.
4. Baixe o JSON das credenciais e escolha-o em **Configurar Google** (ou cole o Client ID e o Client Secret). O arquivo é lido só no navegador; o Client Secret fica criptografado no banco.

Alternativa: defina `SETAPI_GOOGLE_CLIENT_ID` e `SETAPI_GOOGLE_CLIENT_SECRET` e reinicie o serviço. O que estiver salvo no painel vale no lugar dessas variáveis.

`SETAPI_PUBLIC_URL` precisa corresponder ao domínio HTTPS utilizado. Esse cadastro identifica o SETAPI perante o Google; o repositório não inclui credenciais de um aplicativo Google compartilhado. Consulte a [documentação oficial do OAuth](https://developers.google.com/identity/protocols/oauth2/web-server).

A integração usa o escopo `drive.file`, limitado aos arquivos autorizados/criados pelo aplicativo, e cria uma pasta nova em cada conexão. Não solicita acesso irrestrito ao Drive nem oferece seleção de pastas existentes. O botão **Reconectar com Google** preserva a pasta e exige acesso à pasta original. Conexões antigas criadas manualmente podem precisar de uma nova conexão se a pasta anterior não estiver acessível a esse escopo.

O Google pode expirar refresh tokens de aplicativos externos em modo Testing após sete dias. Para uso contínuo, ajuste o status de publicação e os requisitos de consentimento do seu projeto conforme a documentação Google. O fluxo usa state de uso único com validade de dez minutos, vinculado à sessão administrativa, além de PKCE. Tokens e códigos não são exibidos pelo painel; os logs de acesso do Uvicorn estão desativados para não registrar o código no callback. Configure também seu proxy para não registrar parâmetros desse endpoint.

### Cloudflare R2 só com o token

Em **Conectar storage → Cloudflare R2 · só com o token** (a única opção de R2 no painel; conexões antigas com chaves manuais continuam funcionando), cole um token de API da conta com a permissão *Workers R2 Storage Write* (no painel da Cloudflare: R2 → Gerenciar tokens de API). O SETAPI descobre a conta, cria o bucket (`setapi-<organização>-xxxxxx`, ou usa o que você informar) e deriva as chaves S3 do token, como a Cloudflare documenta: Access Key ID é o id do token e Secret Access Key é o SHA-256 do valor. O token em si não é guardado. Na API: `POST /api/integrations/cloudflare/connect`.

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
