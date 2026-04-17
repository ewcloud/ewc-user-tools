#!/usr/bin/env python3
"""
Openstack util functions

Author: Tomas Gonzalo
Notes: many functions adapted from the openstack-migration repository by Ahmed Naga
"""

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

    connection = openstack.connect(cloud=cloud_name)
    connection.authorize()
    return connection

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

def get_image(cloud: Connection, name_or_id: str) -> Image:
    """
    Find and get an image object
    """

    # Make sure the image exists
    image = cloud.compute.find_image(name_or_id, ignore_missing=True)
    if not image:
        raise RuntimeError(f"Image {name_or_id} nor found.")

    return cloud.compute.get_image(image.id)

def get_volume_snapshot(cloud: Connection, name_or_id: str):
    """
    Find and get a volume snapshot
    """

    # Make sure the snapshot exists
    snapshot = cloud.block_storage.find_snapshot(name_or_id, ignore_missing=True)
    if not snapshot:
        raise RuntimeError(f'Snapshot {name_or_id} not found.')

    return cloud.block_storage.get_snapshot(snapshot.id)

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
        attachments.append({'name': volume.name,
                            'id': volume.id,
                            'type': 'volume',
                            'mode': backup['mode'],
                            'detach': True})

    return attachments

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

def ensure_volume_attached(cloud: Connection, volume: Volume, server_id: str, device: str) -> Volume:
    """
    Make sure the volume is attached
    """

    volume = cloud.block_storage.get_volume(volume.id)
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

