#!/usr/bin/env python3

"""
Openstack backup admin. Administration of backups, schedulers and restores

Author: Tomas Gonzalo
"""

import os
import yaml
import argparse

from openstack_backups.logging_utils import logger
from openstack_backups.openstack_utils import openstack_connect
from openstack_backups.create_backups import create_backup
from openstack_backups.schedule_backups import schedule_backup
from openstack_backups.restore_backups import restore_backup


def parse_arguments() -> dict:
    """
    Function to parse the arguments of main function
    """

    parser = argparse.ArgumentParser()

    parser.add_argument('config_file_path',
                        help="Path to configuration file for the backups")

    # Get the list of options from the parser
    args = parser.parse_args()

    return args


def parse_config_file(config_file_path: str) -> dict:
    """
    Function to parse the contents of the provided config file
    """

    yaml_error = 'Error reading YAML file'

    if os.path.exists(config_file_path):

         with open(config_file_path, 'r') as f:

            yaml_contents = yaml.safe_load(f)
            logger.debug(yaml_contents)
            config = {}

            # Validate the cloud entry
            if 'cloud' in yaml_contents:
                config['cloud'] = yaml_contents['cloud']
            else:
                raise RuntimeError(f'{yaml_error}: Cloud name missing.')


            # Authentication method can be with Application Credentials from an OpenRC file or a clouds.yaml file
            if 'authentication' not in yaml_contents:
                raise RuntimeError(f'{yaml_error}: Missing application credentials. The entry \'authentication\' is required.')
            elif yaml_contents['authentication'] not in ['openrc','clouds.yaml']:
                raise RuntimeError(f'{yaml_error}: Unknown application credentials: The entry \'authentication\' should be either \'openrc\' or \'clouds.yaml\'.')
            else:
                config['authentication'] = yaml_contents['authentication']

            # Validate the backup entry
            if 'backup' in yaml_contents:

                backups = yaml_contents['backup']

                if not isinstance(backups, list):
                    raise RuntimeError(f'{yaml_error}: Unrecognised backup list.')

                # Check that each entry has the minimum amount of properties
                for backup in backups:
                    
                    logger.debug(backup.keys())

                    # Needs either name or id
                    if 'name' not in backup.keys() and 'id' not in backup.keys():
                        raise RuntimeError(f'{yaml_error}: Backup entry missing name or id.')
                    # Needs both type and mode
                    if 'type' not in backup.keys():
                        raise RuntimeError(f'{yaml_error}: Backup entry missing type.')
                    if 'mode' not in backup.keys():
                        raise RuntimeError(f'{yaml_error}: Backup entry missing mode.')

                    # By default, stop or detach the instance/volume
                    backup['stop'] = backup.get('stop', True)
                    backup['detach'] = backup.get('detach',True)

                    # Store scheduling options
                    backup['scheduled'] = True if 'scheduler' in backup.keys() else False
                    if backup['scheduled']:

                        if not isinstance(backup['scheduler'], dict):
                            raise RuntimeError(f'{yaml_error}: Wrong format for scheduler entries.')

                        # Start is required, others are not
                        if not 'start' in backup['scheduler'].keys():
                            raise RuntimeError(f'{yaml_error}: Start of scheduler is required.')
                        backup['scheduler']['repeat_every'] = backup['scheduler'].get('repeat_every', None)
                        backup['scheduler']['retention_count'] = backup['scheduler'].get('retention_count',None)
                        backup['scheduler']['end'] = backup['scheduler'].get('end',None)

                    # If there are attachments, they should be either in a list specifying each attachment to be backed up or the keyword `all` to backup all attachments
                    if "attachments" in backup.keys():
                        if isinstance(backup['attachments'],str):
                            if backup['attachments'] != "all":
                                raise RuntimeError(f'{yaml_error}: Wrong format for attachments, it should contain a list of attachments to back up or the keyword `all` to backup all attachments')
                        elif isinstance(backup['attachments'], list):
                            for attachment in backup['attachments']:

                                # Needs either name or id
                                if 'name' not in attachment.keys() and 'id' not in attachment.keys():
                                    raise RuntimeError(f'{yaml_error}: Backup entry missing name or id.')
                                # Needs both type and mode
                                if 'type' not in attachment.keys():
                                    raise RuntimeError(f'{yaml_error}: Backup entry missing type.')
                                if 'mode' not in attachment.keys():
                                    raise RuntimeError(f'{yaml_error}: Backup entry missing mode.')

                                # By default, stop or detach the instance/volume
                                attachment['stop'] = attachment['stop'] if 'stop' in attachment.keys() else True
                                attachment['detach'] = attachment['detach'] if 'detach' in attachment.keys() else True

                config['backup'] = backups

            # Validate the restore entry
            if 'restore' in yaml_contents:

                restores = yaml_contents['restore']

                if not isinstance(restores, list):
                    raise RuntimeError(f'{yaml_error}: Unrecognised restore list.')


                # Check that each entry has the minimum amount of properties
                for restore in restores:
                    
                    logger.debug(restore.keys())

                    # Needs either name or id
                    if 'name' not in restore.keys() and 'id' not in restore.keys():
                        raise RuntimeError(f'{yaml_error}: Restore entry missing name or id.')
                    # Needs both type and mode
                    if 'type' not in restore.keys():
                        raise RuntimeError(f'{yaml_error}: Restore entry missing type.')
                    if 'mode' not in restore.keys():
                        raise RuntimeError(f'{yaml_error}: Restore entry missing mode.')

                    # By default, to restoration as new copy
                    restore['in_place'] = restore.get('in_place', False)

                    # If the restoration is for an instance and not in place, one needs to provide flavor, network and (optionally) security groups
                    if restore['type'] == 'instance' and not restore['in_place']:
                        if 'flavor' not in restore.keys():
                            raise RuntimeError(f'{yaml_error}: Restore entry missing flavor, required for non-in-place restorations.')
                        if 'network' not in restore.keys():
                            raise RuntimeError(f'{yaml_error}: Restore entry missing network, required for non-in-place restorations.')
                        restore['security_groups'] = restore.get('security_groups', [])

                config['restore'] = restores

            if 'backup' not in yaml_contents and 'restore' not in yaml_contents:
                raise RuntimeError(f'{yaml_error}: No recognised option in config file.')

            return config

    else:
        raise FileNotFoundError('Backups config file not found.')

def authenticate(authentication: str) -> None:
    """
    Ensure application credentials are ready.
    Two methods of authentication, with openrc file (pre-source) or clouds.yaml file
    """

    if authentication=="openrc":

        # Check if authentication method uses application credentials of password

        if "OS_AUTH_TYPE" in os.environ:

            required_envs = ['OS_AUTH_TYPE', 'OS_AUTH_URL', 'OS_IDENTITY_API_VERSION', 'OS_REGION_NAME', 'OS_INTERFACE']

            # Application credentials
            if os.getenv("OS_AUTH_TYPE") == "v3applicationcredential":
                required_envs += ['OS_APPLICATION_CREDENTIAL_ID', 'OS_APPLICATION_CREDENTIAL_SECRET']
            elif os.getenv("OS_AUTH_TYPE") == "v3oidcpassword":
                required_envs += ['OS_USERNAME', 'OS_PASSWORD', 'OS_CLIENT_ID', 'OS_CLIENT_SECRET', 'OS_PROTOCOL', 'OS_IDENTITY_PROVIDER', 'OS_DISCOVERY_ENDPOINT']
            else:
                raise RuntimeError(f'Authentication not possible. Unrecognised authentication type {os.getenv("OS_AUTH_TYPE")}')

            for env in required_envs:
                if env not in os.environ:
                    raise RuntimeError(f'Authentication not possible. Environment variable {env} not set. Source the openrc file in order to set all required environment varibles')

        else:
           raise RuntimeError(f'Authentication not possible. Environment variable {env} not set. Source the openrc file in order to set all required environment varibles')

    elif authentication=='clouds.yaml':

        # Check the the appropriate clouds.yaml file exists in the current directory

        if not os.path.exists('clouds.yaml'):
            raise RuntimeError(f'Authentication not possible. Required file `clouds.yaml` not present in the current directory.')

        # Check contents of clouds.yaml
        with open("clouds.yaml", 'r') as f:

            clouds_yaml = yaml.safe_load(f)

            if 'clouds' not in clouds_yaml:
                raise RuntimeError(f'Authentication not possible, missing `clouds` entry in `clouds.yaml` file.')

            clouds = clouds_yaml['clouds']
            if 'openstack' not in clouds:
                raise RuntimeError(f'Authentication not possible, missing `openstack` entry in `clouds.yaml` file.')

            openstack = clouds['openstack']

            if 'auth_type' not in openstack:
                raise RuntimeError(f'Authentication not possible, missing `auth_type` in `clouds.yaml` file.')

            if 'auth' not in openstack or  \
               'auth_url' not in openstack['auth'] or \
               'application_credential_id' not in openstack['auth'] or \
               'application_credential_secret' not in openstack['auth'] :
                    raise RuntimeError(f'Authenciation not possible, missing application credentials in `clouds.yaml` file.')

            if 'regions' not in openstack:
                raise RuntimeError(f'Authentication not possible, missing `region` in `clouds.yaml` file.')

            if 'interface' not in openstack:
                raise RuntimeError(f'Authentication not possible, missing `interface` in `clouds.yaml` file.')

            if 'identity_api_version' not in openstack:
                raise RuntimeError(f'Authentication not possible, missing `identity_api_version` in `clouds.yaml` file.')

    else:
        raise RuntimeError(f'Authentication not possible. Unrecognised authentication metod.')



def main():
    """
    Main function
    """

    try:

        # Parse the arguments
        args = parse_arguments()

        # Parse the provided file
        config = parse_config_file(args.config_file_path)

        # Ensure authentication credentials are ready
        authenticate(config['authentication'])

        # Connect to the cloud
        cloud = openstack_connect(config['cloud'])
        logger.info(f"Connected to cloud {config['cloud']}.")

        # If there are any backups to be performed, do them
        if 'backup' in config:
            for backup in config['backup']:
                if backup['scheduled']:
                    result = schedule_backup(cloud, backup)
                else:
                    result = create_backup(cloud, backup)

        # If there are any restores to be performed, do them
        if 'restore' in config:
            for restore in config['restore']:
                result = restore_backup(cloud, restore)

        logger.info(f'Backup operations complete. Results: {result}.')

    except Exception as e:
        logger.error(f'{e}')


if __name__ == "__main__":
    main()
