#!/usr/bin/env python3

"""
Create backups of instances and volumes

Author: Tomas Gonzalo
"""

from .logging_utils import logger
from .openstack_utils import *

def create_instance_snapshot(cloud: Connection, backup: dict) -> dict:
    """
    Create a snapshot of an instance / server
    """

    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']

    server = get_server(cloud, name_or_id)

    # Only image-backed instances can be backed up this way
    if server.image.id is not None:

        logger.info(f'Backing up instance {name_or_id}.')

        # It is recommended to stop the server before backing it up
        if backup['stop']:
            ensure_server_stopped(cloud, server)

        # Now create an image from the server
        image_name = stable_name(server.name, server.id, "snapshot")
        logger.info(f'Creating snapshot image of server {server.name}.')
        image = cloud.compute.create_server_image(server, image_name)

        image_id = None
        if isinstance(image, str):
            image_id = image
        elif isinstance(image, dict):
            image_id = image.get("image_id") or image.get("id")
        else:
            image_id = getattr(image, "id", None)
        
        if not image_id:
            raise RuntimeError(f"Could not determine snapshot image id for {server.name}.")

        # Wait for image to be ready
        wait_for_status(lambda rid: cloud.compute.find_image(rid, ignore_missing=True),
                image_id, wanted="ACTIVE", fail_states=["KILLED", "DELETED", "DEACTIVATED"],
                timeout=7200, interval=10,  desc=f"create image {image_id}")
        logger.info(f"Snapshot created with id {image_id}")

        # If instance was shut down by us, start it again
        ensure_server_started(cloud, server)

        result = {
            'instance_name': server.name, 
            'instance_id': server.id, 
            'snapshot_name': image_name, 
            'snapshot_id': image_id
        }


        return result

    # Volume-backed instances require backing up their root volume
    else:

        # First find the root volume of the instance
        attachments = get_attachments(cloud, backup)
        for attachment in attachments:
            # The root volume is bootable
            if attachment['bootable']:

                logger.info(f'Backing up root volume {attachment["id"]} of instance {name_or_id}.')

                # Root volumes cannot be detached
                attachment['detach'] = False

                result = create_volume_snapshot(cloud, attachment)
                result['instance_name'] = server.name
                result['instance_id'] = server.id

                return result
        

def create_instance_backup(cloud: Connection, backup: dict) -> dict:
    """
    Create a backup of an instance / server
    """

    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']

    server = get_server(cloud, name_or_id)

    # Only image-backed instances can be backed up this way
    if server.image.id is not None:

        logger.info(f'Backing up instance {name_or_id}.')

        # It is recommended to stop the server before backing it up
        if backup['stop']:
            ensure_server_stopped(cloud, server)

        # Now create an image from the server
        image_name = stable_name(server.name, server.id, "snapshot")
        logger.info(f'Creating backup of server {server.name}.')
        image = cloud.compute.backup_server(server, image_name, backup_type='daily', rotation=1)

        image_id = None
        if image == None:
            image = get_image(cloud, image_name)

        if isinstance(image, str):
            image_id = image
        elif isinstance(image, dict):
            image_id = image.get("image_id") or image.get("id")
        else:
            image_id = getattr(image, "id", None)
        
        if not image_id:
            raise RuntimeError(f"Could not determine backup id for {server.name}.")

        # Wait for image to be ready
        wait_for_status(lambda rid: cloud.compute.find_image(rid, ignore_missing=True),
                image_id, wanted="ACTIVE", fail_states=["KILLED", "DELETED", "DEACTIVATED"],
                timeout=7200, interval=10,  desc=f"create image {image_id}")
        logger.info(f"Backup created with id {image_id}")

        # If instance was shut down by us, start it again
        ensure_server_started(cloud, server)

        result = {
            'instance_name': server.name, 
            'instance_id': server.id, 
            'backup_name': image_name, 
            'backup_id': image_id
        }


        return result

    # Volume-backed instances require backing up their root volume
    else:

        # First find the root volume of the instance
        attachments = get_attachments(cloud, backup)
        for attachment in attachments:
            # The root volume is bootable
            if attachment['bootable']:

                logger.info(f'Backing up root volume {attachment["id"]} of instance {name_or_id}.')

                # Root volumes cannot be detached
                attachment['detach'] = False

                result = create_volume_backup(cloud, attachment)
                result['instance_name'] = server.name
                result['instance_id'] = server.id

                return result
        


def create_volume_snapshot(cloud: Connection, backup: dict) -> dict:
    """
    Create a snapshot of a volume
    """

    name_or_id = backup['name'] if 'name' in backup.keys() and backup['name'] != '' else backup['id']

    volume = get_volume(cloud, name_or_id)
    logger.info(f'Backing up volume {name_or_id}.')

    # Get list of servers attached to this volume (if any)
    attachments = getattr(volume, "attachments", [])

    # It is recommended to detach the volume before backing it up
    if backup['detach']:
        for attachment in attachments:
            if "server_id" in attachment.keys():
                ensure_volume_detached(cloud, volume, attachment["server_id"])

    # Now create an snapshot from the volume
    snapshot_name = stable_name(volume.name, volume.id, "snapshot")
    logger.info(f'Creating snapshot of volume {name_or_id}.')
    snapshot = cloud.create_volume_snapshot(volume.id, name=snapshot_name, force=not backup['detach'])

    snapshot_id = None
    if isinstance(snapshot, str):
        snapshot_id = snapshot
    elif isinstance(snapshot, dict):
        snapshot_id = snapshot.get("id")
    else:
        snapshot_id = getattr(snapshot, "id", None)

    if not snapshot_id:
        raise RuntimeError(f"Could not determine snapshot id for {volume.name}.")

    # Wait for snapshot to be ready
    wait_for_status(lambda rid: cloud.get_volume_snapshot(rid),
            snapshot_id, wanted="available", fail_states=["ERROR"],
            timeout=7200, interval=10,  desc=f"create snapshot {snapshot_id}")
    logger.info(f"Snapshot created with id {snapshot_id}")

    # If volume was detached, attach it back
    for attachment in attachments:
        if 'server_id' in attachment.keys():
            ensure_volume_attached(cloud, volume.id, attachment['server_id'], attachment['device'])

    result = {
        'volume_name': volume.name,
        'volume_id': volume.id,
        'snapshot_name': snapshot.name,
        'snapshot_id': snapshot.id
    }

    return result


def create_volume_backup(cloud: Connection, backup: dict) -> dict:
    """
    Create a backup of a volume
    """

    name_or_id = backup['name'] if 'name' in backup.keys() and backup['name'] != '' else backup['id']

    volume = get_volume(cloud, name_or_id)
    logger.info(f'Backing up volume {name_or_id}.')

    # Get list of servers attached to this volume (if any)
    attachments = getattr(volume, "attachments", [])

    # It is recommended to detach the volume before backing it up
    if backup['detach']:
        for attachment in attachments:
            logger.debug(attachment)
            if "server_id" in attachment.keys():
                ensure_volume_detached(cloud, volume, attachment["server_id"])

    # Now create an backup from the volume
    backup_name = stable_name(volume.name, volume.id, "backup")
    logger.info(f'Creating backup of volume {name_or_id}.')
    backup = cloud.create_volume_backup(volume.id, name=backup_name, force=not backup['detach'])

    backup_id = None
    if isinstance(backup, str):
        backup_id = backup
    elif isinstance(backup, dict):
        backup_id = backup.get("id")
    else:
        backup_id = getattr(backup, "id", None)

    if not backup_id:
        raise RuntimeError(f"Could not determine backup id for {volume.name}.")

    # Wait for snapshot to be ready
    wait_for_status(lambda rid: cloud.get_volume_backup(rid),
            backup_id, wanted="available", fail_states=["ERROR"],
            timeout=7200, interval=10,  desc=f"create backup {backup_id}")
    logger.info(f"Backup created with id {backup_id}")

    # If volume was detached, attach it back
    for attachment in attachments:
        if 'server_id' in attachment.keys():
            ensure_volume_attached(cloud, volume.id, attachment['server_id'], attachment['device'])

    result = {
        'volume_name': volume.name,
        'volume_id': volume.id,
        'backup_name': backup.name,
        'backup_id': backup.id
    }

    return result


def create_backup(cloud: Connection, backup: dict) -> None:
    """
    Create a backup of an instance or volume or both
    """

    # Three possible types of backups: instance snapshot, volume snapshot, volume backup
    # Other options are not recognised

    type = backup['type']
    mode = backup['mode']

    if type == 'instance' and mode == 'snapshot':
        result = [create_instance_snapshot(cloud, backup)]
    elif type == 'instance' and mode == 'backup':
        result = [create_instance_backup(cloud, backup)]
    elif type == 'volume' and mode == 'snapshot':
        result = [create_volume_snapshot(cloud, backup)]
    elif type == 'volume' and mode == 'backup':
        result = [create_volume_backup(cloud, backup)]
    else:
        raise RuntimeError(f'Backup configuration for type: `{type}` and mode: `{mode}` not valid.')

    # If the resource has attachments, back those up too
    if 'attachments' in backup.keys():
        attachments = backup['attachments']
        if isinstance(attachments, str) and attachments == 'all':
            # Get all attachments of resource
            attachments = get_attachments(cloud, backup)
        elif not isinstance(attachments, list):
            raise RuntimeError(f'Attachments not recognised')
        for attachment in attachments:
            # Any bootable volume is a root volume and was already backed up with the instance
            if 'bootable' not in attachment or not attachment['bootable']:
                result.append(create_backup(cloud, attachment))


    return result

