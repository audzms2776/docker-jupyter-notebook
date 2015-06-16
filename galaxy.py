from bioblend import galaxy
from bioblend.galaxy.tools import ToolClient
from bioblend.galaxy.histories import HistoryClient
from bioblend.galaxy.datasets import DatasetClient
from bioblend.galaxy import objects
import subprocess
import argparse
import os
from string import Template
import logging
logging.getLogger("bioblend").setLevel(logging.WARNING)
DEBUG = os.environ.get('DEBUG', False) == 'True'
if DEBUG:
    logging.basicConfig(level=logging.DEBUG)
tlog = logging.getLogger()


# Consider not using objects deprecated.
DEFAULT_USE_OBJECTS = True
ENV_KEYS = ('DEBUG', 'GALAXY_WEB_PORT', 'NOTEBOOK_PASSWORD', 'CORS_ORIGIN',
            'DOCKER_PORT', 'API_KEY', 'HISTORY_ID', 'REMOTE_HOST',
            'GALAXY_URL')


def _get_conf():
    conf = {}
    for key in ENV_KEYS:
        conf[key.lower()] = os.environ.get(key, None)
    conf['galaxy_paster_port'] = conf['galaxy_web_port']
    return conf


def _get_ip():
    """Get IP address for the docker host
    """
    cmd_netstat = ['netstat','-nr']
    p1 = subprocess.Popen(cmd_netstat, stdout=subprocess.PIPE)
    cmd_grep = ['grep', '^0\.0\.0\.0']
    p2 = subprocess.Popen(cmd_grep, stdin=p1.stdout, stdout=subprocess.PIPE)
    cmd_awk = ['awk', '{ print $2 }']
    p3 = subprocess.Popen(cmd_awk, stdin=p2.stdout, stdout=subprocess.PIPE)
    galaxy_ip = p3.stdout.read()
    log.debug('Host IP determined to be %s', galaxy_ip)
    return galaxy_ip


def _test_url(url, key, history_id, use_objects=False):
    """Test the functionality of a given galaxy URL, to ensure we can connect
    on that address."""
    try:
        if use_objects:
            gi = objects.GalaxyInstance(url, key)
            gi.histories.get(history_id)
        else:
            gi = galaxy.GalaxyInstance(url=url, key=key)
            gi.histories.get_histories()
        log.debug('Galaxy URL %s is functional', url)
        return gi
    except Exception:
        return None


def get_galaxy_connection( use_objects=DEFAULT_USE_OBJECTS ):
    """
        Given access to the configuration dict that galaxy passed us, we try and connect to galaxy's API.

        First we try connecting to galaxy directly, using an IP address given
        us by docker (since the galaxy host is the default gateway for docker).
        Using additional information collected by galaxy like the port it is
        running on and the application path, we build a galaxy URL and test our
        connection by attempting to get a history listing. This is done to
        avoid any nasty network configuration that a SysAdmin has placed
        between galaxy and us inside docker, like disabling API queries.

        If that fails, we failover to using the URL the user is accessing
        through. This will succeed where the previous connection fails under
        the conditions of REMOTE_USER and galaxy running under uWSGI.
    """
    conf = _get_conf()
    history_id = conf["history_id"]
    key = conf['api_key']

    ### Customised/Raw galaxy_url ###
    galaxy_ip = _get_ip()
    # Substitute $DOCKER_HOST with real IP
    url = Template(conf['galaxy_url']).safe_substitute({'DOCKER_HOST': galaxy_ip})
    gi = _test_url(url, key, history_id, use_objects=use_objects)
    if gi is not None:
        return gi

    ### Failover, fully auto-detected URL ###
    # Remove trailing slashes
    app_path = conf['galaxy_url'].rstrip('/')
    # Remove protocol+host:port if included
    app_path = ''.join(app_path.split('/')[3:])

    if 'galaxy_paster_port' not in conf:
        # We've failed to detect a port in the config we were given by
        # galaxy, so we won't be able to construct a valid URL
        raise Exception("No port")
    else:
        # We should be able to find a port to connect to galaxy on via this
        # conf var: galaxy_paster_port
        galaxy_port = conf['galaxy_paster_port']

    built_galaxy_url = 'http://%s:%s/%s' %  (galaxy_ip.strip(), galaxy_port, app_path.strip())
    url = built_galaxy_url.rstrip('/')

    gi = _test_url(url, key, history_id, use_objects=use_objects)
    if gi is not None:
        return gi

    ### Fail ###
    raise Exception("Could not connect to a galaxy instance. Please contact your SysAdmin for help with this error")


def _get_history_id():
    """
    Extract the history ID from the config file.
    """
    conf = _get_conf()
    return conf['history_id']


def put(filename, file_type='auto', history_id=None, use_objects=DEFAULT_USE_OBJECTS):
    """
        Given a filename of any file accessible to the docker instance, this
        function will upload that file to galaxy using the current history.
        Does not return anything.
    """
    gi = get_galaxy_connection(use_objects)
    if use_objects:
        history = gi.histories.get( history_id )
        history.upload_dataset(filename, file_type=file_type)
    else:
        tc = ToolClient( gi )
        tc.upload_file(filename, history_id, file_type=file_type)


def get(dataset_id, history_id=None, use_objects=DEFAULT_USE_OBJECTS):
    """
        Given the history_id that is displayed to the user, this function will
        download the file from the history and stores it under /import/
        Return value is the path to the dataset stored under /import/
    """
    gi = get_galaxy_connection(use_objects)

    file_path = '/import/%s' % dataset_id

    # Cache the file requests. E.g. in the example of someone doing something
    # silly like a get() for a Galaxy file in a for-loop, wouldn't want to
    # re-download every time and add that overhead.
    if not os.path.exists(file_path):
        if use_objects:
            history = gi.histories.get(history_id)
            datasets = dict([( d.wrapped["hid"], d.id ) for d in history.get_datasets()])
            dataset = history.get_dataset( datasets[dataset_id] )
            dataset.download( open(file_path, 'wb') )
        else:
            hc = HistoryClient(gi)
            dc = DatasetClient(gi)
            dataset_mapping = dict([(dataset['hid'], dataset['id']) for dataset in hc.show_history(history_id, contents=True)])
            try:
                hc.download_dataset(history_id, dataset_mapping[dataset_id], file_path, use_default_filename=False, to_ext=None)
            except:
                dc.download_dataset(dataset_mapping[dataset_id], file_path, use_default_filename=False)

    return file_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Connect to Galaxy through the API')
    parser.add_argument('--action',   help='Action to execute', choices=['get', 'put'])
    parser.add_argument('--argument', help='File/ID number to Upload/Download, respectively')
    parser.add_argument('--history-id', dest="history_id", default=None,
        help='History ID. The history ID and the dataset ID uniquly identify a dataset. Per default this is set to the current Galaxy history.')
    parser.add_argument('-t', '--filetype', help='Galaxy file format. If not specified Galaxy will try to guess the filetype automatically.', default='auto')
    args = parser.parse_args()

    history_id = args.history_id or _get_history_id()

    if args.action == 'get':
        # Ensure it's a numerical value
        get(int(args.argument), history_id=history_id)
    elif args.action == 'put':
        put(args.argument, file_type=args.filetype, history_id=history_id)
