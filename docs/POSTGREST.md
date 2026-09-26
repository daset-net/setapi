# PostgREST no SETAPI

O SETAPI inclui PostgREST **16.4**, com binário oficial fixado por SHA-256, para
consultas de listagem e leitura por ID em `/api/data`. O painel e os aplicativos
continuam usando as mesmas rotas, cookies, tokens opacos e respostas.

```
Aplicativo/painel → SETAPI (autenticação, limites, campos)
                 → PostgREST privado → PostgreSQL (RLS)
```

## Instalação e configuração

Docker e os novos templates EasyPanel habilitam `SETAPI_READ_ENGINE=postgrest`.
Permanecem três serviços: `setapi_app`, `setapi_db`, `setapi_redis`.
API, worker e PostgREST são supervisionados dentro de `setapi_app`; a falha de
qualquer processo encerra os demais para o orquestrador reiniciar o serviço.
As portas 3000 (dados) e 3001 (saúde) escutam somente em **127.0.0.1**; a única
porta publicada continua sendo 8055. Não configure domínio/proxy para 3000.

`SETAPI_POSTGREST_THREADS=1` limita o runtime Haskell a uma capacidade de CPU;
ajuste somente após medir sua carga. O keep-alive HTTP da API é de 15 segundos.

`SETAPI_POSTGREST_POOL=10` controla o pool adicional de leitura. Some esse pool
aos pools da API e do worker ao dimensionar `max_connections` do PostgreSQL.
`SETAPI_DB_POOL_MAX=20` continua controlando cada pool Python.

O bootstrap exige que a conexão administrativa possa criar roles e conceder
permissões. Ele cria roles distintas por banco: autenticador com LOGIN/NOINHERIT
e leitor NOLOGIN, ambos NOSUPERUSER/NOBYPASSRLS/NOCREATEROLE/NOCREATEDB.
O leitor recebe SELECT somente nas tabelas gerenciadas. O autenticador não usa
as credenciais administrativas. Senha interna e chave JWT são derivadas com
HMAC e contextos distintos de `SETAPI_ENCRYPTION_KEY`; nenhum novo segredo
precisa ser copiado manualmente. Não compartilhe essa chave entre instalações.

A restauração offline omite somente as políticas RLS geradas pelo SETAPI no
arquivo de seleção do `pg_restore`; o bootstrap as recria com os papéis do banco
de destino. RLS permanece habilitado durante esse intervalo, sem conceder leitura
aos papéis restritos. Políticas SQL personalizadas podem exigir papéis externos.

O bootstrap também habilita RLS nas tabelas gerenciadas e instala políticas
restritivas por organização/proprietário. Conexões SQL externas que não sejam
donas das tabelas podem precisar de suas próprias políticas RLS. Faça backup
antes de atualizar uma instalação existente.

Alterações de esquema notificam o cache do PostgREST. Consultas a tabelas/colunas
recém-criadas têm uma pequena janela de atualização; o gateway repete somente
leituras rejeitadas por cache desatualizado, com limite de tentativas.

## Fronteira de segurança

- O navegador nunca recebe os JWTs internos. Cada JWT dura 30 segundos e é
  vinculado ao usuário, token original, organização, tabela e política vigente.
- Antes de cada consulta, uma função de validação verifica no PostgreSQL se o
  token continua válido, usuário/organização ativos e permissões atuais.
  Alterações de política invalidam requisições com a política anterior.
- RLS restringe as linhas mesmo se os filtros de organização forem omitidos.
  Filtros adicionais do gateway ajudam o planejador a usar índices.
- Permissões **por coluna** continuam sendo aplicadas pelo gateway SETAPI.
  O leitor interno possui SELECT nas colunas da tabela: a porta privada e os
  JWTs internos não são uma API pública e não devem ser distribuídos.
- Não há papel anônimo. Escritas e RPC pelo PostgREST são bloqueadas.
- Erros SQL/upstream não são repassados ao cliente. Respostas mantêm o limite
  de 200 registros e 2 MiB; tempo de consulta é limitado a 15 segundos.
- Falha do PostgREST retorna 503; não ocorre fallback silencioso para conexão
  administrativa.

## Gravações e reversão

Criação, atualização e exclusão de registros permanecem no caminho transacional
Python, com as verificações existentes de organização, proprietário, campos,
referências, auditoria e outbox para WebSocket. Administração, autenticação,
storage e backups também permanecem no SETAPI. **Esta integração não significa
que todas as operações da aplicação já executam sob RLS.** O caminho de escrita
ainda usa a conexão administrativa; reduzir seus privilégios é trabalho separado.

Para reversão operacional explícita, use `SETAPI_READ_ENGINE=native` e reinicie.
Isso preserva dados e controles da API, mas as leituras voltam à conexão Python
privilegiada. As políticas RLS permanecem instaladas. Nunca desative a autenticação
para contornar uma falha do motor.

O PostgREST não garante aumento de capacidade. Consulte `PERFORMANCE.md` e os
relatórios comparativos; o gateway mantém controles que também têm custo.
Não extrapole testes locais curtos para o número de alunos em produção.

Referências: https://docs.postgrest.org/en/stable/references/auth.html e
https://docs.postgrest.org/en/stable/references/configuration.html.
