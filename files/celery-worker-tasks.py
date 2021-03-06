#!/usr/bin/env python

import argparse
import os
import sys
import docker
import errno
from random import uniform

from celery import Celery
from celery.utils.log import get_task_logger

LOGGER = get_task_logger(__name__)
APP_NAME = 'tasks'

try:
    app = Celery(APP_NAME)
    app.config_from_object('celeryconfig', force=True)
except ImportError:
    redis_url = os.getenv('REDIS_URL', 'redis://')
    print 'celeryconfig not found, using %s' % redis_url
    app = Celery(APP_NAME, broker=redis_url, backend=redis_url)


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno == errno.EEXIST:
            pass


# http://docs.celeryproject.org/en/latest/userguide/tasks.html
@app.task(bind=True)
def run_docker(self, image, command, user=None,
               logoutfile=None, logerrfile=None,
               inputpath=None, outputpath=None):

    client = docker.from_env()
    kwargs = dict(
        command=command,
        detach=False,
        name='run-%s' % self.request.id,
        remove=True,
        stderr=True,
        stdout=True,
    )
    if user is not None:
        kwargs['user'] = user
    # kwargs['cpu_shares'] = 1

    volumes = {}
    if inputpath:
        if inputpath == outputpath:
            raise ValueError(
                'inputpath cannot be the same as outputpath: %s' % inputpath)
        if not os.path.isabs(inputpath) or not os.path.exists(inputpath):
            raise ValueError(
                'inputpath must be an existing absolute path: %s' % inputpath)
        volumes[inputpath] = {'bind': '/input', 'mode': 'ro'}
    if outputpath:
        if not os.path.isabs(outputpath) or not os.path.exists(outputpath):
            raise ValueError(
                'outputpath must be an existing absolute path: %s' %
                outputpath)
        volumes[outputpath] = {'bind': '/output', 'mode': 'rw'}
    if volumes:
        kwargs['volumes'] = volumes
        LOGGER.info("volumes: %s" % volumes)

    if logoutfile:
        mkdir_p(os.path.dirname(os.path.abspath(logoutfile)))
        LOGGER.info("stdout: %s" % logoutfile)
    # if logerrfile:

    # Only retry on ContainerErrors
    try:
        output = client.containers.run(image, **kwargs)
    except docker.errors.ContainerError as e:
        # Exponential backoff + jitter
        delay_base = app.conf.get('CUSTOM_RETRY_DELAY', 1)
        delay = int(uniform(2, 4) ** self.request.retries * delay_base)
        raise self.retry(countdown=delay, exc=e)

    if logoutfile:
        with open(logoutfile, 'w') as f:
            f.write(output)

    r = {'id': self.request.id, 'output': output}
    return r


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--inputpath',
                        help='Input path (mounted read-only as /input)')
    parser.add_argument('--outputpath',
                        help='Output path (mounted read-write as /output)')
    parser.add_argument('--out', help='Output stdout log file')
    # parser.add_argument('--err', help='Output stderr log file')
    parser.add_argument('image', help='Docker image')
    parser.add_argument('commands', nargs='*', help='Command arguments')

    args = parser.parse_args(argv[1:])
    if args.verbose:
        print app.conf.humanize(with_defaults=False, censored=True)
    return run_docker.delay(
        args.image, args.commands, logoutfile=args.out,
        inputpath=args.inputpath, outputpath=args.outputpath)


if __name__ == "__main__":
    r = main(sys.argv)
    print r
