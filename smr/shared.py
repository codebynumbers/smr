import argparse
import curses
import datetime
import logging
import os
import psutil
from Queue import Empty
import sys

from . import __version__

LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG
}

def ensure_dir_exists(path):
    dir_name = os.path.dirname(path)
    if dir_name != '' and not os.path.exists(dir_name):
        os.makedirs(dir_name)

def get_config():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: %s config.py\n" % (os.path.basename(sys.argv[0])))
        sys.exit(1)

    # this needs to be separate from argparse
    config_name = sys.argv[1]
    config = get_config_module(config_name)

    parser = get_arg_parser(config)
    args = parser.parse_args()

    # add extra options to args that cannot be specified in cli
    args.MAP_FUNC = config.MAP_FUNC
    args.REDUCE_FUNC = config.REDUCE_FUNC
    args.OUTPUT_RESULTS_FUNC = config.OUTPUT_RESULTS_FUNC
    args.AWS_EC2_INITIALIZE_SMR_COMMANDS = config.AWS_EC2_INITIALIZE_SMR_COMMANDS

    configure_logging(args)

    return args

def get_config_module(config_name):
    if config_name.endswith(".py"):
        config_name = config_name[:-3]
    elif config_name.endswith(".pyc"):
        config_name = config_name[:-4]

    directory, config_module = os.path.split(config_name)

    # If the directory isn't in the PYTHONPATH, add it so our import will work
    if directory not in sys.path:
        sys.path.insert(0, directory)

    config = __import__(config_module)

    # settings that are not overriden need to be set to defaults
    from . import default_config
    for k, v in default_config.__dict__.iteritems():
        if k.startswith("_"):
            continue
        if not hasattr(config, k):
            setattr(config, k, v)

    now = datetime.datetime.now()
    config.OUTPUT_FILENAME = config.OUTPUT_FILENAME % {"config_name": config_module, "time": now}
    ensure_dir_exists(config.OUTPUT_FILENAME)
    config.LOG_FILENAME = config.LOG_FILENAME % {"config_name": config_module}

    return config

def get_arg_parser(config):
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="config.py")

    parser.add_argument("--log-level", help="level of logging to be used for this job", choices=LOG_LEVELS.keys(), default=config.LOG_LEVEL)
    parser.add_argument("--paramiko-log-level", help="level of logging to be used for paramiko ssh connections (for smr-ec2 only)", choices=LOG_LEVELS.keys(), default=config.PARAMIKO_LOG_LEVEL)
    parser.add_argument("--log-format", help="""
format of log messages. available format params are:
 - message: actual log message
 - levelname: message log level
""", default=config.LOG_FORMAT)
    parser.add_argument("--log-filename", help="""
filename where log output for this job will be stored. available format params are:
 - config_name: basename of config file that's passed to smr
""", default=config.LOG_FILENAME)
    parser.add_argument("-w", "--workers", type=int, help="number of worker processes to use", default=config.NUM_WORKERS)
    parser.add_argument("--output-filename", help="""
filename where results for this job will be stored. available format params are:
 - config_name: basename of config file that's passed to smr
 - time: current date and time
""", default=config.OUTPUT_FILENAME)
    parser.add_argument("--input-data", help="List of files/directort URIs that contain input data to be processed. for example: ['s3://bucket/path', 'file://absolute/path']", nargs="*", default=config.INPUT_DATA)
    parser.add_argument("--aws-access-key", help="AWS access key used for S3/EC2 access", default=config.AWS_ACCESS_KEY)
    parser.add_argument("--aws-secret-key", help="AWS secret key used for S3/EC2 access", default=config.AWS_SECRET_KEY)
    parser.add_argument("--aws-ec2-region", help="region to use when running smr-ec2 workers", default=config.AWS_EC2_REGION)
    parser.add_argument("--aws-ec2-ami", help="AMI to use when running smr-ec2 workers", default=config.AWS_EC2_AMI)
    parser.add_argument("--aws-ec2-instance-type", help="instance type to use for EC2 instances", default=config.AWS_EC2_INSTANCE_TYPE)
    parser.add_argument("--aws-ec2-keyname", help="keyname to use for starting EC2 instances", default=config.AWS_EC2_KEYNAME)
    parser.add_argument("--aws-ec2-local-keyfile", help="local private key file used for ssh access to EC2 instances", default=config.AWS_EC2_LOCAL_KEYFILE)
    parser.add_argument("--aws-ec2-security-group", help="security group to use for accessing EC2 workers (needs port 22 open)", nargs="*", default=config.AWS_EC2_SECURITY_GROUPS)
    parser.add_argument("--aws-ec2-ssh-username", help="username to use when logging into EC2 workers over SSH", default=config.AWS_EC2_SSH_USERNAME)
    parser.add_argument("--aws-ec2-workers", help="number of EC2 instances to use for this job", type=int, default=config.AWS_EC2_WORKERS)
    parser.add_argument("--aws-ec2-remote-config-path", help="where to store smr config on EC2 instances", default=config.AWS_EC2_REMOTE_CONFIG_PATH)
    parser.add_argument("--pip-requirements", help="List of extra python packages needed for this job. for example: ['warc']", nargs="*", default=config.PIP_REQUIREMENTS)

    parser.add_argument("--version", action="version", version="%s %s" % (os.path.basename(sys.argv[0]), __version__))

    return parser

def configure_logging(config):
    level_str = config.log_level.lower()
    level = LOG_LEVELS.get(level_str, logging.INFO)
    ensure_dir_exists(config.log_filename)
    logging.basicConfig(level=level, format=config.log_format, filename=config.log_filename)

    if level_str not in LOG_LEVELS:
        logging.warn("invalid value for LOG_LEVEL: %s", config.log_level)

    paramiko_level_str = config.paramiko_log_level.lower()
    paramiko_level = LOG_LEVELS.get(paramiko_level_str, logging.WARNING)
    logging.getLogger("paramiko").setLevel(paramiko_level)

def reduce_thread(reduce_process, output_queue, abort_event):
    while not abort_event.is_set():
        try:
            # result has a trailing linebreak
            result = output_queue.get(timeout=2)
            if reduce_process.poll() is not None:
                # don't want to write if process has already terminated
                logging.error("reduce process %d ended with code %d", reduce_process.pid, reduce_process.returncode)
                abort_event.set()
                break
            reduce_process.stdin.write(result)
            reduce_process.stdin.flush()
            output_queue.task_done()
        except Empty:
            pass
    reduce_process.stdin.close()

def curses_thread(abort_event, map_processes, reduce_processes, window, start_time):
    map_pids = [psutil.Process(x.pid) for x in map_processes]
    reduce_pids = [psutil.Process(x.pid) for x in reduce_processes]
    while not abort_event.is_set() and not abort_event.wait(1.0):
        #curses.endwin()
        window.clear()
        now = datetime.datetime.now()
        window.addstr(0, 0, "smr v%s - %s - elapsed: %s" % (__version__, datetime.datetime.ctime(now), now - start_time))
        #overwrite_line(window, 1, "master job progress: {0:%}".format(files_processed / float(files_total)))
        #overwrite_line(window, 2, "last file processed: %s" % (file_name))
        i = 1
        for p in map_pids:
            print_pid(p, window, i, "smr-map")
            i += 1
        for p in reduce_pids:
            print_pid(p, window, i, "smr-reduce")
            i += 1
        window.refresh()

def print_pid(process, window, line_num, process_name):
    try:
        cpu_percent = process.get_cpu_percent(1.0)
    except:
        cpu_percent = 0.0
    window.addstr(line_num, 0, "  {0} pid {1} CPU {2}".format(process_name, process.pid, cpu_percent))

def progress_thread(processed_files_queue, abort_event):
    files_processed = 0
    while not abort_event.is_set():
        try:
            file_name = processed_files_queue.get(timeout=2)
            logging.debug("master received signal that %s is processed", file_name)
            files_processed += 1
            processed_files_queue.task_done()
        except Empty:
            pass

def write_file_to_descriptor(input_queue, descriptor):
    """
    get item from input_queue and write it to descriptor
    returns True if and only if it was successfully written
    """
    try:
        file_name = input_queue.get(timeout=2)
        descriptor.write("%s\n" % file_name)
        descriptor.flush()
        input_queue.task_done()
        return True
    except Empty:
        # no more files in queue
        descriptor.close()
        return False
    except IOError:
        return False # probably bad descriptor
