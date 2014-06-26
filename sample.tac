# Setup Config
import conf.ConfigLoader as ConfigLoader
ConfigLoader.CONFIG_FILE = ''
import lib.settings as settings

# Bootstrap Stratum framework and run listening when mining service is ready
from twisted.internet import defer
from twisted.application.service import Application, IProcess
import lib.stratum
on_startup = defer.Deferred()
application = lib.stratum.setup(on_startup)
IProcess(application).processName = settings.STRATUM_MINING_PROCESS_NAME

# Start the Pool
import mining
from mining.interfaces import Interfaces
from mining.interfaces import WorkerManagerInterface, TimestamperInterface, ShareManagerInterface, ShareLimiterInterface

if settings.VARIABLE_DIFF == True:
	from mining.basic_share_limiter import BasicShareLimiter
	Interfaces.set_share_limiter(BasicShareLimiter())
else:
	from mining.interfaces import ShareLimiterInterface
	Interfaces.set_share_limiter(ShareLimiterInterface())

Interfaces.set_share_manager(ShareManagerInterface())
Interfaces.set_worker_manager(WorkerManagerInterface())
Interfaces.set_timestamper(TimestamperInterface())

mining.setup(on_startup)

