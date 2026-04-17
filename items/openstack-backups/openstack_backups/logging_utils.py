#!/usr/bin/env python3

"""
Logger

Author: Tomas Gonzalo
"""

import os
import sys
import logging

# Create root logger
rootlogger = logging.getLogger()
basicformat = "[%(levelname)s] %(message)s"
if os.getenv('DEBUG', False):
    logging.basicConfig(level=logging.DEBUG, format=basicformat)
else:
    logging.basicConfig(level=logging.INFO, format=basicformat)

# Ensure logging directory exists
logging_directory = os.path.join(os.path.abspath('.'),'logs')
if not os.path.exists(logging_directory):
    os.makedirs(logging_directory)
filepath = os.path.join(logging_directory,'backup.log')

# Set up file handler
filehandler = logging.FileHandler(filepath)
filehandler.setFormatter(logging.Formatter('[%(levelname)s, %(asctime)s] %(message)s'))

# Create child logger
logger = logging.getLogger('backup')
logger.addHandler(filehandler)
