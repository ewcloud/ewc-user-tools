#!/usr/bin/env python3
"""
Openstack util functions

Author: Tomas Gonzalo
Notes: many functions adapted from the openstack-migration repository by Ahmed Naga
"""

import os
import hashlib
import time
from datetime import datetime

import openstack
from openstack.connection import Connection
from openstack.compute.v2.server import Server
from openstack.compute.v2.image import Image
from openstack.block_storage.v3.volume import Volume

from .logging_utils import logger

def openstack_connect(cloud_name: str) -> Connection:
    """
    Connect to the OS cloud
    """

    connection = openstack.connect(
        auth_type = os.getenv('OS_AUTH_TYPE'),
        auth_url = os.getenv('OS_AUTH_URL'),
        identity_api_version = os.getenv('OS_IDENTITY_API_VERSION'),
        region_name = os.getenv('OS_REGION_NAME'),
        interface = os.getenv('OS_INTERFACE'),
        username = os.getenv('OS_USERNAME'),
        user_domain_name = os.getenv('OS_USER_DOMAIN_NAME'),
        project_domain_id = os.getenv('OS_PROJECT_DOMAIN_ID'),
        application_credential_id = os.getenv('OS_APPLICATION_CREDENTIAL_ID'), 
        application_crendetial_secret = os.getenv('OS_APPLICATION_CREDENTIAL_SECRET') 
    )
    connection.authorize()
    return connection

def find_server(cloud: Connection, name_or_id: str) -> Server:
    """
    Find a server
    """

    return cloud.compute.find_server(name_or_id, ignore_missing=True)

def get_server(cloud: Connection, name_or_id: str) -> Server:
    """
    Find and get server object
    """

    # Make sure the instance name or id exists
    server = cloud.compute.find_server(name_or_id, ignore_missing=True)
    if not server:
        raise RuntimeError(f"Instance {name_or_id} not found.")

    return cloud.compute.get_server(server.id)

def get_volume(cloud: Connection, name_or_id: str) -> Volume:
    """
    Find and get a volume object
    """

    # Make sure the volume name exists
    volume = cloud.block_storage.find_volume(name_or_id, ignore_missing=True)
    if not volume:
        raise RuntimeError(f"Volume {name_or_id} not found.")

    return cloud.block_storage.get_volume(volume.id)

def find_image(cloud: Connection, name_or_id: str) -> Image:
    """
    Find an image
    """

    return cloud.compute.find_image(name_or_id, ignore_missing=True)

def get_image(cloud: Connection, name_or_id: str) -> Image:
    """
    Find and get an image object
    """

    # Make sure the image exists
    image = cloud.compute.find_image(name_or_id, ignore_missing=True)
    if not image:
        raise RuntimeError(f"Image {name_or_id} nor found.")

    return cloud.compute.get_image(image.id)

def find_volume_snapshot(cloud: Connection, name_or_id: str):
    """
    Find volume snapshot
    """

    return cloud.block_storage.find_snapshot(name_or_id, ignore_missing=True)

def get_volume_snapshot(cloud: Connection, name_or_id: str):
    """
    Find and get a volume snapshot
    """

    # Make sure the snapshot exists
    snapshot = cloud.block_storage.find_snapshot(name_or_id, ignore_missing=True)
    if not snapshot:
        raise RuntimeError(f'Snapshot {name_or_id} not found.')

    return cloud.block_storage.get_snapshot(snapshot.id)

def find_volume_backup(cloud: Connection, name_or_id: str):
    """
    Find volume backup
    """

    return cloud.block_storage.find_backup(name_or_id, ignore_missing=True)

def get_volume_backup(cloud: Connection, name_or_id: str):
    """
    Find and get a volume backup
    """

    # Make sure the backup exists
    backup = cloud.block_storage.find_backup(name_or_id, ignore_missing=True)
    if not backup:
        raise RuntimeError(f'Backup {name_or_id} not found.')

    return cloud.block_storage.get_backup(backup.id)


def get_attachments(cloud: Connection, backup: dict):
    """
    Get list of attachments of resource
    """

    # Only instances have attachments
    if backup['type'] != 'instance':
        raise RuntimeError(f'Resource {backup["name"]} is not an instance and therefore cannot have attachments')

    server = get_server(cloud, backup['name'])

    logger.info(f"Getting attachments for server {server.name}")
    raw_attachments = cloud.compute.volume_attachments(server)

    # Parse the attachments into a dict that we can handle
    # The mode of backup will be same as that of the parent instance
    attachments = []
    for attachment in raw_attachments:
        volume = get_volume(cloud, attachment.volume_id)
        attachments.append({'id': volume.id,
                            'type': 'volume',
                            'mode': backup["mode"],
                            'bootable': volume['bootable'],
                            'device': attachment.device,
                            'detach': False if volume['bootable'] else True})

    return attachments

def get_fixed_ips(cloud: Connection, server_id: str) -> list:
    """
    Get the fixed ips from a server
    """

    logger.info(f'Getting fixed ips for server {server_id}')

    ports = cloud.network.ports(device_id=server_id)
    fixed_ips = []

    # Record fixed IPs
    for port in ports:
        for fixed_ip in port.fixed_ips:
            fixed_ips.append(fixed_ip)

    return fixed_ips

def get_floating_ips(cloud: Connection, server_id: str) -> list:
    """
    Get the floating ips from a server
    """

    logger.info(f'Getting floating ips for server {server_id}')

    server = get_server(cloud, server_id)
    floating_ips = []

    # Record floating IPs
    for ip in cloud.list_floating_ips(filters={"port_details": {"device_id": server_id}}):
        floating_ips.append({
                             'port_id': ip['port_id'],
                             'ip_id':   ip['id'],
                             'ip_address': ip['floating_ip_address'],
                             'fixed_ip_address': ip['fixed_ip_address'],
                             'network_id': ip['port_details']['network_id'],
        })

    return floating_ips

def recreate_ports(cloud: Connection, fixed_ips: list) -> list:
    """
    Recreate the ports with the original fixed IPs
    """

    new_port_ids = []
    for fixed_ip in fixed_ips:
        ports = cloud.network.ports()
        port_exists = False
        for port in ports:
            for ip in port.fixed_ips:
                if ip['subnet_id'] == fixed_ip['subnet_id'] and ip['ip_address'] == fixed_ip['ip_address']:
                    port_exists = True
                    new_port_ids.append(port.id)

        if not port_exists:
            port = cloud.network.create_port(
                network_id=cloud.network.get_subnet(fixed_ip["subnet_id"]).network_id,
                fixed_ips=[{"subnet_id": fixed_ip["subnet_id"], "ip_address": fixed_ip["ip_address"]}],
            )
            new_port_ids.append(port.id)

    return new_port_ids

def add_floating_ips_to_server(cloud: Connection, floating_ips: list, server_id: str) -> None:
    """
    Add floating ips to server
    """

    server = get_server(cloud, server_id)

    for ip in floating_ips:
        floating_ip = cloud.get_floating_ip(ip['ip_id'])

        if floating_ip and floating_ip.status != "ACTIVE":
            logger.info(f"Adding floating ip {ip['ip_address']} to server {server_id}")
            cloud.compute.add_floating_ip_to_server(server, floating_ip, fixed_address=ip['fixed_ip_address'])

            ports = list(cloud.network.ports(device_id=server.id))
            for port in ports:
                if port.network_id == ip['network_id']:
                    # Attach the floating IP to the port
                    floating_ip.port_id = port.id
                    cloud.dns.update_floating_ip(floating_ip.id, port_id=port.id)
            logger.warning(f"Attachment of floating ip {ip['ip_address']} may have failed, if so you must add it manually using the OpenStack CLI")

        elif not floating_ip:
            raise RuntimeError(f"Error restoring backup: floating IP {ip} not found")

def stable_name(*parts: str) -> str:
    """
    Create a stable name for images/snapshots/backups
    Credit to Ahmed Naga
    """

    raw = "::".join(parts)
    raw += str(datetime.now()) # Add current time to the hash so that ids are unique
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    base = "-".join(p for p in parts if p)
    base = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in base)
    return f"{base}-{digest}"

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
    """
    Wait until a resource reaches some status
    """

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
            logger.info(f"-- Waiting to {desc or resource_id}: current status={status}, target={wanted}")
            last_heartbeat = now

        if now - start > timeout:
            raise TimeoutError(f"-- Timeout waiting for {desc or resource_id} -> {wanted}, current={status}")

        time.sleep(interval)

def ensure_server_stopped(cloud: Connection, server: Server) -> Server:
    """
    Make sure that server is stopped, and wait until it is
    """
    server = cloud.compute.get_server(server.id)
    status = server.status
    if status != "SHUTOFF":
        logger.info(f"Stopping server {server.name}.")
        cloud.compute.stop_server(server.id)
        server = wait_for_status(
            lambda rid: cloud.compute.get_server(rid),
            server.id,
            wanted="SHUTOFF",
            fail_states=["ERROR"],
            timeout=3600,
            interval=10,
            desc=f"stop server {server.id}",
        )
    return server

def ensure_server_started(cloud: Connection, server: Server) -> Server:
    """
    Make sure that server is started, an wait until it is
    """
    server = cloud.compute.get_server(server.id)
    status = server.status
    if status != "ACTIVE":
        logger.info(f"Starting server {server.name}.")
        cloud.compute.start_server(server.id)
        server = wait_for_status(
            lambda rid: cloud.compute.get_server(rid),
            server.id,
            wanted="ACTIVE",
            fail_states=["ERROR"],
            timeout=3600,
            interval=10,
            desc=f"start server {server.id}",
        )
    return server

def ensure_volume_detached(cloud: Connection, volume: Volume, server_id: str) -> Volume:
    """
    Make sure the volume is detached
    """

    volume = cloud.block_storage.get_volume(volume.id)
    status = volume.status
    logger.debug(f"Volume status {status}")
    if status != "available":
        logger.info(f"Detaching volume {volume.name}.")
        server = get_server(cloud, server_id)
        cloud.detach_volume(server, volume)
        volume = wait_for_status(
            lambda rid: cloud.block_storage.get_volume(rid),
            volume.id,
            wanted="available",
            fail_states=["ERROR"],
            timeout=3600,
            interval=10,
            desc=f"detach volume {volume.id}",
        )
    return volume

def ensure_volume_attached(cloud: Connection, volume_id: str, server_id: str, device: str) -> Volume:
    """
    Make sure the volume is attached
    """

    volume = cloud.block_storage.get_volume(volume_id)
    status = volume.status
    if status != "in-use":
        logger.info(f"Attaching volume {volume.name}.")
        server = get_server(cloud, server_id)
        cloud.attach_volume(server, volume, device=device)
        volume = wait_for_status(
            lambda rid: cloud.block_storage.get_volume(rid),
            volume.id,
            wanted="in-use",
            fail_states=["ERROR"],
            timeout=3600,
            interval=10,
            desc=f"attach volume {volume.id}",
        )
    return volume

def ensure_server_deleted(cloud: Connection, server_id: str):
    """
    Make sure the server is deleted
    """

    wait_for_status(lambda rid: find_server(cloud,rid), server_id, wanted=None, fail_states=["ACTIVE"], timeout=7200, interval=10, desc=f"delete server {server_id}")



def delete_server(cloud: Connection, server_id: str) -> None:
    """
    Delete server
    """

    server = get_server(cloud, server_id)
    ensure_server_stopped(cloud, server)

    cloud.compute.delete_server(server_id)


def delete_volume(cloud: Connection, volume_id: str) -> None:
    """
    Delete volume
    """

    cloud.block_storage.delete_volume(volume_id, wait=True)
