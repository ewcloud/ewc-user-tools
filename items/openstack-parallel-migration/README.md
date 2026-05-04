# OpenStack Parallel Migrate

A script automating the process of copying resources (instances, volumes, and configurations) from one OpenStack cloud to another. It is designed specifically for users of the [European Weather Cloud (EWC)](https://europeanweather.cloud/), supporting migrations with resume capabilities, parallel transfers (e.i. multiple simultaneous operations), and preservation of networking and security settings.

## Features

* Performance-focused:
  * Multiprocessing via `ProcessPoolExecutor` instead of `ThreadPoolExecutor`
  * BufferedStream generator that coalesces source chunks into 4 MiB blocks
  * RAW format for temporary volume-export images on the source cloud
* Resume logic supported:
  * If source temporary snapshot / clone volume / temp image already exists and is
    not in error, reuse it on rerun and wait until it becomes usable.
  * If source temporary resources are missing or in error, recreate them.
  * If target image already exists and is usable, skip streaming and resume from
    target volume creation.
  * If target volume creation failed but target image succeeded, rerun resumes
    target volume creation.
  * If target server creation failed after the server was created, rerun resumes
    from the existing target server.
  * Source temporary resources are cleaned up after target image upload succeeds,
    and also on rerun when a usable target image already exists.
* CLI overrides:
    * `--source CLOUD`
    * `--target CLOUD`
    * `--servers vm1 vm2 ...`
    * `--parallel N`
* Parallel instance migrations
* Live phase-based progress bars
* Per-server current stage tracking in separate state files
* Glance HTTP streaming source -> target
* Preserve fixed IPs by creating target ports with same fixed IPs on same-named target networks
* Target is always boot-from-volume
* Flavor mapping file support
* Security groups reused by exact name on target, with source rules copied into them
* Source workload support:
    * Source instances booted from volume
    * Source instances booted directly from image
    * Attached data volumes in both cases

## Prerequisites

* Install [python](https://www.python.org/downloads) (version 3.12 or higher)
* Install [pv](https://www.ivarch.com/programs/pv.shtml) (version 1.6.6)
* Install [openstacksdk](https://docs.openstack.org/openstacksdk/latest/install/index.html) (version 4.9.0)
* Install [PyYAML](https://pypi.org/project/PyYAML/) (version 6.0.3)


## Usage

### 1. Clone or download this repository.
  ```bash
  git clone https://github.com/ewcloud/ewc-user-tools.git
```
#### 1.1. Change to the specific Item's subdirectory
  ```bash
  cd ewc-user-tools/items/haproxy-flavour
  ```
#### 1.2. (Optional) Checkout an specific Item's version

  > ⚠️ Make sure to replace `x.y.z` in the command below, with your version of preference.

  ```bash
  git checkout x.y.z
  ```
### 2. Update configuration files
> ✅ See [Configuration](#configuration) section below for further details

Modify the sample `clouds.yaml`  in your working directory with entries for your source and target clouds, and update the sample `migrate.yaml` file to specify which resources to migration.

### 3. Run the migration

#### 3.1. Basic run
```bash
python3 openstack_parallel_migrate.py
```

#### 3.2. Run overrides
```bash
python3 openstack_parallel_migrate.py \
  --source source \
  --target target_oidc \
  --servers ahmedtst3 ahmedtst4 \
  --parallel 2
```


## Configuration


### Authentication and authorization

The [clouds.yaml](./clouds.yaml) file contains authentication and connection details for your source and target OpenStack clouds. It is configuration file with standard structure, used by OpenStack SDK and the OpenStack CLI (see the [OpenStack's official documentation](https://docs.openstack.org/python-openstackclient/latest/configuration/index.html#clouds-yaml) for file structure details).

### Source and target
We rely on a configuration file with custom structure, to specify where the resources to be migrated are located, and where they should be copied over to. Checkout the attributes their descriptions in the sample [migrate.yaml](./migrate.yaml) file.

### CLI arguments
Arguments passed to the script at runtime override those in the static configuration files. To list available CLI
arguments run

```bash
python3 openstack_parallel_migrate.py -h
```

```
usage: openstack_parallel_migrate.py [-h] [--config CONFIG] [--source SOURCE] [--target TARGET] [--servers SERVERS [SERVERS ...]] [--parallel PARALLEL]

OpenStack migration with resume support

options:
  -h, --help            show this help message and exit
  --config CONFIG       Path to migrate.yaml (default: migrate.yaml)
  --source SOURCE       Override source cloud name from clouds.yaml
  --target TARGET       Override target cloud name from clouds.yaml
  --servers SERVERS [SERVERS ...]
                        Override servers list from migrate.yaml
  --parallel PARALLEL   Override parallel_streams from migrate.yaml
```
## Output

For an example run command such as:
```bash
python3 openstack_parallel_migrate_curl.py --source source --target target_oidc --servers ahmedtst3 ahmedtst4
```

The expected output would be:
```
[INFO] Selected 2 server(s)
[INFO] Parallel streams: 2
[INFO] Buffered stream chunk size: 4194304 bytes
[INFO] ahmedtst3: detecting source workload layout
ahmedtst3:phases:   0%|                                                                                                                                                                            | 0/10 [00:00<?, ?it/s][INFO] ahmedtst4: detecting source workload layout
[INFO] ahmedtst3: preparing source image for root image c27886c2-f152-4a70-9c4d-d99b33b1df53
[INFO] ahmedtst4: preparing source image for root volume 9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba                                                                                                      | 0/10 [00:00<?, ?it/s][INFO] ahmedtst3: streaming root image c27886c2-f152-4a70-9c4d-d99b33b1df53 to target image
[INFO] ahmedtst3: starting stream of image:c27886c2-f152-4a70-9c4d-d99b33b1df53 to target image mig-img-ahmedtst3-image-c27886c2-f152-4a70-9c4d-d99b33b1df53-afdbf665b9f2
[INFO] ahmedtst3: source Glance endpoint = https://api.waw3-1.cloudferro.com:9292/v2/
[INFO] ahmedtst4: streaming root volume 9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba to target image                                                                                               | 1/10 [00:00<00:01,  6.32it/s][INFO] ahmedtst4: starting stream of volume:9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba to target image mig-img-ahmedtst4-volume-9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba-477d041735d5
[INFO] ahmedtst4: source Glance endpoint = https://api.waw3-1.cloudferro.com:9292/v2/
[INFO] ahmedtst3: target Glance endpoint = https://glance-api.cloud.central.data.destination-earth.eu:443/v2/
[INFO] ahmedtst4: target Glance endpoint = https://glance-api.cloud.central.data.destination-earth.eu:443/v2/                                                                              | 1/10 [00:00<00:01,  6.32it/s][INFO] Source curl URL: https://api.waw3-1.cloudferro.com:9292/v2/images/ea807a6d-9b81-4e80-b17f-317cc353892a/file
[INFO] Target curl URL: https://glance-api.cloud.central.data.destination-earth.eu:443/v2/images/aeecd94a-35d6-4642-be13-285c94239cce/file
[INFO] Source curl URL: https://api.waw3-1.cloudferro.com:9292/v2/images/21ef8013-8a7d-472b-9817-9fb1e032e624/file                                                                         | 1/10 [00:00<00:01,  6.32it/s][INFO] Target curl URL: https://glance-api.cloud.central.data.destination-earth.eu:443/v2/images/20fdec4d-8e6a-4776-b9e4-d2caee11510a/file                                                    | 0.00/16.0G [00:00<?, ?B/s]ahmedtst4:volume:9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba:image:   0%|                                                                                                                            | 0.00/16.0G [00:00<?, ?B/saahmedtst4:root: 2.45GiB 0:00:38 [66.9MiB/s] [============ahmedtst3:root: 2.53GiB 0:00:40 [65.3MiB/s] [======================>                                                                                             ahmedtst3:root: 16.0GiB 0:04:20 [62.8MiB/s] [==========================================================================================================================================================>] 100%
[INFO] ahmedtst3: target image upload submitted: aeecd94a-35d6-4642-be13-285c94239cce
[INFO] ahmedtst3: creating target volume for root c27886c2-f152-4a70-9c4d-d99b33b1df53
[INFO] ahmedtst3: attempting target volume creation from image aeecd94a-35d6-4642-be13-285c94239cce                                                                                                                      
ahmedtst3:phases:  20%|████████████████████████████████▌                                                                                                                                  | 2/10 [04:23<20:38, 154.75s/ita
[INFO] -- Waiting for target volume a8bbf2fd-4d19-49da-ad33-d243128aa19a: current status=creating, target=available
ahmedtst3:phases:  20%|████████████████████████████████▌                                                                                                                                  | 2/10 [04:24<20:38, 154.75s/itahmedtst4:root: 16.0GiB 0:04:23 [62.2MiB/s] [==========================================================================================================================================================>] 100%
[INFO] ahmedtst4: target image upload submitted: 20fdec4d-8e6a-4776-b9e4-d2caee11510a                                                                                                               | 0/1 [00:00<?, ?it/s]
[INFO] ahmedtst4: creating target volume for root 9f1a1b7e-8b93-4556-ba3e-f0637a6d2fba
[INFO] ahmedtst4: attempting target volume creation from image 20fdec4d-8e6a-4776-b9e4-d2caee11510a
[INFO] -- Waiting for target volume a1505c72-9345-4cde-ad8e-e48af19c1528: current status=creating, target=available
[INFO] ahmedtst3: preparing source image for data volume d3bae72c-e83b-4096-bf0f-02611a69af00
[INFO] Source volume d3bae72c-e83b-4096-bf0f-02611a69af00 is in-use; creating snapshot mig-snap-d3bae72c-e83b-4096-bf0f-02611a69af00-dfad2c4d8956
[INFO] -- Waiting for source snapshot 61a2ab3c-24c8-44e5-a612-63d984f93476: current status=creating, target=available
[INFO] ahmedtst4: preparing source image for data volume d6b9208b-e15b-4ddb-b3f9-452cdf797826                                                                                              | 3/10 [04:34<10:23, 89.09s/it]
[INFO] Source volume d6b9208b-e15b-4ddb-b3f9-452cdf797826 is in-use; creating snapshot mig-snap-d6b9208b-e15b-4ddb-b3f9-452cdf797826-c6adb44b6e46
[INFO] -- Waiting for source snapshot 4d7c5b73-777b-4251-86e8-a6e3afcc0956: current status=creating, target=available                                                                               | 0/1 [00:00<?, ?it/s]
[INFO] ahmedtst3: Creating temporary clone volume mig-clone-d3bae72c-e83b-4096-bf0f-02611a69af00-3309bfd2d1f5 from snapshot 61a2ab3c-24c8-44e5-a612-63d984f93476
[INFO] -- Waiting for source temp volume 5ad137e8-7e3d-43b0-bd33-96b082b587e6: current status=creating, target=available
[INFO] ahmedtst4: Creating temporary clone volume mig-clone-d6b9208b-e15b-4ddb-b3f9-452cdf797826-a73ee87281c5 from snapshot 4d7c5b73-777b-4251-86e8-a6e3afcc0956                           | 3/10 [04:45<10:23, 89.09s/it]
[INFO] -- Waiting for source temp volume e9e96b78-24cf-4c54-8637-b7fc9d78799e: current status=creating, target=available                                                                   | 3/10 [04:37<10:28, 89.84s/it]
[INFO] ahmedtst3: Exporting source volume 5ad137e8-7e3d-43b0-bd33-96b082b587e6 -> source image src-export-d3bae72c-e83b-4096-bf0f-02611a69af00-raw-418eeb3c715b (raw)
[INFO] ahmedtst3: Source export image created: 9b908ffc-1a33-49fb-90aa-8791d576f6d3
[INFO] -- Waiting for source export image 9b908ffc-1a33-49fb-90aa-8791d576f6d3: current status=queued, target=active
[INFO] ahmedtst4: Exporting source volume e9e96b78-24cf-4c54-8637-b7fc9d78799e -> source image src-export-d6b9208b-e15b-4ddb-b3f9-452cdf797826-raw-ed9c8adde6e1 (raw)                      | 3/10 [04:55<10:23, 89.09s/it]
[INFO] ahmedtst4: Source export image created: e2f21a47-7fed-44c4-a188-e8c57ed3226e
[INFO] -- Waiting for source export image e2f21a47-7fed-44c4-a188-e8c57ed3226e: current status=queued, target=active
[INFO] -- Waiting for source export image 9b908ffc-1a33-49fb-90aa-8791d576f6d3: current status=saving, target=active
[INFO] -- Waiting for source export image e2f21a47-7fed-44c4-a188-e8c57ed3226e: current status=saving, target=active                                                                       | 3/10 [05:56<10:23, 89.09s/it]
[INFO] ahmedtst3: streaming data volume d3bae72c-e83b-4096-bf0f-02611a69af00 to target image
[INFO] ahmedtst3: starting stream of volume:d3bae72c-e83b-4096-bf0f-02611a69af00 to target image mig-img-ahmedtst3-volume-d3bae72c-e83b-4096-bf0f-02611a69af00-b2ae24fbd98b
[INFO] ahmedtst3: source Glance endpoint = https://api.waw3-1.cloudferro.com:9292/v2/
[INFO] ahmedtst3: target Glance endpoint = https://glance-api.cloud.central.data.destination-earth.eu:443/v2/
[INFO] Source curl URL: https://api.waw3-1.cloudferro.com:9292/v2/images/9b908ffc-1a33-49fb-90aa-8791d576f6d3/file
[INFO] Target curl URL: https://glance-api.cloud.central.data.destination-earth.eu:443/v2/images/60c13c68-b807-4344-97d2-215c18c5a4de/file                                                                               
ahmedtst3:phases:  40%|█████████████████████████████████████████████████████████████████▏                                                                                                 | 4/10 [06:58<11:04, 110.81s/ita[INFO] ahmedtst4: streaming data volume d6b9208b-e15b-4ddb-b3f9-452cdf797826 to target image                                                                                                             ]  1% ETA 0:02:36
[INFO] ahmedtst4: starting stream of volume:d6b9208b-e15b-4ddb-b3f9-452cdf797826 to target image mig-img-ahmedtst4-volume-d6b9208b-e15b-4ddb-b3f9-452cdf797826-bb9472147cba
[INFO] ahmedtst4: source Glance endpoint = https://api.waw3-1.cloudferro.com:9292/v2/
[INFO] ahmedtst4: target Glance endpoint = https://glance-api.cloud.central.data.destination-earth.eu:443/v2/
[INFO] Source curl URL: https://api.waw3-1.cloudferro.com:9292/v2/images/e2f21a47-7fed-44c4-a188-e8c57ed3226e/file
[INFO] Target curl URL: https://glance-api.cloud.central.data.destination-earth.eu:443/v2/images/2828c53c-df72-46e2-9239-6bb561518043/file
                                                                                                                                                                                                                         aahmedtst3:data: 10.0GiB 0:02:39 [64.1MiB/s] [==========================================================================================================================================================>] 100%
ahmedtst4:data: 10.0GiB 0:02:36 [65.3MiB/s] [==========================================================================================================================================================>] 100%
[INFO] ahmedtst3: target image upload submitted: 60c13c68-b807-4344-97d2-215c18c5a4de
[INFO] ahmedtst3: creating target volume for data d3bae72c-e83b-4096-bf0f-02611a69af00                                                                                                                                   
[INFO] ahmedtst3: attempting target volume creation from image 60c13c68-b807-4344-97d2-215c18c5a4de                                                                                                                      
[INFO] -- Waiting for target volume 5279e923-8cb8-47a5-bc9f-678cb03f531c: current status=creating, target=available
[INFO] ahmedtst4: target image upload submitted: 2828c53c-df72-46e2-9239-6bb561518043███████████████████▌                                                                                 | 5/10 [09:40<10:45, 129.11s/it]
[INFO] ahmedtst4: creating target volume for data d6b9208b-e15b-4ddb-b3f9-452cdf797826
[INFO] ahmedtst4: attempting target volume creation from image 2828c53c-df72-46e2-9239-6bb561518043                                                                                                 | 0/1 [00:00<?, ?it/s]
[INFO] -- Waiting for target volume 3ea20a95-c262-4dab-971c-205aa4833823: current status=creating, target=available
[INFO] ahmedtst3: creating target security groups and fixed ports
[INFO] ahmedtst3: creating target instance with root disk
[INFO] ahmedtst4: creating target security groups and fixed ports████████████████████████████████████████████████████████████████████████▊                                                 | 7/10 [09:51<03:00, 60.31s/it]
[INFO] ahmedtst4: creating target instance with root disk███████████████████████████████████████████████▌                                                                                 | 5/10 [09:41<10:45, 129.01s/it][INFO] -- Waiting for target server 98ccd72e-d46d-4e52-aa8e-5b67bb541d5c: current status=BUILD, target=ACTIVE
[INFO] -- Waiting for target server f01ac55e-3f6f-4fe2-8650-28d691189c41: current status=BUILD, target=ACTIVE████████████████████████████▊                                                 | 7/10 [09:53<03:00, 60.31s/it]
[INFO] ahmedtst3: stopping target instance to attach data volumes
[INFO] ahmedtst3: stopping target server before attaching data volumes
[INFO] -- Waiting for target server 98ccd72e-d46d-4e52-aa8e-5b67bb541d5c: current status=ACTIVE, target=SHUTOFF
[INFO] ahmedtst4: stopping target instance to attach data volumes█████████████████████████████████████████████████████████████████████████████████████████▏                                | 8/10 [10:04<01:29, 44.85s/it]
[INFO] ahmedtst4: stopping target server before attaching data volumes
[INFO] -- Waiting for target server f01ac55e-3f6f-4fe2-8650-28d691189c41: current status=ACTIVE, target=SHUTOFF
[INFO] -- Waiting for target server 98ccd72e-d46d-4e52-aa8e-5b67bb541d5c: current status=ACTIVE, target=SHUTOFF
ahmedtst3:phases:  80%|███████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████▏                                | 8/10 [11:04<01:29, 44.85s/it]
[INFO] ahmedtst4: attaching data volume 3ea20a95-c262-4dab-971c-205aa4833823
[INFO] ahmedtst4: attaching target volume 3ea20a95-c262-4dab-971c-205aa4833823████████████████████████████████████████████████████████████████████████████▏                                | 8/10 [10:05<01:29, 44.85s/it]
[INFO] -- Waiting for target volume 3ea20a95-c262-4dab-971c-205aa4833823: current status=reserved, target=in-use
[INFO] ahmedtst3: attaching data volume 5279e923-8cb8-47a5-bc9f-678cb03f531c
[INFO] ahmedtst3: attaching target volume 5279e923-8cb8-47a5-bc9f-678cb03f531c
[INFO] -- Waiting for target volume 5279e923-8cb8-47a5-bc9f-678cb03f531c: current status=reserved, target=in-use
[INFO] ahmedtst4: starting target instance████████████████████████████████████████████████████████████████████████████████████████████████████████████████▏                                | 8/10 [11:16<01:29, 44.85s/it]
[INFO] ahmedtst4: starting target server
[INFO] -- Waiting for target server f01ac55e-3f6f-4fe2-8650-28d691189c41: current status=SHUTOFF, target=ACTIVE
[INFO] ahmedtst3: starting target instance
[INFO] ahmedtst3: starting target server
[INFO] -- Waiting for target server 98ccd72e-d46d-4e52-aa8e-5b67bb541d5c: current status=SHUTOFF, target=ACTIVE
[INFO] ahmedtst4: completed███████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████▌                | 9/10 [11:26<00:56, 56.66s/it]
[INFO] ahmedtst4: migration completed
ahmedtst4:phases: 100%|███████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 10/10 [11:28<00:00, 68.88s/it]


ahmedtst4:phases: 100%|███████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 10/10 [11:28<00:00, 40.34s/it]
[INFO] ahmedtst3: completed
[INFO] ahmedtst3: migration completed
ahmedtst3:phases: 100%|███████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 10/10 [11:37<00:00, 69.72s/it]
```
