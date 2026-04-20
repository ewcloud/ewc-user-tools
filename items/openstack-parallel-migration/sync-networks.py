#!/usr/bin/env python3
#
# License: MIT
# Copyright (c) 2026 EUMETSAT
# See the LICENSE file for more details

"""
sync-networks.py

Sync tenant networks/subnets and routers from a source OpenStack cloud/project
to a target OpenStack cloud/project using clouds.yaml.

Behavior:
- Syncs regular tenant networks/subnets. Ports synced when instance is created.
- Skips any network whose name contains 'sfs'
- Skips sync external/shared networks
- Syncs routers but skips 'sfs' networks
- Skips router creation if an equivalent router already exists in target
- If same router name exists but differs, creates a renamed router with suffix
- If target policy forbids setting external_gateway_info during router create,
  retries router creation without external gateway
- Connects target routers to the same target internal subnets/networks
  as the source routers were connected to
- Connects target routers to the mapped target external network if the
  source router had an external gateway and the target external network exists
- Sets gateway to the matching target external network using
  set_gateway_to_network(), if the source router had an external network
- Writes JSON and CSV reports
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import openstack
from openstack.exceptions import SDKException


LOG = logging.getLogger("network-sync")


# ---------------------------------------------------------------------------
# CLI / logging
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync OpenStack tenant networks/subnets and routers between two clouds/projects"
    )
    parser.add_argument("--source", required=True, help="Source cloud name in clouds.yaml")
    parser.add_argument("--target", required=True, help="Target cloud name in clouds.yaml")
    parser.add_argument("--network", action="append", dest="networks", help="Only sync this network name (repeatable)")
    parser.add_argument("--router", action="append", dest="routers", help="Only sync this router name (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created/attached, but do not create anything")
    parser.add_argument("--report-json", default="network-sync-report.json", help="Path to JSON report output")
    parser.add_argument("--report-csv", default="network-sync-report.csv", help="Path to CSV report output")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def connect_cloud(cloud_name: str):
    try:
        conn = openstack.connect(cloud=cloud_name)
        list(conn.network.networks(limit=1))
        return conn
    except Exception as exc:
        raise RuntimeError(f"Failed to connect to cloud '{cloud_name}': {exc}") from exc


def get_network_extensions(conn) -> set[str]:
    try:
        return set(conn.get_network_extensions())
    except Exception:
        pass
    try:
        return {getattr(ext, "alias", None) for ext in conn.network.extensions() if getattr(ext, "alias", None)}
    except Exception:
        pass
    return set()


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_dns(dns_nameservers: Optional[Iterable[str]]) -> List[str]:
    if not dns_nameservers:
        return []
    return sorted(str(x).strip() for x in dns_nameservers if x is not None)


def normalize_allocation_pools(pools: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
    if not pools:
        return []
    out = []
    for pool in pools:
        out.append({"start": str(pool.get("start", "")).strip(), "end": str(pool.get("end", "")).strip()})
    return sorted(out, key=lambda x: (x["start"], x["end"]))


def normalize_host_routes(host_routes: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
    if not host_routes:
        return []
    out = []
    for route in host_routes:
        out.append({
            "destination": str(route.get("destination", "")).strip(),
            "nexthop": str(route.get("nexthop", "")).strip(),
        })
    return sorted(out, key=lambda x: (x["destination"], x["nexthop"]))


def normalize_routes(routes: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
    if not routes:
        return []
    out = []
    for route in routes:
        out.append({
            "destination": str(route.get("destination", "")).strip(),
            "nexthop": str(route.get("nexthop", "")).strip(),
        })
    return sorted(out, key=lambda x: (x["destination"], x["nexthop"]))


# ---------------------------------------------------------------------------
# Network / subnet signatures
# ---------------------------------------------------------------------------

def subnet_signature(subnet: Any) -> Dict[str, Any]:
    return {
        "name": getattr(subnet, "name", None),
        "ip_version": int(getattr(subnet, "ip_version", 4)),
        "cidr": str(getattr(subnet, "cidr", "") or ""),
        "gateway_ip": str(getattr(subnet, "gateway_ip", "") or ""),
        "enable_dhcp": bool(getattr(subnet, "is_dhcp_enabled", getattr(subnet, "enable_dhcp", False))),
        "dns_nameservers": normalize_dns(getattr(subnet, "dns_nameservers", [])),
        "allocation_pools": normalize_allocation_pools(getattr(subnet, "allocation_pools", [])),
        "host_routes": normalize_host_routes(getattr(subnet, "host_routes", [])),
    }


def network_signature(conn, network: Any) -> Dict[str, Any]:
    subnets = []
    for subnet_id in list(getattr(network, "subnet_ids", []) or []):
        subnet = conn.network.get_subnet(subnet_id)
        if subnet is None:
            LOG.warning("Subnet %s referenced by network %s was not found", subnet_id, network.name)
            continue
        subnets.append(subnet_signature(subnet))
    subnets = sorted(subnets, key=lambda x: (x["ip_version"], x["cidr"], x["gateway_ip"]))
    return {"name": getattr(network, "name", None), "subnets": subnets}


def signatures_equivalent(sig_a: Dict[str, Any], sig_b: Dict[str, Any]) -> bool:
    return sig_a.get("subnets", []) == sig_b.get("subnets", [])


# ---------------------------------------------------------------------------
# Network selection
# ---------------------------------------------------------------------------

def should_skip_network_name(name: Optional[str]) -> bool:
    return bool(name and "sfs" in name.lower())


def list_candidate_networks(conn) -> List[Any]:
    networks = []
    for net in conn.network.networks():
        name = getattr(net, "name", None)

        if bool(getattr(net, "is_router_external", False)):
            LOG.info("Skipping external network '%s'", name)
            continue

        if bool(getattr(net, "is_shared", False)):
            LOG.info("Skipping shared network '%s'", name)
            continue

        if should_skip_network_name(name):
            LOG.info("Skipping network '%s' because it matches forbidden pattern 'sfs'", name)
            continue

        networks.append(net)

    return sorted(networks, key=lambda n: (str(getattr(n, "name", "")), str(getattr(n, "id", ""))))


def get_networks_by_name(conn, name: str) -> List[Any]:
    return [net for net in conn.network.networks(name=name) if getattr(net, "name", None) == name]


def find_free_network_name(conn, base_name: str) -> str:
    existing_names = {getattr(n, "name", None) for n in conn.network.networks()}
    if base_name not in existing_names:
        return base_name
    idx = 1
    while True:
        candidate = f"{base_name}-{idx}"
        if candidate not in existing_names:
            return candidate
        idx += 1


# ---------------------------------------------------------------------------
# Subnet / network mapping helpers
# ---------------------------------------------------------------------------

def build_subnet_mapping(source_conn, target_conn, source_network: Any, target_network: Any) -> Dict[str, str]:
    source_subnets = []
    for sid in list(getattr(source_network, "subnet_ids", []) or []):
        subnet = source_conn.network.get_subnet(sid)
        if subnet:
            source_subnets.append(subnet)

    target_subnets = []
    for sid in list(getattr(target_network, "subnet_ids", []) or []):
        subnet = target_conn.network.get_subnet(sid)
        if subnet:
            target_subnets.append(subnet)

    target_sig_index: Dict[str, str] = {}
    for subnet in target_subnets:
        target_sig_index[json.dumps(subnet_signature(subnet), sort_keys=True)] = subnet.id

    mapping: Dict[str, str] = {}
    for src_subnet in source_subnets:
        key = json.dumps(subnet_signature(src_subnet), sort_keys=True)
        if key in target_sig_index:
            mapping[src_subnet.id] = target_sig_index[key]

    return mapping


def build_global_subnet_id_map(source_conn, target_conn, source_networks, target_network_id_map) -> Dict[str, str]:
    subnet_id_map: Dict[str, str] = {}
    for src_net in source_networks:
        tgt_net_id = target_network_id_map.get(src_net.id)
        if not tgt_net_id:
            continue
        tgt_net = target_conn.network.get_network(tgt_net_id)
        if not tgt_net:
            continue
        subnet_id_map.update(build_subnet_mapping(source_conn, target_conn, src_net, tgt_net))
    return subnet_id_map


# ---------------------------------------------------------------------------
# Router helpers
# ---------------------------------------------------------------------------

def list_candidate_routers(conn) -> List[Any]:
    routers = list(conn.network.routers())
    return sorted(routers, key=lambda r: (str(getattr(r, "name", "")), str(getattr(r, "id", ""))))


def get_routers_by_name(conn, name: str) -> List[Any]:
    return [router for router in conn.network.routers(name=name) if getattr(router, "name", None) == name]


def find_free_router_name(conn, base_name: str) -> str:
    existing_names = {getattr(r, "name", None) for r in conn.network.routers()}
    if base_name not in existing_names:
        return base_name
    idx = 1
    while True:
        candidate = f"{base_name}-{idx}"
        if candidate not in existing_names:
            return candidate
        idx += 1


def get_external_network_name(conn, network_id: Optional[str]) -> Optional[str]:
    if not network_id:
        return None
    try:
        net = conn.network.get_network(network_id)
        if net:
            return getattr(net, "name", None)
    except Exception:
        pass
    return None


def get_router_external_gateway_info(router: Any) -> Dict[str, Any]:
    return getattr(router, "external_gateway_info", None) or {}


def get_router_interface_subnet_ids(conn, router: Any) -> List[str]:
    subnet_ids = set()
    for port in conn.network.ports(device_id=router.id):
        if str(getattr(port, "device_owner", "") or "") != "network:router_interface":
            continue
        for fixed in list(getattr(port, "fixed_ips", []) or []):
            subnet_id = fixed.get("subnet_id")
            if subnet_id:
                subnet_ids.add(subnet_id)
    return sorted(subnet_ids)


def router_attached_to_skipped_sfs_network(source_conn, router: Any) -> Tuple[bool, List[Dict[str, str]]]:
    hits: List[Dict[str, str]] = []
    for subnet_id in get_router_interface_subnet_ids(source_conn, router):
        subnet = source_conn.network.get_subnet(subnet_id)
        if not subnet:
            continue
        net = source_conn.network.get_network(getattr(subnet, "network_id", None))
        net_name = getattr(net, "name", None) if net else None
        if should_skip_network_name(net_name):
            hits.append({
                "network_id": getattr(net, "id", "") if net else "",
                "network_name": net_name or "",
                "subnet_id": subnet_id,
            })
    return (len(hits) > 0, hits)


def router_signature(conn, router: Any) -> Dict[str, Any]:
    external_gateway_info = get_router_external_gateway_info(router)
    ext_net_id = external_gateway_info.get("network_id")
    ext_net_name = get_external_network_name(conn, ext_net_id)

    return {
        "name": getattr(router, "name", None),
        "admin_state_up": bool(getattr(router, "is_admin_state_up", True)),
        "distributed": bool(getattr(router, "is_distributed", False)),
        "ha": bool(getattr(router, "is_ha", False)),
        "external_gateway_network_name": ext_net_name,
        "routes": normalize_routes(getattr(router, "routes", []) or []),
        "interface_subnet_ids": get_router_interface_subnet_ids(conn, router),
    }


def router_settings_equivalent(sig_a: Dict[str, Any], sig_b: Dict[str, Any]) -> bool:
    return (
        sig_a.get("admin_state_up") == sig_b.get("admin_state_up")
        and sig_a.get("distributed") == sig_b.get("distributed")
        and sig_a.get("ha") == sig_b.get("ha")
        and sig_a.get("external_gateway_network_name") == sig_b.get("external_gateway_network_name")
        and sig_a.get("routes") == sig_b.get("routes")
        and sig_a.get("interface_subnet_ids") == sig_b.get("interface_subnet_ids")
    )


def map_router_signature_to_target(
    source_sig: Dict[str, Any],
    subnet_id_map: Dict[str, str],
    ext_net_name: Optional[str],
    target_exts: set[str],
) -> Dict[str, Any]:
    mapped = dict(source_sig)
    mapped["external_gateway_network_name"] = ext_net_name
    mapped["interface_subnet_ids"] = sorted(
        subnet_id_map[sid] for sid in source_sig.get("interface_subnet_ids", []) if sid in subnet_id_map
    )
    if "dvr" not in target_exts:
        mapped["distributed"] = False
    if "l3-ha" not in target_exts:
        mapped["ha"] = False
    if "extraroute" not in target_exts:
        mapped["routes"] = []
    return mapped


def lookup_target_external_network_by_name(target_conn, network_name: Optional[str]) -> Optional[Any]:
    if not network_name:
        return None
    nets = [
        n for n in target_conn.network.networks(name=network_name)
        if getattr(n, "name", None) == network_name and bool(getattr(n, "is_router_external", False))
    ]
    return nets[0] if nets else None


def build_router_create_args(source_conn, target_conn, source_router: Any) -> Tuple[Dict[str, Any], List[str]]:
    src_sig = router_signature(source_conn, source_router)
    target_exts = get_network_extensions(target_conn)
    notes: List[str] = []

    router_args: Dict[str, Any] = {
        "name": getattr(source_router, "name", None),
        "admin_state_up": bool(getattr(source_router, "is_admin_state_up", True)),
    }

    if "dvr" in target_exts and hasattr(source_router, "is_distributed"):
        router_args["distributed"] = bool(getattr(source_router, "is_distributed", False))
    elif hasattr(source_router, "is_distributed"):
        notes.append("target cloud does not support 'dvr'; omitted router attribute 'distributed'")

    if "l3-ha" in target_exts and hasattr(source_router, "is_ha"):
        router_args["ha"] = bool(getattr(source_router, "is_ha", False))
    elif hasattr(source_router, "is_ha"):
        notes.append("target cloud does not support 'l3-ha'; omitted router attribute 'ha'")

    if getattr(source_router, "routes", None):
        if "extraroute" in target_exts:
            router_args["routes"] = normalize_routes(getattr(source_router, "routes", []))
        else:
            notes.append("target cloud does not support 'extraroute'; omitted router routes")

    # Intentionally do NOT set external_gateway_info here.
    ext_name = src_sig.get("external_gateway_network_name")
    if ext_name:
        notes.append(
            f"source router uses external gateway network '{ext_name}'; "
            "gateway will be set later using set_gateway_to_network() in final reconciliation"
        )

    return router_args, notes


def create_router_like(
    source_conn,
    target_conn,
    source_router: Any,
    new_name: str,
    dry_run: bool,
) -> Tuple[str, Optional[str], Optional[str]]:
    router_args, notes = build_router_create_args(source_conn, target_conn, source_router)
    router_args["name"] = new_name

    if dry_run:
        LOG.info("[DRY-RUN] Would create router: %s args=%s", new_name, json.dumps(router_args, sort_keys=True))
        return new_name, None, "; ".join(notes) if notes else None

    created_router = target_conn.network.create_router(**router_args)
    LOG.info("Created target router '%s' id=%s", new_name, created_router.id)
    return new_name, created_router.id, "; ".join(notes) if notes else None


def attach_subnet_to_router(target_conn, router_id: str, subnet_id: str, dry_run: bool) -> None:
    if dry_run:
        LOG.info("[DRY-RUN] Would attach subnet %s to router %s", subnet_id, router_id)
        return
    target_conn.network.add_interface_to_router(router_id, subnet_id=subnet_id)
    LOG.info("Attached subnet %s to router %s", subnet_id, router_id)


def set_gateway_to_network(target_conn, router_id: str, external_network: Any, dry_run: bool) -> None:
    if dry_run:
        LOG.info(
            "[DRY-RUN] Would set gateway of router %s to external network %s (%s)",
            router_id,
            getattr(external_network, "name", None),
            getattr(external_network, "id", None),
        )
        return

    target_conn.network.update_router(
        router_id,
        external_gateway_info={
            "network_id": external_network.id
        }
    )
    LOG.info(
        "Set gateway of router %s to external network %s (%s)",
        router_id,
        getattr(external_network, "name", None),
        getattr(external_network, "id", None),
    )


# ---------------------------------------------------------------------------
# Create network/subnets
# ---------------------------------------------------------------------------

def create_network_like(
    target_conn,
    source_network: Any,
    source_sig: Dict[str, Any],
    new_name: str,
    dry_run: bool,
) -> Tuple[str, Optional[str]]:
    net_create_args = {
        "name": new_name,
        "admin_state_up": bool(getattr(source_network, "is_admin_state_up", True)),
    }

    mtu = getattr(source_network, "mtu", None)
    if mtu:
        net_create_args["mtu"] = mtu

    if dry_run:
        LOG.info("[DRY-RUN] Would create network: %s args=%s", new_name, json.dumps(net_create_args, sort_keys=True))
        network_id = None
    else:
        created_network = target_conn.network.create_network(**net_create_args)
        network_id = created_network.id
        LOG.info("Created target network '%s' id=%s", new_name, network_id)

    for subnet_sig in source_sig["subnets"]:
        subnet_args = {
            "name": subnet_sig["name"],
            "network_id": network_id if network_id else "DRYRUN-NETWORK-ID",
            "ip_version": subnet_sig["ip_version"],
            "cidr": subnet_sig["cidr"],
            "gateway_ip": subnet_sig["gateway_ip"] or None,
            "enable_dhcp": subnet_sig["enable_dhcp"],
            "dns_nameservers": subnet_sig["dns_nameservers"],
            "allocation_pools": subnet_sig["allocation_pools"] or None,
            "host_routes": subnet_sig["host_routes"] or None,
        }
        subnet_args = {k: v for k, v in subnet_args.items() if v is not None}

        if dry_run:
            LOG.info("[DRY-RUN] Would create subnet on network '%s': %s", new_name, json.dumps(subnet_args, sort_keys=True))
        else:
            created_subnet = target_conn.network.create_subnet(**subnet_args)
            LOG.info(
                "Created subnet '%s' id=%s on network '%s'",
                getattr(created_subnet, "name", None),
                getattr(created_subnet, "id", None),
                new_name,
            )

    return new_name, network_id


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def init_report(source_cloud: str, target_cloud: str, dry_run: bool) -> Dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_cloud": source_cloud,
        "target_cloud": target_cloud,
        "dry_run": dry_run,
        "networks": [],
        "routers": [],
        "router_network_reconcile": [],
        "router_external_reconcile": [],
        "summary": {
            "created_networks": 0,
            "skipped_networks": 0,
            "renamed_networks": 0,
            "error_networks": 0,
            "skipped_sfs_networks": 0,
            "created_routers": 0,
            "skipped_routers": 0,
            "renamed_routers": 0,
            "error_routers": 0,
            "skipped_sfs_routers": 0,
            "reconciled_router_networks": 0,
            "error_reconciled_router_networks": 0,
            "reconciled_router_external": 0,
            "error_reconciled_router_external": 0,
        },
    }


def write_json_report(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=False)


def write_csv_report(report: Dict[str, Any], path: str) -> None:
    rows = []

    for n in report.get("networks", []):
        rows.append({
            "record_type": "network",
            "source_network_name": n.get("source_network_name", ""),
            "source_network_id": n.get("source_network_id", ""),
            "target_network_name": n.get("target_network_name", ""),
            "target_network_id": n.get("target_network_id", ""),
            "source_router_name": "",
            "source_router_id": "",
            "target_router_name": "",
            "target_router_id": "",
            "action": n.get("action", ""),
            "reason": n.get("reason", ""),
            "source_subnet_id": "",
            "target_subnet_id": "",
        })

    for r in report.get("routers", []):
        rows.append({
            "record_type": "router",
            "source_network_name": "",
            "source_network_id": "",
            "target_network_name": "",
            "target_network_id": "",
            "source_router_name": r.get("source_router_name", ""),
            "source_router_id": r.get("source_router_id", ""),
            "target_router_name": r.get("target_router_name", ""),
            "target_router_id": r.get("target_router_id", ""),
            "action": r.get("action", ""),
            "reason": r.get("reason", ""),
            "source_subnet_id": "",
            "target_subnet_id": "",
        })

    for rr in report.get("router_network_reconcile", []):
        rows.append({
            "record_type": "router_network_reconcile",
            "source_network_name": rr.get("source_network_name", ""),
            "source_network_id": rr.get("source_network_id", ""),
            "target_network_name": rr.get("target_network_name", ""),
            "target_network_id": rr.get("target_network_id", ""),
            "source_router_name": rr.get("source_router_name", ""),
            "source_router_id": rr.get("source_router_id", ""),
            "target_router_name": rr.get("target_router_name", ""),
            "target_router_id": rr.get("target_router_id", ""),
            "action": rr.get("action", ""),
            "reason": rr.get("reason", ""),
            "source_subnet_id": rr.get("source_subnet_id", ""),
            "target_subnet_id": rr.get("target_subnet_id", ""),
        })

    for re in report.get("router_external_reconcile", []):
        rows.append({
            "record_type": "router_external_reconcile",
            "source_network_name": re.get("source_network_name", ""),
            "source_network_id": re.get("source_network_id", ""),
            "target_network_name": re.get("target_network_name", ""),
            "target_network_id": re.get("target_network_id", ""),
            "source_router_name": re.get("source_router_name", ""),
            "source_router_id": re.get("source_router_id", ""),
            "target_router_name": re.get("target_router_name", ""),
            "target_router_id": re.get("target_router_id", ""),
            "action": re.get("action", ""),
            "reason": re.get("reason", ""),
            "source_subnet_id": "",
            "target_subnet_id": "",
        })

    fieldnames = [
        "record_type",
        "source_network_name",
        "source_network_id",
        "target_network_name",
        "target_network_id",
        "source_router_name",
        "source_router_id",
        "target_router_name",
        "target_router_id",
        "action",
        "reason",
        "source_subnet_id",
        "target_subnet_id",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Sync networks
# ---------------------------------------------------------------------------

def sync_one_network(source_conn, target_conn, source_network: Any, dry_run: bool, report: Dict[str, Any]) -> Dict[str, Optional[str]]:
    src_name = getattr(source_network, "name", None)

    if not src_name:
        report["networks"].append({
            "source_network_name": None,
            "source_network_id": getattr(source_network, "id", None),
            "target_network_name": None,
            "target_network_id": None,
            "action": "skip",
            "reason": "source network has no name",
        })
        report["summary"]["skipped_networks"] += 1
        return {"source_network_id": getattr(source_network, "id", None), "target_network_id": None, "target_network_name": None}

    if should_skip_network_name(src_name):
        report["networks"].append({
            "source_network_name": src_name,
            "source_network_id": getattr(source_network, "id", None),
            "target_network_name": None,
            "target_network_id": None,
            "action": "skip-sfs",
            "reason": "network name contains 'sfs'",
        })
        report["summary"]["skipped_sfs_networks"] += 1
        return {"source_network_id": getattr(source_network, "id", None), "target_network_id": None, "target_network_name": None}

    src_sig = network_signature(source_conn, source_network)
    target_same_name = get_networks_by_name(target_conn, src_name)

    target_network = None
    target_network_name = None
    target_network_id = None
    action = None
    reason = None

    if not target_same_name:
        created_name, _ = create_network_like(target_conn, source_network, src_sig, src_name, dry_run)
        target_network_name = created_name
        action = "create"
        reason = "network name did not exist in target"

        if not dry_run:
            candidates = get_networks_by_name(target_conn, created_name)
            if not candidates:
                raise RuntimeError(f"Created network '{created_name}' was not found afterwards")
            target_network = candidates[0]
            target_network_id = target_network.id

        report["summary"]["created_networks"] += 1
    else:
        matched = None
        for tgt_net in target_same_name:
            if signatures_equivalent(src_sig, network_signature(target_conn, tgt_net)):
                matched = tgt_net
                break

        if matched:
            target_network = matched
            target_network_name = matched.name
            target_network_id = matched.id
            action = "skip"
            reason = "matching target network already exists"
            report["summary"]["skipped_networks"] += 1
        else:
            new_name = find_free_network_name(target_conn, src_name)
            created_name, _ = create_network_like(target_conn, source_network, src_sig, new_name, dry_run)
            target_network_name = created_name
            action = "create-with-suffix"
            reason = "same network name exists but subnet/gateway/range/dns/dhcp settings differ"

            if not dry_run:
                candidates = get_networks_by_name(target_conn, created_name)
                if not candidates:
                    raise RuntimeError(f"Created renamed network '{created_name}' was not found afterwards")
                target_network = candidates[0]
                target_network_id = target_network.id

            report["summary"]["created_networks"] += 1
            report["summary"]["renamed_networks"] += 1

    report["networks"].append({
        "source_network_name": source_network.name,
        "source_network_id": source_network.id,
        "target_network_name": target_network_name,
        "target_network_id": target_network_id,
        "action": action if not dry_run else f"dry-run-{action}",
        "reason": reason,
    })

    return {
        "source_network_id": source_network.id,
        "target_network_id": target_network_id,
        "target_network_name": target_network_name,
    }


# ---------------------------------------------------------------------------
# Sync routers
# ---------------------------------------------------------------------------

def sync_one_router(
    source_conn,
    target_conn,
    source_router: Any,
    subnet_id_map: Dict[str, str],
    dry_run: bool,
    report: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    skip_router, sfs_hits = router_attached_to_skipped_sfs_network(source_conn, source_router)
    if skip_router:
        hit_desc = ", ".join(f"{x['network_name']}:{x['subnet_id']}" for x in sfs_hits)
        report["routers"].append({
            "source_router_name": source_router.name,
            "source_router_id": source_router.id,
            "target_router_name": None,
            "target_router_id": None,
            "action": "skip-router-sfs",
            "reason": f"router is attached to skipped sfs network(s): {hit_desc}",
        })
        report["summary"]["skipped_sfs_routers"] += 1
        return {
            "source_router_id": source_router.id,
            "target_router_id": None,
            "target_router_name": None,
        }

    source_sig = router_signature(source_conn, source_router)
    target_exts = get_network_extensions(target_conn)

    ext_name = source_sig.get("external_gateway_network_name")
    target_ext_name = ext_name if lookup_target_external_network_by_name(target_conn, ext_name) else None

    mapped_source_sig = map_router_signature_to_target(source_sig, subnet_id_map, target_ext_name, target_exts)
    same_name = get_routers_by_name(target_conn, source_router.name)

    target_router_name = None
    target_router_id = None
    action = None
    reason = None
    gateway_note = None

    if not same_name:
        created_name, created_id, gateway_note = create_router_like(
            source_conn, target_conn, source_router, source_router.name, dry_run
        )
        target_router_name = created_name
        target_router_id = created_id
        action = "create-router"
        reason = "router name did not exist in target"
        report["summary"]["created_routers"] += 1
    else:
        matched = None
        for tgt_router in same_name:
            if router_settings_equivalent(mapped_source_sig, router_signature(target_conn, tgt_router)):
                matched = tgt_router
                break

        if matched:
            target_router_name = matched.name
            target_router_id = matched.id
            action = "skip-router"
            reason = "matching target router already exists"
            report["summary"]["skipped_routers"] += 1
        else:
            new_name = find_free_router_name(target_conn, source_router.name)
            created_name, created_id, gateway_note = create_router_like(
                source_conn, target_conn, source_router, new_name, dry_run
            )
            target_router_name = created_name
            target_router_id = created_id
            action = "create-router-with-suffix"
            reason = "same router name exists but settings differ"
            report["summary"]["created_routers"] += 1
            report["summary"]["renamed_routers"] += 1

    if gateway_note:
        reason = f"{reason}; {gateway_note}"

    report["routers"].append({
        "source_router_name": source_router.name,
        "source_router_id": source_router.id,
        "target_router_name": target_router_name,
        "target_router_id": target_router_id,
        "action": action if not dry_run else f"dry-run-{action}",
        "reason": reason,
    })

    return {
        "source_router_id": source_router.id,
        "target_router_id": target_router_id,
        "target_router_name": target_router_name,
    }


# ---------------------------------------------------------------------------
# Final router reconciliation
# ---------------------------------------------------------------------------

def reconcile_router_network_connections(
    source_conn,
    target_conn,
    source_routers: List[Any],
    source_to_target_router_map: Dict[str, Dict[str, Optional[str]]],
    subnet_id_map: Dict[str, str],
    source_to_target_network_map: Dict[str, Dict[str, Optional[str]]],
    dry_run: bool,
    report: Dict[str, Any],
) -> None:
    for source_router in source_routers:
        router_map = source_to_target_router_map.get(source_router.id, {})
        target_router_id = router_map.get("target_router_id")
        target_router_name = router_map.get("target_router_name")

        if not target_router_id and not dry_run:
            continue

        target_existing_subnets: set[str] = set()
        if not dry_run and target_router_id:
            target_router = target_conn.network.get_router(target_router_id)
            if target_router:
                target_existing_subnets = set(get_router_interface_subnet_ids(target_conn, target_router))

        source_subnet_ids = get_router_interface_subnet_ids(source_conn, source_router)
        for source_subnet_id in source_subnet_ids:
            src_subnet = source_conn.network.get_subnet(source_subnet_id)
            if not src_subnet:
                report["router_network_reconcile"].append({
                    "source_router_name": source_router.name,
                    "source_router_id": source_router.id,
                    "target_router_name": target_router_name,
                    "target_router_id": target_router_id,
                    "source_network_name": "",
                    "source_network_id": "",
                    "target_network_name": "",
                    "target_network_id": "",
                    "source_subnet_id": source_subnet_id,
                    "target_subnet_id": "",
                    "action": "error-reconcile-router-network",
                    "reason": "source subnet not found",
                })
                report["summary"]["error_reconciled_router_networks"] += 1
                continue

            source_network_id = getattr(src_subnet, "network_id", None)
            source_network = source_conn.network.get_network(source_network_id) if source_network_id else None
            source_network_name = getattr(source_network, "name", "") if source_network else ""

            if should_skip_network_name(source_network_name):
                report["router_network_reconcile"].append({
                    "source_router_name": source_router.name,
                    "source_router_id": source_router.id,
                    "target_router_name": target_router_name,
                    "target_router_id": target_router_id,
                    "source_network_name": source_network_name,
                    "source_network_id": source_network_id or "",
                    "target_network_name": "",
                    "target_network_id": "",
                    "source_subnet_id": source_subnet_id,
                    "target_subnet_id": "",
                    "action": "skip-reconcile-router-network-sfs",
                    "reason": "source router interface belongs to skipped sfs network",
                })
                continue

            target_subnet_id = subnet_id_map.get(source_subnet_id)
            network_map = source_to_target_network_map.get(source_network_id, {}) if source_network_id else {}
            target_network_id = network_map.get("target_network_id")
            target_network_name = network_map.get("target_network_name")

            entry = {
                "source_router_name": source_router.name,
                "source_router_id": source_router.id,
                "target_router_name": target_router_name,
                "target_router_id": target_router_id,
                "source_network_name": source_network_name,
                "source_network_id": source_network_id or "",
                "target_network_name": target_network_name or "",
                "target_network_id": target_network_id or "",
                "source_subnet_id": source_subnet_id,
                "target_subnet_id": target_subnet_id or "",
            }

            if not target_subnet_id or not target_network_id:
                entry["action"] = "error-reconcile-router-network"
                entry["reason"] = "no mapped target subnet/network found"
                report["router_network_reconcile"].append(entry)
                report["summary"]["error_reconciled_router_networks"] += 1
                continue

            if not dry_run and target_subnet_id in target_existing_subnets:
                entry["action"] = "skip-reconcile-router-network"
                entry["reason"] = "target router already connected to mapped target subnet/network"
                report["router_network_reconcile"].append(entry)
                continue

            try:
                if dry_run:
                    entry["action"] = "dry-run-reconcile-router-network"
                    entry["reason"] = "would connect target router to mapped target subnet/network"
                else:
                    attach_subnet_to_router(target_conn, target_router_id, target_subnet_id, False)
                    target_existing_subnets.add(target_subnet_id)
                    entry["action"] = "reconcile-router-network"
                    entry["reason"] = "connected target router to mapped target subnet/network"
                    report["summary"]["reconciled_router_networks"] += 1
                report["router_network_reconcile"].append(entry)
            except Exception as exc:
                entry["action"] = "error-reconcile-router-network"
                entry["reason"] = str(exc)
                report["router_network_reconcile"].append(entry)
                report["summary"]["error_reconciled_router_networks"] += 1


def reconcile_router_external_gateways(
    source_conn,
    target_conn,
    source_routers: List[Any],
    source_to_target_router_map: Dict[str, Dict[str, Optional[str]]],
    dry_run: bool,
    report: Dict[str, Any],
) -> None:
    for source_router in source_routers:
        router_map = source_to_target_router_map.get(source_router.id, {})
        target_router_id = router_map.get("target_router_id")
        target_router_name = router_map.get("target_router_name")

        if not target_router_id and not dry_run:
            continue

        source_ext_info = get_router_external_gateway_info(source_router)
        source_ext_net_id = source_ext_info.get("network_id")
        source_ext_net_name = get_external_network_name(source_conn, source_ext_net_id)

        entry = {
            "source_router_name": source_router.name,
            "source_router_id": source_router.id,
            "target_router_name": target_router_name,
            "target_router_id": target_router_id,
            "source_network_name": source_ext_net_name or "",
            "source_network_id": source_ext_net_id or "",
            "target_network_name": "",
            "target_network_id": "",
        }

        if not source_ext_net_id or not source_ext_net_name:
            entry["action"] = "skip-reconcile-router-external"
            entry["reason"] = "source router has no external gateway"
            report["router_external_reconcile"].append(entry)
            continue

        target_ext_net = lookup_target_external_network_by_name(target_conn, source_ext_net_name)
        if not target_ext_net:
            entry["action"] = "skip-reconcile-router-external"
            entry["reason"] = "no target external network with matching name found"
            report["router_external_reconcile"].append(entry)
            continue

        entry["target_network_name"] = getattr(target_ext_net, "name", "") or ""
        entry["target_network_id"] = getattr(target_ext_net, "id", "") or ""

        if not dry_run and target_router_id:
            target_router = target_conn.network.get_router(target_router_id)
            target_ext_info = get_router_external_gateway_info(target_router) if target_router else {}
            current_target_ext_net_id = target_ext_info.get("network_id")

            if current_target_ext_net_id == target_ext_net.id:
                entry["action"] = "skip-reconcile-router-external"
                entry["reason"] = "target router already has gateway on matching external network"
                report["router_external_reconcile"].append(entry)
                continue

        try:
            if dry_run:
                entry["action"] = "dry-run-reconcile-router-external"
                entry["reason"] = "would set gateway to matching target external network"
            else:
                set_gateway_to_network(
                    target_conn=target_conn,
                    router_id=target_router_id,
                    external_network=target_ext_net,
                    dry_run=False,
                )
                entry["action"] = "reconcile-router-external"
                entry["reason"] = "set gateway to matching target external network"
                report["summary"]["reconciled_router_external"] += 1

            report["router_external_reconcile"].append(entry)
        except Exception as exc:
            entry["action"] = "error-reconcile-router-external"
            entry["reason"] = str(exc)
            report["router_external_reconcile"].append(entry)
            report["summary"]["error_reconciled_router_external"] += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    setup_logging(args.debug)
    report = init_report(args.source, args.target, args.dry_run)

    try:
        source_conn = connect_cloud(args.source)
        target_conn = connect_cloud(args.target)
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2

    try:
        source_networks = list_candidate_networks(source_conn)
        if args.networks:
            wanted = set(args.networks)
            source_networks = [n for n in source_networks if getattr(n, "name", None) in wanted]

        source_to_target_network_map: Dict[str, Dict[str, Optional[str]]] = {}

        for net in source_networks:
            try:
                LOG.info("Processing source network '%s' (%s)", net.name, net.id)
                res = sync_one_network(source_conn, target_conn, net, args.dry_run, report)
                source_to_target_network_map[net.id] = res
            except SDKException as exc:
                LOG.exception("OpenStack SDK error while syncing network '%s': %s", getattr(net, "name", None), exc)
                report["networks"].append({
                    "source_network_name": getattr(net, "name", None),
                    "source_network_id": getattr(net, "id", None),
                    "target_network_name": None,
                    "target_network_id": None,
                    "action": "error",
                    "reason": str(exc),
                })
                report["summary"]["error_networks"] += 1
            except Exception as exc:
                LOG.exception("Unexpected error while syncing network '%s': %s", getattr(net, "name", None), exc)
                report["networks"].append({
                    "source_network_name": getattr(net, "name", None),
                    "source_network_id": getattr(net, "id", None),
                    "target_network_name": None,
                    "target_network_id": None,
                    "action": "error",
                    "reason": str(exc),
                })
                report["summary"]["error_networks"] += 1

        subnet_id_map = build_global_subnet_id_map(
            source_conn=source_conn,
            target_conn=target_conn,
            source_networks=source_networks,
            target_network_id_map={
                src_id: v.get("target_network_id")
                for src_id, v in source_to_target_network_map.items()
                if v.get("target_network_id")
            },
        )

        source_routers = list_candidate_routers(source_conn)
        if args.routers:
            wanted_routers = set(args.routers)
            source_routers = [r for r in source_routers if getattr(r, "name", None) in wanted_routers]

        source_to_target_router_map: Dict[str, Dict[str, Optional[str]]] = {}

        for router in source_routers:
            try:
                LOG.info("Processing source router '%s' (%s)", router.name, router.id)
                res = sync_one_router(
                    source_conn,
                    target_conn,
                    router,
                    subnet_id_map,
                    args.dry_run,
                    report,
                )
                source_to_target_router_map[router.id] = res
            except SDKException as exc:
                LOG.exception("OpenStack SDK error while syncing router '%s': %s", getattr(router, "name", None), exc)
                report["routers"].append({
                    "source_router_name": getattr(router, "name", None),
                    "source_router_id": getattr(router, "id", None),
                    "target_router_name": None,
                    "target_router_id": None,
                    "action": "error-router",
                    "reason": str(exc),
                })
                report["summary"]["error_routers"] += 1
            except Exception as exc:
                LOG.exception("Unexpected error while syncing router '%s': %s", getattr(router, "name", None), exc)
                report["routers"].append({
                    "source_router_name": getattr(router, "name", None),
                    "source_router_id": getattr(router, "id", None),
                    "target_router_name": None,
                    "target_router_id": None,
                    "action": "error-router",
                    "reason": str(exc),
                })
                report["summary"]["error_routers"] += 1

        LOG.info("Starting final router-to-network reconciliation pass")
        reconcile_router_network_connections(
            source_conn=source_conn,
            target_conn=target_conn,
            source_routers=source_routers,
            source_to_target_router_map=source_to_target_router_map,
            subnet_id_map=subnet_id_map,
            source_to_target_network_map=source_to_target_network_map,
            dry_run=args.dry_run,
            report=report,
        )

        LOG.info("Starting final router external gateway reconciliation pass")
        reconcile_router_external_gateways(
            source_conn=source_conn,
            target_conn=target_conn,
            source_routers=source_routers,
            source_to_target_router_map=source_to_target_router_map,
            dry_run=args.dry_run,
            report=report,
        )

        write_json_report(report, args.report_json)
        write_csv_report(report, args.report_csv)

        print("\n=== SUMMARY ===")
        print(json.dumps(report["summary"], indent=2, sort_keys=False))
        print(f"\nJSON report: {args.report_json}")
        print(f"CSV report:  {args.report_csv}")
        return 0

    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        try:
            write_json_report(report, args.report_json)
            write_csv_report(report, args.report_csv)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
