# Segurança — SETAPI 0.2

Esta versão incorpora controles e testes de regressão. Não é uma certificação de segurança, pentest externo ou promessa de risco zero.

## Fronteiras de confiança

- Alunos usam contas `audience=app` e tokens individuais; nunca recebem credenciais SQL, Redis, storage ou tokens administrativos.
- Usuários administrativos controlam estrutura, políticas, credenciais e dados. Trate seus tokens como segredos de alto privilégio.
- Um token de serviço com escopos, criado por um administrador, permanece compatível com integrações antigas: sem política na tabela, seus escopos determinam acesso. Com política, também se aplicam as restrições. Membros sem política são bloqueados.
- Políticas limitam campos, proprietário e tenant. As restrições de proprietário e tenant se somam. O servidor atribui esses campos e impede substituição por membros. Relações escritas por membros exigem acesso ao registro referenciado.
- As regras são aplicadas pela API. Quem recebe acesso SQL direto está fora dessa fronteira: não entregue credenciais SQL aos alunos. O runtime deve utilizar conta PostgreSQL dedicada, sem SUPERUSER/CREATEROLE/REPLICATION, com propriedade dos schemas SETAPI necessários. O usuário criado automaticamente por uma imagem PostgreSQL pode ter privilégios administrativos; revise isso no servidor antes de produção.

## Autenticação e recuperação

Senhas Argon2id, tokens aleatórios armazenados por SHA-256, revogação consultada em cada requisição. A verificação de senhas possui limite de concorrência para evitar consumo ilimitado de memória. Sessões duram 12 horas. Troca/reset de senha revoga sessões, tokens e links pendentes. Mudanças de permissões/tenant também revogam os tokens do usuário.

MFA usa TOTP e dez códigos de recuperação, guardados por hash e consumidos uma única vez. Um passo TOTP não pode ser reutilizado. Habilitar MFA revoga os outros tokens da conta. Reset de senha exige MFA se habilitado; e-mail sozinho não remove a segunda etapa.

Recuperação responde igualmente para e-mails existentes e desconhecidos, inclui atraso mínimo com variação e possui limites por IP/conta. Links expiram após 30 minutos e são usados uma única vez. São enviados na URL como fragmento, removido pelo painel, sem passar pelos logs HTTP. Mensagens ficam criptografadas na fila. SMTP exige TLS com validação de certificado. Testes usam transporte simulado, sem envio real.

Configure `SETAPI_SMTP_HOST`, `SETAPI_SMTP_PORT`, `SETAPI_SMTP_FROM`, `SETAPI_SMTP_USER`, `SETAPI_SMTP_PASSWORD` no serviço app. Cadastro público permanece desativado até `SETAPI_ALLOW_REGISTRATION=true`; contas novas precisam confirmar e-mail e começam sem acesso a tabelas. O administrador atribui escopos e tenant pelo painel.

## Proteções de API

- Cookie HttpOnly, Secure em HTTPS e SameSite=Lax; operações com cookie exigem origem exata e cabeçalho CSRF. Lax permite callback OAuth; state está vinculado à sessão e tem uso único, com PKCE.
- Documentação interativa e OpenAPI exigem administrador. Endpoints públicos intencionais: página de login/assets, verificações de saúde, login/registro/verificação/recuperação. Dados, usuários, arquivos e configurações não são públicos.
- Erros de validação não repetem o conteúdo enviado, evitando eco de senhas/tokens. Erros do banco não expõem SQL, credenciais ou mensagens internas.
- Valores SQL são parametrizados e identificadores validados. Campos novos não entram automaticamente nas listas de uma política existente.
- Registros têm limite de escrita de 64 KiB; páginas têm limite de 2 MiB e leitura por cursor. Limites de corpo, paginação, espera do pool e tamanho de mensagens WebSocket reduzem pressão de memória.
- Contagem, filtros, ordenação, leitura, escrita e exclusão respeitam a política. WebSockets usam canais por tabela/proprietário/tenant; antes da entrega, autenticam novamente e verificam a política atual. Metadados internos de roteamento não saem para o navegador.
- Uploads e exclusões de arquivos são administrativos. Falha em registrar um upload tenta remover o objeto órfão. Falha de compensação é registrada sem segredo e exige reconciliação no provedor.

## Limites de carga

Por processo: pool PostgreSQL de até 20 conexões, fila de até 100 esperas, timeout de cinco segundos; máximo de 1000 WebSockets e dez por usuário. API e worker possuem pools separados. Login: 200 tentativas/IP e 20/conta a cada cinco minutos. API: 1200/minuto por usuário e 10000/minuto por IP. WebSocket: 1000 aberturas/IP por minuto.

Esses limites não substituem proteção DDoS. Configure proxies confiáveis em `FORWARDED_ALLOW_IPS`: sem isso, alunos atrás do proxy podem compartilhar o mesmo limite de IP; confiar em qualquer origem permite forjar o IP. O instalador não adivinha o endereço do proxy do seu servidor.

## Storage e rede

Credenciais e backups são criptografados. Endpoints S3 personalizados exigem HTTPS/443, host autorizado em `SETAPI_STORAGE_HOSTS` e resolução para endereços públicos. AWS/R2 são reconhecidos. Redirecionamentos e resolução DNS são também responsabilidade do SDK/rede: a validação inicial não substitui controle de egress contra DNS rebinding. Bloqueie destinos internos e metadados de nuvem na rede do contêiner, preservando apenas conexões explicitamente necessárias ao banco/Redis/SMTP.

Use buckets privados e credenciais restritas ao bucket/pasta necessário. Configure o aplicativo OAuth Google e teste autorização/upload/backup com sua conta real. Segredos reais não foram incluídos no repositório.

## Evidências e pendências de implantação

- Testes integrados: isolamento entre alunos/tenants, campos, relacionamentos, revogação, OAuth, MFA/replay/recuperação, registro/SMTP simulado, SQL transacional, eventos e backup/restauração em PostgreSQL real.
- Teste de navegador: formulários, permissões, MFA inicial, Google e layout móvel.
- `pip-audit`: 40 dependências do lock examinadas; nenhuma vulnerabilidade conhecida encontrada na execução de 25/09/2026. Isso depende da cobertura e atualização da base.
- Bandit: nenhum achado HIGH; dois MEDIUM revisados (bind `0.0.0.0` necessário no contêiner e healthcheck com URL HTTP fixa em localhost); avisos LOW sobre subprocessos e nome da variável PGPASSWORD. Comandos usam argumentos separados, sem shell, e executáveis da imagem. O PATH do contêiner não deve conter diretórios graváveis por terceiros.

Ainda exigem configuração/validação real: HTTPS/proxy/firewall, privilégios PostgreSQL, limites de contêiner, SMTP, OAuth e permissões dos provedores; teste de restauração operacional; testes prolongados com consultas/dados do aplicativo; revisão independente. Rotação da chave de criptografia exige procedimento de manutenção e preservação das chaves dos backups antigos. Não faça migração de produção com base apenas no teste curto de carga.

Referências usadas na revisão: [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html), [Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html), [SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).
