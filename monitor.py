#!/usr/bin/env python3

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import oci


SHAPE = "VM.Standard.A1.Flex"

TARGETS = [
    {
        "name": "principal",
        "ocpus": 2.0,
        "memory_gbs": 12.0,
    },
    {
        "name": "fallback",
        "ocpus": 1.0,
        "memory_gbs": 6.0,
    },
]


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Variável obrigatória ausente: {name}"
        )

    return value.strip()


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {
        "1",
        "true",
        "yes",
        "sim",
        "on",
    }


def create_oci_clients():
    tenancy = required_env("OCI_TENANCY_OCID")
    user = required_env("OCI_USER_OCID")
    fingerprint = required_env("OCI_FINGERPRINT")
    region = required_env("OCI_REGION")
    private_key = required_env("OCI_PRIVATE_KEY")

    # Aceita chave PEM multiline e também secret contendo \n literais.
    private_key = private_key.replace("\\n", "\n")

    key_file = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="oci_api_key_",
        suffix=".pem",
        delete=False,
    )

    try:
        key_file.write(private_key)
        key_file.flush()
        key_path = key_file.name
    finally:
        key_file.close()

    os.chmod(key_path, 0o600)

    config = {
        "tenancy": tenancy,
        "user": user,
        "fingerprint": fingerprint,
        "key_file": key_path,
        "region": region,
    }

    oci.config.validate_config(config)

    compute_client = oci.core.ComputeClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )

    identity_client = oci.identity.IdentityClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )

    return (
        compute_client,
        identity_client,
        tenancy,
        key_path,
    )


def short_ad_name(full_name: str) -> str:
    """
    Exemplo:
    xxxxx:US-ASHBURN-AD-2 -> AD-2

    Evita expor no log público o nome completo
    específico da tenancy.
    """

    match = re.search(
        r"(AD-\d+)$",
        full_name,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1).upper()

    return "AD"


def list_availability_domains(
    identity_client,
    tenancy: str,
):
    response = identity_client.list_availability_domains(
        compartment_id=tenancy
    )

    domains = [
        ad.name
        for ad in (response.data or [])
        if getattr(ad, "name", None)
    ]

    if not domains:
        raise RuntimeError(
            "A OCI não retornou nenhum Availability Domain."
        )

    return sorted(
        domains,
        key=short_ad_name,
    )


def build_report_details(
    tenancy: str,
    availability_domain: str,
):
    requests = []

    for target in TARGETS:
        requests.append(
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE,
                instance_shape_config=(
                    oci.core.models.CapacityReportInstanceShapeConfig(
                        ocpus=target["ocpus"],
                        memory_in_gbs=target["memory_gbs"],
                    )
                ),
            )
        )

    return oci.core.models.CreateComputeCapacityReportDetails(
        # A Oracle exige o root compartment
        # para Compute Capacity Report.
        compartment_id=tenancy,
        availability_domain=availability_domain,
        shape_availabilities=requests,
    )


def config_key(item):
    cfg = item.instance_shape_config

    ocpus = (
        float(cfg.ocpus)
        if cfg and cfg.ocpus is not None
        else None
    )

    memory = (
        float(cfg.memory_in_gbs)
        if cfg and cfg.memory_in_gbs is not None
        else None
    )

    return (
        ocpus,
        memory,
    )


def summarize(report):
    grouped = defaultdict(list)

    for item in report.shape_availabilities or []:
        grouped[config_key(item)].append(item)

    summary = []

    for target in TARGETS:
        key = (
            target["ocpus"],
            target["memory_gbs"],
        )

        matches = grouped.get(
            key,
            [],
        )

        statuses = sorted(
            {
                getattr(
                    item,
                    "availability_status",
                    None,
                )
                or "UNKNOWN"
                for item in matches
            }
        )

        available_items = [
            item
            for item in matches
            if getattr(
                item,
                "availability_status",
                None,
            )
            == "AVAILABLE"
        ]

        available_count = sum(
            (
                getattr(
                    item,
                    "available_count",
                    None,
                )
                or 0
            )
            for item in available_items
        )

        summary.append(
            {
                **target,
                "available": bool(
                    available_items
                ),
                "available_count": (
                    available_count
                ),
                "statuses": (
                    statuses
                    or ["NO_RESULT"]
                ),
                "fault_domains": [
                    {
                        "fault_domain": getattr(
                            item,
                            "fault_domain",
                            None,
                        ),
                        "status": getattr(
                            item,
                            "availability_status",
                            None,
                        ),
                        "available_count": getattr(
                            item,
                            "available_count",
                            None,
                        ),
                    }
                    for item in matches
                ],
            }
        )

    return summary


def target_from(
    result,
    name: str,
):
    return next(
        item
        for item in result["targets"]
        if item["name"] == name
    )


def notify_ntfy(
    topic: str,
    title: str,
    message: str,
    priority: str = "high",
):
    url = f"https://ntfy.sh/{topic}"

    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": (
                "cloud,computer,warning"
            ),
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            if response.status >= 300:
                raise RuntimeError(
                    "ntfy retornou HTTP "
                    f"{response.status}"
                )

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Falha ao enviar "
            f"notificação ntfy: {exc}"
        ) from exc


def main():
    notify_fallback = bool_env(
        "NOTIFY_FALLBACK",
        default=True,
    )

    ntfy_topic = os.getenv(
        "NTFY_TOPIC",
        "",
    ).strip()

    key_path = None
    results = []
    errors = []

    try:
        (
            compute_client,
            identity_client,
            tenancy,
            key_path,
        ) = create_oci_clients()

        availability_domains = (
            list_availability_domains(
                identity_client,
                tenancy,
            )
        )

        for availability_domain in (
            availability_domains
        ):
            ad = short_ad_name(
                availability_domain
            )

            try:
                details = (
                    build_report_details(
                        tenancy,
                        availability_domain,
                    )
                )

                response = (
                    compute_client
                    .create_compute_capacity_report(
                        details
                    )
                )

                summary = summarize(
                    response.data
                )

                results.append(
                    {
                        "ad": ad,
                        "targets": summary,
                    }
                )

            except oci.exceptions.ServiceError as exc:
                # Não deixa uma falha isolada em um AD
                # impedir a consulta dos demais.
                errors.append(
                    {
                        "ad": ad,
                        "http_status": (
                            exc.status
                        ),
                        "code": exc.code,
                    }
                )

    finally:
        if key_path:
            try:
                os.remove(key_path)
            except OSError:
                pass

    if not results:
        raise RuntimeError(
            "Não foi possível consultar "
            "nenhum Availability Domain. "
            f"Erros: {errors}"
        )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    output = {
        "checked_at_utc": now,
        "shape": SHAPE,
        "availability_domains_checked": (
            len(results)
        ),
        "results": results,
    }

    if errors:
        output["errors"] = errors

    print(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False,
        )
    )

    primary_available = [
        result
        for result in results
        if target_from(
            result,
            "principal",
        )["available"]
    ]

    fallback_available = [
        result
        for result in results
        if target_from(
            result,
            "fallback",
        )["available"]
    ]

    if primary_available:
        ads = ", ".join(
            result["ad"]
            for result
            in primary_available
        )

        message = (
            f"Capacidade detectada para "
            f"{SHAPE}: "
            "2 OCPU / 12 GB.\n"
            f"Disponível em: {ads}\n"
            "Entre agora no OCI Console "
            "e tente criar a VM manualmente. "
            "O monitor NÃO cria "
            "nenhuma instância."
        )

        print(
            "ALERTA: capacidade principal "
            f"disponível em {ads}."
        )

        if ntfy_topic:
            notify_ntfy(
                ntfy_topic,
                (
                    "Oracle A1 "
                    "2 OCPU / 12 GB "
                    "disponível"
                ),
                message,
                priority="urgent",
            )

        return 0

    if (
        fallback_available
        and notify_fallback
    ):
        ads = ", ".join(
            result["ad"]
            for result
            in fallback_available
        )

        message = (
            "Ainda não há "
            "2 OCPU / 12 GB, "
            "mas há capacidade "
            f"indicada para {SHAPE} "
            "com 1 OCPU / 6 GB.\n"
            f"Disponível em: {ads}\n"
            "Use apenas se quiser "
            "a alternativa temporária. "
            "O monitor NÃO cria "
            "nenhuma instância."
        )

        print(
            "ALERTA: capacidade fallback "
            f"disponível em {ads}."
        )

        if ntfy_topic:
            notify_ntfy(
                ntfy_topic,
                (
                    "Oracle A1 fallback "
                    "1 OCPU / 6 GB "
                    "disponível"
                ),
                message,
                priority="high",
            )

        return 0

    print(
        "Sem capacidade A1 para as "
        "configurações monitoradas "
        "nesta verificação."
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except Exception as exc:
        print(
            f"ERRO: "
            f"{type(exc).__name__}: "
            f"{exc}",
            file=sys.stderr,
        )

        sys.exit(1)