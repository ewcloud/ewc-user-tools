FROM docker.io/python:3.10

ENV APP_DIR=/usr/local/bin/openstack-backups
RUN mkdir APP_DIR

# Copy pip to use pinned down versions
COPY Pipfile      $APP_DIR/
COPY Pipfile.lock $APP_DIR/

# set working directory
WORKDIR $APP_DIR

# change file ownership
RUN chmod -R +x $APP_DIR

RUN echo "Install pipenv" && \
    pip install pipenv

# Create virtualenv and install dependencies
RUN echo "Create virtualenv" && \
    pipenv install

# copy main file
COPY . $APP_DIR

# run main script
# change the test yaml with the configuration file of your choice to run the container
ENTRYPOINT ["pipenv", "run", "python", "openstack_backups.py", "tests/test_backup.yaml"]

