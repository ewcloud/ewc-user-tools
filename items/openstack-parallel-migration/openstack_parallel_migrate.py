#!/usr/bin/env python3
#
# License: MIT
# Copyright (c) 2026 EUMETSAT
# See the LICENSE file for more details

# Need to install pv on instance #

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
import subprocess
import shutil
import shlex
from pathlib import Path
from typing import Optional
import openstack
import yaml
from tqdm import tqdm

STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)

IMAGE_STALE_STATUSES = {
    "queued",
    "saving",
    "uploading",
    "importing",
    "deactivated",
    "killed",
    "deleted",
    "pending_delete",
}
IMAGE_READY_STATUSES = {"active"}
SOURCE_IMAGE_WAITABLE = {"queued", "saving", "uploading", "importing"}
SNAPSHOT_WAITABLE = {"creating"}
VOLUME_WAITABLE = {"creating", "downloading", "uploading"}
VOLUME_FAIL_STATUSES = {"error", "error_restoring", "error_extending", "error_managing"}
SERVER_FAIL_STATUSES = {"ERROR"}

STREAM_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

PRINT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    with PRINT_LOCK:
        tqdm.write(f"[INFO] {msg}")


def warn(msg: str) -> None:
    with PRINT_LOCK:
        tqdm.write(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    with PRINT_LOCK:
        tqdm.write(f"[ERROR] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def state_path(server_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in server_name)
    return STATE_DIR / f"{safe}.json"


def load_state(server_name: str) -> dict:
    path = state_path(server_name)
    if not path.exists():
        return {
            "server_name": server_name,
            "artifact_order": [],
            "artifacts": {},
            "ports": [],
            "source_server_id": None,
            "source_server_was_volume_backed": None,
            "target_server_id": None,
            "target_server_name": None,
            "target_root_created": False,
            "target_data_attached": [],
            "target_started": False,
            "current_stage": None,
            "started_at": None,
            "updated_at": None,
        }
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, payload: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def save_state(server_name: str, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not state.get("started_at"):
        state["started_at"] = state["updated_at"]
    atomic_write_json(state_path(server_name), state)


def set_stage(server_name: str, state: dict, stage: str) -> None:
    state["current_stage"] = stage
    save_state(server_name, state)
    log(f"{server_name}: {stage}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_yaml_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping/object")
    return data


def load_flavor_map(path: str | None) -> dict:
    if not path:
        return {}
    data = load_yaml_file(path)
    return {str(k): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# OpenStack connection
# ---------------------------------------------------------------------------

def connect(cloud_name: str):
    try:
        conn = openstack.connect(cloud=cloud_name)
        conn.authorize()
        return conn
    except Exception as e:
        raise RuntimeError(
            f"Failed to connect/authenticate to cloud '{cloud_name}'. "
            f"Check clouds.yaml (including OIDC auth plugin settings if used). "
            f"Original error: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Generic wait helpers
# ---------------------------------------------------------------------------

def wait_for_status(
    getter,
    resource_id: str,
    wanted: str,
    fail_states: set[str] | None = None,
    timeout: int = 3600,
    interval: int = 5,
    desc: str | None = None,
    heartbeat_every: int = 60,
):
    start = time.time()
    last_heartbeat = 0

    while True:
        obj = getter(resource_id)
        status = getattr(obj, "status", None) or getattr(obj, "state", None)

        if status == wanted:
            return obj

        if fail_states and status in fail_states:
            raise RuntimeError(f"-- {desc or resource_id} entered failure state {status}")

        now = time.time()
        if now - last_heartbeat >= heartbeat_every:
            log(f"-- Waiting for {desc or resource_id}: current status={status}, target={wanted}")
            last_heartbeat = now

        if now - start > timeout:
            raise TimeoutError(f"-- Timeout waiting for {desc or resource_id} -> {wanted}, current={status}")

        time.sleep(interval)


def wait_until_deleted(getter, resource_id: str, timeout: int = 900, interval: int = 5, desc: str | None = None):
    start = time.time()
    while True:
        try:
            obj = getter(resource_id)
        except Exception:
            return
        if obj is None:
            return
        if time.time() - start > timeout:
            raise TimeoutError(f"-- Timeout waiting for deletion of {desc or resource_id}")
        time.sleep(interval)


def wait_for_image_ready(conn, image_id: str, timeout: int = 7200, interval: int = 10, desc: str | None = None):
    start = time.time()
    last_heartbeat = 0
    desc = desc or f"image {image_id}"

    while True:
        img = conn.image.find_image(image_id, ignore_missing=True)
        status = getattr(img, "status", None) if img else None

        if status == "active":
            return img

        if status in {"killed", "deleted", "deactivated"}:
            raise RuntimeError(f"-- {desc} entered failure state {status}")

        now = time.time()
        if now - last_heartbeat >= 60:
            log(f"-- Waiting for {desc}: current status={status}, target=active")
            last_heartbeat = now

        try:
            stream = conn.image.download_image(image_id, stream=True)
            iterator = iter(stream)
            first_chunk = next(iterator, None)
            if first_chunk is not None:
                log(f"-- {desc} is downloadable; treating it as ready")
                return img or conn.image.get_image(image_id)
        except Exception:
            pass

        if now - start > timeout:
            raise TimeoutError(f"-- Timeout waiting for {desc} -> active, current={status}")

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stable_name(*parts: str) -> str:
    raw = "::".join(parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    base = "-".join(p for p in parts if p)
    base = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in base)
    return f"{base}-{digest}"


def iter_server_names(source, configured_servers):
    if configured_servers == "all":
        return [s.name for s in source.compute.servers(all_projects=False)]
    if not isinstance(configured_servers, list):
        raise RuntimeError("servers in migrate.yaml must be a list or the string 'all'")
    return [str(x) for x in configured_servers]


def get_server(source, server_name: str):
    srv = source.compute.find_server(server_name, ignore_missing=True)
    if not srv:
        raise RuntimeError(f"Source server '{server_name}' not found")
    return source.compute.get_server(srv.id)


def flavor_name_from_server(source, server) -> str:
    flavor_info = getattr(server, "flavor", {}) or {}
    original_name = flavor_info.get("original_name")
    if original_name:
        return original_name
    flavor_id = flavor_info.get("id")
    if not flavor_id:
        raise RuntimeError(f"{server.name}: could not determine source flavor")
    flv = source.compute.get_flavor(flavor_id)
    return flv.name


def flavor_root_disk_gb_from_server(source, server) -> int:
    flavor_info = getattr(server, "flavor", {}) or {}
    disk = flavor_info.get("disk")
    if disk is not None:
        try:
            return int(disk)
        except Exception:
            pass
    flavor_id = flavor_info.get("id")
    if not flavor_id:
        return 0
    flv = source.compute.get_flavor(flavor_id)
    try:
        return int(getattr(flv, "disk", 0) or 0)
    except Exception:
        return 0


def bytes_to_gib_ceil(value) -> int:
    if value is None:
        return 1
    try:
        value = int(value)
    except Exception:
        return 1
    gib = 1024 ** 3
    return max(1, math.ceil(value / gib))


def volume_attachment_device(vol, server_id: str):
    for att in getattr(vol, "attachments", []) or []:
        if att.get("server_id") == server_id:
            return att.get("device") or att.get("mountpoint")
    return None


def artifact_key(kind: str, source_id: str) -> str:
    return f"{kind}:{source_id}"


def record_artifacts_in_state(server_name: str, state: dict, artifacts: list[dict]) -> None:
    state["artifact_order"] = []
    for art in artifacts:
        entry = {
            "kind": art["kind"],
            "role": art["role"],
            "source_id": art["source_id"],
            "device_name": art.get("device_name"),
            "boot_index": art.get("boot_index"),
            "source_flavor_root_disk_gb": art.get("source_flavor_root_disk_gb"),
        }
        state["artifact_order"].append(entry)
    save_state(server_name, state)


def load_artifacts_from_state(state: dict) -> list[dict]:
    return list(state.get("artifact_order", []) or [])

def _normalize_glance_base(endpoint: str) -> str:
    """
    Convert a Glance endpoint into a base URL without a trailing /v2.

    Examples:
      https://glance.example.com:9292           -> https://glance.example.com:9292
      https://glance.example.com:9292/          -> https://glance.example.com:9292
      https://glance.example.com:9292/v2        -> https://glance.example.com:9292
      https://glance.example.com:9292/v2/       -> https://glance.example.com:9292
    """
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v2"):
        endpoint = endpoint[:-3]
    return endpoint.rstrip("/")


def _get_token_and_glance_base(conn):
    token = conn.session.get_token()
    endpoint = conn.image.get_endpoint()
    base = _normalize_glance_base(endpoint)
    return token, base

def _target_supports_import(target) -> bool:
    """
    If your target cloud supports glance-direct import, return True.
    Otherwise return False and the code will use direct file upload.
    """
    return False


def reserve_progress_lines(n: int) -> None:
    for _ in range(max(0, n)):
        print()


def _build_positioned_pv_cmd(
    image_size: Optional[int] = None,
    label: Optional[str] = None,
    line_up: int = 1,
) -> list[str]:
    pv = "exec pv --cursor -f -i 1 -p -t -e -r -b"
    if image_size and image_size > 0:
        pv += f" -s {int(image_size)}"
    if label:
        pv += f" -N {shlex.quote(label)}"

    script = f'printf "\\033[{int(line_up)}A" >&2; {pv}'
    return ["bash", "-lc", script]

def stream_image_via_curl(
    source,
    target,
    source_image_id: str,
    target_image_id: str,
    image_size: int | None = None,
    label: str | None = None,
    progress_line_up: int | None = None,
):
    src_token, src_base = _get_token_and_glance_base(source)
    tgt_token, tgt_base = _get_token_and_glance_base(target)

    src_url = f"{src_base}/v2/images/{source_image_id}/file"

    if _target_supports_import(target):
        tgt_url = f"{tgt_base}/v2/images/{target_image_id}/stage"
    else:
        tgt_url = f"{tgt_base}/v2/images/{target_image_id}/file"

    src_cmd = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--no-buffer",
        "--http1.1",
        "-H", f"X-Auth-Token: {src_token}",
        src_url,
    ]

    have_pv = shutil.which("pv") is not None
    pv_cmd = None

    if have_pv:
        if progress_line_up and progress_line_up > 0:
            pv_cmd = _build_positioned_pv_cmd(
                image_size=image_size,
                label=label,
                line_up=progress_line_up,
            )
        else:
            pv_cmd = [
                "pv",
                "--cursor",
                "-f",
                "-i", "1",
                "-p",
                "-t",
                "-e",
                "-r",
                "-b",
            ]
            if image_size and image_size > 0:
                pv_cmd += ["-s", str(image_size)]
            if label:
                pv_cmd += ["-N", label]

    tgt_cmd = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--http1.1",
        "-X", "PUT",
        "-H", f"X-Auth-Token: {tgt_token}",
        "-H", "Content-Type: application/octet-stream",
        "-H", "Expect:",
        "--upload-file", "-",
        tgt_url,
    ]

    log(f"Source curl URL: {src_url}")
    log(f"Target curl URL: {tgt_url}")

    p1 = subprocess.Popen(
        src_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )

    p_pv = None
    if have_pv:
        p_pv = subprocess.Popen(
            pv_cmd,
            stdin=p1.stdout,
            stdout=subprocess.PIPE,
            stderr=None,
            text=False,
        )
        if p1.stdout is not None:
            p1.stdout.close()

        p2 = subprocess.Popen(
            tgt_cmd,
            stdin=p_pv.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        if p_pv.stdout is not None:
            p_pv.stdout.close()
    else:
        p2 = subprocess.Popen(
            tgt_cmd,
            stdin=p1.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        if p1.stdout is not None:
            p1.stdout.close()

    out2, err2 = p2.communicate()

    if p_pv is not None:
        pv_rc = p_pv.wait()
        if progress_line_up and progress_line_up > 0:
            print(f"\033[{int(progress_line_up)}B", end="", flush=True)
    else:
        pv_rc = 0

    out1, err1 = p1.communicate()

    err1_txt = err1.decode(errors="replace") if err1 else ""
    err2_txt = err2.decode(errors="replace") if err2 else ""

    if p2.returncode != 0:
        raise RuntimeError(f"Target curl upload failed rc={p2.returncode}: {err2_txt}")

    if p_pv is not None and pv_rc != 0:
        raise RuntimeError(f"pv failed rc={pv_rc}")

    if p1.returncode != 0:
        raise RuntimeError(f"Source curl download failed rc={p1.returncode}: {err1_txt}")



# ---------------------------------------------------------------------------
# Source storage detection
# ---------------------------------------------------------------------------

def list_cinder_volumes_attached_to_server(source, server) -> list:
    attached = []
    for vol in source.block_storage.volumes(details=True):
        for att in getattr(vol, "attachments", []) or []:
            if att.get("server_id") == server.id:
                attached.append(vol)
                break
    return attached


def detect_source_workload_storage(source, server) -> list[dict]:
    srv = source.compute.get_server(server.id)
    bdm = getattr(srv, "block_device_mapping", None) or []
    attached_vols = list_cinder_volumes_attached_to_server(source, srv)
    attached_by_id = {v.id: v for v in attached_vols}

    volume_entries = []
    root_volume_id = None

    if isinstance(bdm, list):
        for entry in bdm:
            if not isinstance(entry, dict):
                continue
            vol_id = entry.get("uuid") or entry.get("volume_id") or entry.get("source_id")
            if not vol_id:
                continue
            boot_index = entry.get("boot_index")
            try:
                bi = int(boot_index)
            except Exception:
                bi = None
            device_name = entry.get("device_name")
            volume_entries.append({
                "source_id": vol_id,
                "boot_index": bi,
                "device_name": device_name,
            })
            if bi == 0:
                root_volume_id = vol_id

    if root_volume_id:
        artifacts = []
        root_vol = attached_by_id.get(root_volume_id)
        root_device = volume_attachment_device(root_vol, srv.id) if root_vol else None
        root_bdm = next((x for x in volume_entries if x["source_id"] == root_volume_id), {})
        artifacts.append({
            "kind": "volume",
            "role": "root",
            "source_id": root_volume_id,
            "device_name": root_bdm.get("device_name") or root_device,
            "boot_index": 0,
            "source_flavor_root_disk_gb": None,
        })

        data_candidates = []
        for vol in attached_vols:
            if vol.id == root_volume_id:
                continue
            bdm_info = next((x for x in volume_entries if x["source_id"] == vol.id), {})
            device_name = bdm_info.get("device_name") or volume_attachment_device(vol, srv.id)
            boot_index = bdm_info.get("boot_index")
            data_candidates.append({
                "kind": "volume",
                "role": "data",
                "source_id": vol.id,
                "device_name": device_name,
                "boot_index": boot_index if boot_index is not None else 9999,
                "source_flavor_root_disk_gb": None,
            })

        data_candidates.sort(key=lambda x: (x.get("boot_index", 9999), x.get("device_name") or "", x["source_id"]))
        artifacts.extend(data_candidates)
        return artifacts

    warn(f"{server.name}: Nova block_device_mapping did not reveal root volume cleanly, checking image-backed boot")

    server_image = getattr(srv, "image", None) or {}
    image_id = server_image.get("id") if isinstance(server_image, dict) else None

    if image_id:
        flavor_root_disk_gb = flavor_root_disk_gb_from_server(source, srv)
        artifacts = [{
            "kind": "image",
            "role": "root",
            "source_id": image_id,
            "device_name": None,
            "boot_index": 0,
            "source_flavor_root_disk_gb": flavor_root_disk_gb,
        }]

        data_candidates = []
        for vol in attached_vols:
            device_name = volume_attachment_device(vol, srv.id)
            data_candidates.append({
                "kind": "volume",
                "role": "data",
                "source_id": vol.id,
                "device_name": device_name,
                "boot_index": 9999,
                "source_flavor_root_disk_gb": None,
            })
        data_candidates.sort(key=lambda x: (x.get("device_name") or "", x["source_id"]))
        artifacts.extend(data_candidates)
        return artifacts

    if attached_vols:
        bootable = [v for v in attached_vols if str(getattr(v, "is_bootable", "")).lower() == "true"]
        if len(bootable) == 1:
            root_vol = bootable[0]
            artifacts = [{
                "kind": "volume",
                "role": "root",
                "source_id": root_vol.id,
                "device_name": volume_attachment_device(root_vol, srv.id),
                "boot_index": 0,
                "source_flavor_root_disk_gb": None,
            }]
            data_candidates = []
            for vol in attached_vols:
                if vol.id == root_vol.id:
                    continue
                data_candidates.append({
                    "kind": "volume",
                    "role": "data",
                    "source_id": vol.id,
                    "device_name": volume_attachment_device(vol, srv.id),
                    "boot_index": 9999,
                    "source_flavor_root_disk_gb": None,
                })
            data_candidates.sort(key=lambda x: (x.get("device_name") or "", x["source_id"]))
            artifacts.extend(data_candidates)
            return artifacts

    raise RuntimeError(f"{server.name}: could not determine whether the workload is booted from volume or from image")


def is_volume_backed(artifacts: list[dict]) -> bool:
    return bool(artifacts) and artifacts[0]["kind"] == "volume"


# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

def sg_rule_ethertype(rule):
    return getattr(rule, "ether_type", getattr(rule, "ethertype", None))


def normalize_sg_rule_dict(rule_dict: dict) -> dict:
    return {
        "direction": rule_dict.get("direction"),
        "ether_type": rule_dict.get("ether_type"),
        "protocol": rule_dict.get("protocol"),
        "port_range_min": rule_dict.get("port_range_min"),
        "port_range_max": rule_dict.get("port_range_max"),
        "remote_ip_prefix": rule_dict.get("remote_ip_prefix"),
        "remote_group_id": rule_dict.get("remote_group_id"),
    }


def normalize_sg_rule_obj(rule) -> dict:
    return {
        "direction": getattr(rule, "direction", None),
        "ether_type": sg_rule_ethertype(rule),
        "protocol": getattr(rule, "protocol", None),
        "port_range_min": getattr(rule, "port_range_min", None),
        "port_range_max": getattr(rule, "port_range_max", None),
        "remote_ip_prefix": getattr(rule, "remote_ip_prefix", None),
        "remote_group_id": getattr(rule, "remote_group_id", None),
    }


def build_sg_rule_payload(rule, target_sg_id: str, remote_group_id: str | None = None) -> dict:
    payload = {
        "security_group_id": target_sg_id,
        "direction": getattr(rule, "direction", None),
        "ether_type": sg_rule_ethertype(rule),
    }
    if getattr(rule, "protocol", None) is not None:
        payload["protocol"] = rule.protocol
    if getattr(rule, "port_range_min", None) is not None:
        payload["port_range_min"] = rule.port_range_min
    if getattr(rule, "port_range_max", None) is not None:
        payload["port_range_max"] = rule.port_range_max
    if getattr(rule, "remote_ip_prefix", None):
        payload["remote_ip_prefix"] = rule.remote_ip_prefix
    if remote_group_id:
        payload["remote_group_id"] = remote_group_id
    return payload


def ensure_security_groups(source, target, server) -> list[str]:
    src_srv = source.compute.get_server(server.id)
    attached_sg_names = [sg["name"] for sg in getattr(src_srv, "security_groups", [])]
    if not attached_sg_names:
        return []

    chosen_names = []
    src_sg_cache_by_id = {sg.id: sg for sg in source.network.security_groups()}
    tgt_sg_cache_by_name = {sg.name: sg for sg in target.network.security_groups()}

    for name in attached_sg_names:
        src_sg = source.network.find_security_group(name, ignore_missing=True)
        if not src_sg:
            raise RuntimeError(f"{server.name}: source security group '{name}' not found")

        tgt_sg = tgt_sg_cache_by_name.get(name)
        if not tgt_sg:
            log(f"{server.name}: creating target security group '{name}'")
            tgt_sg = target.network.create_security_group(name=name)
            tgt_sg_cache_by_name[tgt_sg.name] = tgt_sg

        def map_remote_group(src_remote_group_id: str | None):
            if not src_remote_group_id:
                return None
            src_remote = src_sg_cache_by_id.get(src_remote_group_id)
            if not src_remote:
                warn(f"{server.name}: remote_group_id={src_remote_group_id} not visible on source; omitting remote_group_id")
                return None
            tgt_remote = tgt_sg_cache_by_name.get(src_remote.name)
            if not tgt_remote:
                log(f"{server.name}: creating referenced remote security group '{src_remote.name}'")
                tgt_remote = target.network.create_security_group(name=src_remote.name)
                tgt_sg_cache_by_name[tgt_remote.name] = tgt_remote
            return tgt_remote.id

        src_rules = list(source.network.security_group_rules(security_group_id=src_sg.id))
        tgt_rules = list(target.network.security_group_rules(security_group_id=tgt_sg.id))
        existing = {json.dumps(normalize_sg_rule_obj(r), sort_keys=True) for r in tgt_rules}

        for rule in src_rules:
            mapped_remote_group_id = map_remote_group(getattr(rule, "remote_group_id", None))
            payload = build_sg_rule_payload(rule, tgt_sg.id, mapped_remote_group_id)

            key = json.dumps(
                normalize_sg_rule_dict({
                    "direction": payload.get("direction"),
                    "ether_type": payload.get("ether_type"),
                    "protocol": payload.get("protocol"),
                    "port_range_min": payload.get("port_range_min"),
                    "port_range_max": payload.get("port_range_max"),
                    "remote_ip_prefix": payload.get("remote_ip_prefix"),
                    "remote_group_id": payload.get("remote_group_id"),
                }),
                sort_keys=True,
            )

            if key in existing:
                continue

            try:
                target.network.create_security_group_rule(**payload)
                existing.add(key)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "conflict" in msg or "409" in msg:
                    existing.add(key)
                    continue
                raise RuntimeError(f"{server.name}: failed creating SG rule in '{tgt_sg.name}': {e}") from e

        chosen_names.append(tgt_sg.name)

    return chosen_names


# ---------------------------------------------------------------------------
# Networking / ports
# ---------------------------------------------------------------------------

def source_fixed_ip_plan(server) -> list[dict]:
    plan = []
    for network_name, addr_list in (getattr(server, "addresses", {}) or {}).items():
        for addr in addr_list:
            if addr.get("OS-EXT-IPS:type") == "fixed":
                plan.append({"network_name": network_name, "fixed_ip": addr["addr"]})
    return plan


def ensure_target_ports(target, server, sg_names: list[str], state: dict) -> list[str]:
    existing_port_ids = []
    for port_id in list(state.get("ports", []) or []):
        try:
            port = target.network.get_port(port_id)
        except Exception:
            port = None
        if port:
            existing_port_ids.append(port.id)

    if state.get("ports") and len(existing_port_ids) == len(state.get("ports")):
        state["ports"] = existing_port_ids
        return existing_port_ids

    state["ports"] = []
    
    plan = source_fixed_ip_plan(server)
    if not plan:
        raise RuntimeError(f"{server.name}: no fixed IPs discovered on source server")

    sg_ids = []
    for name in sg_names:
        sg = target.network.find_security_group(name, ignore_missing=True)
        if not sg:
            raise RuntimeError(f"{server.name}: target security group '{name}' missing before port creation")
        sg_ids.append(sg.id)

    created = []
    try:
        for item in plan:
            network_name = item["network_name"]
            fixed_ip = item["fixed_ip"]

            tgt_net = target.network.find_network(network_name, ignore_missing=True)
            if not tgt_net:
                raise RuntimeError(f"{server.name}: target network '{network_name}' not found")

            desired_name = stable_name("migrated-port", server.name, network_name, fixed_ip)
            reused = False

            for port in target.network.ports(network_id=tgt_net.id):
                fixed_ips = getattr(port, "fixed_ips", []) or []
                if port.name == desired_name and any(x.get("ip_address") == fixed_ip for x in fixed_ips):
                    created.append(port.id)
                    reused = True
                    break

            if reused:
                continue

            port = target.network.create_port(
                name=desired_name,
                network_id=tgt_net.id,
                fixed_ips=[{"ip_address": fixed_ip}],
                security_group_ids=sg_ids,
            )
            created.append(port.id)

        state["ports"] = created
        return created
    except Exception:
        for port_id in created:
            with contextlib.suppress(Exception):
                target.network.delete_port(port_id, ignore_missing=True)
        raise


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def cleanup_target_image(target, image_id: str | None):
    if not image_id:
        return
    with contextlib.suppress(Exception):
        target.image.delete_image(image_id, ignore_missing=True)
    with contextlib.suppress(Exception):
        wait_until_deleted(lambda rid: target.image.find_image(rid, ignore_missing=True), image_id, desc=f"image {image_id}")


def cleanup_target_volume(target, volume_id: str | None):
    if not volume_id:
        return
    with contextlib.suppress(Exception):
        target.block_storage.delete_volume(volume_id, force=True, ignore_missing=True)
    with contextlib.suppress(Exception):
        wait_until_deleted(lambda rid: target.block_storage.find_volume(rid, ignore_missing=True), volume_id, desc=f"volume {volume_id}")


def cleanup_source_snapshot(source, snapshot_id: str | None):
    if not snapshot_id:
        return
    with contextlib.suppress(Exception):
        source.block_storage.delete_snapshot(snapshot_id, ignore_missing=True)
    with contextlib.suppress(Exception):
        wait_until_deleted(lambda rid: source.block_storage.find_snapshot(rid, ignore_missing=True), snapshot_id, desc=f"snapshot {snapshot_id}")


def cleanup_source_volume(source, volume_id: str | None):
    if not volume_id:
        return
    with contextlib.suppress(Exception):
        source.block_storage.delete_volume(volume_id, force=True, ignore_missing=True)
    with contextlib.suppress(Exception):
        wait_until_deleted(lambda rid: source.block_storage.find_volume(rid, ignore_missing=True), volume_id, desc=f"source temp volume {volume_id}")


# ---------------------------------------------------------------------------
# Source temp artifact resume helpers
# ---------------------------------------------------------------------------

def get_or_init_artifact_state(state: dict, source_kind: str, source_id: str, role: str, device_name=None) -> dict:
    key = artifact_key(source_kind, source_id)
    artifacts = state.setdefault("artifacts", {})
    entry = artifacts.get(key)
    if not entry:
        entry = {
            "role": role,
            "source_kind": source_kind,
            "source_id": source_id,
            "device_name": device_name,
            "source_temp_image_id": None,
            "source_temp_snapshot_id": None,
            "source_temp_clone_volume_id": None,
            "target_image_id": None,
            "target_image_name": None,
            "target_volume_id": None,
            "target_volume_name": None,
        }
        artifacts[key] = entry
    else:
        if device_name and not entry.get("device_name"):
            entry["device_name"] = device_name
    return entry


def persist(server_name: str, state: dict):
    save_state(server_name, state)


def get_source_image_if_reusable(source, image_id: str | None):
    if not image_id:
        return None
    img = source.image.find_image(image_id, ignore_missing=True)
    if not img:
        return None
    status = getattr(img, "status", None)
    if status == "active":
        return img
    if status in SOURCE_IMAGE_WAITABLE:
        return wait_for_image_ready(source, image_id, desc=f"source temp image {image_id}")
    cleanup_target_image(source, image_id) if False else None  # no-op marker
    with contextlib.suppress(Exception):
        source.image.delete_image(image_id, ignore_missing=True)
    return None


def get_source_snapshot_if_reusable(source, snapshot_id: str | None):
    if not snapshot_id:
        return None
    snap = source.block_storage.find_snapshot(snapshot_id, ignore_missing=True)
    if not snap:
        return None
    status = getattr(snap, "status", None)
    if status == "available":
        return snap
    if status in SNAPSHOT_WAITABLE:
        return wait_for_status(
            lambda rid: source.block_storage.get_snapshot(rid),
            snapshot_id,
            wanted="available",
            fail_states={"error"},
            timeout=7200,
            interval=10,
            desc=f"source snapshot {snapshot_id}",
        )
    cleanup_source_snapshot(source, snapshot_id)
    return None


def get_source_volume_if_reusable(source, volume_id: str | None):
    if not volume_id:
        return None
    vol = source.block_storage.find_volume(volume_id, ignore_missing=True)
    if not vol:
        return None
    status = getattr(vol, "status", None)
    if status == "available":
        return vol
    if status in VOLUME_WAITABLE:
        return wait_for_status(
            lambda rid: source.block_storage.get_volume(rid),
            volume_id,
            wanted="available",
            fail_states=VOLUME_FAIL_STATUSES,
            timeout=7200,
            interval=10,
            desc=f"source temp volume {volume_id}",
        )
    if status in VOLUME_FAIL_STATUSES:
        cleanup_source_volume(source, volume_id)
        return None
    return None


def cleanup_source_temp_resources_for_artifact(source, server_name: str, state: dict, artifact_state: dict):
    """
    Best-effort cleanup of temporary source-side artifacts.
    - Do not block waiting for actual deletion.
    - Just issue delete requests and clear state.
    - On rerun, the resume logic should detect leftovers and either reuse or
      recreate as needed.
    """
    img_id = artifact_state.get("source_temp_image_id")
    clone_id = artifact_state.get("source_temp_clone_volume_id")
    snap_id = artifact_state.get("source_temp_snapshot_id")

    if img_id:
        try:
            log(f"{server_name}: requesting deletion of source temp image {img_id}")
            source.image.delete_image(img_id, ignore_missing=True)
        except Exception as e:
            warn(f"{server_name}: failed requesting deletion of source temp image {img_id}: {e}")

    if clone_id:
        try:
            log(f"{server_name}: requesting deletion of source temp clone volume {clone_id}")
            source.block_storage.delete_volume(clone_id, force=True, ignore_missing=True)
        except Exception as e:
            warn(f"{server_name}: failed requesting deletion of source temp clone volume {clone_id}: {e}")

    if snap_id:
        try:
            log(f"{server_name}: requesting deletion of source temp snapshot {snap_id}")
            source.block_storage.delete_snapshot(snap_id, ignore_missing=True)
        except Exception as e:
            warn(f"{server_name}: failed requesting deletion of source temp snapshot {snap_id}: {e}")

    artifact_state["source_temp_image_id"] = None
    artifact_state["source_temp_clone_volume_id"] = None
    artifact_state["source_temp_snapshot_id"] = None
    persist(server_name, state)

# ---------------------------------------------------------------------------
# Source export helpers
# ---------------------------------------------------------------------------

def make_available_clone_for_inuse_volume(source, src_volume, server_name: str, state: dict, artifact_state: dict):
    snap = get_source_snapshot_if_reusable(source, artifact_state.get("source_temp_snapshot_id"))
    if not snap:
        snap_name = stable_name("mig-snap", src_volume.id)
        log(f"Source volume {src_volume.id} is in-use; creating snapshot {snap_name}")
        snap = source.block_storage.create_snapshot(volume_id=src_volume.id, name=snap_name, force=True)
        artifact_state["source_temp_snapshot_id"] = snap.id
        persist(server_name, state)
        snap = wait_for_status(
            lambda rid: source.block_storage.get_snapshot(rid),
            snap.id,
            wanted="available",
            fail_states={"error"},
            timeout=7200,
            interval=10,
            desc=f"source snapshot {snap.id}",
        )

    clone = get_source_volume_if_reusable(source, artifact_state.get("source_temp_clone_volume_id"))
    if clone:
        return clone, snap.id

    tmp_vol_name = stable_name("mig-clone", src_volume.id)
    log(f"{server_name}: Creating temporary clone volume {tmp_vol_name} from snapshot {snap.id}")
    clone = source.block_storage.create_volume(name=tmp_vol_name, snapshot_id=snap.id, size=int(src_volume.size))
    artifact_state["source_temp_clone_volume_id"] = clone.id
    persist(server_name, state)
    clone = wait_for_status(
        lambda rid: source.block_storage.get_volume(rid),
        clone.id,
        wanted="available",
        fail_states=VOLUME_FAIL_STATUSES,
        timeout=7200,
        interval=10,
        desc=f"source temp volume {clone.id}",
    )
    return clone, snap.id


def export_source_volume_to_source_image(source, src_volume, server_name: str, state: dict, artifact_state: dict, disk_format: str = "qcow2"):
    existing_img = get_source_image_if_reusable(source, artifact_state.get("source_temp_image_id"))
    if existing_img:
        return existing_img

    upload_source_volume = src_volume
    vol_status = getattr(src_volume, "status", None)

    if vol_status == "in-use":
        clone_vol, _ = make_available_clone_for_inuse_volume(source, src_volume, server_name, state, artifact_state)
        upload_source_volume = clone_vol
    elif vol_status != "available":
        raise RuntimeError(f"Source volume {src_volume.id} has unsupported status '{vol_status}' for export")

    img_name = stable_name("src-export", src_volume.id, disk_format)
    log(f"{server_name}: Exporting source volume {upload_source_volume.id} -> source image {img_name} ({disk_format})")
    result = source.block_storage.upload_volume_to_image(
        upload_source_volume,
        force=False,
        image_name=img_name,
        disk_format=disk_format,
        container_format="bare",
        visibility="private",
        protected=False,
    )

    image_id = (
        getattr(result, "id", None)
        or (result.get("image_id") if isinstance(result, dict) else None)
        or (result.get("id") if isinstance(result, dict) else None)
    )
    if not image_id and isinstance(result, dict):
        image_id = result.get("os-volume_upload_image", {}).get("image_id")

    if not image_id:
        raise RuntimeError(f"Could not determine exported image id for source volume {src_volume.id}")

    artifact_state["source_temp_image_id"] = image_id
    persist(server_name, state)
    log(f"{server_name}: Source export image created: {image_id}")

    return wait_for_image_ready(source, image_id, timeout=7200, interval=10, desc=f"source export image {image_id}")


def snapshot_server_to_source_image(source, src_server, server_name: str, state: dict, artifact_state: dict):
    existing_img = get_source_image_if_reusable(source, artifact_state.get("source_temp_image_id"))
    if existing_img:
        return existing_img

    img_name = stable_name("src-server-snapshot", src_server.id, "snapshot")
    log(f"{src_server.name}: creating source server snapshot image {img_name}")
    result = source.compute.create_server_image(src_server, img_name)

    image_id = None
    if isinstance(result, str):
        image_id = result
    elif isinstance(result, dict):
        image_id = result.get("image_id") or result.get("id")
    else:
        image_id = getattr(result, "id", None)

    if not image_id:
        raise RuntimeError(f"{src_server.name}: could not determine source server snapshot image id")

    artifact_state["source_temp_image_id"] = image_id
    persist(server_name, state)
    log(f"{src_server.name}: source server snapshot image created: {image_id}")

    return wait_for_image_ready(source, image_id, timeout=7200, interval=10, desc=f"source server snapshot image {image_id}")


def prepare_source_image_for_artifact(source, server_name: str, state: dict, artifact: dict, artifact_state: dict, src_server):
    source_kind = artifact["kind"]
    source_id = artifact["source_id"]

    if source_kind == "volume":
        src_volume = source.block_storage.get_volume(source_id)
        src_image = export_source_volume_to_source_image(
            source=source,
            src_volume=src_volume,
            server_name=server_name,
            state=state,
            artifact_state=artifact_state,
            disk_format="qcow2",
        )
        return {
            "source_image_id_for_stream": src_image.id,
            "source_image_size": getattr(src_image, "size", None) or 0,
            "target_disk_format": "qcow2",
            "container_format": "bare",
        }

    if source_kind == "image":
        if artifact["role"] == "root":
            src_image = snapshot_server_to_source_image(source, src_server, server_name, state, artifact_state)
        else:
            raise RuntimeError(f"{server_name}: unexpected non-root image artifact {source_id}")
        return {
            "source_image_id_for_stream": src_image.id,
            "source_image_size": getattr(src_image, "size", None) or 0,
            "target_disk_format": getattr(src_image, "disk_format", None) or "qcow2",
            "container_format": getattr(src_image, "container_format", None) or "bare",
        }

    raise RuntimeError(f"{server_name}: unsupported source artifact kind '{source_kind}'")


# ---------------------------------------------------------------------------
# Target image / volume helpers
# ---------------------------------------------------------------------------

def image_name_for_artifact(server_name: str, source_kind: str, source_id: str) -> str:
    return stable_name("mig-img", server_name, source_kind, source_id)


def volume_name_for_artifact(server_name: str, source_name: str, role: str) -> str:
    base = source_name or role
    return stable_name("mig-vol", server_name, role, base)


def get_target_image_if_usable(target, artifact_state: dict):
    image_id = artifact_state.get("target_image_id")
    if not image_id:
        return None

    img = target.image.find_image(image_id, ignore_missing=True)
    if not img:
        return None

    status = getattr(img, "status", None)
    if status in IMAGE_READY_STATUSES:
        return img

    if status in IMAGE_STALE_STATUSES:
        warn(f"Deleting stale target image {img.id} ({img.name}) in status={status}")
        cleanup_target_image(target, img.id)
        artifact_state["target_image_id"] = None
        return None

    warn(f"Deleting target image {img.id} ({img.name}) in unexpected status={status}")
    cleanup_target_image(target, img.id)
    artifact_state["target_image_id"] = None
    return None


class BufferedStream:
    def __init__(self, iterable, chunk_size: int, progress_bar=None):
        self.iterable = iter(iterable)
        self.chunk_size = chunk_size
        self.progress_bar = progress_bar

    def __iter__(self):
        buffer = bytearray()
        for chunk in self.iterable:
            if not chunk:
                continue
            buffer.extend(chunk)
            while len(buffer) >= self.chunk_size:
                out = bytes(buffer[:self.chunk_size])
                del buffer[:self.chunk_size]
                if self.progress_bar is not None:
                    self.progress_bar.update(len(out))
                yield out
        if buffer:
            out = bytes(buffer)
            if self.progress_bar is not None:
                self.progress_bar.update(len(out))
            yield out

def ensure_target_image_via_stream(
    source,
    target,
    server_name: str,
    state: dict,
    artifact: dict,
    artifact_state: dict,
    prepared_source_image: dict,
    position: int = 0,
    pv_line_up: int | None = None,
):
    existing = get_target_image_if_usable(target, artifact_state)
    if existing:
        return existing

    source_kind = artifact["kind"]
    source_id = artifact["source_id"]

    target_img_name = artifact_state.get("target_image_name") or image_name_for_artifact(
        server_name, source_kind, source_id
    )
    artifact_state["target_image_name"] = target_img_name
    persist(server_name, state)

    bar = None
    try:
        log(f"{server_name}: starting stream of {source_kind}:{source_id} to target image {target_img_name}")
        log(f"{server_name}: source Glance endpoint = {source.image.get_endpoint()}")
        log(f"{server_name}: target Glance endpoint = {target.image.get_endpoint()}")

        bar = tqdm(
            total=prepared_source_image["source_image_size"] if prepared_source_image["source_image_size"] > 0 else None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"{server_name}:{source_kind}:{source_id}:image",
            leave=False,
            position=position,
        )

        # Create metadata record only
        tgt_img = target.image.create_image(
            name=target_img_name,
            disk_format=prepared_source_image["target_disk_format"],
            container_format=prepared_source_image["container_format"],
            visibility="private",
            protected=False,
        )
        artifact_state["target_image_id"] = tgt_img.id
        persist(server_name, state)

        # Stream bytes via curl
        stream_image_via_curl(
            source=source,
            target=target,
            source_image_id=prepared_source_image["source_image_id_for_stream"],
            target_image_id=tgt_img.id,
            image_size=prepared_source_image["source_image_size"],
            label=f"{server_name}:{artifact['role']}",
            progress_line_up=pv_line_up,
        )

        # Finalize import if needed
        if _target_supports_import(target):
            target.image.import_image(tgt_img, method="glance-direct")

        artifact_state["target_image_id"] = tgt_img.id
        persist(server_name, state)
        log(f"{server_name}: target image upload submitted: {tgt_img.id}")
        return tgt_img

    except Exception:
        img_id = artifact_state.get("target_image_id")
        if img_id:
            warn(f"{server_name}: image stream failed for {source_kind}:{source_id}; cleaning up target image")
            cleanup_target_image(target, img_id)
            artifact_state["target_image_id"] = None
            persist(server_name, state)
        raise
    finally:
        if bar is not None:
            bar.close()

def get_target_volume_if_usable(target, artifact_state: dict):
    volume_id = artifact_state.get("target_volume_id")
    if not volume_id:
        return None

    vol = target.block_storage.find_volume(volume_id, ignore_missing=True)
    if not vol:
        return None

    status = getattr(vol, "status", None)
    if status in {"available", "in-use"}:
        return vol
    if status in VOLUME_FAIL_STATUSES:
        warn(f"Deleting failed target volume {vol.id} ({vol.name}) status={status}")
        cleanup_target_volume(target, vol.id)
        artifact_state["target_volume_id"] = None
        return None

    try:
        return wait_for_status(
            lambda rid: target.block_storage.get_volume(rid),
            vol.id,
            wanted="available",
            fail_states=VOLUME_FAIL_STATUSES,
            timeout=1800,
            interval=5,
            desc=f"target volume {vol.id}",
        )
    except Exception:
        warn(f"Deleting non-resumable target volume {vol.id} ({vol.name})")
        cleanup_target_volume(target, vol.id)
        artifact_state["target_volume_id"] = None
        return None


def ensure_target_volume_from_image_with_retry(target, server_name: str, state: dict, artifact: dict, artifact_state: dict, position: int = 0, timeout: int = 7200, retry_interval: int = 15):
    existing = get_target_volume_if_usable(target, artifact_state)
    if existing:
        return existing

    target_image_id = artifact_state.get("target_image_id")
    if not target_image_id:
        raise RuntimeError(f"{server_name}: target image id missing before target volume creation")

    role = artifact["role"]
    source_kind = artifact["kind"]
    source_id = artifact["source_id"]

    if source_kind == "volume":
        src_obj = artifact["source_obj"]
        size_gb = int(getattr(src_obj, "size", 0))
        source_name = src_obj.name or src_obj.id
    elif source_kind == "image":
        src_obj = artifact["source_obj"]
        source_name = src_obj.name or src_obj.id
        flavor_root_disk_gb = int(artifact.get("source_flavor_root_disk_gb") or 0)
        if role == "root" and flavor_root_disk_gb > 0:
            size_gb = flavor_root_disk_gb
        else:
            virtual_size = getattr(src_obj, "virtual_size", None)
            min_disk = getattr(src_obj, "min_disk", None)
            image_size_bytes = getattr(src_obj, "size", None)
            if virtual_size:
                size_gb = bytes_to_gib_ceil(virtual_size)
            elif min_disk:
                size_gb = max(1, int(min_disk))
            else:
                size_gb = bytes_to_gib_ceil(image_size_bytes)
    else:
        raise RuntimeError(f"{server_name}: unsupported source artifact kind '{source_kind}'")

    target_volume_name = artifact_state.get("target_volume_name") or volume_name_for_artifact(server_name, source_name, role)
    artifact_state["target_volume_name"] = target_volume_name
    persist(server_name, state)

    payload = {"name": target_volume_name, "size": size_gb, "image_id": target_image_id}

    start = time.time()
    last_heartbeat = 0
    bar = tqdm(total=1, desc=f"{server_name}:{source_kind}:{source_id}:volume", leave=False, position=position)

    try:
        while True:
            try:
                log(f"{server_name}: attempting target volume creation from image {target_image_id}")
                tgt_vol = target.block_storage.create_volume(**payload)
                artifact_state["target_volume_id"] = tgt_vol.id
                persist(server_name, state)

                tgt_vol = wait_for_status(
                    lambda rid: target.block_storage.get_volume(rid),
                    tgt_vol.id,
                    wanted="available",
                    fail_states=VOLUME_FAIL_STATUSES,
                    timeout=7200,
                    interval=10,
                    desc=f"target volume {tgt_vol.id}",
                )
                bar.update(1)
                return tgt_vol

            except Exception as e:
                msg = str(e).lower()
                not_ready_signals = [
                    "image status must be active",
                    "invalid image",
                    "image is not active",
                    "image not active",
                    "imageref",
                    "badrequest",
                ]
                if any(token in msg for token in not_ready_signals):
                    now = time.time()
                    if now - last_heartbeat >= 60:
                        log(f"{server_name}: target image {target_image_id} not yet consumable by cinder, retrying")
                        last_heartbeat = now
                    if now - start > timeout:
                        raise TimeoutError(f"{server_name}: timeout waiting for target image {target_image_id} to become consumable by Cinder") from e
                    time.sleep(retry_interval)
                    continue

                if artifact_state.get("target_volume_id"):
                    cleanup_target_volume(target, artifact_state["target_volume_id"])
                    artifact_state["target_volume_id"] = None
                    persist(server_name, state)
                raise

    finally:
        bar.close()


# ---------------------------------------------------------------------------
# Target server creation / resume
# ---------------------------------------------------------------------------

def find_or_get_target_server(target, state: dict):
    srv_id = state.get("target_server_id")
    if srv_id:
        try:
            return target.compute.get_server(srv_id)
        except Exception:
            pass

    srv_name = state.get("target_server_name")
    if srv_name:
        srv = target.compute.find_server(srv_name, ignore_missing=True)
        if srv:
            state["target_server_id"] = srv.id
            return target.compute.get_server(srv.id)
    return None


def ensure_target_server_root_only(target, source, src_server, server_name: str, state: dict, flavor_map: dict, target_name_prefix: str, root_target_volume_id: str, port_ids: list[str], create_target_server: bool):
    existing = find_or_get_target_server(target, state)
    if existing:
        status = getattr(existing, "status", None)
        if status == "BUILD":
            return wait_for_status(
                lambda rid: target.compute.get_server(rid),
                existing.id,
                wanted="ACTIVE",
                fail_states=SERVER_FAIL_STATUSES,
                timeout=7200,
                interval=10,
                desc=f"target server {existing.id}",
            )
        return existing

    if not create_target_server:
        log(f"{src_server.name}: create_target_server=false, skipping instance launch")
        return None

    src_flavor_name = flavor_name_from_server(source, src_server)
    tgt_flavor_name = flavor_map.get(src_flavor_name, src_flavor_name)
    tgt_flavor = target.compute.find_flavor(tgt_flavor_name, ignore_missing=True)
    if not tgt_flavor:
        raise RuntimeError(f"{src_server.name}: target flavor '{tgt_flavor_name}' not found (mapped from '{src_flavor_name}')")

    target_name = f"{target_name_prefix}{src_server.name}" if target_name_prefix else src_server.name
    state["target_server_name"] = target_name

    bdm = [{
        "boot_index": 0,
        "uuid": root_target_volume_id,
        "source_type": "volume",
        "destination_type": "volume",
        "delete_on_termination": False,
    }]

    srv = target.compute.create_server(
        name=target_name,
        flavor_id=tgt_flavor.id,
        networks=[{"port": p} for p in port_ids],
        block_device_mapping_v2=bdm,
    )
    state["target_server_id"] = srv.id
    state["target_root_created"] = True
    persist(server_name, state)

    return wait_for_status(
        lambda rid: target.compute.get_server(rid),
        srv.id,
        wanted="ACTIVE",
        fail_states=SERVER_FAIL_STATUSES,
        timeout=7200,
        interval=10,
        desc=f"target server {srv.id}",
    )


def ensure_target_server_stopped(target, server, server_name: str):
    srv = target.compute.get_server(server.id)
    status = getattr(srv, "status", None)
    if status != "SHUTOFF":
        log(f"{server_name}: stopping target server before attaching data volumes")
        target.compute.stop_server(srv.id)
        srv = wait_for_status(
            lambda rid: target.compute.get_server(rid),
            srv.id,
            wanted="SHUTOFF",
            fail_states=SERVER_FAIL_STATUSES,
            timeout=3600,
            interval=10,
            desc=f"target server {srv.id}",
        )
    return srv


def ensure_target_volume_attached(target, server, volume_id: str, device_name: str | None, server_name: str):
    vol = target.block_storage.get_volume(volume_id)
    for att in getattr(vol, "attachments", []) or []:
        if att.get("server_id") == server.id:
            return

    log(f"{server_name}: attaching target volume {volume_id}")
    target.compute.create_volume_attachment(server.id, volumeId=volume_id, device=device_name or None)

    wait_for_status(
        lambda rid: target.block_storage.get_volume(rid),
        volume_id,
        wanted="in-use",
        fail_states=VOLUME_FAIL_STATUSES,
        timeout=3600,
        interval=10,
        desc=f"target volume {volume_id}",
    )


def ensure_target_server_started(target, server, server_name: str):
    srv = target.compute.get_server(server.id)
    status = getattr(srv, "status", None)
    if status != "ACTIVE":
        log(f"{server_name}: starting target server")
        target.compute.start_server(srv.id)
        srv = wait_for_status(
            lambda rid: target.compute.get_server(rid),
            srv.id,
            wanted="ACTIVE",
            fail_states=SERVER_FAIL_STATUSES,
            timeout=3600,
            interval=10,
            desc=f"target server {srv.id}",
        )
    return srv


# ---------------------------------------------------------------------------
# Per-instance migration worker
# ---------------------------------------------------------------------------

def total_phases_for_artifacts(artifacts: list[dict]) -> int:
    data_count = sum(1 for x in artifacts if x["role"] == "data")
    return len(artifacts) + len(artifacts) + len(artifacts) + 1 + 1 + data_count + 1


def migrate_one_server_worker(source_cloud: str, target_cloud: str, server_name: str, cfg: dict, flavor_map: dict, worker_idx: int = 0):
    source = connect(source_cloud)
    target = connect(target_cloud)

    state = load_state(server_name)
    src_server = get_server(source, server_name)
    state["source_server_id"] = src_server.id
    save_state(server_name, state)

    set_stage(server_name, state, "detecting source workload layout")

    artifacts = load_artifacts_from_state(state)
    if not artifacts:
        artifacts = detect_source_workload_storage(source, src_server)
        record_artifacts_in_state(server_name, state, artifacts)

    state["source_server_was_volume_backed"] = is_volume_backed(artifacts)
    save_state(server_name, state)

    overall = tqdm(total=total_phases_for_artifacts(artifacts), desc=f"{server_name}:phases", leave=True, position=worker_idx * 3)

    try:
        tgt_root_vol_id = None
        tgt_data = []

        for art in artifacts:
            art_state = get_or_init_artifact_state(state, art["kind"], art["source_id"], art["role"], art.get("device_name"))

            if art["kind"] == "volume":
                art["source_obj"] = source.block_storage.get_volume(art["source_id"])
            else:
                art["source_obj"] = source.image.get_image(art["source_id"])

            # If target image already exists and is usable, skip source prep/streaming and
            # clean up any leftover source temp resources from previous attempts.
            existing_tgt_img = get_target_image_if_usable(target, art_state)
            if existing_tgt_img:
                set_stage(server_name, state, f"reusing existing target image for {art['role']} {art['source_id']}")
                #cleanup_source_temp_resources_for_artifact(source, server_name, state, art_state)
                overall.update(1)
                overall.update(1)
                tgt_img = existing_tgt_img
            else:
                set_stage(server_name, state, f"preparing source image for {art['role']} {art['kind']} {art['source_id']}")
                prepared_source_image = prepare_source_image_for_artifact(
                    source=source,
                    server_name=server_name,
                    state=state,
                    artifact=art,
                    artifact_state=art_state,
                    src_server=src_server,
                )
                overall.update(1)
                save_state(server_name, state)

                set_stage(server_name, state, f"streaming {art['role']} {art['kind']} {art['source_id']} to target image")
                tgt_img = ensure_target_image_via_stream(
                    source=source,
                    target=target,
                    server_name=server_name,
                    state=state,
                    artifact=art,
                    artifact_state=art_state,
                    prepared_source_image=prepared_source_image,
                    position=worker_idx * 3 + 1,
                    pv_line_up=max(1, int(cfg.get("parallel_streams", 1)) - int(worker_idx)),
                )

                overall.update(1)
                save_state(server_name, state)

            set_stage(server_name, state, f"creating target volume for {art['role']} {art['source_id']}")
            tgt_vol = ensure_target_volume_from_image_with_retry(
                target=target,
                server_name=server_name,
                state=state,
                artifact=art,
                artifact_state=art_state,
                position=worker_idx * 3 + 2,
            )

            # it is safe to clean up source temp
            # artifacts for source images and volumes.
            #cleanup_source_temp_resources_for_artifact(source, server_name, state, art_state)

            overall.update(1)
            save_state(server_name, state)

            if art["role"] == "root":
                tgt_root_vol_id = tgt_vol.id
            else:
                tgt_data.append({
                    "volume_id": tgt_vol.id,
                    "device_name": art.get("device_name"),
                    "source_id": art["source_id"],
                    "boot_index": art.get("boot_index", 9999),
                })

        if not tgt_root_vol_id:
            raise RuntimeError(f"{server_name}: root target volume was not created")

        set_stage(server_name, state, "creating target security groups and fixed ports")
        sg_names = ensure_security_groups(source, target, src_server)
        port_ids = ensure_target_ports(target, src_server, sg_names, state)
        save_state(server_name, state)
        overall.update(1)

        set_stage(server_name, state, "creating target instance with root disk")
        tgt_server = ensure_target_server_root_only(
            target=target,
            source=source,
            src_server=src_server,
            server_name=server_name,
            state=state,
            flavor_map=flavor_map,
            target_name_prefix=cfg["target_server_name_prefix"],
            root_target_volume_id=tgt_root_vol_id,
            port_ids=port_ids,
            create_target_server=cfg["create_target_server"],
        )
        save_state(server_name, state)
        overall.update(1)

        if cfg["create_target_server"] and tgt_server:
            if tgt_data:
                set_stage(server_name, state, "stopping target instance to attach data volumes")
                tgt_server = ensure_target_server_stopped(target, tgt_server, server_name)

            already_attached = set(state.get("target_data_attached", []) or [])
            tgt_data.sort(key=lambda x: (x.get("boot_index", 9999), x.get("device_name") or "", x["source_id"]))

            for data_art in tgt_data:
                vol_id = data_art["volume_id"]
                if vol_id in already_attached:
                    overall.update(1)
                    continue

                set_stage(server_name, state, f"attaching data volume {vol_id}")
                ensure_target_volume_attached(
                    target=target,
                    server=tgt_server,
                    volume_id=vol_id,
                    device_name=data_art.get("device_name"),
                    server_name=server_name,
                )
                state.setdefault("target_data_attached", []).append(vol_id)
                save_state(server_name, state)
                overall.update(1)

            set_stage(server_name, state, "starting target instance")
            tgt_server = ensure_target_server_started(target, tgt_server, server_name)
            state["target_started"] = True
            save_state(server_name, state)
            overall.update(1)
        else:
            for _ in tgt_data:
                overall.update(1)
            overall.update(1)

        set_stage(server_name, state, "completed")
        log(f"{server_name}: migration completed")
        return {"server": server_name, "status": "ok"}

    except Exception as e:
        set_stage(server_name, state, f"failed: {e}")
        err(f"{server_name}: migration failed: {e}")
        raise
    finally:
        overall.close()


# ---------------------------------------------------------------------------
# Config resolution / CLI
# ---------------------------------------------------------------------------

def merge_cli_overrides(cfg: dict, args) -> dict:
    merged = dict(cfg)
    if args.source:
        merged["source_cloud"] = args.source
    if args.target:
        merged["target_cloud"] = args.target
    if args.servers:
        merged["servers"] = args.servers
    if args.parallel is not None:
        merged["parallel_streams"] = args.parallel

    merged.setdefault("source_cloud", None)
    merged.setdefault("target_cloud", None)
    merged.setdefault("servers", [])
    merged.setdefault("flavor_map_file", None)
    merged.setdefault("create_target_server", True)
    merged.setdefault("target_server_name_prefix", "")
    merged.setdefault("attach_data_volumes", True)
    merged.setdefault("parallel_streams", 1)
    return merged


def validate_config(cfg: dict) -> None:
    if not cfg.get("source_cloud"):
        raise RuntimeError("source_cloud missing (from migrate.yaml or --source)")
    if not cfg.get("target_cloud"):
        raise RuntimeError("target_cloud missing (from migrate.yaml or --target)")
    if not isinstance(cfg.get("parallel_streams"), int) or cfg["parallel_streams"] < 1:
        raise RuntimeError("parallel_streams / --parallel must be an integer >= 1")


def parse_args():
    p = argparse.ArgumentParser(description="OpenStack migration with resume support")
    p.add_argument("--config", default="migrate.yaml", help="Path to migrate.yaml (default: migrate.yaml)")
    p.add_argument("--source", help="Override source cloud name from clouds.yaml")
    p.add_argument("--target", help="Override target cloud name from clouds.yaml")
    p.add_argument("--servers", nargs="+", help="Override servers list from migrate.yaml")
    p.add_argument("--parallel", type=int, help="Override parallel_streams from migrate.yaml")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    file_cfg = load_yaml_file(args.config)
    cfg = merge_cli_overrides(file_cfg, args)
    validate_config(cfg)

    source = connect(cfg["source_cloud"])
    _ = connect(cfg["target_cloud"])
    flavor_map = load_flavor_map(cfg.get("flavor_map_file"))

    server_names = iter_server_names(source, cfg["servers"])
    if not server_names:
        log("No servers selected")
        return 0

    log(f"Selected {len(server_names)} server(s)")
    log(f"Parallel streams: {cfg['parallel_streams']}")
    log(f"Buffered stream chunk size: {STREAM_CHUNK_SIZE} bytes")
    reserve_progress_lines(cfg["parallel_streams"])

    if cfg["parallel_streams"] == 1:
        for idx, server_name in enumerate(server_names):
            migrate_one_server_worker(
                cfg["source_cloud"],
                cfg["target_cloud"],
                server_name,
                cfg,
                flavor_map,
                worker_idx=idx,
            )
        return 0

    failures = []
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=cfg["parallel_streams"]) as pool:
            future_map = {}
            for idx, server_name in enumerate(server_names):
                fut = pool.submit(
                    migrate_one_server_worker,
                    cfg["source_cloud"],
                    cfg["target_cloud"],
                    server_name,
                    cfg,
                    flavor_map,
                    idx,
                )
                future_map[fut] = server_name

            for fut in concurrent.futures.as_completed(future_map):
                server_name = future_map[fut]
                try:
                    fut.result()
                except Exception as e:
                    failures.append((server_name, str(e)))
    except KeyboardInterrupt:
        err("Interrupted by user. Some worker processes may still be completing active API calls.")
        return 130

    if failures:
        for srv, msg in failures:
            err(f"{srv}: failed - {msg}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
