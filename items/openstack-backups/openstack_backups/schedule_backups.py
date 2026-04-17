#!/usr/bin/env python3

"""
Schedule backups

Author: Tomas Gonzalo
"""

import os
import time
import schedule
from datetime import datetime, timedelta
from multiprocessing import Process

from .logging_utils import logger
from .create_backups import create_backup
from .delete_extra_backups import delete_extra_backups
from .openstack_utils import *

def scheduler(cloud : Connection, backup: dict, start: datetime.date, end: datetime.date, repeat: str, retention_count: int) -> None:
    """
    Scheduling function
    """

    # Fork to detach the scheduler process
    if os.fork() != 0:
        return 

    # Wait until start date
    today = datetime.now().replace(microsecond=0)
    time_til_start = (start-today)
    logger.info(f'Scheduled backup will commence in {time_til_start}.')
    time.sleep(time_til_start.total_seconds())

    # From now on do not propagate the logs to stdout
    logger.propagate = False

    # Run first iteration of the job
    now = datetime.now().replace(microsecond=0)
    logger.info(f"Scheduled backup begins at {now}.")
    try:
        create_backup(cloud, backup)
    except Exception as e:
        logger.error(f"Failed scheduled backup: {e}")
    else:
        now = datetime.now().replace(microsecond=0)
        logger.info(f"Scheduled backup complete at {now}.")

        # After a succesful backup, check if we are over the retention count limit
        if retention_count:
            delete_extra_backups(cloud, backup, retention_count)


    # If no repetition is provided, just do the job once
    if not repeat:
        now = datetime.now().replace(microsecond=0)
        logger.info(f"No more scheduled backups, finishing at {now}.")
        return

    # If job has repetition, schedule it    

    # Prepare the scheduler
    sch = getattr(schedule.every(), repeat)
    
    # If there is an end date, run until then
    if end:
        sch.until(end)

    # Schedule the job
    sch = sch.do(create_backup, cloud=cloud, backup=backup)

    # Run permanently as long as the job is scheduled
    while True:
        # Sleep until next execution
        n = schedule.idle_seconds()
        next_one = schedule.next_run()
        logger.debug(f"idle seconds {n}")
        logger.debug(f"{next_one}")
        if not n or n < 0 or (end and next_one > end):
            now = datetime.now().replace(microsecond=0)
            logger.info(f"No more scheduled backups, finishing at {now}.")
            break
        else:
            if n < 60:
                logger.info(f"Next scheduled backup will commence in {n} seconds.")
            else:
                next_one = schedule.next_run().replace(microsecond=0)
                logger.info(f"Next scheduled backup will commence on {next_one}.")
            time.sleep(n)
        now = datetime.now().replace(microsecond=0)
        logger.info(f"Scheduled backup begins at {now}.")
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"Failed scheduled backup: {e}")
            # After the exception the scheduler is messed up, so reschedule it
            schedule.cancel_job(sch)
            sch.do(create_backup, cloud=cloud, backup=backup)
        else:
            now = datetime.now().replace(microsecond=0)
            logger.info(f"Scheduled backup complete at {now}.")

            # After a succesful backup, check if we are over the retention count limit
            if retention_count:
                delete_extra_backups(cloud, backup, retention_count)

    

def schedule_backup(cloud: Connection, backup: dict) -> dict:
    """
    Schedule a snapshot of an instance/server
    """

    name_or_id = backup['name'] if 'name' in backup.keys() else backup['id']

    # Check the formatting of the date, and check it is in the future
    start = datetime.strptime(backup['scheduler']['start'], '%Y-%m-%d, %H:%M')
    today = datetime.today()
    if start < today:
        raise RuntimeError(f"Wrong starting date, {start}. Select a date in the future.")

    # End should be a valid date in the future
    end = None
    if backup['scheduler']['end']:
        end = datetime.strptime(backup['scheduler']['end'], '%Y-%m-%d, %H:%M')
        if end < today or end < start:
            raise RuntimeError(f"Wrong end date, {end}. Select a date in the future and later than starting date.")

    # Repetition must be "day" or "week"
    repeat = backup['scheduler']['repeat_every']
    if repeat and repeat not in ['day', 'week']:
            raise RuntimeError(f'Repeat pattern {repeat} is not accepted, must be \'day\' or \'week\'.')

    # Retention count must be a positive integer
    retention_count = backup['scheduler']['retention_count']
    if retention_count and (not isinstance(retention_count, int) or retention_count <= 0):
        raise RuntimeError(f'Retention count {retention_count} is not valid, must be a positive integer')

    # Create the process that will run the scheduling
    logger.info(f'Launching scheduling process.')
    p = Process(target=scheduler, 
                name="backup_scheduler",
                args=(cloud, backup, start, end, repeat, retention_count))
    p.start()
    p.join()
    logger.info(f'Scheduler launched')

    result = {
        'instance': name_or_id,
        'scheduled': True,
        'start': start.strftime("%Y-%m-%d, %H:%M"),
        'end': end.strftime("%Y-%m-%d, %H:%M") if end else None,
        'repeat_every': repeat,
        'retention_count': retention_count
    }

    return result

