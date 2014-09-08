# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Send lease updates to the server.

This code runs inside node-group workers.  It watches for changes to DHCP
leases, and notifies the MAAS server so that it can rewrite DNS zone files
as appropriate.

Leases in this module are represented as dicts, mapping each leased IP
address to the MAC address that it belongs to.

The modification time and leases of the last-uploaded leases are cached,
so as to suppress unwanted redundant updates.  This cache is updated
*before* the actual upload, so as to prevent thundering-herd problems:
if an upload takes too long for whatever reason, subsequent updates
should not be uploaded until the first upload is done.  Some uploads may
be lost due to concurrency or failures, but the situation will right
itself eventually.
"""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    'upload_leases',
    'update_leases',
    ]


from collections import defaultdict
import cPickle
import errno
import json
from os import (
    fstat,
    stat,
    )

from apiclient.maas_client import (
    MAASClient,
    MAASDispatcher,
    MAASOAuth,
    )
from provisioningserver.auth import (
    get_recorded_api_credentials,
    get_recorded_nodegroup_uuid,
    )
from provisioningserver.cluster_config import get_maas_url
from provisioningserver.dhcp.leases_parser_fast import parse_leases
from provisioningserver.logger import get_maas_logger
from provisioningserver.utils.shell import pipefork


maaslog = get_maas_logger("dhcp.leases")

# This used to be the cache in provisioningserver.cache, but that
# unfortunately makes Twisted fail in ways we can't work out, probably
# because of its use of multiprocessing.  Instead it's now using a
# simple dict, because there are no plans to make pserv multi-process.
cache = defaultdict()


# Cache key for the modification time on last-processed leases file.
LEASES_TIME_CACHE_KEY = 'leases_time'


# Cache key for the leases as last parsed.
LEASES_CACHE_KEY = 'recorded_leases'


def get_leases_file():
    """Return the location of the DHCP leases file."""
    # This used to be celery config-based so that the development env could
    # have a different location.  However, nobody seems to be
    # provisioning from a dev environment so it's hard-coded until that
    # need arises, as converting to the pserv config would be wasted
    # work right now.
    return "/var/lib/maas/dhcp/dhcpd.leases"


def get_leases_timestamp():
    """Return the last modification timestamp of the DHCP leases file.

    None will be returned if the DHCP lease file cannot be found.
    """
    try:
        return stat(get_leases_file()).st_mtime
    except OSError as exception:
        # Return None only if the exception is a "No such file or
        # directory" exception.
        if exception.errno == errno.ENOENT:
            return None
        else:
            raise


def parse_leases_file():
    """Parse the DHCP leases file.

    :return: A tuple: (timestamp, leases).  The `timestamp` is the last
        modification time of the leases file, and `leases` is a dict
        mapping leased IP addresses to their associated MAC addresses.
        None will be returned if the DHCP lease file cannot be found.
    """
    try:
        with open(get_leases_file(), 'rb') as leases_file:
            contents = leases_file.read().decode('utf-8')
            return fstat(leases_file.fileno()).st_mtime, parse_leases(contents)
    except IOError as exception:
        # Return None only if the exception is a "No such file or
        # directory" exception.
        if exception.errno == errno.ENOENT:
            return None
        else:
            raise


def check_lease_changes():
    """Has the DHCP leases file changed in any significant way?"""
    # These variables are shared between worker threads/processes.
    # A bit of inconsistency due to concurrent updates is not a problem,
    # but read them both at once here to reduce the scope for trouble.
    previous_leases = cache.get(LEASES_CACHE_KEY)
    previous_leases_time = cache.get(LEASES_TIME_CACHE_KEY)

    if get_leases_timestamp() == previous_leases_time:
        return None

    with pipefork() as (pid, fin, fout):
        if pid == 0:
            # Child, where we'll do the parsing.
            cPickle.dump(
                parse_leases_file(),
                fout, cPickle.HIGHEST_PROTOCOL)
        else:
            # Parent, where we'll receive the results.
            parse_result = cPickle.load(fin)

    if parse_result is not None:
        timestamp, leases = parse_result
        if leases == previous_leases:
            return None
        else:
            return timestamp, leases
    else:
        return None


def record_lease_state(last_change, leases):
    """Record a snapshot of the state of DHCP leases.

    :param last_change: Modification date on the leases file with the given
        leases.
    :param leases: A dict mapping each leased IP address to the MAC address
        that it has been assigned to.
    """
    cache[LEASES_TIME_CACHE_KEY] = last_change
    cache[LEASES_CACHE_KEY] = leases


def list_missing_items(knowledge):
    """Report items from dict `knowledge` that are still `None`."""
    return sorted(name for name, value in knowledge.items() if value is None)


def send_leases(leases):
    """Send lease updates to the server API."""
    # Items that the server must have sent us before we can do this.
    knowledge = {
        'maas_url': get_maas_url(),
        'api_credentials': get_recorded_api_credentials(),
        'nodegroup_uuid': get_recorded_nodegroup_uuid(),
    }
    if None in knowledge.values():
        # The MAAS server hasn't sent us enough information for us to do
        # this yet.  Leave it for another time.
        maaslog.info(
            "Not sending DHCP leases to server: not all required knowledge "
            "received from server yet.  "
            "Missing: %s"
            % ', '.join(list_missing_items(knowledge)))
        return

    api_path = 'api/1.0/nodegroups/%s/' % knowledge['nodegroup_uuid']
    oauth = MAASOAuth(*knowledge['api_credentials'])
    MAASClient(oauth, MAASDispatcher(), knowledge['maas_url']).post(
        api_path, 'update_leases', leases=json.dumps(leases))


def process_leases(timestamp, leases):
    """Send new leases to the MAAS server."""
    record_lease_state(timestamp, leases)
    send_leases(leases)


def upload_leases():
    """Unconditionally send the current DHCP leases to the server.

    Run this periodically just so no changes slip through the cracks.
    Examples of such cracks would be: subtle races, failure to upload,
    server restarts, or zone-file update commands getting lost on their
    way to the DNS server.
    """
    parse_result = parse_leases_file()
    if parse_result:
        timestamp, leases = parse_result
        process_leases(timestamp, leases)
    else:
        maaslog.info(
            "The DHCP leases file does not exist.  This is only a problem if "
            "this cluster controller is managing its DHCP server.  If that's "
            "the case then you need to install the 'maas-dhcp' package on "
            "this cluster controller.")


def update_leases():
    """Check for DHCP lease updates, and send them to the server if needed.

    Run this whenever a lease has been added, removed, or changed.  It
    will be very cheap to run if the leases file has not been touched,
    and it won't upload unless there have been relevant changes.
    """
    updated_lease_info = check_lease_changes()
    if updated_lease_info is not None:
        timestamp, leases = updated_lease_info
        process_leases(timestamp, leases)
