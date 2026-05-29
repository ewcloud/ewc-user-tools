# Automation of OpenStack backups

The OpenStack Horizon UI allows the manual creation of backups and snapshots from individual instances and volumes, but does not provide automation features for backing up or restoring multiple resources nor it offers options for the scheduling of backups. The OpenStack Command Line Interface (CLI) provides the same capabilities, without any automation features. The scheduling of backups using the OpenStack CLI must be done using `cron` jobs. Documentation on how to use the OpenStack CLI to create and schedule backups can be found [here](https://confluence.ecmwf.int/x/OISnHg) and to restore backups [here](https://confluence.ecmwf.int/x/2ijPJQ). With this script, users can create, schedule or restore multiple backups automatically.

## Functionality
This script automates the creation, restoration and scheduling of backups using the OpenStack SDK. This allows users with OpenStack credentials to create backups or snapshots of multiple instances and their attached volumes, scheduling backups for a future time with and without repetition and with and without a retention count. It also allows the restoration of multiple instances or volumes, in-place or to a new instance or volume.


## Prerequisites
* `python>=3.12`
* `openstacksdk>=3.0.0`
* `PyYAML>=6.0.1`
* `schedule>=1.1.0`

OR

* `docker>=29.1.3`

## Usage
The script can be run either directly using the current python environment, satisfying the prerequisites above, or inside a docker container. In either case, it is necessary to first set up the authentication credentials file and the configuration file. It is highly recommended not to run this script within the VM you wish to back up or restore.

### 1. Application credentials

To run this script it is necessary to have the required OpenStack application credentials to access the project/domain/cloud specified in the configuration file. You can find information on how to create application credentials and obtain the RC file or clouds.yaml file in [here](https://confluence.ecmwf.int/x/U3AEJQ)

### 2. Configuration file

A configuration YAML file that contains the requested information to create, schedule or restore backups is required to run the script. A template YAML file can be found in the `templates` directory and some examples in the `tests` directory. The structure of this configuration file is as follows
```
cloud: <cloud_name>
authentication: <credentials_method>

backup:
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
- ...

restore:
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
- ...
```
where the `<cloud_name>` corresponds to the name of cloud (domain name) to which the various resources belong. The `authentication` node indicates the mode in which the credentials will be provided, which should be either `openrc` if the application credentials have been sourced from an OpenRC file, or `clouds.yaml` if the application credentials are stored in an eponymous file in the current directory. The optional `backup` node contains instructions to create and schedule backups, as explained below, and the optional `restore` node contains instructions to restore backups.


#### 2.1 Creating backups

The instructions to create backups must be provided in the `backup` node of the configuration file, following the structure
```
...
backup:
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
  attachments:
  - name: <attachment_name>
    type: <attachment_type>
    mode: <attachment_backup_mode>
  - ...
- ...
```

where `<resource_name>` is the name of the instance or volume to back up, `<resource_type>` is either `instance` or `volume` and `<backup_mode>` is either `snapshot` or `backup`. Any number of entries, corresponding to the resources to backup, can be provided to the `backup` node. By default, attachments are not backed up along with the resource (with the exception of root volume of volume-backed instances). In order to back these up, one must provide the `attachment` field to the resource, as seen above. It is possible to select to backup all attachments of the resource, with `attachments: all`, or a specific set, by providing a list of the resources to backup. It is recommended to stop instances and detaching volumes before backing them up. This is the default behaviour. The options `stop` and `detach` can be supplied to instances and volumes, respectively, to change this default behaviour.

#### 2.2 Scheduling backups

In order to schedule backups, one can add the `scheduler` field to any resource in the `backup` node. The `scheduler` node must containt, at least, the `start` field, which determines when to run the backup, in the format "yyyy-mm-dd, HH:MM". If repeated backups are desired, the field `repeat_every` can be either `day` or `week`, to repeat the backup daily or weekly. To stop the scheduled backups at some future point in time, the field `end` contains the date on which the scheduler will stop. Lastly, to only keep some amount of backups at a given time, the field `retention_count` can be provided. An example scheduled backup of an instance `my_instance` in `my_cloud`, to run weekly from the first of April of 2026 for a year, with a maximum of five backups on disk, could be
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

#### 2.3 Restoring backups

The `restore` node in the configuration file is used to specify any backups to be restored. The `backup` and `restore` nodes are not mutually exclusive, if both are provided all operations will be run. The structure of the `restore` node is as follows
```
...
restore:
- name: <resource_name>
  type: instance
  mode: <backup_mode>
  in_place: False
  new_name: <new_resource_name>
  flavor: <new_flavor>
  network: <new_network>
- name: <resource_name>
  type: <resource_type>
  mode: <backup_mode>
  in_place: True
- ...
```
where, as before, `<resource_name>` is the name of the instance or volume to restore, `<resource_type>` is either `instance` or `volume` and `<backup_mode>` is either `snapshot` or `backup`. The attachments of resources are not automatically restored, and if required to be restored, they should just be added to the list as another resource to restore in the same way. The field `in_place`, which defaults to `False`, can be provided for each resource and determines whether to restore in-place or as a new resource. For non-in-place restorations the field `new_name` can also be provided to fix the name of the new resource, otherwise a new name will be created for it. The non-in-place restoration of instances requires also the fields `flavor` and `network`,  and optionally `security_groups`, as these are required to create a new server.


### 3. Running the script

As stated above, this script can be run directly within the current python environment or in a docker container. To run it directly using python, one must simply provide a positional argument corresponding to the path to the configuration file.

```
$ python openstack_backups.py -h
usage: openstack_backups.py [-h] config_file_path

positional arguments:
  config_file_path  Path to configuration file for the backups

options:
  -h, --help        show this help message and exit
```

As an example, to run the test configuration file provided in the `tests` directory, which schedules daily instance snapshots of a server called `TestVM`, do

```
python openstack_backups.py test/test_backup.yaml
```

![Usage Example](https://github.com/ewcloud/ewc-user-tools/blob/ce799b24432479bacf3bd8479ba6b8cbb7ccb275/items/openstack-backups/images/Usage.webp)

### 4. (Optional) Running the container

Optionally, instead of running the script in a local python environment, it can be run within a docker container. These can be done, after preparing the custom `clouds.yaml` and configuration file, by following these steps:

#### 4.1 Specify the location of the configuration file

First one must modify the last line of the `Dockerfile` file to specify the location of the configuration file of choice:
```
ENTRYPOINT ["pipenv", "run", "python", "openstack_backups.py", "<custom_configuration_yaml_file>"]
```
#### 4.2. Create the docker image

Then one must create the docker image with
```
sudo docker build . -t openstack-backups
```
Note that this step might take some time

#### 4.3 Launch the docker container

Lastly one can launch the docker container with
```
sudo docker run openstack-backups
```

## Inputs

As described in the usage above, two inputs are required
* Authentication file. Must be called `clouds.yaml` and present in the directory where the script is run.
* Configuration file. YAML file with the instructions to create, schedule or restore backups.

## Outputs

The result of any operations performed by the script can be tested using the OpenStack UI or CLI. For most operations all logging output will be, by default, redirected to stdout. In the case of scheduled backups, the logging output of any operations performed at a future time will be redirected to the file `backups.log` in the `logs` directory. To enable debugging output set the `DEBUG` environment variable to `True`.

## Documentation

* [How to request Openstack Application Credentials](https://confluence.ecmwf.int/x/TiRNH)
* [How to use the OpenStack CLI](https://confluence.ecmwf.int/x/TyRNH)
* [How to create a VM using the OpenStack CLI](https://confluence.ecmwf.int/x/UiRNH)
* [How to create backups from VMs and volumes](https://confluence.ecmwf.int/x/OISnHg)
* [How to restore backups from VMs and volumes](https://confluence.ecmwf.int/x/2ijPJQ)

