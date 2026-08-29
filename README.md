# Oracle A1 Capacity Monitor

Monitor de disponibilidade para instâncias Oracle Cloud.

## Funcionalidades

- consulta disponibilidade de capacidade;
- suporta configuração principal e fallback;
- executa automaticamente pelo GitHub Actions;
- envia notificação quando encontra capacidade;
- não cria ou modifica instâncias;
- não altera volumes ou outros recursos OCI.

## Segurança

Todas as credenciais OCI são armazenadas utilizando GitHub Actions Secrets.

Nenhuma credencial, chave privada ou identificador da tenancy deve ser
armazenado neste repositório.

## Configuração

Configure os secrets necessários através de:

`Settings → Secrets and variables → Actions`

## Execução

O monitor pode ser executado manualmente através do GitHub Actions ou
automaticamente pelo workflow agendado.

## License

Uso pessoal.