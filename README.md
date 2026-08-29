# Oracle A1 Capacity Monitor

Monitor somente-leitura operacional para disponibilidade de `VM.Standard.A1.Flex` via OCI Compute Capacity Report.

## O que ele faz

- consulta 2 OCPU / 12 GB;
- consulta 1 OCPU / 6 GB como fallback;
- não cria VM;
- não inicia/paralisa instâncias;
- não toca em boot volumes, backups ou rede;
- envia push via ntfy quando houver capacidade;
- roda manualmente ou por GitHub Actions.

## Política OCI mínima

Crie um grupo dedicado `A1CapacityMonitorGroup`, adicione um usuário dedicado e aplique:

```text
Allow group A1CapacityMonitorGroup to manage compute-capacity-reports in tenancy
```

Para o resource type `compute-capacity-reports`, `manage` concede a operação necessária para `CreateComputeCapacityReport`.

## GitHub Secrets

Crie os seguintes Repository Secrets:

- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_PRIVATE_KEY`
- `OCI_REGION`
- `OCI_AVAILABILITY_DOMAIN`
- `NTFY_TOPIC`

Use o OCID da tenancy também como root compartment. Copie o Availability Domain exatamente como aparece no OCI Console no volume que precisa ser recuperado.

## ntfy

Instale o app ntfy no celular, gere um tópico longo e imprevisível e assine o tópico no app.

Exemplo local para gerar nome de tópico:

```bash
printf 'oracle-a1-%s\n' "$(openssl rand -hex 20)"
```

Salve somente o valor resultante no secret `NTFY_TOPIC`.

## Frequência e custo do GitHub Actions

O workflow entregue roda a cada 5 minutos. Use essa frequência preferencialmente em um repositório **público**, pois runners padrão em repositórios públicos são gratuitos.

Se optar por repositório **privado**, altere o cron para 30 minutos para reduzir consumo de minutos:

```yaml
- cron: "7,37 * * * *"
```

Isso dá cerca de 1.440 execuções por mês. Jobs privados são arredondados para o minuto seguinte e competem com a franquia da conta.

## Primeiro teste

1. Faça push dos arquivos para o branch padrão.
2. Abra `Actions` no GitHub.
3. Selecione `Oracle A1 capacity monitor`.
4. Clique `Run workflow`.
5. Confira o JSON do passo `Check Oracle A1 capacity`.
6. Faça um teste do ntfy separadamente se desejar.

## Observação importante

`AVAILABLE` é um sinal de capacidade de host no momento da consulta. Ele não reserva capacidade e não garante que ela continuará disponível até você abrir o OCI Console. O monitor deliberadamente não faz launch automático.
