# Automation of OpenStack backups

This scripts automates the creation and restoration of backups using the OpenStack SDK.
- Create instance snapshots
- Create volume snapshots
- Create volume backups
- Restore instace snapshots
- Restore volume snapshots
- Restore volume backups
- Schedule backups

Requirements
------------
```
python>=3.12
openstacksdk>=3.0.0
PyYAML>=6.0.1
schedule>=1.1.0
```

Usage
----
The script takes a single positional argument, the path to the configuration YAML file.

```
$ python openstack_backups.py -h
usage: openstack_backups.py [-h] config_file_path

positional arguments:
  config_file_path  Path to configuration file for the backups

options:
  -h, --help        show this help message and exit
```

Configuration file
------------------

The YAML configuration file that needs to be provided to run the `openstack_backups.py` file has the following structure
```
cloud: <cloud_name>

backup:
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
  attachments:
  - name: <attachment_name>
    type: <attachmentt_type>
    mode: <backup_mode>
  - ...
- ...

restore:
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
- ...
```
where the `<cloud_name>` corresponds to the name of the domain to which the resource belong, `<resource_name>` is the name of the instance or volume to back up, `<resource_type>` is either `instance` or `volume` and `<backup_mode>` is either `snapshot` or `backup`. The root nodes `backup` and `restore` contain an unlimited sequence of mappings corresponding to the resources to backup or restore. Similarly, for each resource in the backup root node, the `attachments` node contains an unlimited sequence of the resources to backup that are attached to the resource in the parent node. Alternatively, one can select to backup all the attachments to a parent resource by specifying `attachments: all`, in place of sequence.

In addition to the fields shown in the `backup` node in the example above, instance resources have the `stop` option, which defaults to `True` and determines whether to stop the instance before backing it up; and volume resources have the `detach` option, which also defaults to `True` and determines whether to detach the volume before backing it up. 

The `restore` node can also take a field `in_place` for each resource to restore, which defaults to `False` and determines whether to restore in-place or as a new resource. For non-in-place restorations, the field `new_name` can also be provided to fix the name of the new resource and, in the case of instances, the `flavor` and `network` fields must be provided and, optionally, `security_groups`.

A template config file can be found in the `templates` directory.

Authentication
--------------
It is necessary to have the required OpenStack credentials to access the project/domain/cloud specified in the YAML file. The program expects a credentials file in the root directory called `clouds.yaml`, which contains the necessary information for authentication into the cloud. An example authentication file to acess in to `my_cloud` with `my_username` is
```
clouds:
    my_cloud:
        auth_type: v3oidcpassword
        auth:
            auth_url: https://keystone.cloudferro.com:5000/v3
            username: my_username
            password: my_password
            project_id: my_project_id
            project_name: my_project_name
            project_domain_name: my_domain_name
            project_domain_id: my_project_domain_id
            client_id: openstack
            client_secret: my_client_secret
            protocol: openid
            identity_provider: eumetsat_provider
            discovery_endpoint: https://identity.cloudferro.com/auth/realms/Eumetsat-elasticity/.well-known/openid-configuration
        region_name: WAW3-1
        interface: public
        identity_api_version: 3
```
The information necesary to fill the fields in the `clouds.yaml` file can be found in the OpenStack RC file that can be obtained from the cloud server provider. A template `clouds.yaml` file can be found in the `templates` directory.

Scheduling backups
------------------
In order to schedule backups, one can add the `scheduler` field to any resource in the main sequence of the `backup` root node. The `scheduler` node contains a mapping with, at least, the `start` field, which determines when to run the backup, in the format "yyyy-mm-dd, HH:MM". If repeated backups are desired, the field `repeat_every` can be either `day` or `week`, to repeat the backup daily or weekly. To stop the scheduled backups at some future point in time, the field `end` contains the date on which the scheduler will stop. Lastly, to only keep some amount of backups at a given time, the field `retention_count` can be provided. An example scheduled backup of an instance `my_instance` in `my_cloud`, to run weekly from the first of April for a year, with a maximum of five backups on disk, could be
```
cloud: my_cloud
backup:
- name: my_instance
  type: instance
  mode: snapshot
  scheduler:
    start: "2026-04-01, 00:00",
    end: "2027-04-01, 00:00",
    repeat_every: week,
    retention_count: 5
```
Note: it is not allowed to set up scheduled backups using this script more than a week apart. If one wishes to set this up, a cron job is recommended.

Running the container
---------------------
To run the script with docker, first one must modify the last line of the `Dockerfile` file with the configuration file of choice:
```
ENTRYPOINT ["pipenv", "run", "python", "openstack_backups.py", "<custom_configuration_yaml_file>"]
```
Then create the image with
```
sudo docker build . -t openstack-backups
```
And then lauch the container
```
sudo docker run openstack-backups
