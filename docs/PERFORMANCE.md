# Performance medida

Execução local em 25/09/2026, antes da adição do cadastro de organizações (já incluía políticas de proprietário/tenant/campos): quatro CPUs disponíveis, aproximadamente 8 GB de RAM total, máquina compartilhada com outros programas e swap já em uso. API, PostgreSQL, Redis, worker e gerador de carga na mesma máquina. **Não é benchmark do servidor do usuário nem comparação com Directus.**

Cenário: 10.000 registros iniciais, 200 contas individuais, isolamento por proprietário/tenant/campos, páginas de 20 registros sem contagem total; aproximadamente 80% leituras e 20% inserções. Uma API, pool máximo 20, mais um worker. Cada cenário executou por cerca de 30 segundos, além do encerramento das requisições em voo e das pausas dos clientes.

| Clientes + WebSockets | Pausa por cliente | Requisições/s | Mediana | p95 | p99 | Erros HTTP |
|---|---|---|---|---|---|---|
| 100 | 2 s | 46.44 | 17.69 ms | 541.31 ms | 973.97 ms | 0 |
| 200 | 2 s | 90.59 | 24.24 ms | 785.05 ms | 1521.28 ms | 0 |
| 100 | 0 s | 96.36 | 571.52 ms | 3496.05 ms | 5050.48 ms | 0 |

API e worker permaneceram em execução; nenhum erro WebSocket foi registrado. Pico RSS observado: API 124.1 MiB e worker 74.5 MiB. Esses números **não incluem PostgreSQL, Redis, gerador de carga nem memória total da stack**.

A carga contínua sem pausas elevou o p95 para segundos. Isso impede prometer latência baixa sob saturação. O ensaio não inclui tempestade de login/Argon2, uploads, downloads, consultas complexas, datasets grandes, backups simultâneos, rede pública, longos períodos ou reinícios. Sessões foram pré-emitidas. Repetir com tráfego real e durante pelo menos uma hora é parte da homologação.

Durante o desenvolvimento, o primeiro ensaio mostrou fanout excessivo no tempo real: todos os sockets examinavam eventos de todas as pessoas. A implementação final publica canais específicos por tabela/proprietário/tenant e mantém a autorização na entrega. Os ensaios intermediários tiveram caudas de latência maiores e alguns erros de transporte; os números acima são da versão com canais separados, não uma média seletiva das execuções.

## Reproduzir sem atingir produção

O script recusa qualquer banco que não seja `setapi_load_test`, recusa host remoto e exige Redis local dedicado `/13`. Ele apaga os schemas desse banco de teste e limpa esse banco Redis; nunca aponte outros serviços a esses recursos. O PostgreSQL e Redis devem estar iniciados.

```bash
export DATABASE_URL=postgresql://USUARIO:SENHA@127.0.0.1:5432/setapi_load_test
export REDIS_URL=redis://127.0.0.1:6379/13
python scripts/load_test.py --reset-test-database --seconds 60 --output /tmp/setapi-load-report.json
```

## Diagnosticar o Directus atual

O relato de travamento com aproximadamente 100 alunos e recuperação após reinício não identifica a causa. Verifique logs antes de reiniciar, eventos OOM/exit code 137, RSS/limite de memória do contêiner, CPU, conexões/esperas PostgreSQL, consultas lentas, locks e uso de Redis. Registre versão, quantidade de requisições por segundo e tipo de operação. Não há evidência aqui de que o Studio seja a causa.

Para o SETAPI, `/api/status` (administrador) expõe estatísticas do pool e fila de eventos. Monitore essas métricas junto aos recursos dos contêineres. Para 100 alunos fazendo uma requisição a cada cinco segundos, a média teórica é 20 requisições/s; picos sincronizados, polling, arquivos e consultas caras podem mudar completamente esse cenário.
