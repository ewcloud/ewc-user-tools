#!/usr/bin/env python3
"""
OpenStack migration prep script (source -> target) with:
- flavor mapping file (no flavor creation on target)
- security group sync to target (create SGs + rules first)
- supports image-boot and volume-boot instances on source
- target is boot-from-volume (always): create target root/data volumes from images
- does NOT boot instances on target (writes manual boot CLI hint)
- instance selection: --instance / --instance-file / migrate.yaml servers:
- knobs:
    --dry-run       (discovery only; no changes)
    --skip-download (skip downloading images from source; stops before upload/target steps)
    --skip-upload   (skip uploading to target; proceeds only if images already exist on target)
- target auth:
    obtain token via OpenStack CLI using an OIDC cloud (e.g. target_oidc),
    then build an explicit keystoneauth token session for openstacksdk Connection.
"""

import os
import sys
import json
import time
import yaml
import hashlib
import csv
import subprocess
import argparse
import getpass
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import openstack
from openstack.config import OpenStackConfig
from openstack.connection import Connection

from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3 as ks_v3


# ----------------------------
# Helpers
# ----------------------------

def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_cloud_config_raw(cloud_name: str) -> Dict[str, Any]:
    """
    Load cloud config without instantiating an auth plugin (works even if v3token token is blank).
    Respects OS_CLIENT_CONFIG_FILE if set.
    """
    cfg_file = os.environ.get("OS_CLIENT_CONFIG_FILE")
    paths = []
    if cfg_file:
        paths.append(Path(cfg_file))
    # Common fallback locations
    paths.extend([
        Path.home() / ".config" / "openstack" / "clouds.yaml",
        Path.home() / ".config" / "openstack" / "clouds.yml",
        Path("/etc/openstack/clouds.yaml"),
        Path("/etc/openstack/clouds.yml"),
    ])

    for p in paths:
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            clouds = data.get("clouds") or {}
            if cloud_name in clouds:
                return clouds[cloud_name]

    raise RuntimeError(
        f"Could not find cloud '{cloud_name}' in clouds.yaml. "
        f"Checked OS_CLIENT_CONFIG_FILE and standard locations."
    )

def safe_get_image(conn: openstack.connection.Connection, image_id: str) -> Optional[Any]:
    try:
        return conn.image.get_image(image_id)
    except Exception:
        return None

def ensure_target_image(target: openstack.connection.Connection,
                        cfg: Dict[str, Any],
                        image_key: str,
                        image_name: str,
                        desired_id: Optional[str],
                        local_file: Optional[Path],
                        disk_format: str,
                        state: Dict[str, Any]) -> Any:
    """
    Ensure a target image exists and is usable.
    - If desired_id exists and image is found: return it
    - Else try find by name
    - Else if local_file is available: upload (create+upload) and return
    Updates state["target"]["images"][image_key] with current id/name.
    """
    img = None

    if desired_id:
        img = safe_get_image(target, desired_id)
        if img:
            state["target"].setdefault("images", {})
            state["target"]["images"][image_key] = {"id": img.id, "name": img.name}
            return img

    # Try by name
    by_name = find_target_image_by_name(target, image_name)
    if by_name:
        img = target.image.get_image(by_name.id)
        state["target"].setdefault("images", {})
        state["target"]["images"][image_key] = {"id": img.id, "name": img.name}
        return img

    # Re-upload if possible
    if local_file and local_file.exists():
        print(f"[INFO] Target image missing; re-uploading: {image_name}")
        img = upload_image(target, cfg, image_name, local_file, disk_format)
        state["target"].setdefault("images", {})
        state["target"]["images"][image_key] = {"id": img.id, "name": img.name}
        return img

    raise RuntimeError(
        f"Target image '{image_name}' not found by id or name, and no local file available to re-upload."
    )

def wait_for_image_active(conn: openstack.connection.Connection,
                          image_id: str,
                          name: str,
                          timeout: int = 7200,
                          poll: int = 10) -> Any:
    start = time.time()
    last_print = 0
    while True:
        img = conn.image.get_image(image_id)
        status = (getattr(img, "status", "") or "").lower()
        size = getattr(img, "size", None)

        # Print progress every ~30s
        if time.time() - last_print >= 30:
            print(f"[WAIT] Image {name} ({image_id}) status={status} size={size}")
            last_print = time.time()

        if status == "active":
            if size is None or int(size) > 0:
                return img

        if status in ("killed", "deleted"):
            raise RuntimeError(f"Image {name} ({image_id}) entered terminal state: {status}")

        if time.time() - start > timeout:
            raise TimeoutError(
                f"Timeout waiting for image {name} ({image_id}) to become ACTIVE. "
                f"Last status={status}, size={size}"
            )
        time.sleep(poll)


def wait_for_status(getter_fn, desired: str, name: str, timeout: int = 3600, poll: int = 5):
    start = time.time()
    while True:
        obj = getter_fn()
        status = (getattr(obj, "status", None) or getattr(obj, "state", None) or "").upper()
        if status == desired.upper():
            return obj
        if time.time() - start > timeout:
            raise TimeoutError(f"Timeout waiting for {name} to reach {desired}. Last status: {status}")
        time.sleep(poll)


def read_state(state_file: Path) -> Dict[str, Any]:
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {}


def write_state(state_file: Path, data: Dict[str, Any]) -> None:
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_file)


def verify_connection(conn: openstack.connection.Connection, label: str) -> None:
    """
    Safer than conn.current_user_id on older SDKs: force authorize().
    """
    try:
        conn.authorize()
    except Exception as e:
        raise RuntimeError(f"[AUTH ERROR] Failed to authenticate to {label} cloud. Underlying error: {e}")


# ----------------------------
# Token acquisition via CLI (OIDC)
# ----------------------------

def get_token_via_openstack_cli(cloud_name: str) -> str:
    """
    Obtain an auth token using the OpenStack CLI for the given clouds.yaml cloud.
    If v3oidcpassword complains about missing password, prompt securely and retry.
    """
    cmd = ["openstack", "--os-cloud", cloud_name, "token", "issue", "-f", "value", "-c", "id"]

    def run(extra_env: Optional[Dict[str, str]] = None) -> str:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, env=env).strip()

    try:
        out = run()
    except subprocess.CalledProcessError as e:
        msg = e.output or ""
        if "Missing value password required for auth plugin v3oidcpassword" in msg:
            pw = getpass.getpass(f"Password required for cloud '{cloud_name}' (will not be stored): ")
            out = run({"OS_PASSWORD": pw})
        else:
            raise RuntimeError(
                "Failed to obtain token via OpenStack CLI.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Output:\n{msg}"
            )

    if not out:
        raise RuntimeError("OpenStack CLI returned an empty token.")
    return out


# ----------------------------
# Connections
# ----------------------------

def connect_cloud(cloud_name: str) -> openstack.connection.Connection:
    return openstack.connect(cloud=cloud_name)

def connect_target_with_token_refresh(cfg: Dict[str, Any]) -> openstack.connection.Connection:
    target_cloud = cfg["target_cloud"]
    refresh_cfg = (cfg.get("target_token_refresh") or {})
    refresh_enabled = bool(refresh_cfg.get("enabled", True))
    refresh_cloud_name = str(refresh_cfg.get("cloud_name", target_cloud))

    # Load RAW target config (no keystoneauth plugin instantiation)
    target_raw = load_cloud_config_raw(target_cloud)

    if not refresh_enabled:
        raise RuntimeError(
            "target_token_refresh.enabled is false, but target cloud uses v3token with blank token. "
            "Enable refresh or set a real token in clouds.yaml (not recommended)."
        )

    # 1) Get token via CLI (OIDC)
    token = get_token_via_openstack_cli(refresh_cloud_name)

    # 2) Build Keystone token auth scoped to project
    auth_cfg = (target_raw.get("auth") or {})
    auth_url = auth_cfg.get("auth_url")
    project_id = auth_cfg.get("project_id")
    project_name = auth_cfg.get("project_name")
    project_domain_id = auth_cfg.get("project_domain_id")
    project_domain_name = auth_cfg.get("project_domain_name")

    if not auth_url:
        raise RuntimeError(f"Missing auth.auth_url for cloud '{target_cloud}' in clouds.yaml")

    token_kwargs: Dict[str, Any] = {"auth_url": auth_url, "token": token}

    if project_id:
        token_kwargs["project_id"] = project_id
    elif project_name:
        token_kwargs["project_name"] = project_name
        if project_domain_id:
            token_kwargs["project_domain_id"] = project_domain_id
        elif project_domain_name:
            token_kwargs["project_domain_name"] = project_domain_name
        else:
            raise RuntimeError(
                f"Cloud '{target_cloud}' uses project_name but no project_domain_id/project_domain_name is set."
            )
    else:
        raise RuntimeError(
            f"Cloud '{target_cloud}' must define project_id or project_name (+ domain) for token-scoped auth."
        )

    auth = ks_v3.Token(**token_kwargs)

    # 3) TLS verify handling (keystoneauth supports verify=bool|path)
    verify = target_raw.get("verify", True)

    sess = ks_session.Session(auth=auth, verify=verify)

    conn = Connection(
        session=sess,
        region_name=target_raw.get("region_name"),
        interface=target_raw.get("interface", "public"),
        identity_api_version=str(target_raw.get("identity_api_version", 3)),
    )

    verify_connection(conn, "target (keystone-token-session)")
    return conn

# ----------------------------
# Flavor map
# ----------------------------

def load_flavor_map(path: str) -> Dict[str, str]:
    data = load_yaml(path)
    mappings = data.get("mappings", {})
    if not isinstance(mappings, dict) or not mappings:
        raise ValueError("flavor_map_file must contain a non-empty 'mappings:' dict.")
    return {str(k): str(v) for k, v in mappings.items()}


def resolve_target_flavor(source: openstack.connection.Connection,
                          target: openstack.connection.Connection,
                          src_flavor_id: str,
                          flavor_map: Dict[str, str]) -> Tuple[str, str]:
    """
    Returns (target_flavor_id, target_flavor_name).
    Mapping keys can be source flavor name or source flavor id.
    Values can be target flavor name or target flavor id.
    """
    src_flavor = source.compute.get_flavor(src_flavor_id)
    src_name = src_flavor.name

    mapped = flavor_map.get(src_name) or flavor_map.get(src_flavor_id)
    if not mapped:
        raise RuntimeError(f"No flavor mapping found for source flavor '{src_name}' ({src_flavor_id}).")

    tgt = target.compute.find_flavor(mapped, ignore_missing=True)
    if tgt:
        return tgt.id, tgt.name

    try:
        tgt = target.compute.get_flavor(mapped)
        return tgt.id, tgt.name
    except Exception:
        raise RuntimeError(f"Mapped target flavor '{mapped}' not found by name or id on target.")


# ----------------------------
# Volume detection (root vs data)
# ----------------------------

def pick_root_and_data_volumes(source: openstack.connection.Connection, server: Any) -> Tuple[Optional[Any], List[Any]]:
    """
    Return (root_volume_or_None, list_of_data_volumes).
    Root is detected as first bootable=True attached volume (common pattern).
    """
    atts = source.compute.volume_attachments(server)
    vols = []
    for att in atts:
        vol = source.block_storage.get_volume(att.volume_id)
        if vol:
            vols.append((att, vol))

    root = None
    data = []
    for att, vol in vols:
        if str(getattr(vol, "is_bootable", "")).lower() == "true":
            root = (att, vol)
            break

    for att, vol in vols:
        if root and vol.id == root[1].id:
            continue
        data.append((att, vol))

    return (root[1] if root else None), [v for _, v in data]

#-----------------------------
# Network helper
#-----------------------------
def resolve_target_network(target: openstack.connection.Connection, cfg: Dict[str, Any]) -> str:
    net_id = cfg.get("target_network_id")
    net_name = cfg.get("target_network_name")

    if net_id:
        net = target.network.get_network(net_id)
        if not net:
            raise RuntimeError(f"Target network_id not found: {net_id}")
        return net.id

    if net_name:
        net = target.network.find_network(net_name, ignore_missing=True)
        if not net:
            raise RuntimeError(f"Target network_name not found: {net_name}")
        return net.id

    raise RuntimeError("You must set target_network_id or target_network_name in migrate.yaml")

def _as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def resolve_network_id(target: openstack.connection.Connection, ref: str) -> str:
    """
    Resolve a network by ID first, then by name.
    """
    # Try as ID
    try:
        net = target.network.get_network(ref)
        if net:
            return net.id
    except Exception:
        pass

    # Try as name
    net = target.network.find_network(ref, ignore_missing=True)
    if not net:
        raise RuntimeError(f"Target network not found by id or name: {ref}")
    return net.id

def resolve_target_networks(
    target: openstack.connection.Connection,
    cfg: Dict[str, Any],
    server_cfg: Optional[Dict[str, Any]] = None,
    cli_target_nets: Optional[List[str]] = None,
) -> List[str]:
    """
    Returns list of network IDs.
    Precedence:
      1) CLI --target-net (if provided)
      2) per-server migrate.yaml server_cfg['target_networks']
      3) global cfg['target_networks']
    """
    cli_target_nets = cli_target_nets or []

    if cli_target_nets:
        refs = cli_target_nets

    else:
        nets = []
        if server_cfg:
            nets = server_cfg.get("target_networks") or server_cfg.get("target_network") or []
        if not nets:
            nets = cfg.get("target_networks") or cfg.get("target_network") or []

        nets = _as_list(nets)

        # normalize yaml forms into refs
        refs = []
        for n in nets:
            if isinstance(n, str):
                refs.append(n)
            elif isinstance(n, dict):
                if n.get("id"):
                    refs.append(str(n["id"]))
                elif n.get("name"):
                    refs.append(str(n["name"]))
                else:
                    raise RuntimeError(f"Invalid network entry (need name or id): {n}")
            else:
                raise RuntimeError(f"Invalid network entry type: {type(n)} value={n}")

    if not refs:
        raise RuntimeError(
            "No target network(s) provided. Set target_networks in migrate.yaml "
            "or pass --target-net."
        )

    net_ids = [resolve_network_id(target, r) for r in refs]
    return net_ids
# ----------------------------
# Security groups sync
# ----------------------------

def normalize_sg_rule(rule: Any) -> Dict[str, Any]:
    # Different SDKs / clouds expose ethertype as either `ethertype` or `ether_type`
    ether = getattr(rule, "ethertype", None)
    if ether is None:
        ether = getattr(rule, "ether_type", None)

    # remote_* fields can vary too; keep robust
    remote_ip = getattr(rule, "remote_ip_prefix", None)
    remote_group = getattr(rule, "remote_group_id", None)

    return {
        "direction": getattr(rule, "direction", None),
        "ethertype": ether,
        "protocol": getattr(rule, "protocol", None),
        "port_range_min": getattr(rule, "port_range_min", None),
        "port_range_max": getattr(rule, "port_range_max", None),
        "remote_ip_prefix": remote_ip,
        "remote_group_id": remote_group,
        "security_group_id": getattr(rule, "security_group_id", None),
        "description": getattr(rule, "description", None),
    }

def discover_security_groups(server: Any) -> List[str]:
    names = []
    for sg in getattr(server, "security_groups", []) or []:
        n = sg.get("name")
        if n:
            names.append(n)
    return names


def sync_security_groups(source: openstack.connection.Connection,
                         target: openstack.connection.Connection,
                         server: Any,
                         state: Dict[str, Any]) -> List[str]:
    sg_names = discover_security_groups(server)
    if not sg_names:
        return []

    src_sgs = list(source.network.security_groups())
    src_by_name = {sg.name: sg for sg in src_sgs}
    src_details = [src_by_name[n] for n in sg_names if n in src_by_name]

    tgt_sgs = list(target.network.security_groups())
    tgt_by_name = {sg.name: sg for sg in tgt_sgs}

    created_map = state.setdefault("security_groups", {})

    # Create missing SGs
    for src_sg in src_details:
        if src_sg.name in tgt_by_name:
            created_map[src_sg.name] = tgt_by_name[src_sg.name].id
            continue
        new_sg = target.network.create_security_group(
            name=src_sg.name,
            description=src_sg.description or ""
        )
        tgt_by_name[new_sg.name] = new_sg
        created_map[src_sg.name] = new_sg.id

    # Existing target rules for dedup
    tgt_rules = []
    for sg_name in sg_names:
        tsg = tgt_by_name.get(sg_name)
        if not tsg:
            continue
        for r in target.network.security_group_rules(security_group_id=tsg.id):
            tgt_rules.append((sg_name, normalize_sg_rule(r)))

    def rule_exists(sg_name: str, r: Dict[str, Any]) -> bool:
        cmp_fields = ["direction", "ethertype", "protocol", "port_range_min", "port_range_max",
                      "remote_ip_prefix", "remote_group_id", "description"]
        for existing_sg_name, existing in tgt_rules:
            if existing_sg_name != sg_name:
                continue
            if all(existing.get(k) == r.get(k) for k in cmp_fields):
                return True
        return False

    # Create rules
    for src_sg in src_details:
        tsg = tgt_by_name[src_sg.name]

        for sr in source.network.security_group_rules(security_group_id=src_sg.id):
            rule = normalize_sg_rule(sr)
            remote_group_id = rule.get("remote_group_id")
            mapped_remote_group_id = None

            if remote_group_id:
                src_remote = source.network.get_security_group(remote_group_id)
                if src_remote and src_remote.name in tgt_by_name:
                    mapped_remote_group_id = tgt_by_name[src_remote.name].id

            payload = {
                "security_group_id": tsg.id,
                "direction": rule["direction"],
                "ethertype": rule["ethertype"],
                "protocol": rule["protocol"],
                "port_range_min": rule["port_range_min"],
                "port_range_max": rule["port_range_max"],
                "remote_ip_prefix": rule["remote_ip_prefix"],
                "remote_group_id": mapped_remote_group_id,
                "description": rule.get("description") or "",
            }

            cmp_rule = {
                "direction": payload["direction"],
                "ethertype": payload["ethertype"],
                "protocol": payload["protocol"],
                "port_range_min": payload["port_range_min"],
                "port_range_max": payload["port_range_max"],
                "remote_ip_prefix": payload["remote_ip_prefix"],
                "remote_group_id": payload["remote_group_id"],
                "description": payload["description"],
            }

            if rule_exists(src_sg.name, cmp_rule):
                continue

            try:
                new_rule = target.network.create_security_group_rule(**payload)
                tgt_rules.append((src_sg.name, normalize_sg_rule(new_rule)))
            except Exception as e:
                msg = str (e)
                if "Security group rule already exists" in msg or "409" in msg:
                    print(f"[INFO] SG rule already exists in {src_sg.name} (skipping)")
                else:
                    print(f"[WARN] Failed to create SG rule in {src_sg.name}: {e}", file=sys.stderr)

    return sg_names


# ----------------------------
# Export / transfer / import
# ----------------------------

def export_image_boot_server(source: openstack.connection.Connection,
                             server: Any,
                             image_name: str) -> Any:
    img = source.compute.create_server_image(server, name=image_name)
    image_id = img if isinstance(img, str) else getattr(img, "id", None) or getattr(img, "image_id", None)
    if not image_id:
        raise RuntimeError("Failed to get snapshot image id from create_server_image()")

    def getter():
        return source.image.get_image(image_id)

    return wait_for_status(getter, "ACTIVE", f"source snapshot image {image_name}", timeout=7200, poll=10)


def export_volume_to_image(source: openstack.connection.Connection,
                           volume: Any,
                           image_name: str,
                           disk_format: str) -> Any:
    res = source.block_storage.upload_volume_to_image(
        volume,
        force=True,
        name=image_name,
        disk_format=disk_format,
        container_format="bare",
    )
    image_id = res.get("image_id") or res.get("os-volume_upload_image", {}).get("image_id")
    if not image_id:
        raise RuntimeError(f"upload-to-image did not return image_id for volume {volume.id}")

    def getter():
        return source.image.get_image(image_id)

    return wait_for_status(getter, "ACTIVE", f"source volume image {image_name}", timeout=7200, poll=10)


def download_image(source: openstack.connection.Connection, image: Any, dest: Path) -> None:
    dest_tmp = dest.with_suffix(dest.suffix + ".part")
    with open(dest_tmp, "wb") as f:
        for chunk in source.image.download_image(image, stream=True):
            f.write(chunk)
    dest_tmp.replace(dest)


def find_target_image_by_name(target: openstack.connection.Connection, name: str) -> Optional[Any]:
    for img in target.image.images(name=name):
        if img.name == name:
            return img
    return None

def cli_image_upload(cloud_name: str,
                     name: str,
                     file_path: Path,
                     disk_format: str) -> str:
    """
    Upload an image using openstackclient (creates image + PUT /file).
    Returns image id.
    """
    cmd = [
        "openstack", "--os-cloud", cloud_name,
        "image", "create", name,
        "--disk-format", disk_format,
        "--container-format", "bare",
        "--private",
        "--file", str(file_path),
        "-f", "value", "-c", "id",
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "CLI image upload failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"Output:\n{e.output}"
        )
    if not out:
        raise RuntimeError("CLI image upload returned empty image id.")
    return out


def upload_image(target: openstack.connection.Connection,
                 cfg: Dict[str, Any],
                 name: str,
                 file_path: Path,
                 disk_format: str) -> Any:
    """
    Use CLI to upload images to target (matches proven working behaviour).
    Then wait with SDK until ACTIVE.
    """
    # Which clouds.yaml entry to use for CLI upload (OIDC)
    cli_cloud = cfg.get("target_cli_upload_cloud") \
        or (cfg.get("target_token_refresh") or {}).get("cloud_name") \
        or cfg.get("target_cloud")

    # If already exists and active, reuse
    existing = find_target_image_by_name(target, name)
    if existing:
        ex = target.image.get_image(existing.id)
        status = (getattr(ex, "status", "") or "").lower()
        size = getattr(ex, "size", None)
        if status == "active" and (size is None or int(size) > 0):
            print(f"[INFO] Target image already ACTIVE, reusing: {name} ({ex.id})")
            return ex
        print(f"[INFO] Deleting incomplete target image {name} ({ex.id}) status={status} size={size}")
        try:
            target.image.delete_image(ex, ignore_missing=True)
        except Exception as de:
            raise RuntimeError(f"Could not delete incomplete image {ex.id}: {de}")

    print(f"[INFO] Uploading image via CLI cloud '{cli_cloud}': {name}")
    img_id = cli_image_upload(cli_cloud, name, file_path, disk_format)

    # Wait until usable
    return wait_for_image_active(target, img_id, name=name, timeout=7200, poll=10)


#def create_volume_from_image(target: openstack.connection.Connection,
#                             vol_name: str,
#                             image: Any,
#                             size_gb: int) -> Any:
#    for v in target.block_storage.volumes(name=vol_name):
#        if v.name == vol_name:
#            return v
#
#    vol = target.block_storage.create_volume(
#        name=vol_name,
#        imageRef=image.id,
#        size=size_gb,
#    )
#

def create_volume_from_image(target, vol_name, image, size_gb, ensure_bootable=False):
    """
    Finds or creates a volume on target from an image.
    Ensures the volume is 'AVAILABLE' and 'bootable'.
    """
    # 1. Check for existing volume
    for v in target.block_storage.volumes(name=vol_name):
        # Exact name match check
        if v.name != vol_name:
            continue
            
        v = target.block_storage.get_volume(v.id)
        status = (v.status or "").upper()
        
        # Robust check for bootable (SDK uses is_bootable, API uses bootable)
        is_boot = getattr(v, 'is_bootable', False) or str(getattr(v, 'bootable', '')).lower() == 'true'

        if status == "AVAILABLE" and (not ensure_bootable or is_boot):
            print(f"[INFO] Using existing valid volume: {vol_name} ({v.id})")
            return v
        
        print(f"[INFO] Found existing volume {vol_name} but it is unsuitable (status={status}, bootable={is_boot}). Deleting...")
        target.block_storage.delete_volume(v, ignore_missing=True)
        # Simple wait for delete
        t0 = time.time()
        while time.time() - t0 < 300:
            if not target.block_storage.find_volume(v.id):
                break
            time.sleep(5)

    # 2. Create new volume
    print(f"[INFO] Creating volume {vol_name} from image {image.name}...")
    vol = target.block_storage.create_volume(
        name=vol_name, 
        imageRef=image.id, 
        size=size_gb
    )
    
    # Wait for AVAILABLE status
    v = wait_for_status(
        lambda: target.block_storage.get_volume(vol.id), 
        "AVAILABLE", 
        f"target volume {vol_name}", 
        timeout=3600, 
        poll=10
    )

    # 3. Handle Bootable Flag
    if ensure_bootable:
        t0 = time.time()
        while True:
            v = target.block_storage.get_volume(v.id)
            is_boot = getattr(v, 'is_bootable', False) or str(getattr(v, 'bootable', '')).lower() == 'true'
            
            if is_boot:
                break
            
            # If not bootable yet, try to force it 
            if time.time() - t0 > 15: 
                print(f"[INFO] Volume {v.id} not yet marked bootable, attempting manual update...")
                try:
                    target.block_storage.set_volume_bootable_status(v, True)
                except Exception as e:
                    print(f"[WARN] Failed to set bootable status (might be policy restricted): {e}")

            if time.time() - t0 > 300:
                raise RuntimeError(f"Timeout: Volume {v.id} reached AVAILABLE but is not bootable.")
            
            time.sleep(5)

    return v

def create_target_server_from_root_volume(
    target: openstack.connection.Connection,
    server_name: str,
    flavor_id: str,
    network_ids: List[str],
    security_group_names: List[str],
    root_volume_id: str,
    wait_active: bool = True,
) -> Any:
    # Avoid duplicate create if server name already exists
    existing = target.compute.find_server(server_name, ignore_missing=True)
    if existing:
        return target.compute.get_server(existing.id)

    networks = [{"uuid": nid} for nid in network_ids]

    bdm = [{
        "boot_index": 0,
        "uuid": root_volume_id,
        "source_type": "volume",
        "destination_type": "volume",
        "delete_on_termination": False,
    }]

    root = target.block_storage.get_volume(root_volume_id)
    if str(getattr(root, "is_bootable", "")).lower() != "true":
        raise RuntimeError(f"Root volume {root_volume_id} is not bootable")

    srv = target.compute.create_server(
        name=server_name,
        flavor_id=flavor_id,
        networks=networks,
        security_groups=[{"name": n} for n in security_group_names],
        block_device_mapping_v2=bdm,
    )

    if not wait_active:
        return srv

    def getter():
        return target.compute.get_server(srv.id)

    return wait_for_status(getter, "ACTIVE", f"target server {server_name}", timeout=3600, poll=10)


def attach_volumes_to_server(
    target: openstack.connection.Connection,
    server_id: str,
    volume_ids: List[str],
    wait: bool = True,
):
    for vid in volume_ids:
        # Skip if already attached
        v = target.block_storage.get_volume(vid)
        atts = getattr(v, "attachments", []) or []
        if any(a.get("server_id") == server_id for a in atts if isinstance(a, dict)):
            continue

        target.compute.create_volume_attachment(server=server_id, volumeId=vid)

        if wait:
            def getter():
                return target.block_storage.get_volume(vid)
            wait_for_status(getter, "IN-USE", f"attach volume {vid}", timeout=1800, poll=5)


def stop_start_if_needed(source: openstack.connection.Connection, server: Any, do_stop: bool, dry_run: bool):
    if not do_stop or dry_run:
        return
    server = source.compute.get_server(server.id)
    if server.status.upper() == "ACTIVE":
        print(f"[INFO] Stopping {server.name} for consistency...")
        source.compute.stop_server(server)

        def getter():
            return source.compute.get_server(server.id)

        wait_for_status(getter, "SHUTOFF", f"server {server.name} stop", timeout=1800, poll=10)


def start_if_stopped(source: openstack.connection.Connection, server: Any, do_stop: bool, dry_run: bool):
    if not do_stop or dry_run:
        return
    server = source.compute.get_server(server.id)
    if server.status.upper() == "SHUTOFF":
        print(f"[INFO] Starting {server.name} back up...")
        source.compute.start_server(server)

        def getter():
            return source.compute.get_server(server.id)

        wait_for_status(getter, "ACTIVE", f"server {server.name} start", timeout=1800, poll=10)


# ----------------------------
# Migration core
# ----------------------------
def image_exists(conn: openstack.connection.Connection, image_id: str) -> bool:
    try:
        conn.image.get_image(image_id)
        return True
    except Exception:
        return False

def migrate_server(
    cfg: Dict[str, Any],
    source: openstack.connection.Connection,
    target: openstack.connection.Connection,
    server_ref: str,
    flavor_map: Dict[str, str],
    dry_run: bool,
    skip_download: bool,
    skip_upload: bool,
    cli_target_nets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    staging = Path(cfg["local_staging_dir"])
    state_dir = Path(cfg["state_dir"])
    ensure_dir(staging)
    ensure_dir(state_dir)

    # --- 1) Find server FIRST (fix NameError) ---
    server = source.compute.find_server(server_ref, ignore_missing=True)
    if not server:
        raise RuntimeError(f"Server not found on source: {server_ref}")
    server = source.compute.get_server(server.id)

    # --- 2) Find per-server config AFTER we have server.name/id ---
    server_cfg = None
    for item in (cfg.get("servers") or []):
        if not isinstance(item, dict):
            continue
        # allow matching by name and/or id if present
        if item.get("id") == server.id or item.get("name") == server.name:
            server_cfg = item
            break

    state_file = state_dir / f"{server.id}.json"
    state = read_state(state_file)
    state.setdefault("source", {})["server_id"] = server.id
    state["source"]["server_name"] = server.name

    print(f"\n=== {'DISCOVER' if dry_run else 'MIGRATE'} {server.name} ({server.id}) ===")

    # Flavor mapping
    src_flavor_id = getattr(server, "flavor", {}).get("id")
    if not src_flavor_id:
        raise RuntimeError(f"Could not determine flavor for {server.name}")
    src_flavor = source.compute.get_flavor(src_flavor_id)
    state["source"]["flavor_name"] = src_flavor.name
    state["source"]["flavor_id"] = src_flavor_id

    tgt_flavor_id, tgt_flavor_name = resolve_target_flavor(source, target, src_flavor_id, flavor_map)
    state.setdefault("target", {})["flavor_id"] = tgt_flavor_id
    state["target"]["flavor_name"] = tgt_flavor_name

    # Security groups
    if dry_run:
        sg_names = discover_security_groups(server)
    else:
        sg_names = sync_security_groups(source, target, server, state)
    state["target"]["security_groups"] = sg_names

    # Root/data volumes
    root_vol, data_vols = pick_root_and_data_volumes(source, server)
    state["source"]["boot_from_volume"] = bool(root_vol)

    disk_format = cfg.get("export_disk_format", "qcow2")
    root_image_name = f"mig-root-{server.id}"

    planned = {
        "root_image_name": root_image_name,
        "root_size_gb": int(root_vol.size) if root_vol else max(int(getattr(src_flavor, "disk", 0) or 0), 10),
        "data_volume_count": len(data_vols),
        "data_image_names": [f"mig-data-{v.id}" for v in data_vols],
        "actions": {
            "sync_security_groups": (not dry_run),
            "export_source_images": (not dry_run),
            "download": (not dry_run and not skip_download),
            "upload": (not dry_run and not skip_upload and not skip_download),
            "create_target_volumes": (not dry_run and (not skip_upload) and (not skip_download)),
        },
    }
    state["plan"] = planned

    if dry_run:
        write_state(state_file, state)
        manual_cli = (
            "openstack server create "
            f"--flavor '{tgt_flavor_name}' "
            f"--volume '<ROOT_VOL_ID_AFTER_CREATE>' "
            + " ".join([f"--security-group '{n}'" for n in sg_names])
            + " --network <TARGET_NET> "
            + f" 'mig-{server.name}'"
        )
        return {
            "mode": "dry-run",
            "source_server_name": server.name,
            "source_server_id": server.id,
            "source_boot_from_volume": state["source"]["boot_from_volume"],
            "source_flavor_name": state["source"]["flavor_name"],
            "source_flavor_id": state["source"]["flavor_id"],
            "target_flavor_name": tgt_flavor_name,
            "target_flavor_id": tgt_flavor_id,
            "target_security_groups": sg_names,
            "planned_root_image_name": root_image_name,
            "planned_root_size_gb": planned["root_size_gb"],
            "planned_data_image_names": planned["data_image_names"],
            "skipped": {"download": skip_download, "upload": skip_upload},
            "manual_boot_command": manual_cli,
            "state_file": str(state_file),
        }

    # Real run: optional stop/start
    stop_start_if_needed(source, server, cfg.get("stop_servers_for_consistency", False), dry_run=False)

    # Export root image on source (self-heal if state points to missing image)
    disk_format = cfg.get("export_disk_format", "qcow2")
    root_image_name = f"mig-root-{server.id}"

    need_root_export = False
    if "root_image" not in state:
        need_root_export = True
    else:
        old_id = state["root_image"].get("source_image_id")
        if not old_id or not image_exists(source, old_id):
            print(f"[WARN] Source root image in state is missing in Glance ({old_id}). Re-exporting...")
            need_root_export = True
            state.pop("root_image", None)
            state.get("downloads", {}).pop("root", None)
    
    if need_root_export:
        if root_vol:
            print(f"[INFO] Root is volume-backed: {root_vol.id} (size={root_vol.size}GB)")
            img = export_volume_to_image(source, root_vol, root_image_name, disk_format)
            root_size_gb = int(root_vol.size)
        else:
            print("[INFO] Root is image-backed; creating server snapshot.")
            img = export_image_boot_server(source, server, root_image_name)
            root_size_gb = max(int(getattr(src_flavor, "disk", 0) or 0), 10)
    
        state["root_image"] = {"name": root_image_name, "source_image_id": img.id, "size_gb": root_size_gb}
        write_state(state_file, state)

    # Export data volumes to images (self-heal missing images)
    state.setdefault("data_images", [])

    # Remove entries whose images no longer exist
    kept = []
    for di in state["data_images"]:
        img_id = di.get("source_image_id")
        if img_id and image_exists(source, img_id):
            kept.append(di)
        else:
            print(f"[WARN] Source data image missing for volume {di.get('source_volume_id')} (image {img_id}); will re-export.")
    state["data_images"] = kept
    write_state(state_file, state)

    exported_vol_ids = {x.get("source_volume_id") for x in state["data_images"]}

    for v in data_vols:
        if v.id in exported_vol_ids:
            continue
        data_image_name = f"mig-data-{v.id}"
        print(f"[INFO] Exporting data volume {v.id} -> image {data_image_name}")
        img = export_volume_to_image(source, v, data_image_name, disk_format)
        state["data_images"].append({
            "name": data_image_name,
            "source_volume_id": v.id,
            "source_image_id": img.id,
            "size_gb": int(v.size),
        })
        write_state(state_file, state)

    start_if_stopped(source, server, cfg.get("stop_servers_for_consistency", False), dry_run=False)

    # Download step
    state.setdefault("downloads", {})
    if skip_download:
        state["skipped"] = {**state.get("skipped", {}), "download": True}
        write_state(state_file, state)
        print("[SKIP] --skip-download set: skipping downloads and all downstream target steps.")
        return build_report_entry_no_target_vols(server, state, sg_names, state_file)

    if "root" not in state["downloads"]:
        src_img = source.image.get_image(state["root_image"]["source_image_id"])
        root_path = staging / f"{state['root_image']['name']}.{disk_format}"
        print(f"[INFO] Downloading root image to {root_path}")
        download_image(source, src_img, root_path)
        state["downloads"]["root"] = {"path": str(root_path), "sha256": sha256_file(root_path)}
        write_state(state_file, state)

    for di in state["data_images"]:
        key = di["name"]
        if key in state["downloads"]:
            continue
        src_img = source.image.get_image(di["source_image_id"])
        p = staging / f"{di['name']}.{disk_format}"
        print(f"[INFO] Downloading data image to {p}")
        download_image(source, src_img, p)
        state["downloads"][key] = {"path": str(p), "sha256": sha256_file(p)}
        write_state(state_file, state)

    # Upload step
    state.setdefault("target", {})
    state["target"].setdefault("images", {})

    if skip_upload:
        state["skipped"] = {**state.get("skipped", {}), "upload": True}
        write_state(state_file, state)
        print("[SKIP] --skip-upload set: will not upload images to target. Will proceed only if images already exist on target.")

        root_existing = find_target_image_by_name(target, state["root_image"]["name"])
        if root_existing:
            state["target"]["images"]["root"] = {"id": root_existing.id, "name": root_existing.name}

        for di in state["data_images"]:
            ex = find_target_image_by_name(target, di["name"])
            if ex:
                state["target"]["images"][di["name"]] = {"id": ex.id, "name": ex.name}

        write_state(state_file, state)
        if "root" not in state["target"]["images"]:
            print("[STOP] Root image not found on target (uploads skipped). Cannot create target volumes.")
            return build_report_entry_no_target_vols(server, state, sg_names, state_file)

    else:
        if "root" not in state["target"]["images"]:
            root_file = Path(state["downloads"]["root"]["path"])
            print(f"[INFO] Uploading root image to target Glance: {state['root_image']['name']}")
            tgt_img = upload_image(target, cfg, state["root_image"]["name"], root_file, disk_format)
            state["target"]["images"]["root"] = {"id": tgt_img.id, "name": tgt_img.name}
            write_state(state_file, state)

        for di in state["data_images"]:
            name = di["name"]
            if name in state["target"]["images"]:
                continue
            fpath = Path(state["downloads"][name]["path"])
            print(f"[INFO] Uploading data image to target Glance: {name}")
            tgt_img = upload_image(target, cfg, name, fpath, disk_format)
            state["target"]["images"][name] = {"id": tgt_img.id, "name": tgt_img.name}
            write_state(state_file, state)

    # Create target volumes from images (no boot)
    state.setdefault("target", {})
    state["target"].setdefault("images", {})
    state["target"].setdefault("volumes", {})
    
    # Ensure root target image exists (self-heal stale IDs)
    root_local = Path(state.get("downloads", {}).get("root", {}).get("path", "")) if state.get("downloads", {}).get("root") else None
    root_img_obj = ensure_target_image(
        target=target,
        cfg=cfg,
        image_key="root",
        image_name=state["root_image"]["name"],
        desired_id=state.get("target", {}).get("images", {}).get("root", {}).get("id"),
        local_file=root_local,
        disk_format=disk_format,
        state=state,
    )
    write_state(state_file, state)
    
    # Wait until actually usable
    root_img_obj = wait_for_image_active(target, root_img_obj.id, name=root_img_obj.name, timeout=7200, poll=10)
    
    if "root" not in state["target"]["volumes"]:
        root_size = int(state["root_image"]["size_gb"])
        vol_name = f"mig-rootvol-{server.id}"
        print(f"[INFO] Creating target root volume {vol_name} (size={root_size}GB) from image {root_img_obj.name}")
        tgt_vol = create_volume_from_image(target, vol_name, root_img_obj, root_size, ensure_bootable=True)
        state["target"]["volumes"]["root"] = {"id": tgt_vol.id, "name": tgt_vol.name, "size_gb": root_size}
        write_state(state_file, state)
    
    # Data volumes: ensure each image exists
    for di in state.get("data_images", []):
        name = di["name"]
        vol_key = f"data:{name}"
        if vol_key in state["target"]["volumes"]:
            continue
    
        local_path = Path(state.get("downloads", {}).get(name, {}).get("path", "")) if state.get("downloads", {}).get(name) else None
        img_obj = ensure_target_image(
            target=target,
            cfg=cfg,
            image_key=name,
            image_name=name,
            desired_id=state.get("target", {}).get("images", {}).get(name, {}).get("id"),
            local_file=local_path,
            disk_format=disk_format,
            state=state,
        )
        write_state(state_file, state)
    
        img_obj = wait_for_image_active(target, img_obj.id, name=img_obj.name, timeout=7200, poll=10)
    
        size_gb = int(di["size_gb"])
        vol_name = f"mig-datavol-{di['source_volume_id']}"
        print(f"[INFO] Creating target data volume {vol_name} (size={size_gb}GB) from image {img_obj.name}")
        tgt_vol = create_volume_from_image(target, vol_name, img_obj, size_gb)
        state["target"]["volumes"][vol_key] = {"id": tgt_vol.id, "name": tgt_vol.name, "size_gb": size_gb}
        write_state(state_file, state)

    manual_cli = (
        "openstack server create "
        f"--flavor '{state['target']['flavor_name']}' "
        f"--volume '{state['target']['volumes']['root']['id']}' "
        + " ".join([f"--security-group '{n}'" for n in sg_names])
        + " --network <TARGET_NET> "
        + f" 'mig-{server.name}'"
    )
    state["target"]["manual_boot_hint"] = {
        "server_name": f"mig-{server.name}",
        "flavor": state["target"]["flavor_name"],
        "root_volume_id": state["target"]["volumes"]["root"]["id"],
        "security_groups": sg_names,
        "example_openstack_cli": manual_cli,
        "note": "Instance creation is intentionally NOT performed by this script.",
    }
    write_state(state_file, state)

    print(f"[DONE] Prepared target artifacts for {server.name} (no boot).")

    # Optionally create the target server (boot from volume) + attach data volumes

    if cfg.get("create_target_server", False):
        net_ids = resolve_target_networks(
            target=target,
            cfg=cfg,
            server_cfg=server_cfg,
            cli_target_nets=cli_target_nets or [],
        )

        prefix = cfg.get("target_server_name_prefix", "mig-")
        target_server_name = f"{prefix}{server.name}"
        wait_active = bool(cfg.get("wait_for_target_server_active", True))

        print(f"[INFO] Creating target server '{target_server_name}' on networks {net_ids} (boot from root volume)")
        srv = create_target_server_from_root_volume(
            target=target,
            server_name=target_server_name,
            flavor_id=state["target"]["flavor_id"],
            network_ids=net_ids,
            security_group_names=sg_names,
            root_volume_id=state["target"]["volumes"]["root"]["id"],
            wait_active=wait_active,
        )
        state["target"]["server"] = {"id": srv.id, "name": target_server_name}
        write_state(state_file, state)

        # Attach data volumes
        if cfg.get("attach_data_volumes", True):
            data_vol_ids = []
            for k, v in state["target"]["volumes"].items():
                if k == "root":
                    continue
                data_vol_ids.append(v["id"])

            if data_vol_ids:
                print(f"[INFO] Attaching {len(data_vol_ids)} data volumes to target server {target_server_name}")
                attach_volumes_to_server(target, srv.id, data_vol_ids, wait=True)

        print(f"[DONE] Target server created: {target_server_name} ({srv.id})")

    return build_report_entry(server, state, sg_names, state_file)
# ----------------------------
# Reporting
# ----------------------------

def build_report_entry_no_target_vols(server: Any, state: Dict[str, Any], sg_names: List[str], state_file: Path) -> Dict[str, Any]:
    manual_cli = (
        "openstack server create "
        f"--flavor '{state.get('target', {}).get('flavor_name', '<FLAVOR>')}' "
        f"--volume '<ROOT_VOL_ID_NOT_CREATED>' "
        + " ".join([f"--security-group '{n}'" for n in sg_names])
        + " --network <TARGET_NET> "
        + f" 'mig-{server.name}'"
    )
    return {
        "mode": "partial",
        "source_server_name": server.name,
        "source_server_id": server.id,
        "source_boot_from_volume": state.get("source", {}).get("boot_from_volume"),
        "source_flavor_name": state.get("source", {}).get("flavor_name"),
        "source_flavor_id": state.get("source", {}).get("flavor_id"),
        "target_flavor_name": state.get("target", {}).get("flavor_name"),
        "target_flavor_id": state.get("target", {}).get("flavor_id"),
        "target_security_groups": sg_names,
        "target_root_volume_id": state.get("target", {}).get("volumes", {}).get("root", {}).get("id"),
        "target_root_volume_name": state.get("target", {}).get("volumes", {}).get("root", {}).get("name"),
        "target_root_volume_size_gb": state.get("target", {}).get("volumes", {}).get("root", {}).get("size_gb"),
        "target_data_volumes": [],
        "target_images": [
            {"key": k, "id": v.get("id"), "name": v.get("name")}
            for k, v in state.get("target", {}).get("images", {}).items()
        ],
        "manual_boot_command": manual_cli,
        "state_file": str(state_file),
        "skipped": state.get("skipped", {}),
    }


def build_report_entry(server: Any, state: Dict[str, Any], sg_names: List[str], state_file: Path) -> Dict[str, Any]:
    data_vol_entries = []
    for k, v in state["target"]["volumes"].items():
        if k == "root":
            continue
        data_vol_entries.append({"key": k, "id": v["id"], "name": v["name"], "size_gb": v["size_gb"]})

    image_entries = []
    for k, v in state["target"]["images"].items():
        image_entries.append({"key": k, "id": v["id"], "name": v["name"]})

    manual_cli = state.get("target", {}).get("manual_boot_hint", {}).get("example_openstack_cli") or ""

    return {
        "mode": "full",
        "source_server_name": server.name,
        "source_server_id": server.id,
        "source_boot_from_volume": state["source"]["boot_from_volume"],
        "source_flavor_name": state["source"]["flavor_name"],
        "source_flavor_id": state["source"]["flavor_id"],
        "target_flavor_name": state["target"]["flavor_name"],
        "target_flavor_id": state["target"]["flavor_id"],
        "target_security_groups": sg_names,
        "target_root_volume_id": state["target"]["volumes"]["root"]["id"],
        "target_root_volume_name": state["target"]["volumes"]["root"]["name"],
        "target_root_volume_size_gb": state["target"]["volumes"]["root"]["size_gb"],
        "target_data_volumes": data_vol_entries,
        "target_images": image_entries,
        "manual_boot_command": manual_cli,
        "state_file": str(state_file),
        "skipped": state.get("skipped", {}),
    }


def write_reports(state_dir: Path, entries: List[Dict[str, Any]]) -> None:
    json_path = state_dir / "migration-report.json"
    json_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    csv_path = state_dir / "migration-report.csv"
    fieldnames = [
        "mode",
        "source_server_name",
        "source_server_id",
        "source_boot_from_volume",
        "source_flavor_name",
        "source_flavor_id",
        "target_flavor_name",
        "target_flavor_id",
        "target_security_groups",
        "target_root_volume_id",
        "target_root_volume_name",
        "target_root_volume_size_gb",
        "target_data_volume_ids",
        "target_data_volume_names",
        "target_data_volume_sizes_gb",
        "target_image_ids",
        "target_image_names",
        "manual_boot_command",
        "skipped_download",
        "skipped_upload",
        "state_file",
    ]

    def join_list(lst: List[str]) -> str:
        return ";".join(lst)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in entries:
            data_vols = e.get("target_data_volumes", []) or []
            data_ids = [dv["id"] for dv in data_vols if dv.get("id")]
            data_names = [dv["name"] for dv in data_vols if dv.get("name")]
            data_sizes = [str(dv["size_gb"]) for dv in data_vols if dv.get("size_gb") is not None]

            imgs = e.get("target_images", []) or []
            img_ids = [im["id"] for im in imgs if im.get("id")]
            img_names = [im["name"] for im in imgs if im.get("name")]

            skipped = e.get("skipped", {}) or {}
            row = {
                "mode": e.get("mode", ""),
                "source_server_name": e.get("source_server_name", ""),
                "source_server_id": e.get("source_server_id", ""),
                "source_boot_from_volume": e.get("source_boot_from_volume", ""),
                "source_flavor_name": e.get("source_flavor_name", ""),
                "source_flavor_id": e.get("source_flavor_id", ""),
                "target_flavor_name": e.get("target_flavor_name", ""),
                "target_flavor_id": e.get("target_flavor_id", ""),
                "target_security_groups": join_list(e.get("target_security_groups", []) or []),
                "target_root_volume_id": e.get("target_root_volume_id", "") or "",
                "target_root_volume_name": e.get("target_root_volume_name", "") or "",
                "target_root_volume_size_gb": e.get("target_root_volume_size_gb", "") or "",
                "target_data_volume_ids": join_list(data_ids),
                "target_data_volume_names": join_list(data_names),
                "target_data_volume_sizes_gb": join_list(data_sizes),
                "target_image_ids": join_list(img_ids),
                "target_image_names": join_list(img_names),
                "manual_boot_command": e.get("manual_boot_command", "") or "",
                "skipped_download": bool(skipped.get("download", False)),
                "skipped_upload": bool(skipped.get("upload", False)),
                "state_file": e.get("state_file", ""),
            }
            w.writerow(row)

    print(f"\n[REPORT] Wrote JSON report: {json_path}")
    print(f"[REPORT] Wrote CSV report : {csv_path}")


# ----------------------------
# Instance selection / CLI
# ----------------------------

def load_instances_from_file(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Instance file not found: {path}")
    items = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    return items


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="OpenStack migration prep (no boot on target).")
    ap.add_argument("config", help="Path to migrate.yaml")
    ap.add_argument("--instance", action="append", default=[],
                    help="Instance name or ID to migrate (repeatable). Overrides migrate.yaml servers.")
    ap.add_argument("--instance-file", default=None,
                    help="File with instance names/IDs (one per line). Overrides migrate.yaml servers.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Discovery only: show what would be exported/created. No changes performed.")
    ap.add_argument("--skip-download", action="store_true",
                    help="Skip downloading images from source. Implies skipping downstream target steps.")
    ap.add_argument("--skip-upload", action="store_true",
                    help="Skip uploading images to target. Will proceed only if images already exist on target.")
    ap.add_argument("--target-net", action="append",default=[], 
                    help="Override target network(s) for created target server. Repeatable. Each value can be a network NAME or ID. Example: --target-net net-dev --target-net net-extra")
    return ap.parse_args()


def main():
    args = parse_args()
    cfg = load_yaml(args.config)

    flavor_map_file = cfg.get("flavor_map_file")
    if not flavor_map_file:
        raise SystemExit("ERROR: flavor_map_file is required.")
    flavor_map = load_flavor_map(flavor_map_file)

    state_dir = Path(cfg.get("state_dir", "./state"))
    ensure_dir(state_dir)

    # Determine instances
    if args.instance_file:
        servers = load_instances_from_file(args.instance_file)
    elif args.instance:
        servers = args.instance
    else:
        servers = cfg.get("servers", [])

    if not servers:
        raise SystemExit("ERROR: No instances specified. Use --instance/--instance-file or set servers: in migrate.yaml")

    # Connections
    source = connect_cloud(cfg["source_cloud"])
    verify_connection(source, "source")
    target = connect_target_with_token_refresh(cfg)
    verify_connection(target, "target")

    entries: List[Dict[str, Any]] = []
    for s in servers:
        if isinstance(s, dict):
            server_ref = s.get("id") or s.get("name")
            if not server_ref:
                raise SystemExit(f"ERROR: invalid server entry in migrate.yaml: {s}")
        else:
            server_ref = s
            
        entries.append(
            migrate_server(
                cfg, source, target, server_ref, flavor_map, # Pass server_ref here!
                dry_run=args.dry_run,
                skip_download=args.skip_download,
                skip_upload=args.skip_upload,
                cli_target_nets=args.target_net
            )
        )

    write_reports(state_dir, entries)


if __name__ == "__main__":
    main()

