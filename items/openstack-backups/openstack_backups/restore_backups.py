#!/usr/bin/env python3

"""
Restore backups of instances and volumes

Author: Tomas Gonzalo
"""

from .logging_utils import logger
from .openstack_utils import *

def restore_instance_snapshot(cloud: Connection, restore: dict) -> dict:
    """
    Restore a snapshot of an instance / server
    """

    name_or_id = restore['name'] if 'name' in restore.keys() else restore['id']

    snapshot = get_image(cloud, name_or_id)
    logger.info(f'Restoring instance snapshot {name_or_id}.')

    # If the restoration is in place, find the original server id and check if it still exists
    if restore['in_place']:

        metadata = getattr(snapshot, "metadata", None)
        if not metadata:
            raise RuntimeError(f'Could not retrieve information from snapshot {name_or_id}.')

        instance_id = metadata.get("instance_uuid",None)

        # Try to find server
        server = cloud.compute.find_server(instance_id, ignore_missing=True)
        if not server:
            raise RuntimeError(f'Original instace not found, in-place restoration is impossible.')
   
        # For in-place restorations, ensure server is shutoff before
        ensure_server_stopped(cloud, server)

        # Now rebuild the server
        logger.info(f'In-place restoration of instance {server.name} with snapshot {name_or_id}.')
        server = cloud.compute.rebuild_server(server.id, snapshot.id)
        if not server:
            raise RuntimeError(f'Error rebuilding instance.')

        # Wait until the server is ready
        wait_for_status(lambda rid: get_server(cloud, rid), server.id, wanted="SHUTOFF", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"rebuild server {server.id}")

        # Restart server
        ensure_server_started(cloud, server)
        logger.info(f'Instance {server.name} successfully restored from snapshot.')

        # TODO: Maybe restore networks?

        result = {
            'instance_name': server.name, 
            'instance_id': server.id, 
            'snapshot_name': snapshot.name, 
            'snapshot_id': snapshot.id
         }

    # If the restoration is not in place, create a new instance
    else:

        # Get the new instance properties
        if 'new_name' in restore:
            server_name = restore['new_name']
        else:
            server_name = stable_name(snapshot.name, snapshot.id, 'restore')
        flavor = cloud.get_flavor(restore['flavor'])
        network = cloud.get_network(restore['network'])

        # Create the new instance
        logger.info(f'New copy restoration of snapshot {name_or_id} to instance {server_name}.')
        server = cloud.compute.create_server(name=server_name, image_id=snapshot.id, flavor_id=flavor.id, networks=[{'uuid':network.id}], security_groups=restore['security_groups'])
        if not server:
            raise RuntimeError(f'Error creating instance from snapshot')

        # Wait until the server is ready
        wait_for_status(lambda rid: get_server(cloud,rid), server.id, wanted="ACTIVE", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"create server {server.id}")

        logger.info(f'Instance {server.name} successfully restored from snapshot.')

        # TODO: Maybe restore networks?

        result = {
            'instance_name': server.name, 
            'instance_id': server.id, 
            'snapshot_name': snapshot.name, 
            'snapshot_id': snapshot.id
         }

    return result


def restore_volume_snapshot(cloud: Connection, restore: dict) -> dict:
    """
    Restore a snapshot of a volume
    """

    name_or_id = restore['name'] if 'name' in restore.keys() else restore['id']

    snapshot = get_volume_snapshot(cloud, name_or_id)
    logger.info(f'Restoring volume snapshot {name_or_id}.')

    # If the restoration is in place, find the original volume id and check if it still exists
    if restore['in_place']:

        volume_id = getattr(snapshot, 'volume_id', None)
        print(volume_id)
        if not volume_id or not cloud.block_storage.find_volume(volume_id, ignore_missing=True):
            raise RuntimeError(f'Original volume not found, in-place restoration impossible.')

        volume = get_volume(cloud, volume_id)

        # For in-place restorations, ensure volume is detached beforehand
        attachments = getattr(volume, "attachments", [])
        for attachment in attachments:
            if "server_id" in attachment.keys():
                ensure_volume_detached(cloud, volume, attachment["server_id"])

        # Now revert the volume to the snapshot
        logger.info(f'In-place restoration of volume {volume.name} with snapshot {snapshot.name}.')
        cloud.block_storage.revert_volume_to_snapshot(volume.id, snapshot.id)

        # Wait until the volume is ready
        wait_for_status(lambda rid: get_volume(cloud, rid), volume.id, wanted="available", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"restore snapshot to volume {volume.id}")

        # Reattach volume if it was attached
        for attachment in attachments:
          if 'server_id' in attachment.keys():
            ensure_volume_attached(cloud, volume, attachment['server_id'], attachment['device'])
        logger.info(f'Volume {volume.name} successfully restored from snapshot.')

        result = {
            'volume_name': volume.name,
            'volume_id': volume.id,
            'snapshot_name': snapshot.name,
            'snapshot_id': snapshot.id
        }

    # If the restoration is not in place, create a new volume
    else:

        # Get the new volume properties
        if 'new_name' in restore:
            volume_name = restore['new_name']
        else:
            volume_name = stable_name(snapshot.name, snapshot.id, 'restore')

        # Create the new volume
        logger.info(f'New copy restoration of snapshot {name_or_id} to volume {volume_name}.')
        volume = cloud.block_storage.create_volume(name=volume_name, snapshot_id=snapshot.id)
        if not volume:
            raise RuntimeError(f'Error creating volume from snapshot')

        # Wait until the volume is ready
        wait_for_status(lambda rid: get_volume(cloud,rid), volume.id, wanted="available", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"restore snapshot to volume {volume.id}")

        logger.info(f'Volume {volume.name} successfully restored from snapshot.')

        result = {
            'volume_name': volume.name,
            'volume_id': volume.id,
            'snapshot_name': snapshot.name,
            'snapshot_id': snapshot.id
         }

    return result


def restore_volume_backup(cloud: Connection, restore: dict) -> dict:
    """
    Restore a backup of a volume
    """

    name_or_id = restore['name'] if 'name' in restore.keys() else restore['id']

    backup = get_volume_backup(cloud, name_or_id)
    logger.info(f'Restoring volume backup {name_or_id}.')

    # If the restoration is in place, find the original volume id and check if it still exists
    if restore['in_place']:

        volume_id = getattr(backup, 'volume_id', None)
        if not volume_id or not cloud.block_storage.find_volume(volume_id, ignore_missing=True):
            raise RuntimeError(f'Original volume not found, in-place restoration impossible.')

        volume = get_volume(cloud, volume_id)

        # For in-place restorations, ensure volume is detached beforehand
        attachments = getattr(volume, "attachments", [])
        for attachment in attachments:
            if "server_id" in attachment.keys():
                ensure_volume_detached(cloud, volume, attachment["server_id"])

        # Now replace the volume with the backup
        logger.info(f'In-place restoration of volume {volume.name} with backup {backup.name}.')
        cloud.block_storage.restore_backup(backup.id, volume_id=volume.id)

        # Wait until the volume is ready
        wait_for_status(lambda rid: get_volume(cloud, rid), volume.id, wanted="available", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"restore backup to volume {volume.id}")

        # Reattach volume if it was attached
        for attachment in attachments:
          if 'server_id' in attachment.keys():
            ensure_volume_attached(cloud, volume, attachment['server_id'], attachment['device'])
        logger.info(f'Volume {volume.name} successfully restored from backup.')

        result = {
            'volume_name': volume.name,
            'volume_id': volume.id,
            'backup_name': backup.name,
            'backup_id': backup.id
        }

    # If the restoration is not in place, create a new volume
    else:

        # Get the new volume properties
        if 'new_name' in restore:
            volume_name = restore['new_name']
        else:
            volume_name = stable_name(backup.name, backup.id, 'restore')

        # Create the new volume
        logger.info(f'New copy restoration of backup {name_or_id} to volume {volume_name}.')
        cloud.block_storage.restore_backup(backup.id, name=volume_name)

        # Wait until the backup has been restored
        wait_for_status(lambda rid: get_volume_backup(cloud,rid), backup.id, wanted="available", fail_states=["ERROR"], timeout=7200, interval=10, desc=f"restore backup {backup.id}")

        # Weirdly, the function does not return any info about the new volume, so we need to look it up
        volumes = cloud.block_storage.volumes(name=volume_name, status="available")
        last_volume = None
        for volume in volumes:
            metadata = getattr(volume, "metadata", None)
            if not metadata:
                raise RuntimeError(f'Could not retrieve information from volume {volume.id}.')
            src_backup = metadata.get("src_backup_id",None)
            if not src_backup:
                raise RuntimeError(f'Could not retrieve information from volume {volume.id}.')
            if src_backup == backup.id:
                if not last_volume or last_volume.created_at < volume.created_at:
                    last_volume = volume

        logger.info(f'Volume {volume_name} successfully restored from backup.')

        result = {
            'volume_name': volume_name,
            'volume_id': last_volume.id,
            'backup_name': backup.name,
            'backup_id': backup.id
         }

    return result


def restore_backup(cloud: Connection, restore: dict) -> None:
    """
    Restore a backup of an instance or volume or both
    """

    # Three possible types of backups: instance snapshot, volume snapshot, volume backup
    # Other options are not recognised

    type = restore['type']
    mode = restore['mode']

    if type == 'instance' and mode == 'snapshot':
        result = [restore_instance_snapshot(cloud, restore)]
    elif type == 'volume' and mode == 'snapshot':
        result = [restore_volume_snapshot(cloud, restore)]
    elif type == 'volume' and mode == 'backup':
        result = [restore_volume_backup(cloud, restore)]
    else:
        raise RuntimeError(f'Restore configuration for type: {type} and mode: {mode} not valid.')

    return result

