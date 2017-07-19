import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from threading import Thread

if sys.version_info > (3, 0):
    from queue import Queue, Empty
    from . import wrenutil
    from . import version

else:
    from Queue import Queue, Empty
    import wrenutil
    import version

TEMP = "D:\local\Temp"

PYTHON_MODULE_PATH = "/tmp/pymodules"

logger = logging.getLogger(__name__)

PROCESS_STDOUT_SLEEP_SECS = 2

def download_runtime_if_necessary(azclient, runtime_s3_bucket, runtime_s3_key):
    """
    Download the runtime if necessary

    return True if cached, False if not (download occured)
    """
    # figure this out later. 
    return True

def b64str_to_bytes(str_data):
    str_ascii = str_data.encode('ascii')
    byte_data = base64.b64decode(str_ascii)
    return byte_data

def az_handler(event, context):
    logger.setlevel(logging.INFO)
    return generic_handler(event, context)

def get_server_info():
    server_info = {'uname': ' '.join(os.uname())}
    return server_info

def generic_handler(event, context_dict):
    """
    context_dict is generic infromation about the context
    that we are running in, provided by the scheduler
    """
    response_status = {'exception': None}
    try:
        if event['storage_config']['storage_backend'] != 'az':
            raise NotImplementedError(("Using {} as storage backend is not supported " +
t
                                       "yet.").format(event['storage_config']['storage_backend']))
        bucket = event['storage_config']['backend_config']['bucket']

        logger.info("invocation started")

        # download the input
        data_byte_range = event['data_byte_range']

        if version.__version__ != event['pywren_version']:
            raise Exception("WRONGVERSION", "Pywren version mismatch",
                            version.__version__, event['pywren_version'])

        start_time = time.time()
        response_status['start_time'] = start_time

        func_filename = os.environ["func_file"]
        data_filename = os.environ["data_file"]
        output_filename = os.environ["output_file"]

        # download times don't make sense on azure since everything's preloaded.

        if not (data_byte_range is None):
            raise NotImplementedError("Using data range not supported yet")

        # now split
        d = json.load(open(func_filename, 'r'))
        shutil.rmtree(PYTHON_MODULE_PATH, True) # delete old modules
        os.mkdir(PYTHON_MODULE_PATH)
        # get modules and save
        for m_filename, m_data in d['module_data'].items():
            m_path = os.path.dirname(m_filename)

            if len(m_path) > 0 and m_path[0] == "/":
                m_path = m_path[1:]
            to_make = os.path.join(PYTHON_MODULE_PATH, m_path)
            #print "to_make=", to_make, "m_path=", m_path
            try:
                os.makedirs(to_make)
            except OSError as e:
                if e.errno == 17:
                    pass
                else:
                    raise e
            full_filename = os.path.join(to_make, os.path.basename(m_filename))
            #print "creating", full_filename
            fid = open(full_filename, 'wb')
            fid.write(b64str_to_bytes(m_data))
            fid.close()
        logger.info("Finished writing {} module files".format(len(d['module_data'])))
        logger.debug(subprocess.check_output("find {}".format(PYTHON_MODULE_PATH), shell=True))
        logger.debug(subprocess.check_output("find {}".format(os.getcwd()), shell=True))

        response_status['runtime_s3_key_used'] = runtime_s3_key_used
        response_status['runtime_s3_bucket_used'] = runtime_s3_bucket_used

        runtime_cached = download_runtime_if_necessary(s3_client, runtime_s3_bucket_used,
                                                       runtime_s3_key_used)
        logger.info("Runtime ready, cached={}".format(runtime_cached))
        response_status['runtime_cached'] = runtime_cached

        cwd = os.getcwd()
        jobrunner_path = os.path.join(cwd, "jobrunner.py")

        extra_env = event.get('extra_env', {})
        extra_env['PYTHONPATH'] = "{}:{}".format(os.getcwd(), PYTHON_MODULE_PATH)

        call_id = event['call_id']
        callset_id = event['callset_id']
        response_status['call_id'] = call_id
        response_status['callset_id'] = callset_id

        CONDA_PYTHON_PATH = "D:\home\site\wwwroot\conda\Miniconda2"
        CONDA_PYTHON_RUNTIME = os.path.join(CONDA_PYTHON_PATH, "python")

        cmdstr = "{} {} {} {} {}".format(CONDA_PYTHON_RUNTIME,
                                         jobrunner_path,
                                         func_filename,
                                         data_filename,
                                         output_filename)

        setup_time = time.time()
        response_status['setup_time'] = setup_time - start_time

        local_env = os.environ.copy()

        local_env["OMP_NUM_THREADS"] = "1"
        local_env.update(extra_env)

        local_env['PATH'] = "{}:{}".format(CONDA_PYTHON_PATH, local_env.get("PATH", ""))

        logger.debug("command str=%s", cmdstr)
        # This is copied from http://stackoverflow.com/a/17698359/4577954
        # reasons for setting process group: http://stackoverflow.com/a/4791612
        process = subprocess.Popen(cmdstr, shell=True, env=local_env, bufsize=1,
                                   stdout=subprocess.PIPE, preexec_fn=os.setsid)

        logger.info("launched process")
        def consume_stdout(stdout, queue):
            with stdout:
                for line in iter(stdout.readline, b''):
                    queue.put(line)

        q = Queue()

        t = Thread(target=consume_stdout, args=(process.stdout, q))
        t.daemon = True
        t.start()

        stdout = b""
        while t.isAlive():
            try:
                line = q.get_nowait()
                stdout += line
                logger.info(line)
            except Empty:
                time.sleep(PROCESS_STDOUT_SLEEP_SECS)
            total_runtime = time.time() - start_time
            if total_runtime > job_max_runtime:
                logger.warn("Process exceeded maximum runtime of {} sec".format(job_max_runtime))
                # Send the signal to all the process groups
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                raise Exception("OUTATIME",
                                "Process executed for too long and was killed")


        logger.info("command execution finished")

        end_time = time.time()

        response_status['stdout'] = stdout.decode("ascii")
        response_status['exec_time'] = time.time() - setup_time
        response_status['end_time'] = end_time
        response_status['host_submit_time'] = event['host_submit_time']
        response_status['server_info'] = get_server_info()

        response_status.update(context_dict)
    except Exception as e:
        # internal runtime exceptions
        response_status['exception'] = str(e)
        response_status['exception_args'] = e.args
        response_status['exception_traceback'] = traceback.format_exc()
    finally:
        status_file = open(os.environ["status_file"], 'w')
        status_file.write(json.dumps(response_status))
        status_file.close()

if __name__ == "__main__":
    event_file = open(os.environ["queueMessage"])
    az_handler(json.load(event_file), {})
