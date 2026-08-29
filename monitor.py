#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import oci

SHAPE = "VM.Standard.A1.Flex"
TARGETS = [
    {"name": "principal", "ocpus": 2.0, "memory_gbs": 12.0},
    {"name": "fallback", "ocpus": 1.0, "memory_gbs": 6.0},
]


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value.strip()


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def create_compute_client():
    tenancy = required_env("OCI_TENANCY_OCID")
    user = required_env("OCI_USER_OCID")
    fingerprint = required_env("OCI_FINGERPRINT")
    region = required_env("OCI_REGION")
    private_key = required_env("OCI_PRIVATE_KEY")

    # Aceita tanto uma chave PEM multiline real quanto um secret com \n literais.
    private_key = private_key.replace("\\n", "\n")

    key_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="oci_api_key_", suffix=".pem", delete=False
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
    client = oci.core.ComputeClient(
        config,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    )
    return client, tenancy, key_path


def build_report_details(tenancy: str, availability_domain: str):
    requests = []
    for target in TARGETS:
        requests.append(
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE,
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=target["ocpus"],
                    memory_in_gbs=target["memory_gbs"],
                ),
            )
        )

    return oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=tenancy,  # Para capacity report, a Oracle exige root compartment.
        availability_domain=availability_domain,
        shape_availabilities=requests,
    )


def config_key(item):
    cfg = item.instance_shape_config
    ocpus = float(cfg.ocpus) if cfg and cfg.ocpus is not None else None
    memory = float(cfg.memory_in_gbs) if cfg and cfg.memory_in_gbs is not None else None
    return (ocpus, memory)


def summarize(report):
    grouped = defaultdict(list)
    for item in report.shape_availabilities or []:
        grouped[config_key(item)].append(item)

    summary = []
    for target in TARGETS:
        key = (target["ocpus"], target["memory_gbs"])
        matches = grouped.get(key, [])

        statuses = sorted({getattr(x, "availability_status", None) or "UNKNOWN" for x in matches})
        available_items = [
            x for x in matches if getattr(x, "availability_status", None) == "AVAILABLE"
        ]
        available_count = sum((getattr(x, "available_count", None) or 0) for x in available_items)

        # AVAILABLE já é o indicador oficial. available_count pode variar conforme FD/serviço.
        available = bool(available_items)

        summary.append(
            {
                **target,
                "available": available,
                "available_count": available_count,
                "statuses": statuses or ["NO_RESULT"],
                "fault_domains": [
                    {
                        "fault_domain": getattr(x, "fault_domain", None),
                        "status": getattr(x, "availability_status", None),
                        "available_count": getattr(x, "available_count", None),
                    }
                    for x in matches
                ],
            }
        )
    return summary


def notify_ntfy(topic: str, title: str, message: str, priority: str = "high"):
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "cloud,computer,warning",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy retornou HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha ao enviar notificação ntfy: {exc}") from exc


def main():
    availability_domain = required_env("OCI_AVAILABILITY_DOMAIN")
    notify_fallback = bool_env("NOTIFY_FALLBACK", default=True)
    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()

    client = None
    key_path = None
    try:
        client, tenancy, key_path = create_compute_client()
        details = build_report_details(tenancy, availability_domain)
        response = client.create_compute_capacity_report(details)
        summary = summarize(response.data)
    finally:
        if key_path:
            try:
                os.remove(key_path)
            except OSError:
                pass

    now = datetime.now(timezone.utc).isoformat()
    output = {
        "checked_at_utc": now,
        "shape": SHAPE,
        "availability_domain": availability_domain,
        "targets": summary,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))

    primary = next(x for x in summary if x["name"] == "principal")
    fallback = next(x for x in summary if x["name"] == "fallback")

    if primary["available"]:
        message = (
            f"Capacidade detectada para {SHAPE}: 2 OCPU / 12 GB.\n"
            f"AD: {availability_domain}\n"
            f"Status fallback 1/6: {'AVAILABLE' if fallback['available'] else ','.join(fallback['statuses'])}\n"
            "Entre agora no OCI Console e tente criar a VM manualmente. O monitor NÃO cria nenhuma instância."
        )
        print("ALERTA: capacidade principal disponível.")
        if ntfy_topic:
            notify_ntfy(
                ntfy_topic,
                "Oracle A1 2 OCPU / 12 GB disponível",
                message,
                priority="urgent",
            )
        return 0

    if fallback["available"] and notify_fallback:
        message = (
            f"Ainda não há 2 OCPU / 12 GB, mas há indicação de capacidade para "
            f"{SHAPE} com 1 OCPU / 6 GB.\n"
            f"AD: {availability_domain}\n"
            "Use apenas se quiser a alternativa temporária. O monitor NÃO cria nenhuma instância."
        )
        print("ALERTA: capacidade fallback disponível.")
        if ntfy_topic:
            notify_ntfy(
                ntfy_topic,
                "Oracle A1 fallback 1 OCPU / 6 GB disponível",
                message,
                priority="high",
            )
        return 0

    print("Sem capacidade A1 para as configurações monitoradas nesta verificação.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
