# Copyright 2012-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the ``maasrackd`` TAP."""


from itertools import count
import os
from pathlib import Path
from subprocess import Popen

import crochet
from fixtures import EnvironmentVariable
from testtools.matchers import (
    AfterPreprocessing,
    Contains,
    Equals,
    IsInstance,
    KeysEqual,
    MatchesAll,
    MatchesStructure,
    Not,
)
from twisted.application.internet import StreamServerEndpointService
from twisted.application.service import MultiService
from twisted.internet.task import Clock

from maastesting import get_testing_timeout
from maastesting.fixtures import TempDirectory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
import provisioningserver
from provisioningserver import logger
from provisioningserver import plugin as plugin_module
from provisioningserver import settings
from provisioningserver.config import ClusterConfiguration
from provisioningserver.plugin import Options, ProvisioningServiceMaker
from provisioningserver.rackdservices.dhcp_probe_service import (
    DHCPProbeService,
)
from provisioningserver.rackdservices.external import RackExternalService
from provisioningserver.rackdservices.image_download_service import (
    ImageDownloadService,
)
from provisioningserver.rackdservices.lease_socket_service import (
    LeaseSocketService,
)
from provisioningserver.rackdservices.networks_monitoring_service import (
    RackNetworksMonitoringService,
)
from provisioningserver.rackdservices.node_power_monitor_service import (
    NodePowerMonitorService,
)
from provisioningserver.rackdservices.service_monitor_service import (
    ServiceMonitorService,
)
from provisioningserver.rackdservices.tftp import TFTPBackend, TFTPService
from provisioningserver.rackdservices.tftp_offload import TFTPOffloadService
from provisioningserver.rackdservices.version_update_check import (
    VersionUpdateCheckService,
)
from provisioningserver.rpc.clusterservice import ClusterClientCheckerService
from provisioningserver.testing.config import ClusterConfigurationFixture


class TestOptions(MAASTestCase):
    """Tests for `provisioningserver.plugin.Options`."""

    def test_defaults(self):
        options = Options()
        self.assertEqual({}, options.defaults)

    def test_parse_minimal_options(self):
        options = Options()
        # The minimal set of options that must be provided.
        arguments = []
        options.parseOptions(arguments)  # No error.


class TestProvisioningServiceMaker(MAASTestCase):
    """Tests for `provisioningserver.plugin.ProvisioningServiceMaker`."""

    run_tests_with = MAASTwistedRunTest.make_factory(
        timeout=get_testing_timeout()
    )

    def setUp(self):
        super().setUp()
        self.useFixture(ClusterConfigurationFixture())
        self.patch(provisioningserver, "services", MultiService())
        self.patch_autospec(crochet, "no_setup")
        self.patch_autospec(logger, "configure")
        self.mock_generate_certificate = self.patch(
            plugin_module, "generate_certificate_if_needed"
        )
        # by default, define a shared secret so that sevices are populated
        self.mock_get_shared_secret = self.patch(
            plugin_module, "get_shared_secret_from_filesystem"
        )
        self.mock_get_shared_secret.return_value = "secret"

    def get_unused_pid(self):
        """Return a PID for a process that has just finished running."""
        proc = Popen(["/bin/true"])
        proc.wait()
        return proc.pid

    def test_init(self):
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        self.assertEqual("Harry", service_maker.tapname)
        self.assertEqual("Hill", service_maker.description)

    def test_makeService_no_shared_secret(self):
        self.mock_get_shared_secret.return_value = None
        service_maker = ProvisioningServiceMaker("foo", "bar")
        self.patch(service_maker, "_loadSettings")
        clock = Clock()

        attempts = count()

        def advance(seconds):
            next(attempts)
            clock.advance(seconds)

        service = service_maker.makeService(
            Options(), clock=clock, sleep=advance
        )
        self.assertIsInstance(service, MultiService)
        self.assertEqual(service.namedServices, {})
        self.mock_generate_certificate.assert_not_called()
        # All 300 attempts (e.g. 5 minutes) fail, next is 301
        self.assertEqual(next(attempts), 301)

    def test_makeService_eventual_shared_secret(self):
        # First two times we look, there's no secret
        self.mock_get_shared_secret.side_effect = [None, None, "secret"]
        service_maker = ProvisioningServiceMaker("foo", "bar")
        self.patch(service_maker, "_loadSettings")
        clock = Clock()

        attempts = count(1)

        def advance(seconds):
            next(attempts)
            clock.advance(60)

        service = service_maker.makeService(
            Options(), clock=clock, sleep=advance
        )
        self.assertIsInstance(service, MultiService)
        self.assertNotEqual(service.namedServices, {})
        # First two fail, the third one succeeds
        self.assertEqual(next(attempts), 3)

    def test_makeService_not_in_debug(self):
        """
        Only the site service is created when no options are given.
        """
        self.patch(settings, "DEBUG", False)
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        self.patch(service_maker, "_loadSettings")
        service = service_maker.makeService(options, clock=None)
        self.assertIsInstance(service, MultiService)
        expected_services = [
            "dhcp_probe",
            "networks_monitor",
            "image_download",
            "lease_socket_service",
            "node_monitor",
            "external",
            "rpc",
            "rpc-ping",
            "http",
            "http_service",
            "tftp",
            "service_monitor",
            "version_update_check",
        ]
        self.assertThat(service.namedServices, KeysEqual(*expected_services))
        self.assertEqual(
            len(service.namedServices),
            len(service.services),
            "Not all services are named.",
        )
        self.assertEqual(service, provisioningserver.services)
        self.assertThat(crochet.no_setup, MockCalledOnceWith())
        self.assertThat(
            logger.configure,
            MockCalledOnceWith(
                options["verbosity"], logger.LoggingMode.TWISTD
            ),
        )

    def test_makeService_in_debug(self):
        """
        Only the site service is created when no options are given.
        """
        self.patch(settings, "DEBUG", True)
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        self.patch(service_maker, "_loadSettings")
        service = service_maker.makeService(options, clock=None)
        self.assertIsInstance(service, MultiService)
        expected_services = [
            "dhcp_probe",
            "networks_monitor",
            "image_download",
            "lease_socket_service",
            "node_monitor",
            "external",
            "rpc",
            "rpc-ping",
            "http",
            "http_service",
            "tftp",
            "service_monitor",
            "version_update_check",
        ]
        self.assertThat(service.namedServices, KeysEqual(*expected_services))
        self.assertEqual(
            len(service.namedServices),
            len(service.services),
            "Not all services are named.",
        )
        self.assertEqual(service, provisioningserver.services)
        self.assertThat(crochet.no_setup, MockCalledOnceWith())
        self.assertThat(
            logger.configure, MockCalledOnceWith(3, logger.LoggingMode.TWISTD)
        )

    def test_makeService_with_EXPERIMENTAL_tftp_offload_service(self):
        """
        Only the site service is created when no options are given.
        """
        # Activate the offload service by setting port to 0.
        self.useFixture(ClusterConfigurationFixture(tftp_port=0))

        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        self.assertIsInstance(service, MultiService)
        self.assertThat(service.namedServices, Not(Contains("tftp")))
        self.assertThat(service.namedServices, Contains("tftp-offload"))
        tftp_offload_service = service.getServiceNamed("tftp-offload")
        self.assertThat(tftp_offload_service, IsInstance(TFTPOffloadService))

    def test_makeService_patches_tftp_service(self):
        mock_tftp_patch = self.patch(plugin_module, "add_patches_to_txtftp")
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service_maker.makeService(options, clock=None)
        self.assertThat(mock_tftp_patch, MockCalledOnceWith())

    def test_makeService_cleanup_prometheus_dir(self):
        tmpdir = Path(self.useFixture(TempDirectory()).path)
        self.useFixture(
            EnvironmentVariable("prometheus_multiproc_dir", str(tmpdir))
        )
        pid = os.getpid()
        file1 = tmpdir / f"histogram_{pid}.db"
        file1.touch()
        file2 = tmpdir / f"histogram_{self.get_unused_pid()}.db"
        file2.touch()

        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service_maker.makeService(Options(), clock=None)
        self.assertTrue(file1.exists())
        self.assertFalse(file2.exists())

    def test_image_download_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        image_service = service.getServiceNamed("image_download")
        self.assertIsInstance(image_service, ImageDownloadService)

    def test_node_monitor_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        node_monitor = service.getServiceNamed("node_monitor")
        self.assertIsInstance(node_monitor, NodePowerMonitorService)

    def test_networks_monitor_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Spike", "Milligan")
        service = service_maker.makeService(options, clock=None)
        networks_monitor = service.getServiceNamed("networks_monitor")
        self.assertIsInstance(networks_monitor, RackNetworksMonitoringService)

    def test_dhcp_probe_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Spike", "Milligan")
        service = service_maker.makeService(options, clock=None)
        dhcp_probe = service.getServiceNamed("dhcp_probe")
        self.assertIsInstance(dhcp_probe, DHCPProbeService)

    def test_service_monitor_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        service_monitor = service.getServiceNamed("service_monitor")
        self.assertIsInstance(service_monitor, ServiceMonitorService)

    def test_rpc_ping_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        rpc_ping = service.getServiceNamed("rpc-ping")
        self.assertIsInstance(rpc_ping, ClusterClientCheckerService)

    def test_external_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        external_service = service.getServiceNamed("external")
        self.assertIsInstance(external_service, RackExternalService)

    def test_version_update_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        version_check_service = service.getServiceNamed("version_update_check")
        self.assertIsInstance(version_check_service, VersionUpdateCheckService)

    def test_http_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        http_service = service.getServiceNamed("http_service")
        self.assertIsInstance(http_service, StreamServerEndpointService)

    def test_tftp_service(self):
        # A TFTP service is configured and added to the top-level service.
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        tftp_service = service.getServiceNamed("tftp")
        self.assertIsInstance(tftp_service, TFTPService)

        with ClusterConfiguration.open() as config:
            tftp_root = config.tftp_root
            tftp_port = config.tftp_port

        expected_backend = MatchesAll(
            IsInstance(TFTPBackend),
            AfterPreprocessing(
                lambda backend: backend.base.path, Equals(tftp_root)
            ),
        )

        self.assertThat(
            tftp_service,
            MatchesStructure(backend=expected_backend, port=Equals(tftp_port)),
        )

    def test_lease_socket_service(self):
        options = Options()
        service_maker = ProvisioningServiceMaker("Harry", "Hill")
        service = service_maker.makeService(options, clock=None)
        lease_socket_service = service.getServiceNamed("lease_socket_service")
        self.assertIsInstance(lease_socket_service, LeaseSocketService)
