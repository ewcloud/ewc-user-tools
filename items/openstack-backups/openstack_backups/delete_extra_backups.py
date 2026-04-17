#!/usr/bin/env python3

"""
Delete extra backups of instances and volumes

Author: Tomas Gonzalo
"""

from .logging_utils import logger
from .openstack_utils import *

def delete_extra_instance_snapshots(cloud: Connection, backup: dict, retention_count: int) -> None:
    """
    Delete extra instance snapshots
    """

    # First the the current image so that we can get the id if not known
    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']
    server = get_server(cloud, name_or_id)

    # Get the list of images
    images = cloud.compute.images()

    # Get the list of servers, will be needed later
    servers = list(cloud.compute.servers())

    # Find the images that are snapshots of the instance
    instance_snapshots = []
    for image in images:
        metadata = getattr(image, "metadata", None)
        if metadata:
            image_type = metadata.get("image_type", None)
            instance_id = metadata.get("instance_uuid",None)
            if image_type == "snapshot" and instance_id == server.id:

                # It is possible that some image become the base of some instance. In this case do not consider them as snapshots, as they cannot be deleted
                if len([x for x in servers if x.image.id == image.id]) == 0:
                    instance_snapshots.append(image)
                else:
                    logger.warning(f"Image {image.name} is the base image of an instance, so it will not be considered as a snapshot, as it cannot be deleted.")
    
    logger.info(f"Found {len(instance_snapshots)} snapshots of instance {server.name}")

    if len(instance_snapshots) > retention_count:
        logger.info(f"Number of snapshots over retention limit of {retention_count}. Deleting extras.")

        # Sort the images by updated date
        instance_snapshots.sort(key=lambda x: getattr(x,'updated_at'))
        
        # Delete any number over the retention count number
        for i in range(len(instance_snapshots) - retention_count):

            image_id = getattr(instance_snapshots[i], "id", None)
            image_name = getattr(instance_snapshots[i], "name", None)
            cloud.delete_image(image_id)
            logger.info(f"Snapshot {image_name} deleted")

    else:
        logger.info(f"Number of snapshots equal or below retention limit of {retention_count}.")

    return


def delete_extra_volume_snapshots(cloud: Connection, backup: dict, retention_count: int) -> None:
    """
    Delete extra volume snapshots
    """

    # First the the current image so that we can get the id if not known
    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']
    volume = get_volume(cloud, name_or_id)

    # Get the list of volumes snapshots
    snapshots = cloud.list_volume_snapshots()

    # Find the snapshots of the volume
    volume_snapshots = []
    for snapshot in snapshots:
            volume_id = getattr(snapshot, "volume_id", None)
            if volume_id == volume.id:
                volume_snapshots.append(snapshot)
    
    logger.info(f"Found {len(volume_snapshots)} snapshots of volume {volume.name}")

    if len(volume_snapshots) > retention_count:
        logger.info(f"Number of snapshots over retention limit of {retention_count}. Deleting extras.")

        # Sort the snapshots by updated date
        volume_snapshots.sort(key=lambda x: getattr(x,'updated_at'))
        
        # Delete any number over the retention count number
        for i in range(len(volume_snapshots) - retention_count):

            snapshot_id = getattr(volume_snapshots[i], "id", None)
            snapshot_name = getattr(volume_snapshots[i], "name", None)
            cloud.delete_volume_snapshot(snapshot_id)
            logger.info(f"Snapshot {snapshot_name} deleted")

    else:
        logger.info(f"Number of snapshots equal or below retention limit of {retention_count}.")

    return


def delete_extra_volume_backups(cloud: Connection, backup: dict, retention_count: int) -> None:
    """
    Delete extra volume backups
    """

    # First the the current image so that we can get the id if not known
    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']
    volume = get_volume(cloud, name_or_id)

    # Get the list of volumes backups
    backups = cloud.list_volume_backups()

    # Find the backups of the volume
    volume_backups = []
    for backup in backups:
            volume_id = getattr(backup, "volume_id", None)
            if volume_id == volume.id:
                volume_backups.append(backup)
    
    logger.info(f"Found {len(volume_backups)} backups of volume {volume.name}")

    if len(volume_backups) > retention_count:
        logger.info(f"Number of backups over retention limit of {retention_count}. Deleting extras.")

        # Sort the backups by updated date
        volume_backups.sort(key=lambda x: getattr(x,'updated_at'))
        
        # Delete any number over the retention count number
        for i in range(len(volume_backups) - retention_count):

            backup_id = getattr(volume_backups[i], "id", None)
            backup_name = getattr(volume_backups[i], "name", None)
            cloud.delete_volume_backup(backup_id)
            logger.info(f"Backup {backup_name} deleted")

    else:
        logger.info(f"Number of backups equal or below retention limit of {retention_count}.")

    return

def delete_extra_backups(cloud: Connection, backup: dict, retention_count: int) -> None:
    """
    Delete extra backups over the retention_count value
    """

    # Select by type and mode
    type = backup['type']
    mode = backup['mode']

    if type == 'instance' and mode == 'snapshot':
        delete_extra_instance_snapshots(cloud, backup, retention_count)
    elif type == 'volume' and mode == 'snapshot':
        delete_extra_volume_snapshots(cloud, backup, retention_count)
    elif type == 'volume' and mode == 'backup':
        delete_extra_volume_backups(cloud, backup, retention_count)
    else:
        raise RuntimeError(f'Backup configuration for type: {type} and mode: {mode} not valid.')

    # If the resource has attachments, delete extras for those too
    if 'attachments' in backup.keys():
        for attachment in backup['attachments']:
            delete_extra_backups(cloud, attachment, retention_count)

